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

Per-camera coverage policy (FOV cone — must match runtime predicate
in src/services/routingService.ts):
  For each camera at (lat, lon, direction):
    1. /locate (verbose=true) → all candidate edges Valhalla sees within
       its search radius.
    2. From the returned edges, keep those that are:
         (a) auto-traversable (edge.access.car == true), AND
         (b) within FOV_RADIUS_M of the snap point — cheap geometric
             reject; if the closest point on the edge is past the FOV
             radius, no point on the edge can be inside the cone, AND
         (c) IF the camera has a direction: SOME point on the edge's
             shape polyline lies inside the camera's FOV cone — i.e.,
             within FOV_RADIUS_M of the camera AND bearing from camera
             to point within ±FOV_HALF_ANGLE_DEG of camera direction.
             IF the camera has NO direction: keep the edge (we can't
             apply the cone test; conservative include matches the
             runtime, which treats direction-less cameras as
             always-reading).
    3. Dedup by edge_id, compute the centroid of each surviving edge's
       shape, write {lat, lon} for each into the camera's `edges` list.

Why this changed: the earlier policy (axis-diff ≤ 70° OR within 15 m
unconditional) over-included edges that ran parallel to the camera's
direction but sat outside its actual cone — and similarly missed edges
the camera does read along the cone's azimuth from beyond 15 m. The
2026-05-28 SF FOV diagnostic showed 6 of 10 "unavoidable" cameras at
runtime were axis-diff false positives that the cone-bearing test
correctly excludes.

Output format (cameras-edges.json), schema version "2":
  {
    "version": "2",
    "generatedAt": "<ISO-8601 UTC>",
    "stateId": "<id from states.json>",
    "valhallaSource": "<wheel-version OR service-url-host>",
    "qa": {
      "totalCameras": 1234, "resolvedCameras": 1100,
      "camerasWithNoEdges": 134, "camerasWithNoEdgesFrac": 0.108,
      "edgesPerCameraMean": 1.7, "camerasWithDirectionalPair": 700,
      "batchFailures": 0
    },
    "cameras": [
      {
        "id": "overpass-12345",          # matches the app's Waypoint.id
        "lat": 37.78,                    # original camera lat/lon
        "lon": -122.41,
        "direction": 90.0,               # original (or omitted if absent)
        "fovEdges": [
          # ONE entry per in-cone DIRECTED edge. A 2-way road's two
          # directed edges share centroid+shape but have OPPOSITE
          # headings (~180° apart): that heading is how A-P2
          # disambiguates the two identical centroids; shape+graphId are
          # the A-P3 soft-cost fallback. (No doubling — the count already
          # includes both directions; one-way roads contribute 1.)
          {"centroid": {"lat": 37.7801, "lon": -122.4099},
           "heading": 171.1, "graphId": 4016499431049,
           "shape": "<encoded polyline6>", "lengthM": 84.2},
          {"centroid": {"lat": 37.7801, "lon": -122.4099},
           "heading": 351.1, "graphId": 4016398767753,
           "shape": "<encoded polyline6>", "lengthM": 84.2}
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
# Documented Flock Falcon FOV. These MUST match the on-device truth
# predicate in src/services/routingService.ts so the exclusion set
# (what we tell Valhalla to avoid) and the verification set (what we
# count as "reading the route") agree on the same geometric question.
#
# Replaces the older axis-diff heuristic (axis-diff ≤ 70° OR within
# 15 m unconditional), which over-counted "reading" edges that
# happened to have parallel headings but sat outside the camera's
# actual cone. 2026-05-28 SF FOV diagnostic showed 6 of 10
# "unavoidable" cameras were exactly this false-positive pattern.
FOV_RADIUS_M         = 25
FOV_HALF_ANGLE_DEG   = 55
# Densification step when checking whether an edge enters the cone.
# 1 m matches the runtime sampler and the diagnostic; catches routes
# that clip the wedge between two coarse vertices.
FOV_SAMPLE_STEP_M    = 1.0

# Minimum cumulative length of an edge inside the FOV cone before we
# consider the edge "read" by the camera. Mirrors the runtime threshold
# in src/services/routingService.ts (CAMERA_FOV_MIN_DWELL_M). Filters
# tangential touches — an edge that grazes the cone boundary for a
# single sample isn't an effective plate read at urban speeds.
FOV_MIN_DWELL_M      = 3.0

# Runtime proximity gate (src/services/routingService.ts CAMERA_PROXIMITY_M): a
# camera within this distance of the route is a candidate reader. Direction-LESS
# cameras (no lens axis) are counted as reading ANY road within this radius
# (cameraReadsRoute returns true on a missing direction), so the sidecar must
# surface every road in this disc for them — not just the nearest snapped edge.
CAMERA_PROXIMITY_M   = 60

# Search radius passed to /locate per camera. Loki returns only the NEAREST edge
# for a bare point — at an intersection that's the side street the camera sits on,
# not the arterial it watches across the junction. A search radius makes /locate
# return EVERY candidate edge within it, surfacing the watched road too.
#   - Directional: the FOV cone reaches FOV_RADIUS_M; 30 m (a little margin) covers
#     it and the cone test then keeps only in-cone edges.
#   - Direction-LESS: surface the whole proximity disc and keep all of it.
# General fix; no route-specific tuning.
LOCATE_RADIUS_DIRECTIONAL_M   = 30
LOCATE_RADIUS_DIRECTIONLESS_M = CAMERA_PROXIMITY_M

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
    defined by {bearing, bearing+180}). 0 = parallel, 90 = perpendicular.
    Retained for legacy logs; not used by the FOV cone predicate."""
    a = bearing_a_deg % 360
    b = bearing_b_deg % 360
    diff = abs(a - b)
    diff = min(diff, 360 - diff)        # 0..180
    return min(diff, 180 - diff)         # 0..90


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing (0 = N, clockwise) from (lat1, lon1) → (lat2, lon2).
    Matches the runtime bearingDeg() and the diagnostic's bearing_deg()."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angular_diff_deg(a: float, b: float) -> float:
    """Smallest difference between two bearings, 0..180."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def point_in_fov(cam_lat: float, cam_lon: float, cam_dir: float,
                 pt_lat: float, pt_lon: float,
                 radius_m: float = FOV_RADIUS_M,
                 half_angle_deg: float = FOV_HALF_ANGLE_DEG) -> bool:
    """True if (pt_lat, pt_lon) lies inside the camera's directional
    FOV wedge: within radius AND bearing-to-point within ±half_angle of
    camera direction. Mirrors the on-device pointInCameraFov() so the
    sidecar's exclusion decisions match the runtime read predicate."""
    dist = haversine_m(cam_lat, cam_lon, pt_lat, pt_lon)
    if dist > radius_m:
        return False
    if dist < 0.1:
        return True
    return angular_diff_deg(bearing_deg(cam_lat, cam_lon, pt_lat, pt_lon),
                            cam_dir) <= half_angle_deg


def polyline_enters_fov(polyline: list[tuple[float, float]],
                        cam_lat: float, cam_lon: float, cam_dir: float,
                        radius_m: float = FOV_RADIUS_M,
                        half_angle_deg: float = FOV_HALF_ANGLE_DEG,
                        sample_m: float = FOV_SAMPLE_STEP_M,
                        min_dwell_m: float = FOV_MIN_DWELL_M) -> bool:
    """True if the polyline's cumulative length inside the camera's
    FOV cone meets the min_dwell_m threshold. Densifies each segment at
    sample_m resolution so a polyline that clips the wedge between two
    coarse vertices is still measured. Mirrors the runtime
    polylineEntersCameraFov() in routingService.ts — keep them in sync.
    """
    if len(polyline) < 2:
        return False
    dwell_m = 0.0
    for i in range(len(polyline) - 1):
        (a_lat, a_lon) = polyline[i]
        (b_lat, b_lon) = polyline[i + 1]
        seg_len = haversine_m(a_lat, a_lon, b_lat, b_lon)
        if seg_len <= 0:
            continue
        n_samples = max(2, int(seg_len / sample_m) + 1)
        step_m = seg_len / n_samples
        prev_inside = None
        for s in range(n_samples + 1):
            t = s / n_samples
            lat = a_lat + (b_lat - a_lat) * t
            lon = a_lon + (b_lon - a_lon) * t
            inside = point_in_fov(cam_lat, cam_lon, cam_dir, lat, lon,
                                  radius_m, half_angle_deg)
            if s > 0:
                if inside and prev_inside:
                    dwell_m += step_m
                elif inside != prev_inside:
                    dwell_m += step_m / 2
            if dwell_m >= min_dwell_m:
                return True
            prev_inside = inside
    return False


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

    def edge_walk_ok(self, shape: str) -> bool:
        """True if Thor's FormPath can edge-walk this shape on the TARGET engine
        (no error 233). Default: assume walkable; subclasses that can verify
        override. A-14 / A-P3-T3: shapes that 233 are dropped at bake time so the
        runtime soft-cost (linear_cost_factors) path never ships an un-walkable
        shape — clean by construction. Engine-version-coupled: re-bake + re-walk
        whenever the on-device Valhalla version changes."""
        return True


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

    def edge_walk_ok(self, shape: str) -> bool:
        # Test the ACTUAL runtime path: a single-shape linear_cost_factors route.
        # CRITICAL: trace_attributes(shape_match=edge_walk) uses a DIFFERENT, more
        # lenient FormPath path and does NOT predict linear_cost_factors 233s —
        # empirically it passed shapes that 233 in add_cost_factor_edges
        # (use_shortcuts=true) even on the same engine. So drive the real path:
        # add_cost_factor_edges edge-walks the shape BEFORE pathfinding and throws
        # 233 atomically if it fails. Route the shape's own endpoints (a 442/no-path
        # is fine — the walk happens first, regardless of routability).
        try:
            coords = decode_polyline_p6(shape)
        except Exception:
            return True
        if len(coords) < 2:
            return True
        (olat, olon), (dlat, dlon) = coords[0], coords[-1]
        url = self.url.rsplit("/locate", 1)[0] + "/route"
        body = json.dumps({
            "locations": [{"lat": olat, "lon": olon}, {"lat": dlat, "lon": dlon}],
            "costing": "auto",
            "linear_cost_factors": [{"shape": shape, "factor": 50}],
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                resp.read()
            return True  # routed → the shape walked in the cost-factor path
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""
            # 233 = FormPath edge-walk failure → DROP. Anything else (442 no-path,
            # transient 5xx) → keep (conservative; only edge-walk failures matter).
            return not ("233" in text or "edge walk" in text.lower())
        except urllib.error.URLError:
            return True


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
    response. Returns a list of v2 fovEdge dicts
    {centroid:{lat,lon}, heading, graphId, shape, lengthM} — one per
    in-cone directed edge — to write to the sidecar; [] if none qualify.

    Predicate (must match src/services/routingService.ts FOV cone):
      (a) Edge is auto-traversable (car access).
      (b) Loki snap distance ≤ FOV_RADIUS_M — cheap reject; if the
          closest point on the edge is past the radius, no point on
          the edge can be in the cone (the cone is bounded by radius).
      (c) If the camera has a direction: SOME point on the edge's
          shape polyline lies inside the camera's FOV cone (directional
          truth — bearing-from-camera-to-point within ±half-angle of
          camera direction). If the camera has NO direction: keep the
          edge (we can't apply the cone test; conservative include
          matches runtime, which treats direction-less cameras as
          always-reading).
    """
    cam_lat = camera.get("lat")
    cam_lon = camera.get("lon")
    direction = camera.get("direction")
    candidate_edges = (locate_entry or {}).get("edges", [])
    if not candidate_edges or cam_lat is None or cam_lon is None:
        return []

    has_direction = (direction is not None and math.isfinite(direction))

    out: list[dict] = []
    seen_edge_ids: set[int] = set()

    for e in candidate_edges:
        # (a) Auto-traversable.
        access = e.get("edge", {}).get("access", {})
        if access.get("car") is not True:
            continue

        # (b) Snap distance ≤ the camera's read radius (camera-relative, since we
        # /locate the camera point): the FOV cone radius for a directional camera
        # (step (c) then applies the cone test), or the full proximity disc for a
        # direction-less one (which reads ANY road within it).
        edge_distance = e.get("distance")
        reach_m = FOV_RADIUS_M if has_direction else CAMERA_PROXIMITY_M
        if edge_distance is None or edge_distance > reach_m:
            continue

        # Dedup by edge_id (each directed edge has a unique GraphId).
        edge_id = e.get("edge_id", {}).get("value")
        if edge_id is not None and edge_id in seen_edge_ids:
            continue

        # Decode the edge's stored shape so we can run the cone test
        # AND emit the centroid as the exclude location.
        shape_enc = e.get("edge_info", {}).get("shape")
        if not shape_enc:
            continue
        try:
            shape_coords = decode_polyline_p6(shape_enc)
        except Exception:
            continue
        if len(shape_coords) < 2:
            continue

        # (c) FOV cone test: any point on the edge inside the wedge?
        if has_direction:
            if not polyline_enters_fov(shape_coords, cam_lat, cam_lon,
                                       direction):
                continue
        # else: missing direction — conservative include (matches
        # runtime cameraReadsRoute treating direction-less cameras as
        # always-reading).

        (cen_lat, cen_lon) = polyline_centroid(shape_coords)
        # v2: carry the per-DIRECTED-EDGE graph truth, not just the
        # centroid. The two directed edges of a 2-way road share this
        # shape (hence an identical centroid) but have opposite headings;
        # that heading is what lets the runtime (A-P2) disambiguate the
        # two identical points, and shape/graphId are the A-P3 soft-cost
        # fallback. See A-P1-T1.
        length_m = sum(
            haversine_m(shape_coords[i][0], shape_coords[i][1],
                        shape_coords[i + 1][0], shape_coords[i + 1][1])
            for i in range(len(shape_coords) - 1)
        )
        out.append({
            "centroid": {"lat": cen_lat, "lon": cen_lon},
            "heading": e.get("heading"),
            "graphId": edge_id,
            "shape": shape_enc,
            "lengthM": round(length_m, 1),
        })
        if edge_id is not None:
            seen_edge_ids.add(edge_id)

    return out


def locate_radius_for(camera: dict) -> int:
    """The /locate search radius for a camera: a wider disc for direction-less
    cameras (whole proximity gate) than for directional ones (FOV cone)."""
    d = camera.get("direction")
    has_dir = d is not None and math.isfinite(d)
    return LOCATE_RADIUS_DIRECTIONAL_M if has_dir else LOCATE_RADIUS_DIRECTIONLESS_M


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
    parser.add_argument("--edge-walk-filter", action="store_true",
                        help="A-14 / A-P3-T3: FormPath edge-walk every in-cone shape on the "
                             "target engine and DROP shapes that error 233, so the runtime "
                             "soft-cost (linear_cost_factors) path is clean by construction. "
                             "Adds one /trace_attributes call per UNIQUE edge (cached). The "
                             "shapeDropRate it reports is the metric that decides whether soft "
                             "cost ships on this engine or an engine bump is worth it.")
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
            "version": "2",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "stateId": args.state_id,
            "valhallaSource": "skipped (empty bbox)",
            "qa": {
                "totalCameras": 0, "resolvedCameras": 0,
                "camerasWithNoEdges": 0, "camerasWithNoEdgesFrac": 0,
                "edgesPerCameraMean": 0, "camerasWithDirectionalPair": 0,
                "batchFailures": 0,
            },
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
    cameras_with_directional_pair = 0
    batch_failures = 0
    # A-14 edge-walk filter state (cache by graphId → each unique edge walked
    # at most once on the target engine).
    edge_walk_cache: dict = {}
    shapes_before_filter = 0
    shapes_dropped = 0
    dropped_ledger: list[str] = []
    progress_interval = max(1, len(in_bbox) // 20)

    # Process in batches. Each batch is one locate call; on failure the
    # whole batch is skipped and counted (batch_failures) — don't bail
    # the entire state on transient errors.
    for batch_start in range(0, len(in_bbox), HTTP_BATCH_SIZE):
        batch = in_bbox[batch_start:batch_start + HTTP_BATCH_SIZE]
        if batch_start // HTTP_BATCH_SIZE % max(1, progress_interval // HTTP_BATCH_SIZE) == 0:
            print(f"[{args.state_id}] processing cameras {batch_start + 1}..{batch_start + len(batch)} of {len(in_bbox)}…")

        # One /locate per camera, each with a search radius so loki returns EVERY
        # candidate edge in the camera's read disc (not just the nearest) — this is
        # what surfaces the watched arterial at a cross-street camera and every road
        # around a direction-less one. filter_edges_for_camera then applies the cone
        # test (directional) or keeps all (direction-less).
        locations = [{"lat": c["lat"], "lon": c["lon"], "radius": locate_radius_for(c)} for c in batch]
        try:
            responses = locator.locate_batch(locations)
        except Exception as e:
            print(f"[{args.state_id}] batch {batch_start}..{batch_start + len(batch)} FAILED: {e}",
                  file=sys.stderr)
            batch_failures += 1
            cameras_with_no_edges += len(batch)
            continue

        # Defensive: locate returns one entry per input location, in order.
        for i, cam in enumerate(batch):
            entry = responses[i] if i < len(responses) else {}
            fov_edges = filter_edges_for_camera(cam, entry)
            if fov_edges and args.edge_walk_filter:
                # A-14: drop shapes that don't FormPath-walk on the target engine.
                kept = []
                for fe in fov_edges:
                    shapes_before_filter += 1
                    gid = fe.get("graphId")
                    if gid is not None and gid in edge_walk_cache:
                        ok = edge_walk_cache[gid]
                    else:
                        ok = locator.edge_walk_ok(fe["shape"])
                        if gid is not None:
                            edge_walk_cache[gid] = ok
                    if ok:
                        kept.append(fe)
                    else:
                        shapes_dropped += 1
                        if len(dropped_ledger) < 50:
                            dropped_ledger.append(fe.get("shape", "")[:28])
                fov_edges = kept
            if not fov_edges:
                cameras_with_no_edges += 1
                continue
            out_entry: dict = {
                "id":   cam["id"],
                "lat":  cam["lat"],
                "lon":  cam["lon"],
                "fovEdges": fov_edges,
            }
            if cam.get("direction") is not None and math.isfinite(cam["direction"]):
                out_entry["direction"] = cam["direction"]
            out_cameras.append(out_entry)
            edges_total += len(fov_edges)
            # Directional-pair QA: a 2-way in-cone road contributes two
            # directed edges whose headings are ~180° apart. NOT a doubling
            # gate — the count already includes both directions (one-way = 1,
            # two-way = 2); this just confirms the pair is captured so A-P2's
            # heading disambiguation has both to work with. (A-P1-T1.)
            headings = [fe["heading"] for fe in fov_edges if fe.get("heading") is not None]
            if any(angular_diff_deg(headings[a], headings[b]) >= 150.0
                   for a in range(len(headings)) for b in range(a + 1, len(headings))):
                cameras_with_directional_pair += 1

    resolved = len(out_cameras)
    total_cameras = len(in_bbox)
    no_edge_frac = (cameras_with_no_edges / total_cameras) if total_cameras else 0.0
    result = {
        "version": "2",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "stateId": args.state_id,
        "valhallaSource": locator.description,
        "qa": {
            "totalCameras": total_cameras,
            "resolvedCameras": resolved,
            "camerasWithNoEdges": cameras_with_no_edges,
            "camerasWithNoEdgesFrac": round(no_edge_frac, 4),
            "edgesPerCameraMean": round(edges_total / resolved, 3) if resolved else 0,
            "camerasWithDirectionalPair": cameras_with_directional_pair,
            "batchFailures": batch_failures,
            "edgeWalkFilter": args.edge_walk_filter,
            "shapesBeforeFilter": shapes_before_filter,
            "shapesDropped": shapes_dropped,
            "shapeDropRate": round(shapes_dropped / shapes_before_filter, 4) if shapes_before_filter else 0,
        },
        "cameras": out_cameras,
    }
    with open(args.output, "w") as f:
        json.dump(result, f, separators=(",", ":"))

    # CI gate (A-P1-T1 [EMPIRICAL-GATE]): flag a high no-edge fraction rather
    # than silently shipping a sparse sidecar.
    if total_cameras and no_edge_frac >= 0.05:
        print(f"[{args.state_id}] WARNING: {no_edge_frac:.1%} of cameras had no "
              f"resolvable edge (gate is <5%) — sidecar may under-cover.",
              file=sys.stderr)

    avg_edges = (edges_total / len(out_cameras)) if out_cameras else 0
    print(f"[{args.state_id}] DONE: {len(out_cameras)} cameras resolved, "
          f"{edges_total} edges total ({avg_edges:.2f} edges/camera avg), "
          f"{cameras_with_no_edges} cameras had no resolvable edge"
          + (f", {batch_failures} batch(es) failed" if batch_failures > 0 else ""))
    if args.edge_walk_filter:
        rate = (shapes_dropped / shapes_before_filter) if shapes_before_filter else 0
        print(f"[{args.state_id}] EDGE-WALK FILTER: dropped {shapes_dropped}/{shapes_before_filter} "
              f"shapes ({rate:.2%}) that 233'd on the target engine "
              f"({len(edge_walk_cache)} unique edges tested). "
              + ("soft cost ships clean on this engine." if rate < 0.05
                 else "HIGH drop — an engine bump may be worth it for coverage (correctness is fine via fallback)."))
        for s in dropped_ledger:
            print(f"[{args.state_id}]   dropped shape head: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
