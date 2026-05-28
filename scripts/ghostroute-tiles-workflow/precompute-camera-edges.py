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
    1. /locate (verbose=true) → all candidate edges Valhalla sees within
       its search radius.
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
    "valhallaSource": "<wheel-version OR service-url-host>",
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

Locator backends:

  Two ways to invoke Valhalla's /locate, chosen at the CLI:

    --valhalla-config FILE     in-process via the pip `valhalla` wheel
                               (use this for local development)
    --service-url URL          HTTP POST to a running valhalla_service
                               (use this from CI — the pip wheel
                               has a broken __init__.py upstream)

  Exactly one must be passed. The HTTP backend batches up to
  HTTP_BATCH_SIZE cameras per request to keep the request count
  manageable on large states.
"""

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional


# ── Tunable constants (mirror docstring §"Per-camera coverage policy") ──────
RADIUS_M             = 25   # Match the rendered FOV radius on-device.
AXIS_TOLERANCE_DEG   = 70   # Heading match for "edge aligned with camera direction"
NEAREST_FALLBACK_M   = 15   # Catch wide-FOV / intersection / wrong-direction cameras

# Batch size for the HTTP locator. Valhalla service accepts arbitrarily
# large `locations[]` arrays, but request size and response parsing
# scale with N. 200 is a sweet spot: ~1 KB per camera in the request,
# ~5–20 KB per camera in the verbose locate response, ~1–4 MB total
# per batch — well within urllib's defaults and Valhalla's own buffer
# limits, while reducing 50× the request count vs per-camera calls.
HTTP_BATCH_SIZE      = 200

# Per-batch HTTP timeout. The locate handler is CPU-bound and lat-lookup
# heavy; 200 cameras typically take <2 s in production, <8 s on a cold
# valhalla_service. 120 s is "something has gone genuinely wrong."
HTTP_TIMEOUT_S       = 120


# ── Geometry helpers ────────────────────────────────────────────────────────

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


# ── Locator backends ────────────────────────────────────────────────────────

class Locator:
    """Abstract: takes a batch of {lat, lon} location dicts, returns the
    raw locate response (list of per-location result objects). The
    response format is whatever Valhalla's /locate emits — the caller
    parses it via `_filter_edges`."""

    description: str = "<abstract>"

    def locate_batch(self, locations: list[dict]) -> list[dict]:
        raise NotImplementedError


class WheelLocator(Locator):
    """In-process locator via the pip-installed `valhalla` wheel. Use
    locally; in CI the wheel's __init__.py has a broken absolute import
    (`from filters import lookup` should be `from .filters import lookup`)
    that ModuleNotFoundError's at import time."""

    def __init__(self, config_path: str):
        try:
            import valhalla  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "valhalla Python wheel not installed (or failed to import). "
                "Run: pip install valhalla\n"
                "If the import fails with `No module named 'filters'`, that's a "
                "known upstream packaging bug. Use --service-url instead."
            ) from e
        with open(config_path) as f:
            config = json.load(f)
        self.actor = valhalla.Actor(config)
        self.description = f"wheel/{getattr(valhalla, '__version__', 'unknown')}"

    def locate_batch(self, locations: list[dict]) -> list[dict]:
        request = {
            "locations": locations,
            "costing": "auto",
            "verbose": True,
        }
        response_str = self.actor.locate(json.dumps(request))
        return _parse_locate_response(response_str)


class HttpLocator(Locator):
    """Locator that POSTs to a running `valhalla_service` HTTP endpoint.
    Used from CI where the same Docker image that builds the tiles also
    serves /locate — no broken pip wheel involved."""

    def __init__(self, service_url: str):
        self.url = service_url.rstrip("/") + "/locate"
        self.description = f"http {service_url}"

    def locate_batch(self, locations: list[dict]) -> list[dict]:
        request = {
            "locations": locations,
            "costing": "auto",
            "verbose": True,
        }
        body = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # Valhalla returns error JSON with a useful message on 4xx/5xx.
            # Surface it to the caller so a batch failure has a real cause
            # in the workflow log, not just "HTTP 500".
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"valhalla_service /locate returned HTTP {e.code}: {body_text}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"valhalla_service /locate connection failed: {e}"
            ) from e
        return _parse_locate_response(raw)


def _parse_locate_response(response_str: str) -> list[dict]:
    """Parse and validate a Valhalla locate response. Returns the
    locations array on success; returns [] (with a warning) on
    structural surprises. Single-camera failures don't taint the
    entire batch — the caller pairs by index."""
    try:
        response = json.loads(response_str)
    except Exception as e:
        print(f"  locate response not JSON: {e}; head={response_str[:200]!r}",
              file=sys.stderr)
        return []
    if not isinstance(response, list):
        # Valhalla returns {error_code, error} as an envelope on
        # full-request failure.
        print(f"  locate returned non-array response: {response_str[:200]!r}",
              file=sys.stderr)
        return []
    return response


# ── Edge filtering ──────────────────────────────────────────────────────────

def filter_edges_for_camera(camera: dict, locate_entry: dict) -> list[dict]:
    """Apply the per-camera coverage policy to ONE camera's locate
    response. Returns the list of {lat, lon} edge centroid dicts to
    write to the sidecar; returns [] if nothing qualifies.
    """
    direction = camera.get("direction")
    candidate_edges = (locate_entry or {}).get("edges", [])
    if not candidate_edges:
        return []

    out: list[dict] = []
    seen_edge_ids: set[int] = set()

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
        if edge_id is not None and edge_id in seen_edge_ids:
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


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--valhalla-config",
                        help="Path to valhalla.json (in-process pip wheel mode)")
    parser.add_argument("--service-url",
                        help="URL of a running valhalla_service (HTTP mode, e.g. http://localhost:8002)")
    parser.add_argument("--cameras-input", required=True,
                        help="Path to a JSON array of cameras: [{id, lat, lon, direction?}, ...]")
    parser.add_argument("--bbox", required=True,
                        help="State bbox as 'swLat,swLon,neLat,neLon' (decimal degrees)")
    parser.add_argument("--state-id", required=True,
                        help="State id (e.g. 'california') — written into the sidecar for traceability")
    parser.add_argument("--output", required=True,
                        help="Path to write cameras-edges.json")
    args = parser.parse_args()

    # Exactly one locator backend required (xor).
    if bool(args.valhalla_config) == bool(args.service_url):
        print("Exactly one of --valhalla-config or --service-url is required.",
              file=sys.stderr)
        return 2

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

    in_bbox = [
        c for c in all_cameras
        if sw_lat <= c["lat"] <= ne_lat and sw_lon <= c["lon"] <= ne_lon
    ]
    print(f"[{args.state_id}] {len(in_bbox)} cameras in bbox of {len(all_cameras)} total")

    # Empty-state shortcut: skip locator init and write empty sidecar.
    if not in_bbox:
        result = {
            "version": "1",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "stateId": args.state_id,
            "valhallaSource": "skipped (empty bbox)",
            "cameras": [],
        }
        with open(args.output, "w") as f:
            json.dump(result, f, separators=(",", ":"))
        print(f"[{args.state_id}] Wrote empty sidecar to {args.output}")
        return 0

    # Build the locator.
    locator: Locator
    if args.service_url:
        locator = HttpLocator(args.service_url)
    else:
        # mypy: validated above that one is set
        locator = WheelLocator(args.valhalla_config)  # type: ignore[arg-type]
    print(f"[{args.state_id}] locator: {locator.description}")

    out_cameras: list[dict] = []
    edges_total = 0
    cameras_with_no_edges = 0
    batch_failures = 0
    progress_interval = max(1, len(in_bbox) // 20)

    # Process in batches. Each batch is one locate call; on failure the
    # whole batch is skipped and counted (batch_failures) — don't bail
    # the entire state on transient errors.
    for batch_start in range(0, len(in_bbox), HTTP_BATCH_SIZE):
        batch = in_bbox[batch_start:batch_start + HTTP_BATCH_SIZE]
        if batch_start // HTTP_BATCH_SIZE % max(1, progress_interval // HTTP_BATCH_SIZE) == 0:
            print(f"[{args.state_id}] processing cameras {batch_start + 1}..{batch_start + len(batch)} of {len(in_bbox)}…")

        locations = [{"lat": c["lat"], "lon": c["lon"]} for c in batch]
        try:
            responses = locator.locate_batch(locations)
        except Exception as e:
            print(f"[{args.state_id}] batch {batch_start}..{batch_start + len(batch)} FAILED: {e}",
                  file=sys.stderr)
            batch_failures += 1
            cameras_with_no_edges += len(batch)
            continue

        # Defensive: locate is supposed to return one entry per input
        # location in order. If the lengths don't match (shouldn't
        # happen), pair what we can.
        for i, cam in enumerate(batch):
            entry = responses[i] if i < len(responses) else {}
            edges = filter_edges_for_camera(cam, entry)
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
        "valhallaSource": locator.description,
        "cameras": out_cameras,
    }
    with open(args.output, "w") as f:
        json.dump(result, f, separators=(",", ":"))

    avg_edges = (edges_total / len(out_cameras)) if out_cameras else 0
    print(f"[{args.state_id}] DONE: {len(out_cameras)} cameras resolved, "
          f"{edges_total} edges total ({avg_edges:.2f} edges/camera avg), "
          f"{cameras_with_no_edges} cameras had no resolvable edge"
          + (f", {batch_failures} batch(es) failed" if batch_failures > 0 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
