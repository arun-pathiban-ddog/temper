# Decision log — ARN-461

**Decision:** (2026-09-03) Three chain_* modules, not an extension of `review_gate_lifecycle`.
**Came up because:** Rita said WASM inspects rows and retracts a bool. `review_gate_lifecycle` already dispatches PassReview / AttachProofPacket.
**Options:** (1) add checks inside `review_gate_lifecycle`; (2) one mega `chain_sdlc_ready`; (3) one module per verb.
**Chose (3) over (1) and (2) because:** (1) hides more dispatch. (2) mixes review, proof, and commit pin. What we gave up: three Cargo.toml files instead of one.
**Where:** `os-apps/paw-patrol/wasm/chain_review_ready`, `chain_proof_ready`, `chain_merge_ready`.

---

**Decision:** (2026-09-03) Merge WASM on_failure returns Proving. The agent still fires Merge.
**Came up because:** Merge is Proving → Merged. A self-loop bool cannot pin `head_sha` until Merge names it.
**Options:** (1) new CheckRecords verb before Merge; (2) Merge stays Merged on a failed check; (3) RetractMerge back to Proving and clear the bools.
**Chose (3) over (1) and (2) because:** (1) is another agent step. (2) leaves a false Merged. Merge does not call GitHub, so retracting the state is safe. What we gave up: a brief Merged flicker if the rows fail.
**Where:** `effort.ioa.toml` Merge / RetractMerge.

---

**Decision:** (2026-09-03) CI lists Temper rows by commit. It does not read PR comments.
**Came up because:** The vendored gate scraped `sdlc-review-record-b64` comments and folded a hidden `<!-- sdlc-review -->` comment.
**Options:** (1) keep comments as fallback; (2) Temper only, fail closed.
**Chose (2) because:** Rita said one book and no hidden comment. What we gave up: a PR with only a comment record and no Temper rows fails until the implementer writes the rows.
**Where:** `.github/workflows/sdlc-review.yml`, `sdlc-verification.yml`.

## Continue the existing recording effort

**Decision:** Repair the confirmed operational gaps under ARN-461 with one PR per affected repository.

**Came up because:** TemperPaw PR 500 installed merge gates before ordinary harnesses had a complete validated submission path; three current tasks are blocked by related contract mismatches.

**Options:** Patch each blocked record with a one-off grant; repair shared contracts; redesign the full lifecycle.

**Chose shared contract repair over one-off grants and a redesign because:** It addresses the recurring failure while retaining validation and keeping the task bounded.

**Where:** docs/efforts/ARN-461; Temper Intent arn461-gate-repair-20260910; user authorization in Codex task 01a08157-a252-7b43-b1d9-facd84cd2695.

## Preserve retired review rounds without blocking their replacements

**Decision:** Exclude explicitly Superseded ReviewRuns from the active review and merge panel, while requiring their original record to remain present.

**Came up because:** Effort appends review IDs across rounds, but both row validators rejected the existing terminal Superseded state, so a historical failed round blocked a later passing panel indefinitely.

**Options:** Delete historical attachments; infer retirement from commit differences; honor the existing Supersede transition.

**Chose explicit supersession over deletion or inference because:** It preserves honest evidence and makes retirement intentional. Requested, failed, or stale active runs still block; retired runs never contribute model votes. Requiring record_present also retains the ReviewRun RecordedHasRecord invariant. The tradeoff is one explicit retirement action after replacement confirmation exists.

**Where:** os-apps/paw-patrol/wasm/chain_review_ready/src/lib.rs; os-apps/paw-patrol/wasm/chain_merge_ready/src/lib.rs; os-apps/paw-patrol/specs/effort.ioa.toml; os-apps/paw-patrol/policies/patrol.cedar.

## Keep contributor pull requests in their original repository

**Decision:** Run privileged SDLC validation from the trusted base repository, inspecting the contribution as Git objects and PR data, and report each gate against the actual contributor commit.

**Came up because:** GitHub withholds STACK_TOKEN from fork pull_request workflows, but our required gates clone private arni-labs/stack. Rehosting Nick's work avoided that restriction without fixing the shared failure.

**Options:** Rehost contributor branches; remove the token while retaining the private clone; publish private Stack code; separate trusted gate execution from unprivileged contribution builds.

**Chose trusted gate execution because:** It preserves Nick's original PRs and keeps Stack private. The workflow must never check out or execute the contributor's code with repository secrets. Explicit checks remain tied to the PR head because pull_request_target itself runs against the base. Merge remains owned by the authorized Temper effort; this privileged validator does not auto-merge.

**Where:** Stack gates/sdlc.yml and check-effort-artifacts.py; Temper .github/workflows/sdlc-*.yml; original Temper PRs 411 and 412.


## MCP approval diagnostics and evidence text

**Decision:** Reuse PR #436 and preserve caller authorization for File content writes.

**Came up because:** Agents could not distinguish unavailable human elicitation from a unanswered request, and the MCP lacked the existing File stream upload operation.

**Options:** Extend the existing transport and File endpoint; add an alternative approval/storage service; directly invoke internal callbacks.

**Chose the existing boundaries because:** Safe outcome diagnostics make pending decisions actionable without granting permission, while a 1 MiB UTF-8 upload method writes through the existing authenticated $value endpoint without host filesystem reads. No new approval route or storage state is introduced.

**Where:** crates/temper-mcp/src/elicit_status.rs; crates/temper-sandbox/src/file_text.rs; integrated PR #436 ancestry.


## MCP transport review corrections

**Decision:** Bound pending client requests without blocking the response reader, preserve transport failures, and limit inline File text to 128 KiB.

**Came up because:** PR436 queued requests without a bound during human elicitation and converted I/O failures to clean shutdown; the new 1 MiB text allowance also exceeded the existing MCP frame budget after encoding.

**Options:** Retain the current behavior; await a bounded request queue; add concurrent dispatch or chunked uploads; use bounded nonblocking admission and the existing transport.

**Chose bounded nonblocking admission because:** Responses must still reach the active human request when ordinary requests fill the queue. Overflow closes the transport explicitly and leaves decisions pending. Reader, writer, and task failures remain errors; normal EOF remains clean. A 128 KiB text budget leaves room for Python and JSON escaping within the unchanged 1 MiB frame cap. Arbitrary extra execute code is still subject to that whole-frame limit.

**Where:** crates/temper-mcp/src/runtime.rs; crates/temper-mcp/src/protocol.rs; crates/temper-sandbox/src/file_text.rs.


**Decision:** Encode large inline text as adjacent Python literals on short source lines, without changing Monty.

**Came up because:** The real stdio test exposed Monty's existing source-column conversion panic for a single large source line; the accepted 128 KiB content itself fits the existing transport when encoded across short lines.

**Options:** Change the interpreter; shrink all uploads below this unrelated source-line constraint; use ordinary multiline Python construction and document it.

**Chose multiline construction because:** It proves the required upload size through the real parser and transport without introducing an interpreter repair into this effort. Arbitrary oversized source lines remain an existing interpreter limitation.

**Where:** crates/temper-mcp/src/file_text_tests.rs; crates/temper-mcp/src/protocol.rs.


## Shared gate rollout order

**Decision:** Merge Stack #17 before installing the consuming SDLC workflow changes in Temper #436.

**Came up because:** The candidate workflows call check-effort-artifacts.py's Git-object support, proof/validate.py --features-commit, and gates/rerun-record-checks.py; those capabilities are in companion Stack #17, not yet its default branch.

**Options:** Pin to a private feature branch; merge shared implementation first.

**Chose shared implementation first because:** It preserves the accepted unpinned Stack-default contract and ensures consumers can resolve their commands.

**Where:** .github/workflows/sdlc-{planning,verification,decision-intake}.yml; Stack #17.


## Intake check visibility

**Decision:** Give decision intake checks:read permission.

**Came up because:** Its current-head rerun helper uses the Check Runs API.

**Options:** Grant checks:read for that read; filter pull_request_target workflow runs using the PR head SHA.

**Chose checks:read because:** It adds read authority only and avoids incorrect PR-head filtering of base-SHA pull_request_target runs.

**Where:** .github/workflows/sdlc-decision-intake.yml; Stack gates/rerun-record-checks.py.
