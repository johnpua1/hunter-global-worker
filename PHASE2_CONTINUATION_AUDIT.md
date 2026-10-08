# Phase 2 continuation release — code audit and tests

Date: 2026-10-08 (MYT). Scope: US/HK daily workers and their deployment controller.

## Implemented

- Reuse an existing COMPLETE daily-run record while continuing unfinished ranking; do not repeat its market fetch/append.
- Persist each completed pre-ranking batch under an input/code-bound continuation key. Resume reads saved calculation state and skips that batch's BASE read and calculation.
- Record output write intents and acknowledged commits. Completed output receipts skip reads and writes of the published files.
- Confirm worker writes from the Bridge response without downloading written files for readback. A response with an incorrect hash or an unknown outcome cannot count as success.
- Do not blindly retry a write with unknown outcome, including during cache cleanup after an exception.
- Reuse needed input bytes within one worker execution. Necessary inputs and continuation state remain readable under the boundary accepted by the user.
- Accept completion from committed stage records, without rescanning RANK and detail files.
- Deploy both daily job templates to one pinned new image. No rollback or launch of a previous image. Cancel old executions only after both templates are confirmed; retain stored data.

## Audit

Reviewed calculation identity, interruption boundaries, write acknowledgement handling, exception cleanup, same-execution input reuse, stage completion, deployment ordering, credential-safe error output, and preservation of unrelated job configuration.

Corrected findings before the final test run:

1. Cache cleanup could retry writes after an ambiguous response: block subsequent writes and skip that cleanup flush.
2. Execution version inspection could reject legitimate Phase-2-only arguments: inspect its single container's source identity without imposing template arguments.
3. Completing an already completed publication could rewrite its receipt: make that operation a no-op.
4. A completed daily record still encountered a redundant external source gate: skip that gate for an acknowledged completed date.
5. A fixed retry counter could stop a progressing batch calculation: reset the stalled retry counter only once per execution that saved new derived batches.
6. Work-budget expiry between a result and its saved receipt could discard progress: use the existing bounded checkpoint reserve for completed-result publication.

## Final tests

106 tests passed. Command:

```sh
PYTHONPATH=/workspace/scratch/13ab152026d1/test-deps:hunter-global:tests python -m unittest test_continuation_policy test_continuation_deploy test_continuation_controller test_derived_resume test_incremental_inputs test_input_pack_concurrency test_patch_snapshot_reuse test_durable_large_reads test_durable_phase2 test_runner_bridge_read_resilience test_pack_write_readback -q
```

Python compilation and shell syntax checks passed. Tests use isolated fixtures and fake cloud clients. No completed production data was rescanned for this audit or test run.

## Operational boundaries

- This is code/test evidence, not evidence of Cloud Run deployment or production Phase 2 acceptance.
- Previously calculated batches that an old execution never saved cannot be retroactively recovered. Newly saved batch results are resumable.
- A PENDING output receipt after an interrupted write remains unresolved; the worker stops instead of replaying that write or asserting completion.
- Required historical inputs may still need restoration after a new container starts. No completion-time guarantee is inferred from the passing tests.
- The deployment controller's closure targets are US 2026-10-07 and HK 2026-10-08. The deployed daily workers continue to select subsequent closed sessions from their daily checkpoints.
