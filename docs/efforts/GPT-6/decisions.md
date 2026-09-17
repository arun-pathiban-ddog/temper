# Decisions and tradeoffs

## D1: Record read decisions at the request boundary

**Decision:** Preserve denied read metadata internally and record a decision
only when that denial becomes the response to an explicit authenticated agent
request.

**Came up because:** Production DsfFlows reads and app-guide reads return 403
without a decision, so MCP cannot offer approval. Current main and the pending
release both retain this behavior.

**Options:** Grant broad read permissions; record every row authorization
failure; record only denied requested operations using the existing native
human decision path.

**Chose the request boundary because:** It retains the authenticated request
session without putting untrusted correlation headers into Cedar context. It
also keeps hidden collection rows from becoming approval prompts or existence
disclosures. No policy is widened by this change.

**Where:** `crates/temper-server/src/odata/authz.rs`;
`crates/temper-server/src/odata/read.rs`;
`crates/temper-platform/src/tenant_api/apps.rs`.

## D2: Keep installation on the governed pinned installer

**Decision:** Route the existing tenant-first MCP `install_app` call to the
pinned Genesis installer and surface that endpoint's same-tenant Cedar denial
through the native decision flow.

**Came up because:** The demo task confirmed its install helper was retired,
`App.Install` only updates metadata, and the real installer returned an
unstructured 403. The existing client also extracts decision IDs from error
messages.

**Options:** Misuse the metadata action; bypass authorization with another
credential; restore the narrow adapter to the existing installer and preserve
both structured and message-based decision IDs.

**Chose the existing installer because:** It owns bundle installation already.
The adapter adds no arbitrary endpoint or credential input; the server still
rejects cross-tenant bodies before requesting approval. Existing sessions
retain their tenant-first call signature and denial-parser compatibility.

**Where:** `crates/temper-sandbox/src/dispatch.rs`;
`crates/temper-platform/src/tenant_api/apps.rs`;
`crates/temper-server/src/authz/denial_response.rs`.

## D3: Give an unconfigured MCP runtime its own session

**Decision:** Generate a distinct session ID when the MCP host does not supply
one, while preserving explicitly configured sessions.

**Came up because:** The compiled-client live-router test still returned a
denial without a decision: the default MCP configuration sent no session, so
the server correctly treated it as a passive request. Typed-router tests alone
did not reveal this difference.

**Options:** Prompt for all passive agent requests; require every host to
configure a session manually; generate one per interactive MCP runtime.

**Chose runtime generation because:** It makes the existing interactive
approval contract work for new clients without creating prompts from passive
background reads. The server continues to keep unvalidated session headers out
of Cedar authorization context. Existing processes must reload the updated
client.

**Where:** `crates/temper-mcp/src/runtime.rs`; `runtime_test.rs`; isolated
compiled-MCP approval probe.
