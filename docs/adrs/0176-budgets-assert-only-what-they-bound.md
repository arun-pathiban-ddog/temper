# ADR-0176: A budget asserts only what it actually bounds

- Status: Accepted
- Date: 2026-09-10
- Deciders: Temper core maintainers
- Related:
  - ARN-467; `docs/efforts/ARN-467/decisions.md` (D44)
  - `crates/temper-server/src/trigger/registry.rs` (`register_tenant_rules`)
  - `crates/temper-server/src/trigger/types.rs` (`MAX_REACTIONS_PER_TENANT`, `MAX_REACTION_DEPTH`)
  - `AGENTS.md`, Rust conventions
  - ADR-0045: Reactions as a First-Class App Primitive (superseded in part: its
    "no change to `MAX_REACTIONS_PER_TENANT`" non-goal no longer holds)

## Context

`MAX_REACTIONS_PER_TENANT = 256` was enforced by an `assert!` in
`register_tenant_rules`. On 2026-09-10 tenant `default`, hosting fifteen apps,
reached 265 reaction rules. The assertion produced three symptoms that were
diagnosed separately over several hours before they were traced to one line:

- every inline spec load returned an unexplained `502` in about 0.2s, because
  registration panicked and the connection was dropped before any handler could
  describe what went wrong;
- the digital twin's `CollectionMeasured` trigger was never registered, so the
  dispatcher found no rule, returned early and silently, and no observation
  could ever be recorded — a silent failure with no log line;
- on restart the panic ran on the main thread while replaying specs already
  committed to disk, so the platform crash-looped and was recoverable only by
  deleting rows from Postgres by hand.

The constant bounded nothing. It sized no buffer and terminated no loop. Rules
live in growable `BTreeMap`s, so rule 257 costs what rule 250 costs. It appeared
in four places: its own definition, a re-export, the assertion, and a test
asserting the assertion. Being tenant-wide it was also unownable — no single app
could be written to respect it, and each app installed tightened it for every
other app in the tenant.

This is worth an ADR rather than only an effort decision because the repository
convention line ("budgets not limits, fail fast on invariant violation") reads
as authorizing exactly the assertion that caused the outage, and would authorize
the next one.

## Decision

A budget asserts only where exceeding it corrupts something: a preallocated
buffer, a fixed-size array, a loop with a static bound. Where the value merely
counts entries in a structure that grows on demand, the count is reported and
not asserted.

Concretely:

- `register_tenant_rules` emits a `tracing::warn!` carrying the tenant, the rule
  count and the advisory threshold, then registers every rule.
- `MAX_REACTIONS_PER_TENANT` survives as that advisory threshold, so a tenant
  accumulating triggers without bound stays visible in logs.
- `MAX_REACTION_DEPTH` is untouched and stays a hard bound. Depth is the limit
  that earns its assertion: an unbounded reaction cascade is a real failure mode
  with no natural stopping point, and the bound is what terminates the loop.

**Why this approach**: fail-fast is a tool for keeping a corrupted process from
continuing. Applied to a number that cannot corrupt anything, it converts a
tenant's ordinary growth into an outage, and does so at the least recoverable
moment — startup, on the main thread, replaying committed state. Reporting keeps
the visibility the constant was there to provide, and costs nothing when the
threshold is crossed legitimately.

## Consequences

### Positive
- A tenant can install apps past the old ceiling without taking the platform
  down, and without the three-way symptom spread above.
- The startup replay path can no longer panic on data already accepted and
  committed, which was the only unrecoverable-by-code failure here.
- The remaining assertion, `MAX_REACTION_DEPTH`, now means something specific.

### Negative
- Nothing mechanically stops a tenant's rule count from growing. The warning is
  the only signal, and a log line can be missed where a panic cannot.

### Risks
- A tenant accumulating rules unnoticed makes each matching event do more work.
  The lookup is a `BTreeMap` hit per event and the rules under one key are
  iterated, so cost grows with rules matching the same entity and action, not
  with the tenant total. If that cost ever becomes real, the answer is a metric
  and per-app ownership of rule counts, not an assertion at registration.

### DST Compliance
Determinism is unchanged: the warning has no effect on the registry's contents
or ordering, and the previous behaviour on this path was an abort, never an
alternative result.

## Non-Goals

- Removing or weakening `MAX_REACTION_DEPTH`.
- A general review of the other TigerStyle budgets in the kernel. Each is judged
  by the same question this ADR states: does exceeding it corrupt something?
- Per-app quotas for reaction rules. That is the right shape if rule growth ever
  needs an owner, and it is not needed to resolve this outage.

## Alternatives Considered

1. **Raise the constant** — Moves the same outage to a later tenant, at a number
   equally unrelated to anything the code allocates. Rejected.
2. **Keep the assertion, exempt the startup replay path** — Leaves the platform
   able to reject specs it has already accepted, with the same opaque `502`, and
   splits one rule into two behaviours by caller. Rejected.
3. **Return an error instead of panicking** — Better than a panic, but it still
   refuses a legitimate installation for a threshold that protects nothing, and
   it leaves every caller to handle an error that cannot occur meaningfully.
   Rejected.
4. **Delete `MAX_REACTIONS_PER_TENANT` entirely** — Loses the visibility into
   unbounded growth, which is the one thing the constant was providing.
   Rejected.

## Rollback Policy

Reverting is a one-line change back to `assert!`. Before doing so, name what
overflows: this ADR is wrong only if the reaction registry gains a preallocated
or fixed-size structure sized by this constant.
