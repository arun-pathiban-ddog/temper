# Restore reliable human approval for remote Foundry operations

Rita is consolidating remote work onto two source computers, Arni and Arni Big,
with isolated copies for each session. Authorized setup operations must not get
stuck because a human approval disappears between the client and Temper.

The installed MCP approval proposal discarded its response waiter after 120
seconds, while Codex still displayed the prompt. A real acceptance arrived after
189 seconds and was not recorded. This repair continues existing Temper PR #436
as part of [ARN-517](https://linear.app/arni-build/issue/ARN-517/consolidate-foundry-to-arni-and-arni-big-source-computers).

## Intended outcome

- A valid human response reaches its outstanding request while the prompt remains
  actionable. Waiting itself never grants authority.
- Cancellation, disconnection, and explicit expiry end the request predictably.
  Broken native pipes must not leave the process stuck.
- Approval resolves the server's structured decision identifier, never text from
  an entity name or an error message. Agent and human credentials remain separate.
- After server and client rollout, a real delayed human approval followed by a
  retry of the same operation succeeds through the normal governed path.

This repository change covers the approval bridge and required server response
fields. Foundry login UI, source selection, credential provisioning, and computer
inventory remain separate work within the broader remote-setup objective. No
permission bypass, approval replay, or additional gate machinery is requested.
