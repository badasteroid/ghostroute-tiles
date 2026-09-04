#!/usr/bin/env python3
"""
Tests for camera-freshness-check.py — the staleness watchdog decision.

pytest-native (`python3 -m pytest test_camera_freshness_check.py -q`) and standalone
(`python3 test_camera_freshness_check.py`), matching test_camera_classify.py.

WHY: the 2026-08-20..09-02 outage was SILENT for 14 days. The daily build kept
"succeeding" at publishing per-state assets while the catalog devices gate on
froze. Nothing watched. This decision powers a scheduled watchdog that fails
loudly (and opens an issue) the moment the served catalog goes stale, whatever
the cause — including causes we have not thought of.
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load():
    spec = importlib.util.spec_from_file_location(
        "camera_freshness_check", os.path.join(_HERE, "camera-freshness-check.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


F = _load()

NOW = "2026-09-03T22:00:00+00:00"


def test_fresh_catalog_passes():
    r = F.evaluate_freshness("2026-09-03T20:00:00+00:00", [], NOW, threshold_hours=26)
    assert not r.stale
    assert r.age_hours < 3


def test_stale_catalog_fails():
    # 15 days old — the exact outage shape.
    r = F.evaluate_freshness("2026-08-19T08:47:13+00:00", [], NOW, threshold_hours=26)
    assert r.stale
    assert r.age_hours > 300
    assert "stale" in r.message.lower()


def test_just_under_threshold_passes():
    r = F.evaluate_freshness("2026-09-02T21:00:00+00:00", [], NOW, threshold_hours=26)
    assert not r.stale  # 25h


def test_just_over_threshold_fails():
    r = F.evaluate_freshness("2026-09-02T19:00:00+00:00", [], NOW, threshold_hours=26)
    assert r.stale  # 27h


def test_individually_stuck_state_is_reported():
    # Catalog itself is fresh, but one state's data has not moved in weeks —
    # the Wyoming shape (guard blocking one state while everything else flows).
    states = [
        {"stateId": "ohio", "generatedAt": "2026-09-03T20:00:00+00:00"},
        {"stateId": "wyoming", "generatedAt": "2026-08-19T08:40:00+00:00"},
    ]
    r = F.evaluate_freshness("2026-09-03T20:00:00+00:00", states, NOW, threshold_hours=26)
    assert not r.stale  # catalog fresh → not a hard failure
    assert "wyoming" in r.stuck_states
    assert "ohio" not in r.stuck_states


def test_no_stuck_states_when_all_fresh():
    states = [
        {"stateId": "ohio", "generatedAt": "2026-09-03T20:00:00+00:00"},
        {"stateId": "texas", "generatedAt": "2026-09-03T19:00:00+00:00"},
    ]
    r = F.evaluate_freshness("2026-09-03T20:00:00+00:00", states, NOW, threshold_hours=26)
    assert not r.stale and not r.stuck_states


def test_missing_updated_at_is_stale():
    r = F.evaluate_freshness(None, [], NOW, threshold_hours=26)
    assert r.stale


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERR  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
