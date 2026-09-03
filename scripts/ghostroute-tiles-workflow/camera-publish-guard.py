#!/usr/bin/env python3
"""
Pre-publish guard for build-cameras.yml — decides whether a freshly-extracted
per-state camera file may be published to the cameras-latest release.

Pure decision in `evaluate_publish`; the CLI wrapper reads files, emits GitHub
`::error::`/`::warning::` annotations + a step-summary line, and exits non-zero
to BLOCK a publish. Decision logic is unit-tested in test_camera_publish_guard.py
(the inline jq in build-cameras.yml could not be tested; this can).

Guards, in order:
  1. id-format — every camera id MUST start with 'overpass-' (the `node/` copies
     miss their precomputed FOV edges and reopen the on-device double-count
     schism). Any offender BLOCKS.
  2. schema    — schemaVersion should be 2 (typed tiers + canonical ids). Wrong
     value WARNS (the client's ACCEPTED_SCHEMA still accepts {1,2}); never blocks.
  3. count     — refuse a drop below 70% of the released count (OSM vandalism /
     mass-delete), UNLESS the new count is within +/-10% of an explicitly RATIFIED
     baseline in camera-count-baselines.json. This breaks the deadlock where
     blocking the publish freezes the released baseline forever, so a real
     upstream drop (Wyoming 546->143) could never self-heal. A ratification older
     than 180 days WARNS so it is re-verified rather than trusted indefinitely.
"""
import argparse
import datetime
import json
import sys

# Refuse a drop steeper than this fraction of the released count (unless ratified).
COUNT_DROP_FLOOR = 0.70
# A ratified baseline covers new counts within this fraction of the acknowledged value.
RATIFIED_TOLERANCE = 0.10
# Re-verify a ratification older than this.
RATIFICATION_MAX_AGE_DAYS = 180
CANONICAL_ID_PREFIX = "overpass-"
EXPECTED_SCHEMA = 2


class Result:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.notes = []

    @property
    def ok(self):
        return not self.errors


def _days_between(a_iso, b_iso):
    a = datetime.date.fromisoformat(a_iso)
    b = datetime.date.fromisoformat(b_iso)
    return abs((b - a).days)


def evaluate_publish(new_obj, prev_count, baselines, state, today):
    """Pure guard decision.

    new_obj    parsed cameras-<state>.json ({schemaVersion, count, cameras:[{id,...}]})
    prev_count int count of the currently-released file, or None on first publish
    baselines  parsed camera-count-baselines.json ({"states": {state: {count, ratifiedAt, reason}}})
    state      state id (e.g. "wyoming")
    today      ISO date string used for ratification-age checks
    """
    r = Result()

    # 1. id-format gate.
    bad = [c.get("id") for c in new_obj.get("cameras", [])
           if not str(c.get("id", "")).startswith(CANONICAL_ID_PREFIX)]
    if bad:
        sample = ", ".join(str(b) for b in bad[:5])
        r.errors.append(
            f"{state}: {len(bad)} camera id(s) do not start with '{CANONICAL_ID_PREFIX}' — "
            f"id-canon regression, refusing to publish. Sample: {sample}")

    # 2. schema gate (warning only).
    schema = new_obj.get("schemaVersion")
    if schema != EXPECTED_SCHEMA:
        r.warnings.append(f"{state}: schemaVersion is {schema} (expected {EXPECTED_SCHEMA})")

    # 3. count guard.
    new_count = new_obj.get("count")
    if prev_count and prev_count > 0 and new_count is not None \
            and new_count < prev_count * COUNT_DROP_FLOOR:
        base = (baselines or {}).get("states", {}).get(state)
        if base and base.get("count"):
            bc = base["count"]
            if abs(new_count - bc) <= RATIFIED_TOLERANCE * bc:
                r.notes.append(
                    f"{state}: count {new_count} within +/-{int(RATIFIED_TOLERANCE * 100)}% of "
                    f"ratified baseline {bc} ({base.get('reason', 'no reason given')}) — publishing.")
                ratified_at = base.get("ratifiedAt")
                if ratified_at and _days_between(ratified_at, today) > RATIFICATION_MAX_AGE_DAYS:
                    r.warnings.append(
                        f"{state}: ratified baseline is >{RATIFICATION_MAX_AGE_DAYS} days old "
                        f"(ratifiedAt {ratified_at}) — re-verify it still reflects reality.")
            else:
                r.errors.append(
                    f"{state} camera count {new_count} < 70% of previous {prev_count}, and outside "
                    f"+/-{int(RATIFIED_TOLERANCE * 100)}% of ratified baseline {bc} — refusing to "
                    f"publish (anomaly beyond the acknowledged drop).")
        else:
            r.errors.append(
                f"{state} camera count {new_count} < 70% of previous {prev_count} — refusing to "
                f"publish (possible vandalism). Ratify in camera-count-baselines.json if this drop "
                f"is real.")

    return r


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pre-publish guard for a per-state camera file.")
    ap.add_argument("new_file", help="cameras-<state>.json just extracted")
    ap.add_argument("--state", required=True)
    ap.add_argument("--prev", help="previously-released cameras-<state>.json, or omit on first publish")
    ap.add_argument("--baselines", help="camera-count-baselines.json")
    ap.add_argument("--today", help="ISO date override (default: system date)")
    ap.add_argument("--summary", help="path to append a GitHub step-summary line to")
    args = ap.parse_args(argv)

    new_obj = _read_json(args.new_file)
    prev_count = None
    if args.prev:
        try:
            prev_count = _read_json(args.prev).get("count")
        except (FileNotFoundError, json.JSONDecodeError):
            prev_count = None
    baselines = _read_json(args.baselines) if args.baselines else {"states": {}}
    today = args.today or datetime.date.today().isoformat()

    r = evaluate_publish(new_obj, prev_count, baselines, args.state, today)

    for n in r.notes:
        print(f"note: {n}")
    for w in r.warnings:
        print(f"::warning::{w}")
    for e in r.errors:
        print(f"::error::{e}")

    if args.summary:
        schema = new_obj.get("schemaVersion")
        count = new_obj.get("count")
        types = {}
        for c in new_obj.get("cameras", []):
            t = c.get("type", "alpr")
            types[t] = types.get(t, 0) + 1
        breakdown = " ".join(f"{k}={v}" for k, v in sorted(types.items()))
        status = "PASS" if r.ok else "REFUSED"
        with open(args.summary, "a") as f:
            f.write(f"### Cameras — {args.state}\n")
            f.write(f"{status} · schemaVersion `{schema}` · total `{count}` · by tier: `{breakdown}`\n")

    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
