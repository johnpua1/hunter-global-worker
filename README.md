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

## Google authorization and safe handoff

The live Apps Script project has `hunterUSWorker` and `hunterHKWorker` triggers.
Keep them enabled until a GitHub **probe** can read Drive and Yahoo. The Google
Drive connector authorization available to ChatGPT Work cannot be transferred
to GitHub. The Drive owner must complete a one-time Google OAuth offline
consent flow. Add these values through GitHub Actions **Secrets**, never in a
commit, issue, or chat message:

- `HUNTER_GLOBAL_FOLDER_ID`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`

Then dispatch `probe` and verify both markets. Wait for Apps Script's current
executions to finish, turn off both triggers, and reread checkpoint/receipts.
Only then add secret `HUNTER_SINGLE_WRITER_CUTOVER=CONFIRMED` and repository
variable `HUNTER_ACTIONS_CUTOVER=CONFIRMED`. Dispatch `base`; the US and HK
jobs run independently. Re-dispatch `base` if a job times out. Each scheduled
run also resumes incomplete base batches automatically; verified batches are
skipped, and an incomplete unverified batch is rebuilt. Once a market's base
is complete, its scheduled job appends missed sessions in date order.
The workflow never runs untrusted pull requests with Drive secrets.

For a read-only local probe, use `python runner.py --mode probe --market US` or
`--market HK`. A job without the three Google OAuth values can test Yahoo
egress, but does **not** prove Drive read access.
