"""Dual-market monthly orchestration.

US keeps the existing V2 D1 + vertical baseline worker. HK runs its independent
STOCK_EDGE LONG/SHORT recertification. ACTIVE_POINTER and the monthly notice
are committed only after both sides finish successfully.
"""
from __future__ import annotations

import datetime as dt
import json

from monthly_v2 import _compact, _sha, run_monthly
from hk_monthly import run_hk_monthly


def run_combined_monthly(drive, snapshot_name: str | None = None,
                         validation: bool = False) -> dict:
    prior_raw = drive.read("ACTIVE_POINTER") if drive.file("ACTIVE_POINTER") else None
    prior = json.loads(prior_raw) if prior_raw else None

    us = run_monthly(drive, snapshot_name=snapshot_name, validation=validation,
                     commit_pointer=False)
    hk = run_hk_monthly(drive, validation=validation)

    us_month = us["month"]
    hk_month = hk["month"]
    expected_hk = "HK_" + us_month
    if hk_month != expected_hk:
        raise RuntimeError(f"MONTH_MARKET_MISMATCH:US={us_month}:HK={hk_month}")

    up = us["pointer"]
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    same_month = bool(
        prior and prior.get("month_file") == us_month and
        prior.get("hk_month_file") == hk_month
    )
    version = int(prior.get("version", 1)) if same_month else (
        1 if not prior else int(prior.get("version", 0)) + 1
    )

    previous = None
    if prior:
        previous = {
            "month_file": prior.get("month_file"),
            "long_direction_signal": prior.get("long_direction_signal"),
            "short_direction_signal": prior.get("short_direction_signal"),
            "long_vertical_baseline": prior.get("long_vertical_baseline"),
            "short_vertical_baseline": prior.get("short_vertical_baseline"),
            "hk_month_file": prior.get("hk_month_file"),
            "hk_long_edge": prior.get("hk_long_edge"),
            "hk_short_edge": prior.get("hk_short_edge"),
            "hk_shortable_list_version": prior.get("hk_shortable_list_version"),
        }

    pointer = {
        "schema": "INVESTMENT_V2_ACTIVE_POINTER_V4",
        "version": version,
        "month_file": us_month,
        "snapshot": us["snapshot"],
        "as_of": up["as_of"],
        "long_direction_signal": up["long_direction_signal"],
        "short_direction_signal": up["short_direction_signal"],
        "long_vertical_baseline": up["long_vertical_baseline"],
        "short_vertical_baseline": up["short_vertical_baseline"],
        "direction_detail": up["direction_detail"],
        "hk_month_file": hk_month,
        "hk_snapshot": hk["snapshot"],
        "hk_as_of": hk["as_of"],
        "hk_long_edge": hk["long"],
        "hk_short_edge": hk["short"],
        "hk_shortable_list_version": hk["shortable_list_version"],
        "hk_shortable_effective_date": hk["shortable_effective_date"],
        "hk_vertical_overlay": hk["hk_vertical_overlay"],
        "previous": previous,
        "month_update_pending": True,
        "writer": "HUNTER_MONTHLY_US_HK_V1",
        "written_at_myt": now,
    }

    if same_month:
        compare_keys = (
            "snapshot", "as_of", "long_direction_signal", "short_direction_signal",
            "long_vertical_baseline", "short_vertical_baseline", "direction_detail",
            "hk_snapshot", "hk_as_of", "hk_long_edge", "hk_short_edge",
            "hk_shortable_list_version", "hk_shortable_effective_date",
            "hk_vertical_overlay",
        )
        conflicts = [k for k in compare_keys if prior.get(k) != pointer.get(k)]
        if conflicts:
            raise RuntimeError("ACTIVE_POINTER_SAME_MONTH_CONFLICT:" + ",".join(conflicts))
        pointer = prior
    else:
        drive.put("ACTIVE_POINTER", _compact(pointer),
                  expected_sha=_sha(prior_raw) if prior_raw is not None else None)

    notice_path = "US/CONTROL/MONTH_NOTICE.json"
    notice_raw = drive.read(notice_path) if drive.file(notice_path) else None
    notice = {
        "schema": "INVESTMENT_V2_MONTH_NOTICE_V3",
        "month_file": us_month,
        "hk_month_file": hk_month,
        "pending": True,
        "pointer_version": pointer["version"],
        "created_at_myt": now,
        "previous": pointer.get("previous"),
        "current": {
            "US": {
                "long_direction_signal": pointer["long_direction_signal"],
                "short_direction_signal": pointer["short_direction_signal"],
                "long_vertical_baseline": pointer["long_vertical_baseline"],
                "short_vertical_baseline": pointer["short_vertical_baseline"],
            },
            "HK": {
                "long_edge": pointer["hk_long_edge"],
                "short_edge": pointer["hk_short_edge"],
                "shortable_list_version": pointer["hk_shortable_list_version"],
                "hk_vertical_overlay": pointer["hk_vertical_overlay"],
            },
        },
    }
    drive.put(notice_path, _compact(notice),
              expected_sha=_sha(notice_raw) if notice_raw is not None else None)

    return {"US": us, "HK": hk, "pointer": pointer, "validation": {
        "US": us.get("validation"), "HK": hk.get("validation")
    }}
