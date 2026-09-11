# Decisions and tradeoffs

The kernel decisions of ARN-499. Genesis's own decisions for the same
effort (Cedar permits, the wire modules) stay in `arni-labs/genesis`,
`docs/efforts/ARN-467/decisions.md`; these are the ones that changed temper.

## D1: Instrument the Genesis object lookup instead of guessing at the 404

**Decision:** Add a temporary `tracing::warn!` to both silent return paths in
`load_genesis_object_by_key` (temper submodule, branch
`claude/arn467-genesis-bundle-diagnostic`) and deploy Genesis on it, rather than
attempting a fix against a hypothesis.

**Came up because.** With git working end to end, the bundle endpoint still
returned 404 instantly for every well-formed request, and it is the code
`install-from-genesis` runs, so installing an app through Genesis is blocked on
it. Everything observable from outside checks out: the row reads back 200 over
OData by the exact composite key the kernel builds
(`Commits('rp-paw-agent-dsf-factory-77150dbf…')`), with `fields.Id`,
`fields.RepositoryId` and `fields.TreeSha` all matching what the comparison
uses; tenant is the same value on both sides (`export_genesis_registry_bundle`
does `let tenant = TenantId::new(registry_tenant)`); `validate_git_object_id` is
pass-through; and the `read_app_bundle` Cedar gate is passed, since a denial
there returns 403 rather than this 404.

I also tested and *disproved* my own leading hypothesis: warming the entity with
an OData read immediately before the bundle call changed nothing, so
`ensure_entity_loaded` failing is not the explanation on its own.

**Options.**
- Guess at the most likely cause and ship a fix.
- Add the diagnostic, deploy, read one line, then fix precisely.
- Leave it and pursue a clone-based install instead.

**Chose instrumenting because** the function has exactly two ways to return
`Ok(None)` and *neither logs anything*, which is why the cause is invisible from
outside — that absence is itself the defect that made this expensive. A fix
chosen without knowing which branch fires would be a guess against production
authorization-adjacent code.

**Rejected the clone-based install** after checking it rather than assuming: the
clone does carry the complete bundle at the pinned commit (app.toml, 16 specs,
5 policies, 291 wasm files at `77150db`), but there is no supported surface to
install it. `submit_specs` takes specs and not policies — that is exactly the
half-install that leaves all 11 `Dsf.Factory` collections answering 403 — and
local OS-app install was deliberately removed (`install_app` now returns "local
OS-app install is removed from the normal agent path; install pinned Genesis
refs through App.Install or /api/genesis/apps/install"). It would also produce
no `owner/app@hash` pinned ref to verify against.

**Where.** temper `70b76d27`; genesis submodule bump `9718282`. Diagnostic only,
no behaviour change; to be reverted or promoted to a permanent log line once the
cause is known.

## D2: Remove the `Id` workaround rather than keep it

**Decision:** Delete the git-sha comparison in `load_genesis_object_by_key`
instead of the earlier change that made it accept either form, and warn at
registration when a CSDL declares a server-derived field name.

**Came up because.** Rita challenged the pattern — every fix revealing another —
as a symptom of local patches rather than real fixes, and she was right about
this one. `Id`, `id`, `Status`, `status` are formally server-derived
(`temper_spec::automaton::is_server_derived_field_name`), and
`canonicalize_entity_field_map` overwrites them with the entity id and state on
every hydrate. So `fields["Id"]` in that function is *always* the entity id and
never the git sha it was being compared against. Accepting both forms papered
over a comparison that never meant anything.

**Chose deleting the comparison because** it is not needed at all: `entity_id`
is derived from `(repository_id, git_sha)` by `genesis_object_entity_id`, so
once the row is loaded at that key the only independent fact left is whether it
belongs to the requested repository. One meaningful check replaces a meaningless
one.

**Chose warning at registration over rejecting** because Genesis and other
existing apps already declare these names; failing registration would take them
down. The defect being fixed is the *silence* — an app may declare `Id`, nothing
objects, and the value is then destroyed on the actor path while OData still
reports the declared property.

**Options.** Keep the comparison and teach it to accept both the bare sha and
the composite `{repo}-{sha}` entity-id form — the band-aid already written; stop
`canonicalize_entity_field_map` from overwriting `Id` for this entity; remove the
comparison, because a git sha and a server-derived field cannot be equal and the
check never had meaning.

**Chose removal because.** The other two options preserve a check that answers a
question nobody asked: whether a git object's sha matches a field the server
writes. Teaching it both forms would make it pass without making it correct, and
changing canonicalization to suit one caller would move the damage into every
entity. Deleting it loses nothing that was being verified.

**Where.** `crates/temper-platform/src/genesis_install.rs`,
`crates/temper-server/src/registry/mod.rs` (temper `35fd32cd`).

## D3: Require a declared `Size` only where the model has one

**Decision:** Treat a declared length as optional in git object materialization,
rather than requiring `Size` on every kind.

**Came up because.** With the `Id` fix in, the bundle endpoint moved 404 → 500
`Genesis object is missing a non-negative Size`. Only `Blob` declares `Size`;
`Tree`, `Commit` and `Tag` never have. The requirement arrived in temper
`8840b4fd` (2026-07-11) — *after* Genesis pinned its kernel — so the two sides
disagreed about the data model and nothing caught it until the bump.

**Chose correcting the July change over adding `Size` to Genesis's Tree spec**
because a git object's canonical bytes are self-describing and `git_object_body`
already rejects any object whose `{kind} {len}\0` header disagrees with its own
body. `Size` is a redundant second check that only blobs can offer. Adding it to
trees would mean a data migration over every git object row to satisfy a check
that adds nothing.

**Given up:** where no length is declared, the exact-length assertion no longer
runs; the budget is charged from an upper bound on the encoded length instead,
so materialization stays bounded.

**Options.** Require `Size` everywhere and add the field to the models that
lack it; default a missing `Size` to zero and carry on; require it only of models
that declare it.

**Chose the declaring models because.** Adding the field to models that have no
size to report invents data. A zero default is worse — it reads as a real,
verified length of nothing, which is exactly the class of bug D6 came from.

**Where.** `crates/temper-platform/src/genesis_install/blob_materialization.rs`
(temper `e18de36b`).

## D4: Serve a public app bundle without a credential

**Decision:** Allow `GET /api/genesis/apps/{owner}/{name}/versions/{hash}/bundle`
through the edge and refuse inside the handler unless the backing repository is
public, rather than adding registry-credential plumbing to the install client.

**Came up because.** The install client sends only `X-Tenant-Id` and no bearer,
while the bundle endpoint required an authenticated context — verified on
production, 401. There is no registry credential anywhere in the kernel for the
client to send.

**Options.** Add a credential to the installing kernel and send it as a bearer;
add a `registry_token` to the install request; serve public bundles anonymously.

**Chose the public-bundle path because** the same content is *already* served to
anonymous callers over `git clone` — requiring a credential for one encoding and
not the other was inconsistent rather than protective. It is also the only option
that ships entirely on Genesis: the others change the kernel the installing side
runs (openpaw), which would mean a coordinated deploy of the very service the
factory work depends on, to unblock the factory work.

**Given up / bounded:** the handler trusts `X-Tenant-Id` as a namespace selector
on this path. That cannot escalate anything, because the only rows reachable are
ones already world-readable over git, and a non-public repository still answers
401. Recorded rather than hidden.

**Where.** `crates/temper-platform/src/tenant_api/apps.rs`,
`crates/temper-server/src/authz/edge.rs` (temper `795934a2`).

## D5: Give the streaming blob read the same legacy fallback as the buffered read

**Decision:** Move the legacy DB blob-store fallback into `stream_blob_object`,
adding `BlobObjectStream::from_bytes` so the legacy store's bytes are returned in
the same shape as the object store's stream.

**Came up because.** After the permit above, the blob returned **200** over HTTP
and the bundle *still* reported it missing. The kernel has two blob reads and
they disagreed:

```rust
get_blob_with_legacy_fallback(...)   // object store, then legacy DB store
stream_blob_object(...)              // object store only
```

Objects written before the object-store migration live in the legacy DB store.
The HTTP blob route finds them; the streaming read the bundle uses did not. The
same blob was simultaneously readable and "not found".

**Chose fixing the kernel over republishing the app because** the data was never
missing — republishing would have rewritten blobs to work around a reader that
cannot see half its own store, leaving every previously-written object still
unreadable through the streaming path.

**Chose the shared shape deliberately:** callers cannot tell which store
answered. Two reads of the same store disagreeing about what exists is the bug;
hiding the difference behind one type is the fix, not an abstraction for its own
sake.

**Options.** Leave the streaming read without the fallback, as the comment
deliberately intended, and let callers retry on the buffered path; duplicate the
buffered read's logic in the streaming path; give the streaming read the same
fallback, bounded before it materializes anything.

**Chose the bounded fallback because.** The first option is what shipped, and it
means the two read paths disagree about whether content exists — one of the
recurring failures of this effort. The second duplicates logic that will drift.
Bounding first preserves the reason the fallback was originally left out (a
streaming read must not materialize an unbounded blob) while removing the
disagreement.

**Where.** `crates/temper-server/src/blob_store/state.rs`,
`crates/temper-server/src/blob_store/streaming.rs` (temper `3cc6461e`).

**Pattern across this effort, worth stating once.** Three separate failures had
one shape — two paths to the same data that do not agree:

1. `Id` means the domain field over OData and the entity id through the actor.
2. `http_call` is served in-process while the streaming host path is delegated out.
3. Blob existence differs between the buffered and streaming reads.

Each surfaced as a misleading error far from its cause ("commit not found",
"401", "blob not found") on data that was present and correct. When a lookup
insists something is missing that you can see with your own eyes, suspect a
second read path before suspecting the data.

## D6: Do not assert a decoded length the model never declared

**Decision:** Route the undeclared-length overflow read through the bounded
stream and decode it directly, instead of the JSON-base64 stream decoder.

**Came up because.** This was my own regression from the earlier `Size` change.
With no declared `Size` I passed the 16 MiB ceiling as the decoder's
`expected_decoded_bytes` — but that argument is an *exact expectation*, not a
cap, so every tree failed with `decoded blob ended at 328 bytes; expected
16777216`. 328 was the correct size; 16777216 was a number I invented.

**Chose decoding directly over deriving the length because** base64 padding makes
the exact decoded length underivable from the encoded length: `serialized_bytes`
maps to three possible decoded sizes, which is precisely why the code demanded a
declared `Size` in the first place. There is genuinely nothing to assert here, so
asserting anything would be a guess dressed as a check. `git_object_body` already
rejects any object whose `{kind} {len}\0` header disagrees with its own body,
which is the real integrity check and is independent of the declared size.

**Second correction in the same commit — a documented intent I had contradicted.**
`stream_blob_object` carried a comment stating that large field-overflow objects
are deliberately *not* read from the legacy database fallback, because that
interface is buffered. My previous commit added exactly that fallback and read
the whole object before checking its size, silently overriding a
recorded decision. The fallback now asks the store to bound the read
(`get_blob_if_size_at_most`) so it never materializes an object above the
caller's ceiling, and the comment says what the code actually does. The intent —
never buffer a large blob — is preserved; only the "therefore pretend it does not
exist" part is gone.

**Options.** Pass the 16 MiB cap as `expected_decoded_bytes` — what I had
written, and wrong, because that field is an exact assertion, not a ceiling, so a
328-byte blob failed as "expected 16777216"; drop the length assertion
altogether; make the expected length optional and assert it only where the model
declares one.

**Chose the optional length because.** Dropping the assertion loses a real check
on the blobs that do declare a size. Passing a cap into an equality is a category
error. Returning `Option` makes the distinction explicit at the type, so the
undeclared case cannot be silently compared against anything.

**Where.** `crates/temper-platform/src/genesis_install/blob_materialization.rs`,
`crates/temper-server/src/blob_store/state.rs` (temper `c3595b47`).

**Note on method.** Two of the defects in this effort were mine, introduced while
fixing something else, and both were caught only because each fix was verified by
its effect rather than assumed. Deploying and re-reading the actual error is what
kept the chain honest.

## D7: A guest's internal calls run as the guest, not as its caller

**Decision:** For the HttpEndpoint path, bind the guest's internal HTTP
capability to the module's own principal rather than to the inbound caller's
security context. Rita chose this over two narrower options.

**Came up because.** `git push` returned 401 with a valid GitToken, and the
failure was symmetric in a way that made it hard to see:

- **Anonymous caller:** resolving the presented token means reading a `GitToken`
  row. Bound to the anonymous caller, that read is denied, the lookup returns
  nothing, and the guest reports anonymous. Authentication cannot run as the
  identity it is about to establish.
- **Authenticated caller — worse:** the same secret turned out to be registered
  *both* as `gt-paw-agent`'s `HashedSecret` and as the id of an Active
  `AgentCredential`. So the edge authenticated the push, the request was not
  anonymous, and the lookup ran as that principal — which has no GitToken read
  permission either. Being authenticated at the edge was strictly worse than
  arriving anonymous.

My first attempt fixed only the anonymous branch and did nothing, because the
caller was never anonymous. That is recorded here because the wrong fix looked
right and shipped.

**Options.**
- *(A, chosen)* Guest always acts as its own module principal.
- *(B)* Permit the AgentCredential's principal to read GitTokens — smaller, but
  papers over the layering and needs repeating for every future caller.
- *(C)* Un-register the AgentCredential so pushes arrive anonymous — trivial, but
  fixes one token and leaves the next person to rediscover it.

**Chose A because** an HttpEndpoint guest is the enforcement point for its own
protocol. Genesis resolves the token itself and applies repository authorization
inside the module; it cannot delegate that to the kernel, because the resolved
git principal has no way to reach the kernel — the headers that carried it are
stripped on purpose (ARN-208/255). Making the guest act as itself matches what it
actually is, and leaves authorization to the tenant's policy, where each module's
reach is narrow by construction.

**Given up, deliberately:** a guest no longer inherits its caller's reach for
internal calls. That is correct for a protocol guest, which was never enforcing
on the caller's behalf, and would be wrong for a guest that expects the kernel to
scope its reads — so it applies to the HttpEndpoint path only, not to
action-triggered integrations.

**Where.** `crates/temper-server/src/state/dispatch/wasm.rs` (temper `12c590de`),
with `policies/git_token.cedar` granting the six wire modules read/list and
MarkUsed.

## D8: Raise the bundle byte budget rather than shrink the app

**Decision:** `MAX_GENESIS_BUNDLE_TOTAL_BYTES` 64 MiB → 256 MiB. Rita chose this
over stripping symbol names from the WASM modules.

**Came up because.** dsf-factory is ~52 MB of legitimate compiled WASM — 59
modules, one per resource operation, averaging 638 KB. Against a 64 MiB total
that leaves ~12 MB for every app in its dependency closure combined. Verified it
is not junk: no `target/`, no debug sections; the size is real code carrying Rust
symbol names.

**Options.** Strip symbol names (30–50% smaller, loses function names in stack
traces); raise the budget; reduce module count (a redesign).

**Chose raising it because** the budget's job is to bound how much one install
may materialize, not to cap an app below a size the platform's own apps already
exceed. Stripping trades debuggability for headroom we can simply grant, and the
module count is a design question that should not be forced by an install limit.

**Kept unchanged:** per-file 16 MiB and file-count 4096. Those are what actually
catch a runaway publish — the paw-fs case tripped the aggregate only incidentally,
and weakening them while relieving the total would have removed the real guard.

**Where.** `crates/temper-platform/src/genesis_install/bundles.rs`.

## D9: Let a protocol handler see the credential it is required to resolve

**Decision:** Add `ForwardsCredential` to HttpEndpoint, off by default, and
default it on for the six handlers that implement credential-carrying protocols.
Rita chose this over having the kernel resolve GitTokens itself.

**Came up because.** `git push` answered 401 with a valid token, and three
successive fixes changed nothing. Instrumenting `resolve_principal` produced no
log at all — which was the answer: it returns anonymous on its *first* line, the
one exit I had not instrumented.

`guest_visible_headers` strips `Authorization` before a guest sees it (ARN-208:
a caller credential must never reach a WASM guest). Genesis authenticates git
callers by reading the GitToken from that header. So `extract_token` found
nothing, every request was anonymous, and the kernel was removing the only thing
the app could authenticate with.

My three earlier attempts were all downstream of this: I kept repairing what
happens *after* the token is found while the token never arrived.

**Options.** *(A)* Kernel resolves the GitToken and passes the identity —
honours the invariant, but teaches the kernel a Genesis concept and is real
design work. *(B, chosen)* Endpoints opt into seeing the header. *(C)* Move git
auth out of the guest entirely — cleanest, largest.

**Chose B, scoped so the invariant survives.** Off by default; the original test
proving credentials never reach a guest is unchanged and still passes. The six
opted-in handlers are keyed on integration module and overridable per endpoint —
the same shape as the pack-size defaults already in that function.

**Why this is not a hole in ARN-208.** The invariant protects against a guest
inheriting a *caller's kernel authority*. The header these endpoints receive is a
GitToken in HTTP Basic: opaque to the kernel, carrying no kernel authority, and
the app's to resolve. Withholding it protects nothing and guarantees every
authenticated git request arrives anonymous. What is forwarded is not a
credential the kernel could act on.

**Given up:** these six endpoints now see an inbound `Authorization` header, so a
compromised git guest could read a token presented to it. That is the same token
it is being asked to authenticate, so the exposure is bounded to the request's
own credential — it gains nothing it was not already handed.

**Covered by test in both directions:** stripped unless opted in, present when
opted in, so a future widening has to defeat an assertion rather than slip past.

**Where.** `crates/temper-server/src/http_endpoint.rs`,
`crates/temper-server/src/router.rs`, `router_test.rs` (temper `958d1efa`).

**Method note.** Every exit from `resolve_principal` returns `anonymous`, so a
broken lookup and a bad token are indistinguishable from outside — three wrong
fixes came from that. The function is now instrumented at all five exits.

## D10: Honour the credential opt-in where the header is actually removed

**Decision:** Apply `ForwardsCredential` in `bearer_auth`, at both the
authenticated and public-route branches, not only in the router's header filter.

**Came up because.** The previous decision added the opt-in to
`guest_visible_headers` and it changed nothing — the fifth failed attempt at this
bug. `bearer_auth` middleware calls `req.headers_mut().remove("authorization")`
on the request itself, *before* the router runs, so the router was filtering a
header that had already been deleted.

Genesis's git endpoints declare `RequiresAuth=false` — deliberately, so the guest
can issue the smart-HTTP challenge itself — which routes them through the public
branch: header removed, anonymous context inserted, every valid GitToken arriving
as anonymous.

**How it was finally found.** By observation, not reasoning. Four inferences were
wrong. Logging the header names the guest actually received
(`["host","user-agent","accept",…]` — no `authorization`) settled it in one
request, and a curl with an explicit `Authorization: Basic` header proved the
kernel was stripping it rather than git failing to send it.

**What I got wrong and why it matters.** I searched for callers of the *filter
helper*, found one, and treated that as complete. The thing to search for was
every site that touches the header — `grep -rn 'remove("authorization")'` finds
`bearer_auth` immediately. My test passed the whole time: it proved the router's
filter worked, and never proved the header survived to the guest. A green test on
a layer the failure does not traverse argues actively for a wrong conclusion.

**Tests.** Both directions, at the layer that strips: an opted-in protocol route
keeps the header; a route without the opt-in still loses it. All 18 existing
`bearer_auth` tests pass unchanged, so ARN-208 holds everywhere it did before.

**Options.** Keep filtering in the router's `guest_visible_headers` only — what
was already there, and it changed nothing; re-attach the header in the router
after `bearer_auth` had removed it; honour the opt-in at both sites in
`bearer_auth` that actually remove it.

**Chose the removal sites because.** The router cannot filter a header that no
longer exists, which is why four inferences and a passing test all pointed the
wrong way. Re-attaching would mean reconstructing a credential the kernel had
just discarded, in a component with no business holding one. Guarding the removal
itself means the header is never lost in the first place, and the guard sits
where a reader looking for "who deletes this?" will find it.

**Where.** `crates/temper-platform/src/bearer_auth.rs` and its tests
(temper `667caada`).

## D11: Install an approved policy into the authorization engine when it activates

**Decision:** Add a `PolicyActivated` consumer that reloads the tenant's Cedar
policy set at the moment a Policy entity reaches `Active`, before the row is
persisted as activated.

**Came up because.** The event existed and nothing listened to it. A policy could
be materialized by an install, transitioned to `Active`, and reported as applied,
while the authorization engine went on evaluating the policy set it loaded at
startup. The effect was ARN-164: every newly installed app's collections answered
403 until a human made a manual policy API call. The install said it succeeded;
nothing about the decision changed.

**Options.** Reload the whole tenant policy set on every authorization check
(correct, and pays the cost on the hot path); poll for changed Policy rows;
consume the activation event that is already published.

**Chose the event consumer because.** The publisher was already there — the gap
was a missing subscriber, not missing machinery. Reloading per check would put a
database read in front of every Cedar decision. Polling would reintroduce the
delay the event exists to remove.

**Ordering matters here.** The reload happens *before* `persist_and_activate_policy`,
not after. If the reload fails, the row is not marked active — so the state that
claims a policy is in force cannot outrun the engine that enforces it. This is
the ARN-497 class: a governed action that reports success without having had an
effect.

**Where.** `crates/temper-platform/src/policy_activation.rs` (new), wired in
`crates/temper-cli/src/serve/mod.rs` beside `spawn_reconciler` (temper `0d63e12e`).
