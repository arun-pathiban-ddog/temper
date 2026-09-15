# Implementation plan

1. Instrument the Genesis object lookup so the 404 names its own cause, rather
   than inferring one.
2. Remove the sha-versus-server-derived-field comparison that the instrumentation
   exposes, instead of teaching it to accept both forms.
3. Correct the blob read paths: legacy fallback on the streaming read, a declared
   `Size` only where the model declares one, no asserted decode length where the
   model declares none, public bundles without a credential.
4. Give a guest its own module identity on both host paths, and address the
   kernel by its loopback origin.
5. Let a protocol route opt into receiving the credential it must resolve, and
   honour that opt-in at the site that actually removes the header — verified by
   observing the header names the guest receives, not by reasoning about them.
6. Move the `PolicyActivated` consumer to its own effort (ARN-505): it is new
   stateful machinery, nothing about running Genesis depends on it, and it
   earned its own review rather than riding along with this one.
7. Run the workspace suite; prove the whole path live against Genesis production
   with a real clone, fetch and authenticated push.
