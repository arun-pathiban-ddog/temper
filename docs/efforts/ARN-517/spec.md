# Approval request lifetime

A human's valid answer must reach its outstanding MCP request while that request remains actionable. Waiting does not grant authority. Only an explicit recognized acceptance may call the existing decision-resolution endpoint.

## State model

States: Awaiting, Answered, Closed, Canceled, Expired. Initial state: Awaiting.

| Input | Condition | Result |
| --- | --- | --- |
| Time passes | No operator deadline | Awaiting |
| Matching response | Awaiting | Answered; deliver once |
| Wrong response ID | Awaiting | Awaiting |
| Originating tool canceled | Awaiting | Canceled; dismiss prompt, resolve no decision |
| Client disconnect | Awaiting | Closed; resolve no decision |
| Deadline passes | Explicit operator deadline | Expired; clear waiter, cancel client prompt, report expiry |
| Late response | Closed, Canceled or Expired | Ignore; resolve no decision |

Invariants: no answer is discarded solely due to elapsed time under default configuration; responses match request IDs; disconnect or expiry never implies approval; explicit expiry is visible to the client and tool caller. The paused-clock regression exercises the observed 189-second delay. Existing transport tests cover accepted, declined, malformed and disconnected responses. Network and client-specific behavior also require live transport verification.

Transport queues hold at most 64 messages each. Queue saturation closes the connection instead of blocking the response reader. Reader and writer I/O failures propagate to the caller. Resolution HTTP requests have a 30-second network deadline distinct from human response time; a timeout reports an unknown outcome, never grants approval or retries automatically.

Inline resolution requires a structured `decision_id` from the denied server response. Text-only legacy denials never choose an approval target. Elicitation is negotiated only for the supported 2025-06-18 protocol and an object capability. The agent key performs the denied operation; the operator key resolves an explicit human answer. Record the resulting human outcome in the tool trajectory.

The session owner supervises reader, writer and dispatch together. Input failure interrupts dispatch even when output is blocked. Only clean input EOF may drain queued requests; transport errors abort the remaining tasks and propagate. A real-process pipe saturation probe and an in-memory backpressure regression cover this distinction.
