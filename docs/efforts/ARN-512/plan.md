# ARN-512 plan

1. Red: a round-trip test with a schema-level `Temper.Twin` (String) and a
   Collection annotation, asserting both parse and survive emit. It does not
   compile: `Schema` has no `annotations`.
2. Add `Schema.annotations: Vec<Annotation>`; parse `Annotation` as a child of
   `Schema` in both element forms; emit after the terms; construct the field in
   the two test fixtures.
3. Merge: replace by term, keep the rest; test.
4. Mutation-check each new test by removing its rule.
5. `cargo check --workspace --tests`.
6. Live local: serve this kernel with an isolated `TURSO_URL`, load
   temperpaw's DSF schema through `/api/specs/load-inline`, read `$metadata`
   before and after; the marker must appear only after.
7. Panel (delta rounds), proof record, merge, kernel deploy, reload the DSF
   schema in production, Twins page shows "Deep Sci-Fi".

Expected end state: production `$metadata` carries `Temper.Twin` under
`Dsf.Twin`, and the Twins page lists the DSF twin.
