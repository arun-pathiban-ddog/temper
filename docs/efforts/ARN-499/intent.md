# Intent

Genesis is a Temper OS app: serving git, resolving a push credential and streaming
an app bundle all run as WASM guests on this kernel. Every one of those paths was
broken from the kernel side, so Genesis returned 504 to agents and could not be
fixed from its own repository.

An agent must be able to clone, fetch and push against Genesis with a GitToken,
and an install must be able to read the bundle it is pinned to. That requires the
kernel to let a guest act under its own identity, to let a protocol handler see
the credential it is required to resolve, to serve blobs it has stored, and to
actually install a policy it has approved.

Tracked as [ARN-499](https://linear.app/arni-build/issue/ARN-499), the kernel
dependency of [ARN-467](https://linear.app/arni-build/issue/ARN-467). Genesis's
half is `arni-labs/genesis` PR #50, which pins this kernel.
