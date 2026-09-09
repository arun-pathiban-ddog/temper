# ADR-0175: One owner of the libSQL connection close

- Status: Superseded by ADR-0176
- Date: 2026-09-08
- Deciders: ARN-467 implementer
- Related: crates/temper-store-turso; docs/efforts/ARN-467/decisions.md (D27)

## Context

libSQL 0.9.29 closes a local connection in both LibsqlConnection::drop and Connection::drop. A concurrent allocation can reuse the freed native address between the two calls, allowing the second close to affect a different connection. The kernel server tests aborted in this path. The isolated published-crate regression also aborted.

## Decision

Use the published 0.9.29 source under vendor/libsql and remove LibsqlConnection's six-line Drop implementation. Connection remains the owner of closure. temper-store-turso depends directly on that source so downstream git consumers, including TemperPaw, also receive the fix. A root-only Cargo patch would be ignored by those consumers. The libsql dependencies retain their locked versions. The vendored package is excluded from the kernel workspace. Its release checksum, source revision and license are retained with the patch description.

A kernel regression exercises 16 concurrent workers and 10,000 connection lifecycles per worker, including a transaction and readback on every lifecycle. It runs with the normal workspace tests. Native allocation is nondeterministic; simulation invariants remain unchanged because this fix affects connection ownership rather than actor state or replay.

## Consequences

Public builds need no private dependency credential. The storage contract and database format are unchanged. Temper owns maintenance of the vendored patch; dependency updates must preserve the lifetime regression and review the new upstream source against this correction.


ADR-0176 replaces the maintained libSQL patch with official Turso packages. This decision remains as the history of the removed patch; no library fork or vendored implementation is retained by that migration.
