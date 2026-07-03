#!/usr/bin/env python3
"""
F-P2-T4 — Extract surveillance / enforcement threat points from a state OSM
extract into a tiny app file. THE SINGLE camera producer for BOTH the fresh
feed (build-cameras.yml) AND the tile bake's sidecar input (build-national.sh).

DECOUPLED from the heavyweight Valhalla tile bake (build-tiles.yml). ALPR
cameras are crowd-added continuously (DeFlock → OSM: dozens to ~336K
worldwide in ~15 months), so they must refresh on their OWN fast cadence
as a kilobyte artifact — re-baking GB-scale tiles to ship a new camera is
the wrong unit of work (user directive 2026-06-05; see
docs/design/CAMERA-FRESHNESS-AND-SEARCH.md).

Data path (research-verified): threats live IN OpenStreetMap as
`man_made=surveillance` (+ `surveillance:type=…`), `highway=speed_camera`,
or `type=enforcement` relations (DeFlock has no bulk API — it reads from
OSM). Pull from OSM directly, under ODbL.

This script consumes a state .osm.pbf (kept current by the CI workflow via
Geofabrik daily extracts or pyosmium-up-to-date) and emits a compact
cameras-<state>.json. Per the osmium author (osmium-tool#163) you must NOT
tag-filter .osc change files directly — they miss deletions; the CI keeps
a FULL pbf current and this script filters the full pbf, so deletions and
tag-flips are handled correctly by construction.

ID CANON (docs/plan/THREAT-TIERS-CORRECTION-2026-07-02.md §1): every OSM id
is emitted as `overpass-<osmid>` for nodes and `overpass-w<osmid>` for ways —
the app-wide canonical form that every consumer keys on (cameraEdgeIndex.ts,
precompute-camera-edges.py, cameraSupplyService.ts). The prior `node/<id>` /
`way/<id>` scheme (this script) did NOT match `build-national.sh`'s
`overpass-<id>` (the sidecar producer): the SAME OSM node got two unmatchable
ids, so every reading camera was COUNTED TWICE on device ("Passes 14 cameras"
where truth was 7) and the `node/` copies missed their precomputed FOV edges.
Unifying the id here is layer 2 of the §1 fix (one extractor, one id scheme).

RELATION PASS (§3 / C-2): taginfo 2026-07-02 — `enforcement` appears on
**35,603 relations** vs 6,082 nodes (~85% of the enforcement data is in
`type=enforcement` relations, not on the node). A relation groups the
enforcement `device`/`camera` nodes with the `from`/`to`/`via` ways it
enforces; the tier lives on the RELATION's `enforcement` tag, and the
avoidable point is its member node. We do a two-pass apply_file: pass 1
collects relation→member-node tiers; pass 2 emits nodes, using the
relation-derived tier for nodes that carry no surveillance tags of their own.

Usage:
    pip install osmium
    python3 build-camera-extract.py <state.osm.pbf> <stateId> cameras-out.json

Output (compact, app-shaped — matches SidecarCamera in cameraEdgeIndex.ts),
schemaVersion 2:
    {
      "schemaVersion": 2,
      "stateId": "texas",
      "generatedAt": "2026-07-02T...Z",
      "sourcePbf": "texas-latest.osm.pbf",
      "count": 12514,
      "cameras": [
        {"id": "overpass-123", "lat": .., "lon": .., "dir": 90?},          # alpr (type omitted)
        {"id": "overpass-456", "lat": .., "lon": .., "type": "speed"},     # every other tier explicit
        {"id": "overpass-w789", "lat": .., "lon": .., "type": "redlight"}  # way-mapped mast
      ]
    }

`dir` (NOT `direction`) is the on-the-wire field the app parses
(StateCameraFile.cameras[].dir in cameraCatalogService.ts) and the
precompute reader accepts (`dir` OR `direction`). Do NOT rename it.
`type` is OMITTED for the 'alpr' default (legacy byte-compat: the app
defaults a missing `type` to 'alpr'); every other tier is written
explicitly so the app can offer it as a selectable toggle.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys
from collections import Counter

import osmium

# schemaVersion 2 (was 1): adds per-tier `type` (omitted for the 'alpr'
# default → legacy byte-compat) and is populated by the classify() v2 tier
# model + the type=enforcement relation pass. The client accepts 1 AND 2
# (cameraCatalogService ACCEPTED_SCHEMA) so a schema bump never orphans a
# device mid-rollout. THE FIELD NAMES ON THE WIRE (`id`, `lat`, `lon`, `dir`,
# `type`) are unchanged from v1 — only the id FORMAT (overpass-*) and the
# presence of `type` differ.
SCHEMA_VERSION = 2


def parse_direction(tags) -> float | None:
    """camera:direction / direction -> bearing in [0,360), or None.

    Accepts numeric degrees AND DeFlock arc ranges like "338-23" (the lens FOV; we
    take the arc's CENTER bearing so the camera bakes directional, not directionless).
    Ignores compass words (rare on ALPR nodes). This MUST stay in sync with
    precompute-camera-edges.py:parse_camera_direction — both parse the same OSM values."""
    raw = tags.get("camera:direction") or tags.get("direction")
    if raw is None:
        return None
    s = str(raw).strip()
    try:
        d = float(s)
        return d % 360.0 if math.isfinite(d) else None
    except (TypeError, ValueError):
        pass
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", s)  # arc "A-B"
    if m:
        a = float(m.group(1))
        arc = (float(m.group(2)) - a) % 360.0  # width, wrapping through 0
        return (a + arc / 2.0) % 360.0
    return None


# ── Tier classification (classify() v2, data-driven) ─────────────────────────
# Maps OSM tags to the app's ThreatType (src/types/index.ts:
# 'alpr'|'speed'|'redlight'|'camera'|'gunshot'), or None if not a mapped threat.
#
# Data-driven from taginfo (2026-07-02 global counts, cited inline). Every value
# is NORMALIZED (strip + lower) and SPLIT on ';' before matching, because OSM
# multi-values a single node's roles with ';' (e.g. surveillance:type=`camera;ALPR`
# 59 uses, enforcement=`maxspeed;traffic_signals` 215 uses). Matching the raw
# string would miss all combos — the exact bug that lost `red_light_camera` (475
# uses) and the ALPR-half of `camera;ALPR`.
#
# 'toll' is NEVER a tier here: the user avoids toll ROADS via the `use_tolls=0`
# costing ("avoid toll roads = don't take toll roads"), not by treating gantries
# as point threats (taginfo enforcement=toll 3,172 — deliberately dropped).
#
# NOTE on `traffic` (surveillance:type=traffic, 48 uses): left UNMAPPED (None).
# It denotes a generic traffic-monitoring CCTV with no plate-reading (ALPR) or
# signal-enforcement (redlight) semantic; folding it into `camera` would flood the
# map with non-actionable CCTV wedges. If a `traffic` tier is ever wanted, add it
# explicitly with its own toggle — do not silently reclassify.

# surveillance:type component -> tier. Priority (highest first) resolves a
# multi-value node: a `camera;ALPR` mast IS an ALPR reader, so alpr wins.
_SURVEILLANCE_ALPR = {"alpr", "anpr"}                       # taginfo: ALPR 119,873 · alpr 22 · anpr (ANPR = the EU name)
_SURVEILLANCE_GUNSHOT = {"gunshot_detector", "gunshot"}     # taginfo: gunshot_detector 2,374
_SURVEILLANCE_CAMERA = {"camera"}                           # taginfo: camera 358,532 (generic CCTV)

# enforcement component -> tier (normalized: underscores/spaces unified, see
# _norm_enforcement). taginfo: maxspeed 27,700 · traffic_signals 6,829 ·
# average_speed 1,684 · red_light_camera 475 · maxspeed;traffic_signals 215 ·
# 'traffic signals' (space) 143.
_ENFORCEMENT_SPEED = {"maxspeed", "average_speed", "maxspeed_average"}
_ENFORCEMENT_REDLIGHT = {"traffic_signals", "red_signal", "red_light_camera", "stop"}


def _norm(value) -> str:
    """Normalize a single OSM tag value for matching: strip + lower."""
    return str(value or "").strip().lower()


def _split_components(value) -> list[str]:
    """Normalize AND split an OSM tag value on ';' → the list of components.
    OSM encodes 'this node does A and B' as `A;B` (semicolon list). Empty
    components (from a trailing ';') are dropped."""
    return [c for c in (_norm(part) for part in str(value or "").split(";")) if c]


def _norm_enforcement(value) -> str:
    """Normalize an enforcement component for set matching: treat spaces as
    underscores so 'traffic signals' (143 uses) ≡ 'traffic_signals' (6,829)."""
    return _norm(value).replace(" ", "_")


def classify_surveillance_type(surveillance_type) -> str | None:
    """man_made=surveillance's `surveillance:type` value -> tier, or None.

    Splits on ';' and applies priority alpr > gunshot > camera (a dual-tagged
    `camera;ALPR` mast reads plates → alpr). Pure function so it's unit-testable
    without a PBF."""
    components = set(_split_components(surveillance_type))
    if components & _SURVEILLANCE_ALPR:
        return "alpr"
    if components & _SURVEILLANCE_GUNSHOT:
        return "gunshot"
    if components & _SURVEILLANCE_CAMERA:
        return "camera"
    return None


def classify_enforcement(enforcement_value) -> str | None:
    """A `type=enforcement` relation's (or node's) `enforcement` value -> tier,
    or None. Splits on ';', normalizes space≡underscore, and maps speed vs
    redlight components. `toll` (and any unmapped component) yields None.

    Pure function — reused by BOTH the node path and the relation pass, and
    unit-tested directly."""
    components = {_norm_enforcement(part) for part in str(enforcement_value or "").split(";")}
    components.discard("")
    # A signal/red component means a red-light-enforcement device even if a
    # speed component is also present (a combined mast is primarily the
    # signal-runner deterrent for our avoidance display).
    if components & _ENFORCEMENT_REDLIGHT:
        return "redlight"
    if components & _ENFORCEMENT_SPEED:
        return "speed"
    return None


def classify(tags) -> str | None:
    """OSM tags -> ThreatType ('alpr'|'speed'|'redlight'|'camera'|'gunshot'), or None if not mapped.
    MUST stay in sync with src/types/index.ts ThreatType + routingService AVOIDABLE_WAYPOINT_TYPES.

    PRECEDENCE (intentional — do not reorder):
      1. man_made=surveillance FIRST. A mast dual-tagged as both a surveillance
         device AND a speed camera (surveillance:type=ALPR + highway=speed_camera,
         seen on shared enforcement masts) is an ALPR reader — the surveillance
         block must win so it classifies 'alpr', not 'speed'.
      2. highway=speed_camera next: redlight if it also enforces the signal
         (enforcement contains a signal/red component), else speed.
      3. bare enforcement node last (a device tagged directly on the node rather
         than via a type=enforcement relation)."""
    if tags.get("man_made") == "surveillance":
        t = classify_surveillance_type(tags.get("surveillance:type"))
        if t is not None:
            return t
    if tags.get("highway") == "speed_camera":
        # A speed-camera device that ALSO enforces the signal is a red-light camera.
        return classify_enforcement(tags.get("enforcement")) or "speed"
    # Enforcement device tagged directly on the node (not via a type=enforcement relation).
    return classify_enforcement(tags.get("enforcement"))


# ── Relation pass (C-2) ──────────────────────────────────────────────────────
# ~85% of enforcement data is on type=enforcement RELATIONS (taginfo: 35,603
# relations vs 6,082 nodes). A relation groups the enforcement device node(s)
# with the ways they enforce; the tier is on the relation's `enforcement` tag
# and the avoidable point is its member node (role 'device', else 'camera',
# else any node member). We classify the relation once, pick its target node,
# and tag that node in pass 2.

# Member role preference for picking the enforcement DEVICE node of a relation.
# 'device' is the OSM-canonical role for the enforcement box; 'camera' is the
# older/looser tag; a bare node member is the last resort. Lower index = higher
# preference.
_ENFORCEMENT_ROLE_PREFERENCE = ("device", "camera", "")


def pick_relation_member_node(members) -> int | None:
    """Given a relation's members (each exposing .type=='n'/'w'/'r', .ref, .role),
    return the node id of the best enforcement-device member, or None if the
    relation has no node member.

    Preference: role 'device' > role 'camera' > any node member (roles beyond
    those, e.g. 'from'/'to'/'via', are the enforced WAYS, not the device — never
    picked unless no better node exists). Pure function (takes plain
    (type, ref, role) tuples in tests; the real member objects duck-type) so it's
    unit-testable without a PBF."""
    node_members = [(m.type, m.ref, _norm(getattr(m, "role", ""))) for m in members]
    node_members = [(ref, role) for (mtype, ref, role) in node_members if mtype == "n"]
    if not node_members:
        return None
    def rank(role: str) -> int:
        try:
            return _ENFORCEMENT_ROLE_PREFERENCE.index(role)
        except ValueError:
            return len(_ENFORCEMENT_ROLE_PREFERENCE)  # unknown role → after '', still a valid fallback
    # Stable: first member wins within the same rank (OSM order).
    best_ref, _best_role = min(node_members, key=lambda rr: rank(rr[1]))
    return best_ref


class RelationCollector(osmium.SimpleHandler):
    """Pass 1: visit type=enforcement relations, classify each via the SAME
    normalized enforcement logic, and record the chosen member node id → tier.

    Needs NO location index — it only reads relation tags + member refs, so the
    first apply_file over the PBF is cheap (relations only)."""

    def __init__(self) -> None:
        super().__init__()
        self.node_tier: dict[int, str] = {}

    def relation(self, r) -> None:
        if r.tags.get("type") != "enforcement":
            return
        tier = classify_enforcement(r.tags.get("enforcement"))
        if tier is None:
            return
        node_id = pick_relation_member_node(r.members)
        if node_id is None:
            return
        # First relation to claim a node wins (relations rarely share a device
        # node; if they did, the first tier is as good as any and deterministic).
        self.node_tier.setdefault(node_id, tier)


class CameraHandler(osmium.SimpleHandler):
    """Pass 2: emit each surveillance/enforcement node (and the rare way-mapped
    mast). A node's OWN classification wins; a node with no surveillance tags of
    its own falls back to the relation-derived tier collected in pass 1."""

    def __init__(self, relation_tier: dict[int, str]) -> None:
        super().__init__()
        self.cameras: list[dict] = []
        self.relation_tier = relation_tier
        self.relation_derived_count = 0

    def node(self, n) -> None:
        if not n.location.valid():
            return
        own_tier = classify(n.tags)
        # Node's own classification WINS over the relation-derived tier when BOTH
        # exist (the node's tags are the more specific truth); a relation-only
        # node (no surveillance tags) uses the relation tier and MUST still be
        # emitted with lat/lon (+direction if tagged).
        via_relation = False
        tier = own_tier
        if tier is None:
            tier = self.relation_tier.get(n.id)
            via_relation = tier is not None
        if tier is None:
            return
        cam = {"id": f"overpass-{n.id}", "lat": round(n.location.lat, 6),
               "lon": round(n.location.lon, 6)}
        d = parse_direction(n.tags)
        if d is not None:
            cam["dir"] = round(d, 1)
        if tier != "alpr":  # omit the default → ALPR sidecars stay byte-compatible with legacy packs
            cam["type"] = tier
        self.cameras.append(cam)
        if via_relation:
            self.relation_derived_count += 1

    def way(self, w) -> None:
        # Camera masts occasionally mapped as ways; take the centroid so the app still sees them.
        # (Relations reference NODE devices, so way-mapped masts only ever classify by their
        # own tags — no relation fallback path for ways.)
        tier = classify(w.tags)
        if len(w.nodes) == 0 or tier is None:
            return
        try:
            lats = [nd.location.lat for nd in w.nodes if nd.location.valid()]
            lons = [nd.location.lon for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            return
        if not lats:
            return
        cam = {"id": f"overpass-w{w.id}",
               "lat": round(sum(lats) / len(lats), 6),
               "lon": round(sum(lons) / len(lons), 6)}
        d = parse_direction(w.tags)
        if d is not None:
            cam["dir"] = round(d, 1)
        if tier != "alpr":
            cam["type"] = tier
        self.cameras.append(cam)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    pbf_path, state_id, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    # PASS 1 — collect type=enforcement relation → member-node tiers. No location
    # index needed (relation tags + member refs only), so this pass is cheap.
    rel_collector = RelationCollector()
    rel_collector.apply_file(pbf_path)

    # PASS 2 — emit nodes/ways, using each node's own tags first and the
    # relation-derived tier as the fallback. locations=True so way centroids
    # resolve AND so relation-only member nodes (which carry no surveillance tags)
    # still resolve their lat/lon; node-only cameras don't strictly need it but
    # it's cheap and covers the rare way-mapped camera.
    handler = CameraHandler(rel_collector.node_tier)
    handler.apply_file(pbf_path, locations=True, idx="flex_mem")

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "stateId": state_id,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sourcePbf": os.path.basename(pbf_path),
        "count": len(handler.cameras),
        "cameras": handler.cameras,
    }

    # ATOMIC WRITE (§5 truncate-in-place corruption risk): write to a sibling
    # .tmp then os.replace() — a crash mid-write can never leave a truncated
    # cameras-<state>.json that the vandalism guard reads as a catastrophic drop.
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp_path, out_path)

    # Self-auditing stats to stdout (CI logs become their own audit trail):
    # total + per-type Counter + directionless + relation-derived counts.
    type_counts = Counter(c.get("type", "alpr") for c in handler.cameras)
    directionless = sum(1 for c in handler.cameras if "dir" not in c)
    size = os.path.getsize(out_path)
    per_type = " ".join(f"{k}={type_counts[k]}" for k in sorted(type_counts))
    print(f"[cameras] {state_id}: {len(handler.cameras)} threats "
          f"[{per_type}] directionless={directionless} "
          f"relation-derived={handler.relation_derived_count} → "
          f"{out_path} ({size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
