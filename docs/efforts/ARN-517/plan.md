# Preserve human approval responses

A Computer rename was denied at 17:34:18 UTC on September 16. Codex logged the human's accepted broad approval at 17:37:27, after the MCP request's 120-second waiter had been removed. The popup could still accept an answer that the server no longer handled.

Keep the existing PR #436 approval bridge. Make human prompts wait for their response or connection closure by default. An explicitly configured deadline must cancel the outstanding client request and disclose expiry in the tool result. Approval still requires a valid accepted response, and resolution errors remain visible. Do not change Cedar permissions or replay an approval ourselves.

1. Reproduce the 189-second response loss with a paused-clock regression test.
2. Change request lifetime and verify delay, cancellation, configured expiry and disconnect behavior.
3. Run the real MCP transport against a local test backend; no production credential is used to synthesize human approval.
4. Review the narrow change, update PR #436, build and install the verified bridge, then verify an actual human approval through Codex.

Review continuation: fix the confirmed transport lifetime defects and the original unresolved wrong-decision identity defect before shipping. Carry structured IDs through the server denial response and sandbox parser; deploy the server before installing the strict MCP client. Confirm the original version/capability gates, distinct approver credential, and recorded outcome. Previously removed CI gate and upload changes remain out of scope.
