# Intent

Genesis is a Temper OS app: serving git, resolving a push credential and streaming
an app bundle all run as WASM guests on this kernel. Every one of those paths was
broken from the kernel side, so Genesis returned 504 to agents and could not be
fixed from its own repository.

An agent must be able to clone, fetch and push against Genesis with a GitToken,
and an install must be able to read the bundle it is pinned to. That requires the
kernel to let a guest act under its own identity, to let a protocol handler see
the credential it is required to resolve, and to serve the blobs it has stored.

Installing an approved policy into the authorization engine (ARN-164) began in
this effort and moved to its own: it is not needed to run Genesis, and it is new
stateful machinery that earned its own review. See ARN-505.

Tracked as [ARN-499](https://linear.app/arni-build/issue/ARN-499), the kernel
dependency of [ARN-467](https://linear.app/arni-build/issue/ARN-467). Genesis's
half is `arni-labs/genesis` PR #50, which pins this kernel.
