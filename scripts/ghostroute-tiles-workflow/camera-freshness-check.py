#!/usr/bin/env python3
"""
Camera freshness WATCHDOG — fails loudly when the served camera catalog goes stale.

The 2026-08-20..09-02 outage was silent for 14 days: the daily build kept
publishing per-state assets while `cameras-catalog.json` — the ONE artifact every
device gates its downloads on — froze. No alarm existed. This watchdog is the
alarm, and it is deliberately cause-agnostic: it checks the OUTCOME (is the
served catalog fresh?), so it catches failure modes nobody predicted, not just
the two bugs fixed in 2026-09-03.

Decision logic is unit-tested in test_camera_freshness_check.py. The workflow
(cameras-freshness-watch.yml) does the I/O: fetch the release, call this, and on
a non-zero exit open/update a GitHub issue.

Signals:
  HARD FAIL — the catalog asset itself is older than `threshold_hours`. Devices
              are being served stale metadata right now; nothing new reaches them.
  WARN      — the catalog is fresh but an individual state's `generatedAt` has
              not moved in > 2x threshold (the Wyoming shape: one state blocked
              while everything else flows). Visible, not a page.
"""
import argparse
import datetime
import json
import sys

DEFAULT_THRESHOLD_HOURS = 26  # daily job + margin for a slow run
STUCK_STATE_MULTIPLIER = 2


class Result:
    def __init__(self, stale, age_hours, message, stuck_states=None):
        self.stale = stale
        self.age_hours = age_hours
        self.message = message
        self.stuck_states = stuck_states or []


def _age_hours(iso, now_iso):
    then = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    now = datetime.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    return (now - then).total_seconds() / 3600.0


def evaluate_freshness(catalog_updated_at, states, now_iso,
                       threshold_hours=DEFAULT_THRESHOLD_HOURS):
    """Pure watchdog decision.

    catalog_updated_at  ISO timestamp of the cameras-catalog.json release asset
                        (None => treat as stale; the asset is missing entirely)
    states              catalog `states` entries [{stateId, generatedAt}, ...]
    now_iso             ISO 'now'
    """
    if not catalog_updated_at:
        return Result(True, float("inf"),
                      "cameras-catalog.json is MISSING from the cameras-latest release — "
                      "devices have no catalog to poll.")

    age = _age_hours(catalog_updated_at, now_iso)

    stuck = []
    for s in states or []:
        ga = s.get("generatedAt")
        if not ga:
            continue
        try:
            if _age_hours(ga, now_iso) > threshold_hours * STUCK_STATE_MULTIPLIER:
                stuck.append(s.get("stateId"))
        except ValueError:
            continue

    if age > threshold_hours:
        msg = (f"cameras-catalog.json is STALE: {age:.1f}h old "
               f"(threshold {threshold_hours}h, updated {catalog_updated_at}). "
               f"Devices are being served stale camera metadata and cannot see new data.")
        return Result(True, age, msg, stuck)

    msg = f"cameras-catalog.json is fresh: {age:.1f}h old (threshold {threshold_hours}h)."
    return Result(False, age, msg, stuck)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Camera catalog freshness watchdog.")
    ap.add_argument("--catalog-updated-at", help="ISO timestamp of the catalog release asset")
    ap.add_argument("--catalog-file", help="downloaded cameras-catalog.json (for per-state checks)")
    ap.add_argument("--now", help="ISO now override (default: system UTC)")
    ap.add_argument("--threshold-hours", type=float, default=DEFAULT_THRESHOLD_HOURS)
    ap.add_argument("--summary", help="path to append a GitHub step-summary line to")
    args = ap.parse_args(argv)

    states = []
    if args.catalog_file:
        try:
            with open(args.catalog_file) as f:
                states = json.load(f).get("states", [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"::warning::could not read catalog file: {e}")

    now = args.now or datetime.datetime.now(datetime.timezone.utc).isoformat()
    r = evaluate_freshness(args.catalog_updated_at, states, now, args.threshold_hours)

    if r.stuck_states:
        print(f"::warning::states whose data has not moved in >"
              f"{args.threshold_hours * STUCK_STATE_MULTIPLIER:.0f}h: "
              f"{', '.join(str(s) for s in r.stuck_states)}")
    if r.stale:
        print(f"::error::{r.message}")
    else:
        print(r.message)

    if args.summary:
        with open(args.summary, "a") as f:
            f.write(f"### Camera freshness — {'STALE' if r.stale else 'OK'}\n{r.message}\n")
            if r.stuck_states:
                f.write(f"\nStuck states: `{', '.join(str(s) for s in r.stuck_states)}`\n")

    return 1 if r.stale else 0


if __name__ == "__main__":
    sys.exit(main())
