# Immutable cache pack recovery — 2026-10-08

## Observed failure

The 23:49 MYT preflight rejected the US execution before building or deploying.
Its traceback identified InputCache._flush -> writer.put(pack_path(...)) ->
Drive._continuation_write, ending in an HTTP 404 from the Google response host.
This was a rotating input-cache pack write, not a derived batch write.

## Change and audit

- Continuation-mode cache packs now use the content SHA-256 as their filename
  slot and immutable=True. Exact-content acknowledgment recovery therefore uses
  the existing Bridge lock/hash branch instead of overwriting a committed pack.
- Existing numeric cache slots remain readable. They are not deleted or migrated
  eagerly. Completed derived-batch code and calculation signatures are unchanged.
- Pack acknowledgment precedes manifest CAS publication. A failed pack write
  leaves the previous committed manifest and its referenced pack intact.
- The controller permits handoff only for recognized source images and the
  exact cache-pack write stack plus the observed transport failure classes.
  Manifest CAS failures, mutable output failures and integrity errors remain
  excluded. This does not add mutable-write retries.
- Four-way bounded checkpoint-piece restoration and refusal to silently rebuild
  an unusable committed cache remain enabled from the preceding release.
- Active executions are preserved. Both job templates receive the new pinned
  worker image; no rollback or historical worker launch is introduced.

## Verification

136 Python test executions passed across immutable packs, deployment preflight,
restoration concurrency, cache refusal, acknowledgment recovery, input packs,
continuation policy, derived resume, durable reads and Phase 2 persistence.
Shell syntax and Python compilation passed.

Focused cases cover legacy-pack compatibility, immutable content naming, old
manifest preservation on lost pack acknowledgment, subsequent pack updates,
invalid hash-slot rejection, the actual US failure stack and rejection of a
manifest-write failure stack.

These are local code/test results, not a production acceptance claim. No live
Cloud Run deployment or completion time is asserted. A restarted process still
must restore inputs needed by unfinished work; this change does not promise
zero input reads or instantaneous Phase 2 completion.
