# Decisions

## Human response lifetime

**Decision:** Keep a human prompt pending until the human responds or the client connection closes, unless an operator explicitly configures a deadline.

**Came up because:** The existing120-second timeout removed the waiter while Codex still displayed an actionable prompt. A real approval arrived189seconds after the denied operation and remained unrecorded.

**Options:** Increase the default timeout; retain responses after returning the tool result; bind the default wait to the human interaction and connection.

**Chose the connection-bound wait over a longer timer because:** A longer timer reproduces the same failure at a later boundary. Keeping approval processing inside the original call preserves the existing human-only resolution path. Explicit deadlines remain available and must be visibly canceled and reported.

**Where:** `crates/temper-mcp/src/elicit.rs`, existing PR436.

## Existing proposal

**Decision:** Continue the existing PR436 approval proposal rather than create a second implementation.

**Came up because:** Codex runs an August local build of that unmerged proposal; current main has no elicitation implementation.

**Options:** Patch only the installed copy; introduce a parallel approval bridge; update the existing proposal.

**Chose the existing proposal because:** It is the source of the installed behavior and preserves the previously accepted scope. Previously reverted gate and upload changes stay removed.

**Where:** `https://github.com/nerdsane/temper/pull/436`; isolated branch `codex/approval-response-lifetime` merged with current main.
