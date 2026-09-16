# Decisions

## Human response lifetime

**Decision:** Keep a human prompt pending until the human responds or the client connection closes, unless an operator explicitly configures a deadline.

**Came up because:** The existing 120-second timeout removed the waiter while Codex still displayed an actionable prompt. A real approval arrived 189 seconds after the denied operation and remained unrecorded.

**Options:** Increase the default timeout; retain responses after returning the tool result; bind the default wait to the human interaction and connection.

**Chose the connection-bound wait over a longer timer because:** A longer timer reproduces the same failure at a later boundary. Keeping approval processing inside the original call preserves the existing human-only resolution path. Explicit deadlines remain available and must be visibly canceled and reported.

**Where:** `crates/temper-mcp/src/elicit.rs`, existing PR #436.

## Existing proposal

**Decision:** Continue the existing PR #436 approval proposal rather than create a second implementation.

**Came up because:** Codex runs an August local build of that unmerged proposal; current main has no elicitation implementation.

**Options:** Patch only the installed copy; introduce a parallel approval bridge; update the existing proposal.

**Chose the existing proposal because:** It is the source of the installed behavior and preserves the previously accepted scope. Previously reverted gate and upload changes stay removed.

**Where:** `https://github.com/nerdsane/temper/pull/436`; isolated branch `codex/approval-response-lifetime` merged with current main.

## Transport lifetime and cancellation

**Decision:** Bound the transport queues, propagate transport failures, and cancel the outstanding human prompt when its originating tool call is canceled. Give the post-answer HTTP resolution its own 30-second network deadline.

**Came up because:** Review found that a broken output stream or canceled tool call could outlive the new untimed human wait, and that the split reader could accumulate requests while waiting for a human.

**Options:** Keep the old unbounded transport and rely on process restart; put a timer back on the human; explicitly supervise the transport and separate human waiting from network waiting.

**Chose explicit supervision because:** Human response time is unbounded, but queue storage and network operations must be bounded. An overfull inbound queue closes the connection rather than blocking the only reader that can receive the human answer. A canceled call never resolves a later answer. A failed or timed-out resolution is reported as uncertain failure, never as approval granted.

**Where:** `crates/temper-mcp/src/runtime.rs`, `crates/temper-mcp/src/elicit.rs`, PR #436.

## Structured decision identity

**Decision:** Carry the recorded decision ID as a structured server field and use only that field for inline approval; never infer an approval target from human-readable text.

**Came up because:** The original PR's unresolved QA finding reproduced: an entity called `PD-victim` caused approval to target that text instead of `PD-test123`, even when the response contained the correct structured ID.

**Options:** Ignore the old finding and ship the timeout repair; change the text parser; propagate the actual decision ID from the server and fail closed when it is absent.

**Chose structured identity because:** A human approval must apply to the server's recorded decision. Text formatting is not an authority boundary. Servers without a structured decision ID continue reporting denials but cannot offer inline resolution; the server update must precede the MCP update. This requires a small server response change in the existing approval PR, not a new permission or policy mechanism.

**Where:** OData denial responses, `temper-sandbox::helpers::format_authz_denied`, and the MCP mock transport regression in PR #436.
