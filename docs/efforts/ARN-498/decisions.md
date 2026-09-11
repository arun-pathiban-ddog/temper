# Decisions

**Decision:** Run secret-bearing validation on the trusted receiving branch and inspect contribution objects without executing them.
**Came up because:** Fork pull_request jobs cannot receive STACK_TOKEN needed to fetch private Stack.
**Options:** Rehost contributions; distribute gate copies; correct workflow execution.
**Chose workflow correction because:** It preserves original contributions and the existing private tooling without another service. Builds stay unprivileged.
**Where:** Stack gates and Temper .github/workflows/sdlc-*.yml.

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

**Decision:** Remove all additions outside the fork-token correction, including the terminal-review record extension.
**Came up because:** Rita explicitly rejected the extra work and authorized its cleanup.
**Options:** Retain the separate review-policy extension as a merge prerequisite; remove it.
**Chose removal because:** The accepted change is token access and its required workflow wiring. An unrelated gate limitation does not authorize another feature.
**Where:** Stack review files restored to the pre-effort versions; broad Stack17 and TemperPaw510 withdrawn; own additions to Temper436 removed.
