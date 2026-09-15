//! Decoding a base64 JSON string as it streams, without buffering the whole value.

use base64::Engine as _;
use bytes::Bytes;
use futures_util::StreamExt as _;

use super::{BASE64_INPUT_CHUNK_BYTES, BlobObjectStream};
use crate::blob_store::BlobByteStream;

/// Incrementally decode a JSON string containing standard base64.
pub fn decode_json_base64_stream(
    encoded: BlobObjectStream,
    expected_decoded_bytes: u64,
) -> BlobObjectStream {
    let mut source = encoded.into_stream();
    let stream: BlobByteStream = Box::pin(async_stream::try_stream! {
        let mut opened = false;
        let mut pending = None;
        let mut encoded_buffer = Vec::with_capacity(BASE64_INPUT_CHUNK_BYTES);
        let mut decoded_bytes = 0u64;
        let mut padding_seen = false;

        while let Some(chunk) = source.next().await {
            let chunk = chunk?;
            for byte in chunk {
                if !opened {
                    if byte != b'"' {
                        Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "overflow blob is not a JSON string"))?;
                    }
                    opened = true;
                    continue;
                }
                if let Some(previous) = pending.replace(byte) {
                    push_base64_byte(previous, &mut encoded_buffer, padding_seen)?;
                }
                if encoded_buffer.len() == BASE64_INPUT_CHUNK_BYTES {
                    let group = base64::engine::general_purpose::STANDARD
                        .decode(&encoded_buffer)
                        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
                    decoded_bytes = decoded_bytes
                        .checked_add(group.len() as u64)
                        .ok_or_else(|| std::io::Error::other("decoded blob byte count overflow"))?;
                    if decoded_bytes > expected_decoded_bytes {
                        Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "decoded blob exceeded expected length"))?;
                    }
                    padding_seen = encoded_buffer.contains(&b'=');
                    encoded_buffer.clear();
                    yield Bytes::from(group);
                }
            }
        }
        if !opened || pending != Some(b'"') {
            Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "overflow blob JSON string is truncated"))?;
        }
        if !encoded_buffer.is_empty() {
            if encoded_buffer.len() % 4 != 0 {
                Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "overflow blob has incomplete base64"))?;
            }
            let group = base64::engine::general_purpose::STANDARD
                .decode(&encoded_buffer)
                .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
            decoded_bytes = decoded_bytes
                .checked_add(group.len() as u64)
                .ok_or_else(|| std::io::Error::other("decoded blob byte count overflow"))?;
            if decoded_bytes > expected_decoded_bytes {
                Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "decoded blob exceeded expected length"))?;
            }
            yield Bytes::from(group);
        }
        if decoded_bytes != expected_decoded_bytes {
            Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                format!("decoded blob ended at {decoded_bytes} bytes; expected {expected_decoded_bytes}"),
            ))?;
        }
    });
    BlobObjectStream {
        content_length: expected_decoded_bytes,
        stream,
    }
}

fn push_base64_byte(
    byte: u8,
    encoded_buffer: &mut Vec<u8>,
    padding_seen: bool,
) -> Result<(), std::io::Error> {
    if padding_seen {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "base64 data followed padding",
        ));
    }
    if !(byte.is_ascii_alphanumeric() || matches!(byte, b'+' | b'/' | b'=')) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "overflow blob contains non-base64 data",
        ));
    }
    encoded_buffer.push(byte);
    Ok(())
}
