# libSQL 0.9.29 connection lifetime

Source: https://crates.io/crates/libsql/0.9.29

Published archive SHA-256: 2329faffc510cc3c6b4f00169a39177cc7099d3ed7647fc92f7cf26e53a8d976.

The source is the published crate. The only Rust source change removes the redundant LibsqlConnection Drop implementation in src/local/impls.rs. Its inner Connection already closes the native handle. The registry's .cargo-ok cache marker is omitted. LICENSE.md is copied from the upstream release commit.

Regression: crates/temper-store-turso/tests/connection_lifetime.rs in the Temper workspace. Rationale: docs/adrs/0175-libsql-connection-lifetime.md.

The storage crate uses a direct path dependency so consumers of Temper also use this fixed source; no root-only Cargo patch is required.
