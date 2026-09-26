# Hunter Global: Phase 1 foundation

The existing US 5,706 and HK 2,760 BASE identities and VERIFIED receipts are
read-only. The Bridge rejects all writes under `US/BASE/*` and `HK/BASE/*`.
It accepts `append` only for new DAILY, REPAIR_PATCH and CORPORATE_ACTIONS
segments. DAILY checkpoints and run logs live under `CONTROL/`.

## Release order

1. Review this change and its offline tests. Do not enable the maintenance
   workflow until the new Bridge deployment is live.
2. Replace only the existing `Gateway.gs` content in `HUNTER_GLOBAL_BRIDGE`
   and update the existing Web app deployment without changing its access.
3. Send one `put` request with a valid authenticated payload to a BASE path.
   It must return `BASE_SEALED`, and the original file SHA must be identical.
   Test DAILY `put` rejection (`DAILY_APPEND_ONLY`) and `append` of a new test
   path; a second append to the same path must return `APPEND_CONFLICT`.
4. After Bridge readback, merge the worker and dispatch a `probe`, then sample
   US, HK, a historical split, and one repair sidecar. Dispatch DAILY twice on
   the same closed date and confirm that the second run added zero bar rows.
5. Set `HUNTER_FOUNDATION_CUTOVER=CONFIRMED` only after these checks.
   The existing `HUNTER_ACTIONS_CUTOVER` controls the DAILY schedule.

The worker reads `CURRENT_UNIVERSE.json` and seeds it once from the frozen BASE
universe. Weekly refreshes read the same official Nasdaq Trader and HKEX
listing sources, retain old security IDs, and append new IDs. Missing entries
or changed identities go to review. Only independently confirmed delistings
may become inactive. Yahoo HTTP 404 is not delisting evidence.

Historical corrections are accepted sidecars, leaving BASE bytes intact.
The query composition order is BASE, accepted REPAIR_PATCH, DAILY. DERIVED
contains split-adjusted prices, moving averages, low-point context, RSI,
MACD, volume and liquidity, and a research rank. Relative strength remains
unset until the user supplies a BENCHMARK list. MAE/MFE requires an explicit
signal or entry anchor; it is calculated for the underlying only. Options
availability is refreshed monthly and never asserts vertical usability without
expiry, strike and bid/ask data.

`tests/test_foundation.py` and `tests/test_bridge.js` cover the local data and
Bridge path guards. Live release gates and `HUNTER_V1_DATA_FOUNDATION=COMPLETE`
require real Bridge, Drive, source and Actions evidence; offline tests cannot
grant that status. Phase 2 sector/industry and earnings calendar are excluded.
