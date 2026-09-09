# ADR-0176: Use the official Turso engine without a maintained database fork

- Status: Proposed
- Date: 2026-09-09
- Decider: Rita Agafonova
- Related: ADR-0175, ARN-467

## Context

ARN-467 copied libSQL into the kernel repository to repair a connection teardown defect. Rita rejected maintaining either copied database source or a fork, and selected the current official Turso engine. This migration is independent of Foundry delivery.

## Decision

Use published stable Turso packages, starting qualification with turso 0.7.2 and turso_serverless 0.1.3. Preserve the existing storage contract: durable event append and replay, atomic writes, tenant isolation, schema migrations, local file data, and remote Turso connections. Remove vendor/libsql and its dedicated patch checks when the replacement passes qualification. Do not introduce another database fork, patch upstream engine internals, silently drop remote support, or change product semantics to fit an incompatible engine.

## Readiness gates

Qualify existing schema and transaction operations against the published engine before adapting callers. Preserve existing-data compatibility with a real old-format fixture and restart proof. Run store contract tests and relevant server e2e before review and deployment. An upstream incompatibility is a concrete limitation to report, not authorization for another engine repair project.

## Rollout and rollback

Keep production unchanged during qualification. A completed migration requires reviewed code, current-head proof, and a separately verified kernel rollout through Temper. Keep an untouched backup of any persistent database before a changed runtime opens it; do not assume reverse file compatibility.

## Alternatives

Vendored libSQL and a separate libSQL fork were rejected by Rita. Restoring the published libSQL package restores a known teardown defect. Replacing Turso with SQLite or PostgreSQL was not selected.
