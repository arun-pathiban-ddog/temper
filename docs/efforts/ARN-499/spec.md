# Guest identity, credential forwarding, blob reads and policy activation

## Guest identity

A WASM guest's internal calls run under the guest's own module identity, not
under the identity of whoever called it. The module identity applies on both host
paths that can issue such a call, not only the intercepted one. A guest asking
the kernel to resolve a credential therefore asks as itself, and Cedar evaluates
the guest as principal.

A guest addresses the kernel by the kernel's own loopback origin. It does not
derive that address from a request's `Host` header, which names whatever public
hostname the client used.

## Credential forwarding

Whether an endpoint receives the client's `Authorization` header is decided by
the kernel, from the set of protocol modules whose credential the kernel cannot
interpret. An endpoint cannot declare it: "send me the caller's credential" is
not a privilege an application may assert about itself, and an unrecognised
module never forwards.

Only the credential formats those protocols use travel — HTTP Basic (git
smart-HTTP presents a GitToken as the username) and the `token` scheme (the
GitHub-compatible REST surface). `Bearer` is never forwarded, whether or not the
kernel could resolve it: resolution is tenant-scoped, so a credential that fails
to resolve may still be valid in the tenant it was issued for.

The opt-in is honoured wherever the header is removed, including the public-route
branch taken by endpoints that declare `RequiresAuth=false` in order to issue
their own protocol challenge. Removal happens at exactly the sites the opt-in
guards; no other route gains access to a credential it did not previously see.

## Blob reads

A streaming blob read resolves the same content a buffered read resolves,
including content held only in the legacy database location, and bounds the read
before materializing it.

A decoded length is asserted only where the model declares one. An undeclared
length is read to completion rather than compared against a cap treated as an
exact expectation. A declared `Size` is required only of models that declare it.

An app bundle that is public serves without a credential.
