# Decisions

## D1: The runtime crate owns the constant

**Decision:** `temper-runtime` keeps the definition; `temper-server` re-exports it.

**Came up because:** Either crate could own it. One must.

**Options:** Runtime owns, server re-exports; server owns, runtime re-exports; a third shared crate.

**Chose runtime because:** `temper-server` already depends on `temper-runtime`, not the reverse, so the re-export flows with the dependency graph and introduces no new edge. A shared crate for one integer is machinery for its own sake.

**Where:** crates/temper-server/src/trigger/types.rs (re-export); crates/temper-runtime/src/reaction.rs:25 (definition).

## D2: No test for equality

**Decision:** Enforce by construction, not by assertion.

**Came up because:** The obvious fix is a test asserting the two values match.

**Options:** An equality test; a re-export.

**Chose the re-export because:** A test checks that two numbers someone could still change are currently equal. A re-export makes a second definition a compile error. That is a rung higher, and it removes the test that would otherwise exist only to guard a duplication that no longer exists.

**Where:** same file.
