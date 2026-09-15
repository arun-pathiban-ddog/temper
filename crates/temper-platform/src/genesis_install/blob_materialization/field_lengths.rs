//! How long a Genesis field is, in each of the forms it can be stored in.
//!
//! A field arrives inline as base64 in the JSON, or spilled to an overflow
//! blob, and a model may or may not declare the decoded length. These helpers
//! answer "how many bytes is this" for each case - and, importantly,
//! distinguish a declared length from an absent one, because asserting a length
//! the model never declared is what made a 328-byte blob fail as "expected
//! 16777216".

use serde_json::Value;

pub(super) fn u64_field(value: &Value, key: &str) -> Option<u64> {
    value
        .get(key)
        .or_else(|| value.get("fields").and_then(|fields| fields.get(key)))
        .and_then(Value::as_u64)
}

pub(super) fn encoded_json_base64_len(decoded_bytes: u64) -> Result<u64, String> {
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
pub(super) fn encoded_field_len(value: &Value) -> Option<u64> {
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
pub(super) fn declared_decoded_len(
    value: &Value,
    kind: Option<&str>,
) -> Result<Option<u64>, String> {
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
pub(crate) fn canonical_field_len(value: &Value, expected_kind: &str) -> Result<u64, String> {
    if let Some(declared) = declared_decoded_len(value, Some(expected_kind))? {
        return Ok(declared);
    }
    let encoded = encoded_field_len(value)
        .ok_or_else(|| "Genesis object is missing CanonicalBytes".to_string())?;
    Ok(encoded / 4 * 3 + 3)
}

pub(crate) fn blob_content_len(value: &Value) -> Result<u64, String> {
    declared_decoded_len(value, None)?
        .ok_or_else(|| "Genesis blob is missing a non-negative Size".to_string())
}
