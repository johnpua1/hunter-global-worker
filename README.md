# Hunter Global GitHub Actions worker

The two market jobs read the existing `HUNTER_GLOBAL` checkpoint in My Drive,
finish missing US/HK base batches, and append each missed trading session under
`<market>/DAILY/<date>/`. The existing Apps Script `VERIFIED` receipts are the
authority. Neither source data nor credentials are committed to GitHub.

## Cost and repository scope

The workflow uses the standard `ubuntu-24.04` GitHub-hosted runner. It is
free in this dedicated public repository. The original engineering repository
and its Git history stay private. This repository contains no live OHLC,
checkpoint, research rules, folder IDs, or OAuth credential values.
Do not upload large data as Actions artifacts or caches. Each market job has a
330-minute timeout, below GitHub's six-hour hard limit. Daily jobs run at
00:37 UTC / 08:37 MYT. A public repository's scheduled workflow can be
disabled by GitHub after 60 days without repository activity. The daily
`keepalive` job makes an empty commit to keep repository activity current;
it contains no data. This lowers the inactivity risk but cannot recover from
an already disabled or dropped schedule. Monitor the last successful run.

## Apps Script bridge cutover

The existing Apps Script project is named `HUNTER_GLOBAL_BRIDGE`. Its `Gateway.gs`
handles authenticated file operations only; GitHub runs the US/HK fetcher.
The script property `HUNTER_GLOBAL_FOLDER_ID` points at the existing Drive root.
Run `setupBridgeKey` once in the Apps Script editor; it creates
`BRIDGE_SHARED_KEY` without printing the value. Deploy a Web app as the owner
with access that allows GitHub's unattended HTTP requests. Add these two
repository Actions secrets, keeping the key out of commits and logs:

- `APPS_SCRIPT_WEBAPP_URL`: the deployed `/exec` URL.
- `APPS_SCRIPT_SHARED_KEY`: the exact `BRIDGE_SHARED_KEY` property value.

Dispatch `probe` to check both markets' existing Drive checkpoints and
the source connection. Dispatch `mini` to write one real security per market
under `_BRIDGE_TEST`; this does not modify production checkpoints.
The old Apps Script Hunter handlers are inert. The repository variable
`HUNTER_ACTIONS_CUTOVER=CONFIRMED` enables production jobs. Dispatch `base`
to resume existing batches. Scheduled runs remain in BASE mode until both
markets finish, then write `BASE_COMPLETE.json` and append closed trading
sessions on subsequent schedules. The shared `REPAIR_QUEUE.json` is preserved
and appended with conditional writes. VERIFIED receipts and checkpoints remain
the authority.

For a local read-only check use `python runner.py --mode probe --market US`
or `--market HK`. The bridge accepts only paths under the configured root.
