# ARN-500: one definition of MAX_REACTION_DEPTH

`MAX_REACTION_DEPTH` was defined twice, independently, in `temper-runtime`
and `temper-server`, with the three enforcement sites split across the two
copies and nothing keeping them equal. ADR-0176 rests on this bound being
the one that still holds, so it must not be a number that can drift from
itself. Found by the independent verifier on nerdsane/temper#464.
