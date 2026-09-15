# Plan

1. Replace the `temper-server` definition with `pub use temper_runtime::reaction::MAX_REACTION_DEPTH;`.
2. Build; confirm exactly one definition remains in the workspace.
3. Run the trigger unit tests and the two reaction integration suites.
4. Panel, then merge.
