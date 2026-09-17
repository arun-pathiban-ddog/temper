# MCP read-denial elicitation and pinned installation

## Read decision at the request boundary

When an authenticated agent performs an explicit OData read that Cedar denies,
the HTTP boundary records a pending decision using the existing native human
decision path and returns the decision ID in the 403 body. Silent row
filtering for collection reads and cross-tenant rejections are left unchanged:
no policy is widened, and hidden-row existence is never disclosed.

Changed surfaces: `crates/temper-server/src/odata/authz.rs`,
`crates/temper-server/src/odata/read.rs`,
`crates/temper-platform/src/tenant_api/apps.rs`.

## Pinned installation adapter

The `install_app` dispatch path is restored to the governed Genesis pinned
installer. Cross-tenant bodies are rejected by Cedar before a decision is
created. Structured decision IDs in error messages and the legacy
message-parsing client path are both preserved so existing sessions continue
without reconfiguration.

Changed surfaces: `crates/temper-sandbox/src/dispatch.rs`,
`crates/temper-platform/src/tenant_api/apps.rs`,
`crates/temper-server/src/authz/denial_response.rs`.

## Session generation for unconfigured MCP runtimes

MCP runtimes that do not supply a session header receive a generated session
ID so the server treats their reads as interactive rather than passive. The
server continues to keep unvalidated session headers out of Cedar
authorization context. Explicitly configured sessions are preserved.

Changed surface: `crates/temper-mcp/src/runtime.rs`.
