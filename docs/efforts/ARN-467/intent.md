# Intent

DSF factory observations must not rewrite intended configuration, operation callbacks must retain the accepted target, and generic writes must not bypass those contracts. This is the kernel dependency of [ARN-467](https://linear.app/arni-build/issue/ARN-467/deliver-the-dsf-software-factory-live-operational-model-resource). The full application contract is in nerdsane/temperpaw, docs/efforts/ARN-467/spec.md.


## Approved cleanup: current Turso engine

Rita selected current official Turso packages to replace the copied libSQL dependency. No fork or vendored engine. Preserve local and remote storage behavior and data. This remains independent of Foundry delivery. Rita explicitly authorized an isolated local worktree while governed Copy is unavailable.
