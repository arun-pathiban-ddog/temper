# Spec

One definition. `temper-runtime` owns the constant; `temper-server`
re-exports it from the same path callers already import. No behaviour
change: both copies were 8. A second definition becomes a compile error,
which is the strongest available enforcement and needs no test.

Out of scope: `temper_runtime::reaction::ReactionRegistry`'s assertion on
`MAX_REACTIONS_PER_ACTOR`, which is the same class of budget the kernel
removed on ARN-467 but has no production callers today.
