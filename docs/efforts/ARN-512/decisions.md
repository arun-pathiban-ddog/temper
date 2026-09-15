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

## Proof

- `cargo test -p temper-spec --lib csdl` — 28 passed (three new). Each new test observed failing with its rule removed (emit loop; merge replace).
- `cargo check --workspace --tests` clean.
- Live local: kernel at `f5864c05`, `temper serve --storage turso`; `POST /api/specs/load-inline` with temperpaw's `os-apps/dsf-twin/specs/model.csdl.xml` (nerdsane/temperpaw main) + `experiment.ioa.toml`; `$metadata` before the load: 0 `Temper.Twin`; after: `<Annotation Term="Temper.Twin" String="Deep Sci-Fi"/>` directly under `<Schema Namespace="Dsf.Twin">`. Production, same payload, same day: 0.

## D3 — one inline-value reader for both annotation element forms (review round 1, codex)

**Decision:** the non-self-closing `<Annotation …></Annotation>` reads the same four inline attributes (`String`, `Float`, `Bool`, `Int`) as the self-closing form; `parse_inline_annotation_override` now defers to `parse_inline_annotation_value`.

**Came up because:** codex showed `<Annotation Term="Temper.Enabled" Bool="true"></Annotation>` under `<Schema>` would emit as `String=""`. The two readers had drifted before this change (entity-level had the same gap); this PR promises both forms survive, so the gap is in scope.

**Options:** leave it (pre-existing); fix only the schema path (a third reader); unify (chosen).

**Where:** `crates/temper-spec/src/csdl/parser/elements.rs`, test `non_self_closing_annotations_keep_bool_and_int_values`.
