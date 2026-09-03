#!/usr/bin/env python3
"""
Tests for camera-publish-guard.py — the pre-publish decision for build-cameras.yml.

pytest-native (`python3 -m pytest test_camera_publish_guard.py -q`) and ALSO runnable
standalone (`python3 test_camera_publish_guard.py`), matching test_camera_classify.py.

WHY: the guard decides whether a freshly-extracted per-state camera file may be
published to the cameras-latest release. It must:
  - REFUSE an id-scheme regression (any id not starting with 'overpass-');
  - REFUSE a >30% count drop vs the released file (OSM vandalism/mass-delete),
    UNLESS the new count sits within +/-10% of an explicitly RATIFIED baseline
    (a real, acknowledged upstream drop — the Wyoming 546->143 deadlock);
  - WARN (not refuse) on a wrong schemaVersion or a stale (>180d) ratification;
  - PASS free growth and first publishes.

The deadlock this fixes: blocking the publish freezes the RELEASED baseline, so a
legitimate drop can never self-heal. Ratified baselines break the loop with an
auditable, human-committed acknowledgement.
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load():
    spec = importlib.util.spec_from_file_location(
        "camera_publish_guard", os.path.join(_HERE, "camera-publish-guard.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _load()


def _file(count, ids=None, schema=2):
    """A minimal extract file object. `ids` overrides the generated overpass- ids."""
    if ids is None:
        ids = [f"overpass-{i}" for i in range(count)]
    return {
        "schemaVersion": schema,
        "count": count,
        "cameras": [{"id": i, "lat": 40.0, "lon": -80.0} for i in ids],
    }


def _baselines(state=None, count=None, ratified_at=None, reason="upstream OSM removal"):
    states = {}
    if state is not None:
        states[state] = {"count": count, "ratifiedAt": ratified_at, "reason": reason}
    return {"states": states}


# ── count guard ──────────────────────────────────────────────────────────────

def test_free_growth_passes():
    r = G.evaluate_publish(_file(150), prev_count=100, baselines=_baselines(),
                           state="ohio", today="2026-09-03")
    assert r.ok, r.errors
    assert not r.errors


def test_first_publish_passes():
    r = G.evaluate_publish(_file(20), prev_count=None, baselines=_baselines(),
                           state="alaska", today="2026-09-03")
    assert r.ok and not r.errors


def test_shallow_drop_above_70pct_passes():
    # 80 >= 70% of 100 — allowed without a baseline.
    r = G.evaluate_publish(_file(80), prev_count=100, baselines=_baselines(),
                           state="ohio", today="2026-09-03")
    assert r.ok and not r.errors


def test_unratified_deep_drop_fails():
    r = G.evaluate_publish(_file(143), prev_count=546, baselines=_baselines(),
                           state="wyoming", today="2026-09-03")
    assert not r.ok
    assert any("70%" in e or "vandalism" in e for e in r.errors)


def test_ratified_drop_passes_with_note():
    r = G.evaluate_publish(
        _file(143), prev_count=546,
        baselines=_baselines("wyoming", 143, "2026-09-03"),
        state="wyoming", today="2026-09-03")
    assert r.ok and not r.errors
    assert any("baseline" in n.lower() for n in r.notes)


def test_ratified_within_tolerance_passes():
    # baseline 143, +/-10% => [128.7, 157.3]; 138 is inside.
    r = G.evaluate_publish(
        _file(138), prev_count=546,
        baselines=_baselines("wyoming", 143, "2026-09-03"),
        state="wyoming", today="2026-09-03")
    assert r.ok and not r.errors


def test_ratified_outside_tolerance_fails():
    # 100 is below the 128.7 floor — a NEW anomaly beyond the ratified drop.
    r = G.evaluate_publish(
        _file(100), prev_count=546,
        baselines=_baselines("wyoming", 143, "2026-09-03"),
        state="wyoming", today="2026-09-03")
    assert not r.ok


def test_stale_ratification_warns_but_passes():
    # ratified 200 days before today, new value within tolerance.
    r = G.evaluate_publish(
        _file(143), prev_count=546,
        baselines=_baselines("wyoming", 143, "2026-02-15"),
        state="wyoming", today="2026-09-03")
    assert r.ok and not r.errors
    assert any("180" in w or "stale" in w.lower() for w in r.warnings)


def test_baseline_for_other_state_does_not_apply():
    r = G.evaluate_publish(
        _file(143), prev_count=546,
        baselines=_baselines("montana", 143, "2026-09-03"),
        state="wyoming", today="2026-09-03")
    assert not r.ok


# ── id-format gate ───────────────────────────────────────────────────────────

def test_bad_id_scheme_fails():
    f = _file(3, ids=["overpass-1", "node/2", "overpass-3"])
    r = G.evaluate_publish(f, prev_count=3, baselines=_baselines(),
                           state="ohio", today="2026-09-03")
    assert not r.ok
    assert any("overpass-" in e for e in r.errors)


def test_all_good_ids_pass():
    f = _file(3, ids=["overpass-1", "overpass-2", "overpass-3"])
    r = G.evaluate_publish(f, prev_count=3, baselines=_baselines(),
                           state="ohio", today="2026-09-03")
    assert r.ok and not r.errors


# ── schema gate ──────────────────────────────────────────────────────────────

def test_wrong_schema_warns_not_fails():
    r = G.evaluate_publish(_file(150, schema=1), prev_count=100, baselines=_baselines(),
                           state="ohio", today="2026-09-03")
    assert r.ok  # schema is a warning, not a hard fail
    assert any("schema" in w.lower() for w in r.warnings)


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
