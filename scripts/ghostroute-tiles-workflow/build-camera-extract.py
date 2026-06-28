#!/usr/bin/env python3
"""
F-P2-T4 — Extract ALPR cameras from a state OSM extract into a tiny app file.

DECOUPLED from the heavyweight Valhalla tile bake (build-tiles.yml). ALPR
cameras are crowd-added continuously (DeFlock → OSM: dozens to ~336K
worldwide in ~15 months), so they must refresh on their OWN fast cadence
as a kilobyte artifact — re-baking GB-scale tiles to ship a new camera is
the wrong unit of work (user directive 2026-06-05; see
docs/design/CAMERA-FRESHNESS-AND-SEARCH.md).

Data path (research-verified): cameras live IN OpenStreetMap as
`man_made=surveillance` + `surveillance:type=ALPR` (DeFlock has no bulk
API — it reads from OSM). Pull from OSM directly, under ODbL.

This script consumes a state .osm.pbf (kept current by the CI workflow via
Geofabrik daily extracts or pyosmium-up-to-date) and emits a compact
cameras-<state>.json. Per the osmium author (osmium-tool#163) you must NOT
tag-filter .osc change files directly — they miss deletions; the CI keeps
a FULL pbf current and this script filters the full pbf, so deletions and
tag-flips are handled correctly by construction.

Usage:
    pip install osmium
    python3 build-camera-extract.py <state.osm.pbf> <stateId> pois-out.json

Output (compact, app-shaped — matches SidecarCamera in cameraEdgeIndex.ts):
    {
      "schemaVersion": 1,
      "stateId": "texas",
      "generatedAt": "2026-06-05T...Z",
      "sourcePbf": "texas-latest.osm.pbf",
      "count": 12514,
      "cameras": [ {"id": "node/123", "lat": .., "lon": .., "dir": 90?}, ... ]
    }
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import sys

import osmium

SCHEMA_VERSION = 1


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


class CameraHandler(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.cameras: list[dict] = []

    def _is_alpr(self, tags) -> bool:
        return (
            tags.get("man_made") == "surveillance"
            and tags.get("surveillance:type") == "ALPR"
        )

    def node(self, n) -> None:
        if not n.location.valid() or not self._is_alpr(n.tags):
            return
        cam = {
            "id": f"node/{n.id}",
            "lat": round(n.location.lat, 6),
            "lon": round(n.location.lon, 6),
        }
        d = parse_direction(n.tags)
        if d is not None:
            cam["dir"] = round(d, 1)
        self.cameras.append(cam)

    def way(self, w) -> None:
        # Camera masts occasionally mapped as ways; take the centroid so the
        # app still sees them. (Most ALPR nodes are points.)
        if not self._is_alpr(w.tags) or len(w.nodes) == 0:
            return
        try:
            lats = [nd.location.lat for nd in w.nodes if nd.location.valid()]
            lons = [nd.location.lon for nd in w.nodes if nd.location.valid()]
        except osmium.InvalidLocationError:
            return
        if not lats:
            return
        cam = {
            "id": f"way/{w.id}",
            "lat": round(sum(lats) / len(lats), 6),
            "lon": round(sum(lons) / len(lons), 6),
        }
        d = parse_direction(w.tags)
        if d is not None:
            cam["dir"] = round(d, 1)
        self.cameras.append(cam)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    pbf_path, state_id, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    handler = CameraHandler()
    # locations=True so way centroids resolve; node-only cameras don't need it
    # but it's cheap and covers the rare way-mapped camera.
    handler.apply_file(pbf_path, locations=True, idx="flex_mem")

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "stateId": state_id,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sourcePbf": os.path.basename(pbf_path),
        "count": len(handler.cameras),
        "cameras": handler.cameras,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))

    size = os.path.getsize(out_path)
    print(f"[cameras] {state_id}: {len(handler.cameras)} ALPR cameras → "
          f"{out_path} ({size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
