#!/usr/bin/env python3
"""
Tests for classify() v2 + the type=enforcement relation-derivation logic in
build-camera-extract.py — the camera EXTRACT layer, the SINGLE camera producer.

pytest-native (`python3 -m pytest test_camera_classify.py -q`) and ALSO runnable
standalone (`python3 test_camera_classify.py`) — every test is a plain `def test_*`
using bare `assert`, matching test_camera_extract_direction.py's convention.

WHY: classify() decides which OSM objects become which app threat TIER
(alpr/speed/redlight/camera/gunshot). The v1 classifier matched RAW tag values,
so it silently missed:
  - semicolon multi-values (`camera;ALPR` → the ALPR half was lost; the mast is
    an ALPR reader),
  - `red_light_camera` (475 OSM uses) — no mapping at all,
  - the 'traffic signals' SPACE spelling (143 uses) vs 'traffic_signals',
  - and it ignored type=enforcement RELATIONS entirely (~85% of enforcement data:
    35,603 relations vs 6,082 nodes, taginfo 2026-07-02).
It also must NEVER classify `toll` (a road-costing concern, not a point threat).
These tests lock every one of those cases, plus the intended precedence
(surveillance block wins over speed_camera so a dual-tagged ALPR mast is 'alpr').
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))


def _load():
    # build-camera-extract.py imports osmium at module load (for the OSM handlers),
    # which the pure classify/relation functions under test don't need. Stub it so
    # the module imports without the heavy C dependency present (same shim as
    # test_camera_extract_direction.py).
    if "osmium" not in sys.modules:
        stub = types.ModuleType("osmium")

        class _SimpleHandler:  # noqa: N801 — minimal base for handler subclassing
            def __init__(self, *a, **k):
                pass

        stub.SimpleHandler = _SimpleHandler
        stub.InvalidLocationError = type("InvalidLocationError", (Exception,), {})
        sys.modules["osmium"] = stub
    path = os.path.join(HERE, "build-camera-extract.py")
    spec = importlib.util.spec_from_file_location("build_camera_extract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ── surveillance:type classification ─────────────────────────────────────────

def test_surveillance_alpr_variants():
    # Canonical + case variants + the EU spelling all → alpr.
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "ALPR"}) == "alpr"
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "alpr"}) == "alpr"
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "  Alpr "}) == "alpr"
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "ANPR"}) == "alpr"


def test_surveillance_semicolon_multivalue_alpr_wins():
    # A dual-tagged camera;ALPR mast READS PLATES → alpr (priority alpr > camera),
    # regardless of order.
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "camera;ALPR"}) == "alpr"
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "ALPR;camera"}) == "alpr"
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "camera;radar"}) == "camera"


def test_surveillance_gunshot_and_camera():
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "gunshot_detector"}) == "gunshot"
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "camera"}) == "camera"


def test_surveillance_type_traffic_is_unmapped():
    # surveillance:type=traffic (48 uses) is a generic traffic-monitoring CCTV with
    # no plate-read/signal-enforce semantic — deliberately UNMAPPED (documented), not
    # folded into 'camera'. If it ever becomes a tier it must be added explicitly.
    assert mod.classify({"man_made": "surveillance", "surveillance:type": "traffic"}) is None


def test_surveillance_without_type_is_unmapped():
    # A bare man_made=surveillance with no surveillance:type isn't a classified tier.
    assert mod.classify({"man_made": "surveillance"}) is None


# ── highway=speed_camera classification ──────────────────────────────────────

def test_speed_camera_plain_is_speed():
    assert mod.classify({"highway": "speed_camera"}) == "speed"
    assert mod.classify({"highway": "speed_camera", "enforcement": "maxspeed"}) == "speed"


def test_speed_camera_enforcing_signal_is_redlight():
    # A speed-camera device that also enforces the signal is a red-light camera.
    assert mod.classify({"highway": "speed_camera", "enforcement": "traffic_signals"}) == "redlight"
    assert mod.classify({"highway": "speed_camera", "enforcement": "red_light_camera"}) == "redlight"
    assert mod.classify({"highway": "speed_camera", "enforcement": "maxspeed;traffic_signals"}) == "redlight"


# ── bare enforcement node classification ─────────────────────────────────────

def test_bare_enforcement_speed_values():
    assert mod.classify({"enforcement": "maxspeed"}) == "speed"
    assert mod.classify({"enforcement": "average_speed"}) == "speed"
    assert mod.classify({"enforcement": "maxspeed_average"}) == "speed"


def test_bare_enforcement_redlight_values():
    assert mod.classify({"enforcement": "traffic_signals"}) == "redlight"
    assert mod.classify({"enforcement": "red_signal"}) == "redlight"
    assert mod.classify({"enforcement": "stop"}) == "redlight"
    # red_light_camera (475 uses) — the v1 classifier missed this entirely.
    assert mod.classify({"enforcement": "red_light_camera"}) == "redlight"


def test_enforcement_space_variant_normalizes():
    # 'traffic signals' (SPACE, 143 uses) ≡ 'traffic_signals' (6,829 uses).
    assert mod.classify({"enforcement": "traffic signals"}) == "redlight"
    assert mod.classify_enforcement("traffic signals") == "redlight"


def test_enforcement_semicolon_signal_wins():
    # A combined maxspeed;traffic_signals mast → redlight (signal component wins).
    assert mod.classify({"enforcement": "maxspeed;traffic_signals"}) == "redlight"
    assert mod.classify_enforcement("maxspeed;traffic_signals") == "redlight"
    # Order-independent + case-insensitive.
    assert mod.classify_enforcement("Traffic_Signals;MaxSpeed") == "redlight"


# ── toll is NEVER a tier ─────────────────────────────────────────────────────

def test_toll_is_never_a_tier():
    # toll (3,172 enforcement uses) is a ROAD-costing concern (use_tolls=0), not a
    # point threat. Must classify to None everywhere it could appear.
    assert mod.classify_enforcement("toll") is None
    assert mod.classify({"enforcement": "toll"}) is None
    assert mod.classify({"highway": "toll_gantry", "enforcement": "toll"}) is None
    # A speed_camera that somehow also carried enforcement=toll still falls back to
    # 'speed' (toll contributes nothing; the device is a speed camera).
    assert mod.classify({"highway": "speed_camera", "enforcement": "toll"}) == "speed"


# ── precedence: dual-tagged speed-mast + ALPR → alpr ─────────────────────────

def test_dual_tagged_speed_mast_plus_alpr_is_alpr():
    # A mast tagged BOTH as a surveillance ALPR device AND highway=speed_camera:
    # the surveillance block must WIN (precedence) → alpr, not speed. This is the
    # documented reason man_made=surveillance is checked first.
    tags = {"man_made": "surveillance", "surveillance:type": "ALPR",
            "highway": "speed_camera", "enforcement": "maxspeed"}
    assert mod.classify(tags) == "alpr"


def test_unmapped_returns_none():
    assert mod.classify({}) is None
    assert mod.classify({"amenity": "cafe"}) is None
    assert mod.classify({"highway": "traffic_signals"}) is None  # a plain signal is NOT enforcement


# ── pure component helpers ───────────────────────────────────────────────────

def test_classify_surveillance_type_pure():
    assert mod.classify_surveillance_type("camera;ALPR") == "alpr"
    assert mod.classify_surveillance_type("gunshot_detector") == "gunshot"
    assert mod.classify_surveillance_type("camera") == "camera"
    assert mod.classify_surveillance_type("traffic") is None
    assert mod.classify_surveillance_type("") is None
    assert mod.classify_surveillance_type(None) is None


def test_split_components_and_norm():
    assert mod._split_components("camera;ALPR") == ["camera", "alpr"]
    assert mod._split_components("  Foo ; BAR ;") == ["foo", "bar"]
    assert mod._norm_enforcement("traffic signals") == "traffic_signals"


# ── relation-derived tier logic (pure — no PBF needed) ───────────────────────

class _FakeMember:
    """Duck-types a pyosmium RelationMember (.type, .ref, .role) for pick_* tests."""
    def __init__(self, mtype, ref, role):
        self.type = mtype
        self.ref = ref
        self.role = role


def test_relation_value_maps_via_classify_enforcement():
    # The relation pass classifies the relation's `enforcement` value with the SAME
    # normalized logic as a node — so every enforcement case above holds for relations.
    assert mod.classify_enforcement("maxspeed") == "speed"
    assert mod.classify_enforcement("red_light_camera") == "redlight"
    assert mod.classify_enforcement("average_speed") == "speed"
    assert mod.classify_enforcement("toll") is None


def test_pick_relation_member_prefers_device():
    members = [
        _FakeMember("w", 900, "from"),      # enforced way — never the device
        _FakeMember("n", 200, "camera"),
        _FakeMember("n", 201, "device"),    # highest preference
        _FakeMember("n", 202, ""),
    ]
    assert mod.pick_relation_member_node(members) == 201


def test_pick_relation_member_falls_back_to_camera_then_any():
    # No 'device' → 'camera' wins.
    m1 = [_FakeMember("w", 900, "to"), _FakeMember("n", 202, ""), _FakeMember("n", 200, "camera")]
    assert mod.pick_relation_member_node(m1) == 200
    # No 'device'/'camera' → any node member (first, stable).
    m2 = [_FakeMember("w", 900, "via"), _FakeMember("n", 202, ""), _FakeMember("n", 203, "")]
    assert mod.pick_relation_member_node(m2) == 202


def test_pick_relation_member_none_when_no_node_member():
    # A relation whose members are all ways (no device node) yields no target.
    members = [_FakeMember("w", 900, "from"), _FakeMember("w", 901, "to")]
    assert mod.pick_relation_member_node(members) is None


# ── standalone runner (mirrors test_camera_extract_direction.py) ─────────────

def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok - {t.__name__}")
    print(f"\nALL PASSED ({len(tests)} tests)")


if __name__ == "__main__":
    _run_all()
