//! Bounded decoding and atomic publication of Genesis git object fields.

use std::io::Read as _;
use std::path::Path;
use std::time::Duration;

use futures::StreamExt as _;
use serde_json::Value;
use temper_runtime::tenant::TenantId;
use temper_server::state::ServerState;
use tokio::io::AsyncWriteExt as _;

pub(super) const MAX_GENESIS_TREE_CANONICAL_BYTES: u64 = 16 * 1024 * 1024;
const GENESIS_STREAM_CHUNK_BYTES: usize = 64 * 1024;
const GENESIS_FILE_CREATE_TIMEOUT: Duration = Duration::from_secs(30);
const GENESIS_MATERIALIZATION_TIMEOUT: Duration = Duration::from_secs(30 * 60);

pub(super) fn git_object_body<'a>(
    canonical: &'a [u8],
    expected_kind: &str,
) -> Result<&'a [u8], String> {
    let Some(nul) = canonical.iter().position(|byte| *byte == 0) else {
        return Err("CanonicalBytes missing git object header terminator".to_string());
    };
    let header = std::str::from_utf8(&canonical[..nul])
        .map_err(|error| format!("CanonicalBytes header is not UTF-8: {error}"))?;
    let body = &canonical[nul + 1..];
    let expected_header = format!("{expected_kind} {}", body.len());
    if header != expected_header {
        return Err(format!(
            "CanonicalBytes header must be '{expected_header}', got '{header}'"
        ));
    }
    Ok(body)
}

fn u64_field(value: &Value, key: &str) -> Option<u64> {
    value
        .get(key)
        .or_else(|| value.get("fields").and_then(|fields| fields.get(key)))
        .and_then(Value::as_u64)
}

fn encoded_json_base64_len(decoded_bytes: u64) -> Result<u64, String> {
    decoded_bytes
        .checked_add(2)
        .map(|bytes| bytes / 3)
        .and_then(|groups| groups.checked_mul(4))
        .and_then(|base64_bytes| base64_bytes.checked_add(2))
        .ok_or_else(|| "base64 JSON length overflowed u64".to_string())
}

/// Encoded size of a `CanonicalBytes` field, without decoding it.
///
/// Inline values carry their own base64 length; overflowed values carry the
/// serialized JSON length in their descriptor.
fn encoded_field_len(value: &Value) -> Option<u64> {
    let field = value.get("CanonicalBytes").or_else(|| {
        value
            .get("fields")
            .and_then(|fields| fields.get("CanonicalBytes"))
    })?;
    if let Some(encoded) = field.as_str() {
        return Some(encoded.len() as u64);
    }
    temper_server::blobs::field_overflow_descriptor(field).map(|d| d.serialized_bytes)
}

/// Exact decoded length, when — and only when — the model declares it.
///
/// Only `Blob` declares `Size`; `Tree`, `Commit` and `Tag` never have. Demanding
/// it for every kind made tree materialization fail with "Genesis object is
/// missing a non-negative Size" on data that was always shaped this way, which
/// took the whole Genesis install path down (the requirement arrived in
/// 8840b4fd, after Genesis had pinned an older kernel, so nothing caught it).
///
/// `None` means "the model does not declare a length for this object". That is
/// not a loss of integrity: a git object's canonical bytes are self-describing,
/// and `git_object_body` already rejects any object whose `{kind} {len}\0`
/// header disagrees with its own body. The declared `Size` is a second,
/// redundant check that only blobs can offer.
fn declared_decoded_len(value: &Value, kind: Option<&str>) -> Result<Option<u64>, String> {
    let Some(raw_size) = u64_field(value, "Size") else {
        return Ok(None);
    };
    match kind {
        Some(kind) => raw_size
            .checked_add(format!("{kind} {raw_size}\0").len() as u64)
            .map(Some)
            .ok_or_else(|| "Genesis canonical object length overflowed u64".to_string()),
        None => Ok(Some(raw_size)),
    }
}

/// Byte count to charge against the materialization budget.
///
/// Uses the declared length when there is one, and otherwise an upper bound
/// from the encoding — base64 never decodes to more than three quarters of its
/// encoded length, so the budget is charged conservatively rather than skipped.
pub(super) fn canonical_field_len(value: &Value, expected_kind: &str) -> Result<u64, String> {
    if let Some(declared) = declared_decoded_len(value, Some(expected_kind))? {
        return Ok(declared);
    }
    let encoded = encoded_field_len(value)
        .ok_or_else(|| "Genesis object is missing CanonicalBytes".to_string())?;
    Ok(encoded / 4 * 3 + 3)
}

pub(super) fn blob_content_len(value: &Value) -> Result<u64, String> {
    declared_decoded_len(value, None)?
        .ok_or_else(|| "Genesis blob is missing a non-negative Size".to_string())
}

pub(super) async fn read_canonical_field_bounded(
    state: &ServerState,
    tenant: &TenantId,
    value: &Value,
    expected_kind: &str,
    max_bytes: u64,
) -> Result<Vec<u8>, String> {
    let expected_bytes = declared_decoded_len(value, Some(expected_kind))?;
    if let Some(expected) = expected_bytes
        && expected > max_bytes
    {
        return Err(format!(
            "Genesis {expected_kind} canonical object is {expected} bytes; budget is {max_bytes}"
        ));
    }
    let Some(field) = value.get("CanonicalBytes").or_else(|| {
        value
            .get("fields")
            .and_then(|fields| fields.get("CanonicalBytes"))
    }) else {
        return Err("Genesis object is missing CanonicalBytes".to_string());
    };

    if let Some(encoded) = field.as_str() {
        return decode_inline_base64_bounded(encoded, expected_bytes, max_bytes);
    }

    let descriptor = temper_server::blobs::field_overflow_descriptor(field)
        .ok_or_else(|| "Genesis CanonicalBytes has an invalid overflow descriptor".to_string())?;
    read_overflow_base64_bounded(state, tenant, descriptor, expected_bytes, max_bytes).await
}

fn decode_inline_base64_bounded(
    encoded: &str,
    expected_bytes: Option<u64>,
    max_bytes: u64,
) -> Result<Vec<u8>, String> {
    if let Some(expected) = expected_bytes
        && expected > max_bytes
    {
        return Err(format!(
            "decoded Genesis field is {expected} bytes; budget is {max_bytes}"
        ));
    }
    let mut decoder = base64::read::DecoderReader::new(
        encoded.as_bytes(),
        &base64::engine::general_purpose::STANDARD,
    );
    let mut decoded = Vec::with_capacity(expected_bytes.unwrap_or(0) as usize);
    decoder
        .by_ref()
        .take(max_bytes.saturating_add(1))
        .read_to_end(&mut decoded)
        .map_err(|error| format!("decode inline Genesis base64 field: {error}"))?;
    match expected_bytes {
        Some(expected) if decoded.len() as u64 != expected => {
            return Err(format!(
                "decoded Genesis field is {} bytes; expected {expected}",
                decoded.len()
            ));
        }
        // Undeclared length: the git header check in `git_object_body` is the
        // integrity check, so only the budget applies here.
        _ if decoded.len() as u64 > max_bytes => {
            return Err(format!(
                "decoded Genesis field is {} bytes; budget is {max_bytes}",
                decoded.len()
            ));
        }
        _ => {}
    }
    Ok(decoded)
}

async fn read_overflow_base64_bounded(
    state: &ServerState,
    tenant: &TenantId,
    descriptor: temper_server::blobs::FieldOverflowDescriptor<'_>,
    expected_bytes: Option<u64>,
    max_bytes: u64,
) -> Result<Vec<u8>, String> {
    // The streaming decoder asserts an exact decoded length, which only exists
    // when the model declared a Size (blobs). For trees, commits and tags there
    // is none to assert -- and base64 padding makes it impossible to derive
    // exactly from the encoded length -- so read those buffered and let
    // `git_object_body` do the integrity check on the git header. Bounded by
    // `max_bytes` either way, and these objects are small by construction.
    if expected_bytes.is_none() {
        return read_overflow_base64_buffered(state, tenant, descriptor, max_bytes).await;
    }

    if let Some(expected) = expected_bytes {
        if expected > max_bytes {
            return Err(format!(
                "decoded Genesis field is {expected} bytes; budget is {max_bytes}"
            ));
        }
        let expected_encoded = encoded_json_base64_len(expected)?;
        if descriptor.serialized_bytes != expected_encoded {
            return Err(format!(
                "Genesis overflow descriptor is {} bytes; expected {expected_encoded}",
                descriptor.serialized_bytes
            ));
        }
    }
    let encoded = match state
        .stream_blob_object(tenant, descriptor.key, descriptor.serialized_bytes)
        .await?
    {
        temper_server::blob_store::BlobStreamRead::Found(stream) => stream,
        temper_server::blob_store::BlobStreamRead::Missing => {
            return Err(format!(
                "Genesis field overflow blob {} not found",
                descriptor.key
            ));
        }
        temper_server::blob_store::BlobStreamRead::TooLarge { .. } => {
            return Err(format!(
                "Genesis field overflow blob {} exceeds its descriptor",
                descriptor.key
            ));
        }
    };
    if encoded.content_length() != descriptor.serialized_bytes {
        return Err(format!(
            "Genesis field overflow blob {} length does not match its descriptor",
            descriptor.key
        ));
    }
    let encoded = encoded.verify_sha256(descriptor.sha256);
    let mut stream =
        temper_server::blob_store::decode_json_base64_stream(encoded, expected_bytes.unwrap_or(max_bytes))
            .into_stream();
    let mut decoded = Vec::with_capacity(expected_bytes.unwrap_or(0) as usize);
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| {
            format!(
                "decode Genesis field overflow blob {}: {error}",
                descriptor.key
            )
        })?;
        if decoded.len().saturating_add(chunk.len()) as u64 > max_bytes {
            return Err(format!(
                "decoded Genesis field overflow blob {} exceeded {max_bytes} bytes",
                descriptor.key
            ));
        }
        decoded.extend_from_slice(&chunk);
    }
    if let Some(expected) = expected_bytes
        && decoded.len() as u64 != expected
    {
        return Err(format!(
            "decoded Genesis field overflow blob {} is {} bytes; expected {expected}",
            descriptor.key,
            decoded.len()
        ));
    }
    Ok(decoded)
}

pub(super) async fn materialize_blob_content_field(
    state: &ServerState,
    tenant: &TenantId,
    value: &Value,
    destination: &Path,
    max_bytes: u64,
) -> Result<(), String> {
    let deadline = tokio::time::Instant::now() + GENESIS_MATERIALIZATION_TIMEOUT; // determinism-ok: production file I/O deadline
    let expected_bytes = blob_content_len(value)?;
    if expected_bytes > max_bytes {
        return Err(format!(
            "Genesis Blob.Content is {expected_bytes} bytes; budget is {max_bytes}"
        ));
    }
    let Some(field) = value
        .get("Content")
        .or_else(|| value.get("fields").and_then(|fields| fields.get("Content")))
    else {
        return Err("Genesis Blob is missing Content".to_string());
    };
    let parent = destination.parent().ok_or_else(|| {
        format!(
            "Genesis destination '{}' has no parent",
            destination.display()
        )
    })?;
    let parent = parent.to_path_buf();
    let staged = tokio::time::timeout(
        GENESIS_FILE_CREATE_TIMEOUT,
        // Production materialization filesystem boundary; never simulation-visible.
        tokio::task::spawn_blocking(move || {
            tempfile::Builder::new()
                .prefix(".genesis-blob-")
                .tempfile_in(parent)
        }),
    )
    .await
    .map_err(|_| "create staged Genesis file timed out".to_string())?
    .map_err(|error| format!("staged Genesis file task failed: {error}"))?
    .map_err(|error| format!("create staged Genesis file: {error}"))?;
    let (file, staged_path) = staged.into_parts();
    let mut output = tokio::fs::File::from_std(file);
    let written = if let Some(encoded) = field.as_str() {
        write_inline_base64(&mut output, encoded, expected_bytes, deadline).await?
    } else {
        let descriptor = temper_server::blobs::field_overflow_descriptor(field)
            .ok_or_else(|| "Genesis Blob.Content has an invalid overflow descriptor".to_string())?;
        write_overflow_base64(
            state,
            tenant,
            &mut output,
            descriptor,
            expected_bytes,
            deadline,
        )
        .await?
    };
    if written != expected_bytes {
        return Err(format!(
            "Genesis Blob.Content decoded {written} bytes; expected {expected_bytes}"
        ));
    }
    tokio::time::timeout_at(deadline, output.flush())
        .await
        .map_err(|_| "flush staged Genesis file exceeded materialization deadline".to_string())?
        .map_err(|error| format!("flush staged Genesis file: {error}"))?;
    tokio::time::timeout_at(deadline, output.sync_data())
        .await
        .map_err(|_| "sync staged Genesis file exceeded materialization deadline".to_string())?
        .map_err(|error| format!("sync staged Genesis file: {error}"))?;
    drop(output);
    staged_path.persist(destination).map_err(|error| {
        format!(
            "publish staged Genesis file '{}': {}",
            destination.display(),
            error.error
        )
    })?;
    Ok(())
}

async fn write_inline_base64(
    output: &mut tokio::fs::File,
    encoded: &str,
    expected_bytes: u64,
    deadline: tokio::time::Instant,
) -> Result<u64, String> {
    let mut decoder = base64::read::DecoderReader::new(
        encoded.as_bytes(),
        &base64::engine::general_purpose::STANDARD,
    );
    let mut buffer = vec![0u8; GENESIS_STREAM_CHUNK_BYTES];
    let mut written = 0u64;
    loop {
        let read = decoder
            .read(&mut buffer)
            .map_err(|error| format!("decode inline Genesis Blob.Content: {error}"))?;
        if read == 0 {
            break;
        }
        written = written
            .checked_add(read as u64)
            .ok_or_else(|| "Genesis Blob.Content byte count overflowed u64".to_string())?;
        if written > expected_bytes {
            return Err("Genesis Blob.Content exceeds its declared Size".to_string());
        }
        tokio::time::timeout_at(deadline, output.write_all(&buffer[..read]))
            .await
            .map_err(|_| {
                "writing inline Genesis Blob.Content exceeded materialization deadline".to_string()
            })?
            .map_err(|error| format!("write staged Genesis file: {error}"))?;
    }
    Ok(written)
}

async fn write_overflow_base64(
    state: &ServerState,
    tenant: &TenantId,
    output: &mut tokio::fs::File,
    descriptor: temper_server::blobs::FieldOverflowDescriptor<'_>,
    expected_bytes: u64,
    deadline: tokio::time::Instant,
) -> Result<u64, String> {
    let expected_encoded = encoded_json_base64_len(expected_bytes)?;
    if descriptor.serialized_bytes != expected_encoded {
        return Err(format!(
            "Genesis Blob.Content descriptor is {} bytes; expected {expected_encoded}",
            descriptor.serialized_bytes
        ));
    }
    let encoded = match state
        .stream_blob_object(tenant, descriptor.key, descriptor.serialized_bytes)
        .await?
    {
        temper_server::blob_store::BlobStreamRead::Found(stream) => stream,
        temper_server::blob_store::BlobStreamRead::Missing => {
            return Err(format!(
                "Genesis Blob.Content overflow object {} not found",
                descriptor.key
            ));
        }
        temper_server::blob_store::BlobStreamRead::TooLarge { .. } => {
            return Err(format!(
                "Genesis Blob.Content overflow object {} exceeds its descriptor",
                descriptor.key
            ));
        }
    };
    if encoded.content_length() != descriptor.serialized_bytes {
        return Err(format!(
            "Genesis Blob.Content overflow object {} length does not match its descriptor",
            descriptor.key
        ));
    }
    let encoded = encoded.verify_sha256(descriptor.sha256);
    let mut stream =
        temper_server::blob_store::decode_json_base64_stream(encoded, expected_bytes).into_stream();
    let mut written = 0u64;
    loop {
        let next = tokio::time::timeout_at(deadline, stream.next())
            .await
            .map_err(|_| {
                "reading Genesis Blob.Content exceeded materialization deadline".to_string()
            })?;
        let Some(chunk) = next else {
            break;
        };
        let chunk = chunk.map_err(|error| {
            format!(
                "decode Genesis Blob.Content overflow object {}: {error}",
                descriptor.key
            )
        })?;
        written = written
            .checked_add(chunk.len() as u64)
            .ok_or_else(|| "Genesis Blob.Content byte count overflowed u64".to_string())?;
        if written > expected_bytes {
            return Err("Genesis Blob.Content exceeds its declared Size".to_string());
        }
        tokio::time::timeout_at(deadline, output.write_all(&chunk))
            .await
            .map_err(|_| {
                "writing Genesis Blob.Content exceeded materialization deadline".to_string()
            })?
            .map_err(|error| format!("write staged Genesis file: {error}"))?;
    }
    Ok(written)
}


/// Read an overflowed field whose decoded length the model does not declare.
///
/// Uses the bounded stream and collects it, rather than the JSON-base64 stream
/// decoder, because that decoder asserts an exact decoded length and there is
/// none to assert here.
async fn read_overflow_base64_buffered(
    state: &ServerState,
    tenant: &TenantId,
    descriptor: temper_server::blobs::FieldOverflowDescriptor<'_>,
    max_bytes: u64,
) -> Result<Vec<u8>, String> {
    let encoded = match state
        .stream_blob_object(tenant, descriptor.key, descriptor.serialized_bytes)
        .await?
    {
        temper_server::blob_store::BlobStreamRead::Found(stream) => stream,
        temper_server::blob_store::BlobStreamRead::Missing => {
            return Err(format!(
                "Genesis field overflow blob {} not found",
                descriptor.key
            ));
        }
        temper_server::blob_store::BlobStreamRead::TooLarge { .. } => {
            return Err(format!(
                "Genesis field overflow blob {} exceeds its descriptor",
                descriptor.key
            ));
        }
    };
    let encoded = encoded.verify_sha256(descriptor.sha256);
    let mut stream = encoded.into_stream();
    let mut raw = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| {
            format!(
                "read Genesis field overflow blob {}: {error}",
                descriptor.key
            )
        })?;
        if raw.len().saturating_add(chunk.len()) as u64 > max_bytes.saturating_mul(2) {
            return Err(format!(
                "Genesis field overflow blob {} exceeded its encoded budget",
                descriptor.key
            ));
        }
        raw.extend_from_slice(&chunk);
    }
    let text = std::str::from_utf8(&raw)
        .map_err(|error| {
            format!(
                "Genesis overflow blob {} is not UTF-8: {error}",
                descriptor.key
            )
        })?
        .trim();
    let body = text
        .strip_prefix('"')
        .and_then(|rest| rest.strip_suffix('"'))
        .ok_or_else(|| format!("Genesis overflow blob {} is not a JSON string", descriptor.key))?;
    let decoded = base64_decode_standard(body).map_err(|error| {
        format!(
            "decode Genesis field overflow blob {}: {error}",
            descriptor.key
        )
    })?;
    if decoded.len() as u64 > max_bytes {
        return Err(format!(
            "decoded Genesis field overflow blob {} is {} bytes; budget is {max_bytes}",
            descriptor.key,
            decoded.len()
        ));
    }
    Ok(decoded)
}

fn base64_decode_standard(encoded: &str) -> Result<Vec<u8>, String> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD
        .decode(encoded.as_bytes())
        .map_err(|error| error.to_string())
}
