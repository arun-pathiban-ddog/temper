# Implementation plan

1. Reproduce all four regressions with local test-client probes: missing
   read decision, retired installer, missing legacy decision text, and missing
   default session.
2. Record denied read metadata internally in the OData authz path and surface
   a decision only at the authenticated-request HTTP boundary; update
   `read.rs` and `tenant_api/apps.rs` accordingly.
3. Restore the tenant-first pinned installer adapter in `dispatch.rs` and
   `denial_response.rs`; preserve both structured and message-based
   decision-ID parsing for existing clients.
4. Generate a session ID in `temper-mcp/src/runtime.rs` when the host
   provides none; add a runtime test verifying session isolation.
5. Run `cargo fmt --check`, `cargo clippy --workspace -- -D warnings`,
   `scripts/readability-ratchet.sh check`, and `cargo test --workspace`.
6. Compile an actual MCP client against the patched router; drive consent,
   narrow approval, retry, and pinned bundle materialisation against an
   isolated real router — no production success claimed.
7. Open PR targeting `staging`; post the proof record; watch gates.
