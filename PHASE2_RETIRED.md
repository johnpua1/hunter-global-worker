# Phase 2 permanently retired

User decision: 2026-10-09 01:15:35 MYT (Asia/Kuala_Lumpur).
Scope: Hunter Phase 2, both US and HK.

Status: RETIRED_BY_USER. Not accepted, not completed.

- Cancel the pending Phase 2 ranking, repair, closeout and acceptance work.
- Do not launch, resume, redeploy or automatically recover Phase 2.
- Do not use any former Phase 2 worker, closeout script or rank-only module
  as a fallback or as a hidden part of daily updates.
- Earlier permissions to finish Phase 2 are superseded by this decision.
- Preserve existing data and files; this instruction does not authorize deletion.
- Do not write a successful completion/acceptance marker.
- This decision does not itself cancel unrelated daily data updates. Their
  no-history-read requirement remains in force, and daily automation must not
  be described as working independently until that separation is implemented.

## Operational evidence

This is the recorded user decision and project rule, not a runtime kill switch.
No cloud execution cancellation or scheduler change has been performed by
this commit. Existing cloud execution and scheduler status is UNCONFIRMED.
Do not claim that cloud processes have stopped from this document alone.
