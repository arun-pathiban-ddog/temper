# ARN-512 decisions

## D1 — fix the kernel, not the page

**Decision:** teach `temper-spec` to keep schema-level annotations rather than
have Foundry infer a twin some other way.

**Came up because:** the Twins page was empty; the annotation it looks for is
in the source schema and absent from `$metadata`.

**Options:** (a) kernel: add `Schema.annotations`, parse, merge, emit;
(b) Foundry: treat any schema whose entities carry `Temper.Role` as a twin;
(c) move the annotation onto an entity or the container.

**Chose (a) over (b)/(c) because:** the kernel losing part of a document on a
round trip is the defect; (b) is a heuristic that loses the display name, and
(c) moves the marker to a place that does not describe the schema. Gave up:
a kernel deploy instead of a Foundry-only ship.

**Where:** `crates/temper-spec/src/csdl/{types,parser/schema,merge,emit}.rs`.

## D2 — on merge, the incoming value for a term wins

**Decision:** `merge_csdl` replaces a schema annotation by term and keeps the
terms the incoming schema does not mention.

**Came up because:** production loads schemas through the merge path
(`load-inline`), so parse+emit alone would still show nothing.

**Options:** replace-by-term (chosen); append-missing (an old name would never
change); replace the whole list (a partial reload would erase other terms).

**Where:** `crates/temper-spec/src/csdl/merge.rs`, `merge_schema`.
