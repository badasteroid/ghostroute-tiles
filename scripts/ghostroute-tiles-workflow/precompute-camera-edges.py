#!/usr/bin/env python3
"""
precompute-camera-edges.py — Resolve every ALPR camera in a state's bbox to
its set of graph edge centroids, using Valhalla's /locate action against
the just-built tile pack.

Run by the ghostroute-tiles CI workflow as a step AFTER valhalla_build_tiles
for each state. The output is a sidecar JSON that the app reads on
tile-pack install; the app then uses the pre-resolved edge centroids as
`exclude_locations` for camera avoidance, eliminating the runtime
snap-ambiguity that has been our last remaining failure mode.

Why we do this at build time, not runtime:
  - Valhalla's /locate is the only reliable way to know which graph edge
    a camera coordinate belongs to. Doing it once at build time is fast,
    deterministic, and reusable across every route.
  - At build time we can be GENEROUS: spend a few seconds per camera to
    find every directed edge within ~25 m and produce a complete
    coverage set. At runtime we'd be paying that cost per route.
  - The result is graph-truth: the edge centroids in the sidecar are
    guaranteed to snap back to the same edge under any runtime locate
    against the same tile pack (provided OSM geometry is stable across
    builds, which it is — we don't need GraphId stability because the
    runtime doesn't see edge_ids, only centroid lat/lon).

Why centroid (not snapped point or edge_id):
  - Centroid = midpoint of the edge's shape polyline. It's a point ON
    the edge, far from the edge's endpoints. Any reasonable nearest-edge
    search snaps it back to its own edge — there's no "edge endpoint
    ambiguity" the way the camera's raw lat/lon coordinate has.
  - Centroids are OSM-geometry only (no Valhalla internal numbering),
    so they're stable across Valhalla versions and tile rebuilds.
  - Edge_ids would be tighter but require lockstep Valhalla versions
    between CI and on-device — fragile.

Per-camera coverage policy:
  For each camera at (lat, lon, direction):
    1. Call /locate with verbose=true.
    2. From the returned edges, keep those that are:
         (a) auto-traversable (edge.access.car == true), AND
         (b) within RADIUS_M of the snap point (locate's `distance`), AND
         (c) one of EITHER:
             (i) heading aligned with camera direction within
                 ±AXIS_TOLERANCE_DEG of the camera direction OR its
                 180° opposite (= the camera reads this edge's traffic), OR
             (ii) within NEAREST_FALLBACK_M of the camera point regardless
                  of heading (= the camera is close enough that we
                  conservatively exclude this edge even without
                  direction-axis alignment — catches wide-FOV intersection
                  cameras and cases where OSM `direction` is wrong/missing).
    3. Dedup by edge_id, compute the centroid of each surviving edge's
       shape, write {lat, lon} for each into the camera's `edges` list.

Output format (cameras-edges.json):
  {
    "version": "1",
    "generatedAt": "<ISO-8601 UTC>",
    "stateId": "<id from states.json>",
    "valhallaVersion": "<from `valhalla` python wheel>",
    "cameras": [
      {
        "id": "overpass-12345",          # matches the app's Waypoint.id
        "lat": 37.78,                    # original camera lat/lon
        "lon": -122.41,
        "direction": 90.0,               # original (or omitted if absent)
        "edges": [
          {"lat": 37.7801, "lon": -122.4099},   # edge centroid 1
          {"lat": 37.7800, "lon": -122.4101}    # edge centroid 2 (etc)
        ]
      },
      ...
    ]
  }

Cameras with NO resolvable edges (e.g., placed in a parking lot, or in
the middle of nowhere) are OMITTED from the cameras array. The app
treats absence as "fall back to runtime spread behavior."
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone


# ── Tunable constants (mirror docstring §"Per-camera coverage policy") ──────
RADIUS_M             = 25   # Match the rendered FOV radius on-device.
AXIS_TOLERANCE_DEG   = 70   # Heading match for "edge aligned with camera direction"
NEAREST_FALLBACK_M   = 15   # Catch wide-FOV / intersection / wrong-direction cameras


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in meters."""
    R = 6_371_000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def axis_angle_deg(bearing_a_deg: float, bearing_b_deg: float) -> float:
    """Angle (0..90) between two bearings considered as axes (each axis
    defined by {bearing, bearing+180}). 0 = parallel, 90 = perpendicular."""
    a = bearing_a_deg % 360
    b = bearing_b_deg % 360
    diff = abs(a - b)
    diff = min(diff, 360 - diff)        # 0..180
    return min(diff, 180 - diff)         # 0..90


def decode_polyline_p6(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google polyline at precision 6 (Valhalla default) to
    a list of (lat, lon) tuples."""
    coords: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lon = 0
    n = len(encoded)
    while index < n:
        # Read varint for lat delta then lon delta.
        deltas: list[int] = []
        for _ in range(2):
            shift = 0
            result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            value = ~(result >> 1) if (result & 1) else (result >> 1)
            deltas.append(value)
        lat += deltas[0]
        lon += deltas[1]
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def polyline_centroid(coords: list[tuple[float, float]]) -> tuple[float, float]:
    """Midpoint of a polyline (the vertex at position len/2, or the
    average of the two middle vertices if the polyline has even length).

    NOT the geographic centroid of the line's bounding box, NOT the
    midpoint by arc length — for short edges these are equivalent; for
    long edges (rural highways) we accept that the centroid is roughly
    in the middle, which is good enough for snap purposes.
    """
    n = len(coords)
    if n == 1:
        return coords[0]
    mid = n // 2
    if n % 2 == 0:
        (lat1, lon1) = coords[mid - 1]
        (lat2, lon2) = coords[mid]
        return ((lat1 + lat2) / 2.0, (lon1 + lon2) / 2.0)
    return coords[mid]


def resolve_camera_edges(actor, camera: dict) -> list[dict]:
    """Resolve one camera to its list of edge centroid {lat, lon} dicts.

    Returns [] if locate fails, returns no edges, or returns no edges
    that pass the auto-traversable + distance + direction filters.
    """
    lat = camera["lat"]
    lon = camera["lon"]
    direction = camera.get("direction")

    request = {
        "locations": [{"lat": lat, "lon": lon}],
        "costing": "auto",
        "verbose": True,
    }
    try:
        response_str = actor.locate(json.dumps(request))
    except Exception as e:
        print(f"  locate threw for {camera['id']}: {e}", file=sys.stderr)
        return []
    try:
        response = json.loads(response_str)
    except Exception as e:
        print(f"  locate response not JSON for {camera['id']}: {e}", file=sys.stderr)
        return []
    if not isinstance(response, list) or not response:
        return []
    candidate_edges = response[0].get("edges", [])
    if not candidate_edges:
        return []

    out: list[dict] = []
    seen_edge_ids: set[int] = set()

    # Per docstring §"Per-camera coverage policy"
    for e in candidate_edges:
        # (a) Auto-traversable
        access = e.get("edge", {}).get("access", {})
        if access.get("car") is not True:
            continue

        # (b) Within RADIUS_M of the snap point
        edge_distance = e.get("distance")
        if edge_distance is None or edge_distance > RADIUS_M:
            continue

        # Dedup by edge_id (each directed edge has a unique GraphId).
        edge_id = e.get("edge_id", {}).get("value")
        if edge_id is not None:
            if edge_id in seen_edge_ids:
                continue

        # (c) Either direction-aligned OR within NEAREST_FALLBACK_M
        keep = False
        if direction is not None and math.isfinite(direction):
            edge_heading = e.get("heading")
            if edge_heading is not None and math.isfinite(edge_heading):
                if axis_angle_deg(direction, edge_heading) <= AXIS_TOLERANCE_DEG:
                    keep = True
        if not keep and edge_distance <= NEAREST_FALLBACK_M:
            keep = True
        if not keep:
            continue

        # Extract the edge's centroid from its stored shape polyline.
        shape_enc = e.get("edge_info", {}).get("shape")
        if not shape_enc:
            continue
        try:
            shape_coords = decode_polyline_p6(shape_enc)
        except Exception:
            continue
        if len(shape_coords) < 2:
            continue
        (cen_lat, cen_lon) = polyline_centroid(shape_coords)

        out.append({"lat": cen_lat, "lon": cen_lon})
        if edge_id is not None:
            seen_edge_ids.add(edge_id)

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--valhalla-config", required=True,
                        help="Path to valhalla.json (tells Valhalla where the tile dir is)")
    parser.add_argument("--cameras-input", required=True,
                        help="Path to a JSON array of cameras: [{id, lat, lon, direction?}, ...]")
    parser.add_argument("--bbox", required=True,
                        help="State bbox as 'swLat,swLon,neLat,neLon' (decimal degrees)")
    parser.add_argument("--state-id", required=True,
                        help="State id (e.g. 'california') — written into the sidecar for traceability")
    parser.add_argument("--output", required=True,
                        help="Path to write cameras-edges.json")
    args = parser.parse_args()

    bbox_parts = [float(x) for x in args.bbox.split(",")]
    if len(bbox_parts) != 4:
        print(f"--bbox must be 4 floats: swLat,swLon,neLat,neLon (got {args.bbox!r})",
              file=sys.stderr)
        return 2
    sw_lat, sw_lon, ne_lat, ne_lon = bbox_parts

    with open(args.cameras_input) as f:
        all_cameras = json.load(f)
    if not isinstance(all_cameras, list):
        print(f"--cameras-input must be a JSON array, got {type(all_cameras).__name__}",
              file=sys.stderr)
        return 2

    # bbox filter
    in_bbox = [
        c for c in all_cameras
        if sw_lat <= c["lat"] <= ne_lat and sw_lon <= c["lon"] <= ne_lon
    ]
    print(f"[{args.state_id}] {len(in_bbox)} cameras in bbox of {len(all_cameras)} total")

    # Empty-state shortcut: skip Valhalla init and write empty sidecar.
    if not in_bbox:
        result = {
            "version": "1",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "stateId": args.state_id,
            "valhallaVersion": _safe_valhalla_version(),
            "cameras": [],
        }
        with open(args.output, "w") as f:
            json.dump(result, f, separators=(",", ":"))
        print(f"[{args.state_id}] Wrote empty sidecar to {args.output}")
        return 0

    # Import valhalla AFTER the bbox-empty shortcut — the wheel takes ~1s
    # to load and we don't want to pay it on empty-state runs.
    try:
        import valhalla  # type: ignore
    except ImportError:
        print("valhalla Python wheel not installed. Run: pip install valhalla", file=sys.stderr)
        return 2

    with open(args.valhalla_config) as f:
        config = json.load(f)
    actor = valhalla.Actor(config)

    out_cameras: list[dict] = []
    edges_total = 0
    cameras_with_no_edges = 0
    progress_interval = max(50, len(in_bbox) // 20)

    for i, cam in enumerate(in_bbox):
        if i % progress_interval == 0:
            print(f"[{args.state_id}] processing camera {i + 1}/{len(in_bbox)}…")
        edges = resolve_camera_edges(actor, cam)
        if not edges:
            cameras_with_no_edges += 1
            continue
        out_entry: dict = {
            "id":   cam["id"],
            "lat":  cam["lat"],
            "lon":  cam["lon"],
            "edges": edges,
        }
        if cam.get("direction") is not None and math.isfinite(cam["direction"]):
            out_entry["direction"] = cam["direction"]
        out_cameras.append(out_entry)
        edges_total += len(edges)

    result = {
        "version": "1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "stateId": args.state_id,
        "valhallaVersion": _safe_valhalla_version(),
        "cameras": out_cameras,
    }
    with open(args.output, "w") as f:
        json.dump(result, f, separators=(",", ":"))

    avg_edges = (edges_total / len(out_cameras)) if out_cameras else 0
    print(f"[{args.state_id}] DONE: {len(out_cameras)} cameras resolved, "
          f"{edges_total} edges total ({avg_edges:.2f} edges/camera avg), "
          f"{cameras_with_no_edges} cameras had no resolvable edge")
    return 0


def _safe_valhalla_version() -> str:
    """Return the installed valhalla wheel version, or 'unknown' if the
    module isn't importable. Defensive — we may be writing the empty-state
    sidecar before any valhalla import."""
    try:
        import valhalla  # type: ignore
        return getattr(valhalla, "__version__", "unknown")
    except ImportError:
        return "not-installed"


if __name__ == "__main__":
    sys.exit(main())
