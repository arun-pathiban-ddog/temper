# Approval request lifetime

A human's valid answer must reach its outstanding MCP request while that request remains actionable. Waiting does not grant authority. Only an explicit recognized acceptance may call the existing decision-resolution endpoint.

## State model

States: Awaiting, Answered, Closed, Expired. Initial state: Awaiting.

| Input | Condition | Result |
| --- | --- | --- |
| Time passes | No operator deadline | Awaiting |
| Matching response | Awaiting | Answered; deliver once |
| Wrong response ID | Awaiting | Awaiting |
| Client disconnect | Awaiting | Closed; resolve no decision |
| Deadline passes | Explicit operator deadline | Expired; clear waiter, cancel client prompt, report expiry |
| Late response | Closed or Expired | Ignore; resolve no decision |

Invariants: no answer is discarded solely due to elapsed time under default configuration; responses match request IDs; disconnect or expiry never implies approval; explicit expiry is visible to the client and tool caller. The paused-clock regression exercises the observed189-second delay. Existing transport tests cover accepted, declined, malformed and disconnected responses. Network and client-specific behavior also require live transport verification.
