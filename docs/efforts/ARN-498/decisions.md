# Decisions

**Decision:** Run secret-bearing validation on the trusted receiving branch and inspect contribution objects without executing them.
**Came up because:** Fork pull_request jobs cannot receive STACK_TOKEN needed to fetch private Stack.
**Options:** Rehost contributions; distribute gate copies; correct workflow execution.
**Chose workflow correction because:** It preserves original contributions and the existing private tooling without another service. Builds stay unprivileged.
**Where:** Stack gates and Temper .github/workflows/sdlc-*.yml.

**Decision:** Keep the existing three-round arbitration and targeted final review decision explicit in the review record.
**Came up because:** Nick’s original PRs have an approved terminal decision but the validator otherwise insists on another full panel.
**Options:** Ignore the human decision; rerun the panel; validate its existing evidence and directed final reviewers.
**Chose explicit evidence because:** It honors the approved workflow and preserves commit binding and unresolved-defect checks. No new approval service or identity model.
**Where:** Stack REVIEW.md and review/{schema.json,validate.py}; Ask en-01a08869-9373-7063-a217-271bb8a34531.

**Decision:** Use local isolated worktrees and GitHub directly for this effort.
**Came up because:** Temper MCP and health endpoint return HTTP 503.
**Options:** Wait indefinitely; repair the platform; use the local exception Rita explicitly approved.
**Chose the approved exception because:** It completes the narrow objective without reopening the broader platform work.
**Where:** Codex parent task 01a08157-a252-7b43-b1d9-facd84cd2695.

**Decision:** Preserve automatic-merge revocation when risk flags or workflow changes require human authorization, while never enabling auto-merge from the privileged validator.
**Came up because:** The original extraction removed that revocation together with automatic merge enabling.
**Options:** Remove both behaviors; retain revocation only.
**Chose revocation only because:** It preserves human control without granting the validator a new merge path. Revocation and Git/API failures remain visible failures.
**Where:** Stack gates/sdlc.yml and Temper sdlc-review.yml.

**Decision:** Use the documented Check Runs update fields and distinguish an absent feature map from a declared empty map.
**Came up because:** Review found a create-only field in the update request and a reproduced empty-map validation regression.
**Options:** Rely on undocumented extra-field handling and empty-set truthiness; preserve the existing contracts explicitly.
**Chose explicit contracts because:** A check is updated by its ID, while a declared feature map must reject unknown names even when it contains no features. The alleged HTTP422 was not observed; the API field correction follows GitHub's documented update contract.
**Where:** Contributor result reporters; Stack proof/validate.py and focused regressions.
