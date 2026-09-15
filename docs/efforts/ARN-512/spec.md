# ARN-512 spec — schema-level CSDL annotations

## Contract

A CSDL document survives its own round trip through the kernel: for every
`<Annotation>` that is a direct child of `<Schema>`, `parse_csdl` produces an
`Annotation` on `Schema.annotations`, `emit_csdl_xml` writes it back under the
same schema, and `parse_csdl(emit_csdl_xml(doc))` yields the same term and
value. This holds for both element forms (`<Annotation …/>` and
`<Annotation …>…</Annotation>`), for the four inline values (`String`, `Float`,
`Bool`, `Int`), and for `Collection` and `Record` children.

`merge_csdl` keeps the existing schema's annotations, replaces any whose term
the incoming schema also carries, and appends the rest of the incoming ones.

Entity-level annotations are unchanged.

## Why

Foundry's Twins page lists a schema as a twin when `$metadata` shows
`<Annotation Term="Temper.Twin">` directly under its `<Schema>`; `$metadata`
is `emit_csdl_xml` of the tenant's merged document. Without this contract the
marker is lost on load and the page is empty.

## Out of scope

Annotations on containers, entity sets, actions and properties beyond what
the parser already keeps; entity unescaping of annotation text (a separate,
pre-existing gap noted in `parser/elements.rs`).

## Targeted blocks (added 2026-09-15)

The same holds for `<Annotations Target="…">` blocks that are direct children
of `<Schema>`: each round-trips with its target and its annotations, and
`merge_csdl` replaces a block whose target the incoming schema also carries.
