//! Bounded text evidence uploads through the ordinary File stream boundary.
use monty::MontyObject;
use reqwest::Method;
use serde_json::Value;

use super::DispatchContext;
use crate::helpers::expect_string_arg;
use crate::http::temper_request_bytes;

const TEXT_BUDGET_BYTES: usize = 128 * 1024;

pub(super) async fn put_file_text(
    ctx: &DispatchContext<'_>,
    args: &[MontyObject],
) -> Result<Value, String> {
    if args.len() != 3 {
        return Err("put_file_text requires file_id, content, content_type".to_owned());
    }
    let file_id = expect_string_arg(args, 0, "file_id", "put_file_text")?;
    if file_id.is_empty()
        || file_id.len() > 256
        || !file_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(
            "file_id must be 1–256 ASCII letters, digits, hyphens or underscores".to_owned(),
        );
    }
    let content = expect_string_arg(args, 1, "content", "put_file_text")?;
    if content.len() > TEXT_BUDGET_BYTES {
        return Err(format!("content exceeds {TEXT_BUDGET_BYTES} UTF-8 bytes"));
    }
    let content_type = expect_string_arg(args, 2, "content_type", "put_file_text")?;
    if !matches!(
        content_type.as_str(),
        "application/json" | "text/plain" | "text/markdown"
    ) {
        return Err(
            "content_type must be application/json, text/plain or text/markdown".to_owned(),
        );
    }
    tokio::time::timeout(
        std::time::Duration::from_secs(30),
        temper_request_bytes(
            ctx.http,
            ctx.base_url,
            ctx.tenant,
            &ctx.identity(),
            ctx.api_key,
            Method::PUT,
            &format!("/tdata/Files('{file_id}')/$value"),
            content.into_bytes(),
            &content_type,
        ),
    )
    .await
    .map_err(|_| "File text upload timed out after 30 seconds".to_owned())?
}
