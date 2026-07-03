#!/usr/bin/env bash
# build-national.sh — REPRODUCIBLE national tile bake for GhostRoute.
#
# WHY THIS REPLACES THE PER-STATE build-tiles.yml
# ------------------------------------------------
# The old workflow built each US state from its own Geofabrik extract, INDEPENDENTLY,
# and shipped every tile level (0/1/2) in every state pack. Valhalla's level-0 (4°)
# long-haul tiles span state lines — e.g. tile 0/002/809 covers both Amarillo TX and
# Clovis NM. Two independent builds produce two DIFFERENT, mutually-incoherent copies
# of that tile; the device merge keeps one (last-writer-wins), so the long-haul graph
# is corrupt across the seam. Proven failure: independent TX+NM builds merged →
# "GraphTile NodeInfo index out of bounds"; on the device it routed Austin→Santa Fe
# the wrong way (SW via I-10) because the NW corridor's shared L0 tile was incoherent.
#
# THE FIX (proven: routes Austin→Santa Fe at 699.8 mi on the correct NW corridor):
# build ONE coherent US graph, then SLICE the resulting tile files into:
#   • base bands  base-0, base-1, …  — the shared L0+L1 (long-haul) tiles, split by
#     longitude so each tar clears GitHub's 2 GB asset cap. Built ONCE → byte-identical
#     wherever a tile is shared. The app installs the band(s) covering a route.
#   • <state>     — that state's L2 (local) tiles + its camera sidecar + POI db.
# A tileSchema:2 catalog tells the app these are base+L2 packs; routingTilePackService
# `shouldWipeForSchema` auto-replaces any legacy per-state packs on first launch.
#
# REQUIREMENTS — a stock GitHub-hosted runner is TOO SMALL (≈14 GB disk). Run on a
# self-hosted / large runner or locally: Docker, gh (authed to the tiles repo),
# osmium-tool (auto-pulled via container), ~60 GB free disk, ~16 GB RAM, ~2 h.
#
# Usage:
#   scripts/ghostroute-tiles-workflow/build-national.sh [--no-publish] [--skip-build]
#     --no-publish : build + slice + catalog locally, do NOT gh-upload (dry run).
#     --skip-build : reuse an existing $WORK/tiles national build (slice/camera/POI only).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# MUST match the device engine (valhalla-mobile HEAD = tag 3.7.0, verified 2026-07-01 from
# source + compiled iOS headers — see memory/project-engine-version-corrected). Baking with a
# mismatched image risks silent graph/costing drift vs what phones execute; the old 3.6.3 pin
# here was the last live copy of the falsified "3.6.3" claim (corrected 2026-07-02).
IMG=ghcr.io/valhalla/valhalla:3.7.0
OSMIUM_IMG=ghcr.io/osmcode/osmium-tool:latest   # `osmium extract` for per-state PBFs
REPO="${TILES_REPO:-badasteroid/ghostroute-tiles}"
TAG="${RELEASE_TAG:-tiles-latest}"
WORK="${WORK:-$PWD/nat-build}"
US_PBF_URL="${US_PBF_URL:-https://download.geofabrik.de/north-america/us-latest.osm.pbf}"
PR_PBF_URL="${PR_PBF_URL:-https://download.geofabrik.de/north-america/us/puerto-rico-latest.osm.pbf}"
BAND_MAX_BYTES="${BAND_MAX_BYTES:-1900000000}"
PUBLISH=1; SKIP_BUILD=0
for a in "$@"; do
  case "$a" in
    --no-publish) PUBLISH=0 ;;
    --skip-build) SKIP_BUILD=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

mkdir -p "$WORK"/{tiles,packs,assets,state_pbf}
# Run a valhalla-image command with the work dir at /data and the scripts at /scripts.
dock() { docker run --rm -v "$WORK:/data" -v "$HERE:/scripts" "$IMG" "$@"; }

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# ── 1. OSM extracts ──────────────────────────────────────────────────────────
if [ "$SKIP_BUILD" = 0 ]; then
  [ -f "$WORK/us.osm.pbf" ] || { log "download us.osm.pbf"; curl -fL --retry 3 --retry-delay 5 -o "$WORK/us.osm.pbf" "$US_PBF_URL"; }
fi
# Per-state PBFs (camera precompute bbox sanity + POI db + L2 bbox). One osmium pass
# over us.osm.pbf using the bboxes in states.json. `osmium extract -c` takes a config
# describing every output; we synthesize it from states.json (id → bbox).
log "synthesize osmium extract config from states.json"
python3 - "$HERE/states.json" "$WORK/extracts.json" <<'PY'
import json, sys
states = json.load(open(sys.argv[1]))
regions = states["regions"] if isinstance(states, dict) else states
cfg = {"directory": "/data/state_pbf", "extracts": []}
for s in regions:
    if s["id"] == "puerto-rico":   # PR is a separate build (not in us.osm.pbf)
        continue
    b = s["bbox"]
    cfg["extracts"].append({"output": f"{s['id']}.osm.pbf",
                            "bbox": [b["swLon"], b["swLat"], b["neLon"], b["neLat"]]})
json.dump(cfg, open(sys.argv[2], "w"), indent=1)
print(f"{len(cfg['extracts'])} state extracts")
PY
if [ "$SKIP_BUILD" = 0 ]; then
  log "osmium extract per-state PBFs"
  docker run --rm -v "$WORK:/data" "$OSMIUM_IMG" \
    extract -v -c /data/extracts.json /data/us.osm.pbf --overwrite
fi

# ── 2. Admins (national) + patch ─────────────────────────────────────────────
# Locate default_speeds.json (telemetry per-state speeds). Lives at assets/valhalla/
# — at the repo root in the main repo, beside the workflow in the tiles repo. Search
# both; copy into $WORK (the only host dir mounted into the container). Optional: the
# build is correct without it (just coarser default speeds), so don't hard-fail.
SPEEDS_FLAG=""
for cand in "$HERE/assets/valhalla/default_speeds.json" "$HERE/../../assets/valhalla/default_speeds.json" "$HERE/../assets/valhalla/default_speeds.json"; do
  if [ -f "$cand" ]; then cp "$cand" "$WORK/default_speeds.json"; SPEEDS_FLAG="--mjolnir-default-speeds-config /data/default_speeds.json"; break; fi
done
[ -n "$SPEEDS_FLAG" ] && log "using default_speeds.json" || log "WARN: default_speeds.json not found — building with coarse class defaults"
log "valhalla_build_config"
# shellcheck disable=SC2086
dock valhalla_build_config \
  --mjolnir-tile-dir /data/tiles \
  --mjolnir-admin /data/tiles/admin.sqlite \
  $SPEEDS_FLAG \
  > "$WORK/valhalla.json"
if [ "$SKIP_BUILD" = 0 ]; then
  log "valhalla_build_admins (national)"
  dock valhalla_build_admins -c /data/valhalla.json /data/us.osm.pbf
  log "admin_patch.py (synthesize US country + Alaska; verify TX/AK drive_on_right)"
  dock python3 /scripts/admin_patch.py /data/tiles/admin.sqlite

  # ── 3. National tile build (ONE coherent graph) ────────────────────────────
  log "valhalla_build_tiles (national — ~72 min)"
  dock valhalla_build_tiles -c /data/valhalla.json /data/us.osm.pbf
fi
COUNT=$(find "$WORK/tiles" -name '*.gph' | wc -l | tr -d ' ')
log "national build has $COUNT .gph tiles"; test "$COUNT" -gt 0

# ── 4. Slice → base + per-state L2, then band-split the base (<2 GB bands) ────
log "slice_tiles.py --bands --verify"
rm -rf "$WORK/packs"; mkdir -p "$WORK/packs"
dock python3 /scripts/slice_tiles.py \
  --tiles-dir /data/tiles --states /scripts/states.json \
  --out /data/packs --base-id base --bands "$BAND_MAX_BYTES" --verify

# ── 5. Puerto Rico — separate coherent build → base-puerto-rico + PR L2 ───────
# (PR is absent from us.osm.pbf; build it alone and ship its own tiny base.)
if [ "$SKIP_BUILD" = 0 ]; then
  log "Puerto Rico build"
  mkdir -p "$WORK/pr/tiles"
  [ -f "$WORK/pr/pr.osm.pbf" ] || curl -fL --retry 3 -o "$WORK/pr/pr.osm.pbf" "$PR_PBF_URL"
  docker run --rm -v "$WORK/pr:/data" "$IMG" valhalla_build_config --mjolnir-tile-dir /data/tiles > "$WORK/pr/valhalla.json"
  docker run --rm -v "$WORK/pr:/data" "$IMG" valhalla_build_tiles -c /data/valhalla.json /data/pr.osm.pbf
fi
docker run --rm -v "$WORK/pr:/data" -v "$HERE:/scripts" "$IMG" python3 /scripts/slice_tiles.py \
  --tiles-dir /data/tiles --states /scripts/states.json --out /data/packs --base-id base-puerto-rico --verify
# Move PR's base + PR L2 pack into the main packs dir.
cp -r "$WORK"/pr/packs/base-puerto-rico "$WORK/packs/" 2>/dev/null || true
cp -r "$WORK"/pr/packs/puerto-rico "$WORK/packs/" 2>/dev/null || true

# ── 6. Per-state camera sidecars + POI dbs against the NATIONAL graph ─────────
# A single valhalla_service over the national tiles resolves every state's cameras
# (so a camera near a state line still resolves to the coherent national edges).
log "start valhalla_service (national) for camera precompute"
SVC=$(docker run -d -v "$WORK:/data" -p 8002:8002 "$IMG" valhalla_service /data/valhalla.json 1)
trap 'docker rm -f "$SVC" >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 60); do curl -fsS --max-time 2 http://localhost:8002/status >/dev/null 2>&1 && break; sleep 1; done
# CAMERA SUPPLY: build-camera-extract.py is now the SINGLE camera producer for the
# bake too (docs/plan/THREAT-TIERS-CORRECTION-2026-07-02.md §1/§3). The old inline
# Overpass fetch (surveillance:type=ALPR ONLY) + bare-float() direction parse that
# lived here is DELETED: it emitted UNTYPED, ALPR-only cameras with `node/`-scheme ids
# that did NOT match the sidecar's `overpass-` ids (the proven double-count schism),
# and it silently dropped DeFlock arc-range directions ("338-23") via `except
# ValueError: pass`. Now each state's cameras come from the SAME extract that feeds
# the fresh feed — polygon-exact, typed (speed/redlight/camera/gunshot + alpr default),
# arc-directions correct, ids canonical — so fresh feed ≡ bake input by construction.
# The extract runs per state in the loop below, over the state PBF already on disk
# (reused from the POI/address stage), and its wrapped output feeds precompute directly
# (precompute unwraps {cameras:[...]} and reads the `dir` field).
# Enumerate state ids to a file and loop over the FILE (not a pipe) so the body runs
# in THIS shell — failure counters persist (a pipe `| while` would lose them) and it's
# bash-3.2 safe (no mapfile).
python3 -c "import json,sys;d=json.load(open(sys.argv[1]));rs=d['regions'] if isinstance(d,dict) else d;print('\n'.join(s['id'] for s in rs))" "$HERE/states.json" > "$WORK/state_ids.txt"
CAM_FAILS=0; POI_FAILS=0; ADDR_FAILS=0
while read -r id; do
  [ -z "$id" ] && continue
  [ "$id" = "puerto-rico" ] && continue  # PR's tiles are in its OWN build, not this US service
  PACK="$WORK/packs/$id"
  [ -d "$PACK" ] || continue
  BBOX=$(python3 -c "import json;d=json.load(open('$HERE/states.json'));rs=d['regions'] if isinstance(d,dict) else d;b=[s for s in rs if s['id']=='$id'][0]['bbox'];print(f\"{b['swLat']},{b['swLon']},{b['neLat']},{b['neLon']}\")")
  if [ -f "$WORK/state_pbf/$id.osm.pbf" ]; then
    # (1) EXTRACT this state's threats from its PBF (the SAME extract that feeds the
    # fresh feed — canonical overpass- ids, typed tiers, arc-aware directions). Writes
    # the wrapped {schemaVersion:2, cameras:[...]} doc precompute now consumes directly.
    docker run --rm -v "$WORK:/data" -v "$HERE:/scripts" "$IMG" sh -c \
      "pip install --quiet osmium >/dev/null 2>&1; python3 /scripts/build-camera-extract.py /data/state_pbf/$id.osm.pbf $id /data/packs/$id/cameras-$id.json" \
      || { log "camera extract FAILED for $id — dropping partial sidecar (state falls back to runtime cameras)"; rm -f "$PACK/cameras-$id.json" "$PACK/cameras-edges-$id.json"; CAM_FAILS=$((CAM_FAILS+1)); }
    # (2) PRECOMPUTE FOV edges from the extract, resolved against the national graph.
    # Share the valhalla_service container's network so localhost:8002 resolves to it.
    if [ -f "$PACK/cameras-$id.json" ]; then
      docker run --rm --network "container:$SVC" -v "$WORK:/data" -v "$HERE:/scripts" "$IMG" python3 /scripts/precompute-camera-edges.py \
        --service-url http://localhost:8002 --edge-walk-filter \
        --cameras-input "/data/packs/$id/cameras-$id.json" --bbox "$BBOX" --state-id "$id" \
        --output "/data/packs/$id/cameras-edges-$id.json" \
        || { log "camera precompute FAILED for $id — dropping partial sidecar (state falls back to runtime cameras)"; rm -f "$PACK/cameras-edges-$id.json"; CAM_FAILS=$((CAM_FAILS+1)); }
    fi
    # The extract's raw cameras-$id.json is a precompute INPUT only — the tile pack
    # ships the resolved cameras-edges-$id.json sidecar, and the fresh feed ships the
    # raw file separately via build-cameras.yml. Drop it so it doesn't linger in the
    # pack dir (the §7 tar globs never include it, but keep the dir clean).
    rm -f "$PACK/cameras-$id.json"
    # POI + address dbs from the same state PBF.
    docker run --rm -v "$WORK:/data" -v "$HERE:/scripts" "$IMG" sh -c \
      "pip install --quiet osmium >/dev/null 2>&1; python3 /scripts/build-poi-db.py /data/state_pbf/$id.osm.pbf /data/packs/$id/pois-$id.sqlite" \
      || { log "POI db FAILED for $id — dropping partial db"; rm -f "$PACK/pois-$id.sqlite"; POI_FAILS=$((POI_FAILS+1)); }
    # On-device HOUSE-LEVEL address db (fix for "addresses show only the street").
    # Built from the SAME state PBF as the POI db (OSM leg). Optional 3rd arg
    # (a per-state OpenAddresses CSV slice, $WORK/oa/$id.csv) is passed when
    # present — that's the residential coverage OSM lacks (spec §4.5). The db is
    # written into the pack dir; the SIZE-AWARE tar step (below) then either tars it
    # INTO the per-state pack (small/medium states — no plumbing) or, for a giant
    # state whose tar would overflow the 2 GB cap (measured: TX tar ~1.89 GB + addr
    # ~214 MB), ships it as a SEPARATE uncompressed addr tar. Either way it lands in
    # tilesDir where addressSearchService.discover() finds it.
    OA_CSV=""; [ -f "$WORK/oa/$id.csv" ] && OA_CSV="/data/oa/$id.csv"
    docker run --rm -v "$WORK:/data" -v "$HERE:/scripts" "$IMG" sh -c \
      "pip install --quiet osmium >/dev/null 2>&1; python3 /scripts/build-address-db.py /data/state_pbf/$id.osm.pbf /data/packs/$id/addr-$id.sqlite $OA_CSV" \
      || { log "address db FAILED for $id — dropping partial db (state falls back to Photon addresses)"; rm -f "$PACK/addr-$id.sqlite"; ADDR_FAILS=$((ADDR_FAILS+1)); }
  fi
done < "$WORK/state_ids.txt"
log "camera precompute failures: $CAM_FAILS ; POI db failures: $POI_FAILS ; address db failures: $ADDR_FAILS"
docker rm -f "$SVC" >/dev/null 2>&1 || true; trap - EXIT

# ── 7. Tar each pack + generate the tileSchema:2 catalog ──────────────────────
#
# ADDRESS-DB SHIPPING (2026-07-01, measured decision — spec §4.3 + the fix for
# "addresses show only the street"):
#   PHASE-1 FAST PATH (default): addr-<state>.sqlite rides INSIDE the per-state tile
#   tar, exactly like pois-<state>.sqlite. The app already downloads+extracts this tar
#   into tilesDir (installTilePack → TileTarExtractor extracts EVERY regular file, then
#   the native ._-strip keeps .sqlite), and addressSearchService.discover() already
#   scans tilesDir for addr-*.sqlite — so NO catalog block, NO separate download, NO
#   on-device gunzip. Just a re-bake + the user's normal pack re-install.
#
#   AUTO-FALLBACK (giant states only): measured — the TX L2 tar is already ~1.89 GB and
#   full-state OSM addr-texas.sqlite is ~214 MB (1.65M rows), so TX (and CA/FL) would
#   blow the hard 2 GB GitHub asset cap if the addr db rode in-tar. Rather than fail the
#   build (which would deny the user's OWN state addresses) we size-check per state: if
#   tiles-tar + addr fits under BUDGET, addr goes IN the tar; else addr is kept OUT and
#   shipped as a SEPARATE on-demand asset — an UNCOMPRESSED tar (ghostroute_<id>_addr.tar),
#   which gen-catalog.py records in an `addresses` block and routingTilePackService installs
#   via the SAME native installTilePack/TileTarExtractor path (no on-device gunzip → no new
#   decompression dependency). Either way the db lands in the SAME place discover() looks —
#   the app path is identical; only delivery differs by size.
log "tar packs (addr db in-tar when it fits; separate addr tar for giant states)"
rm -f "$WORK"/assets/*.tar
# 2 GB GitHub per-asset hard cap. Reserve 32 MB headroom for tar block padding + any
# slack so a pack that measures just under never trips the cap after re-tar.
TAR_HARD_CAP=2147483647          # 2 GiB - 1
# addr-in-tar budget = cap - headroom. Overridable (ADDR_IN_TAR_BUDGET env) ONLY so a
# test can force the separate-asset fallback branch on a tiny fixture; CI/prod leave it
# unset and get the real 2 GB-minus-headroom budget.
ADDR_IN_TAR_BUDGET="${ADDR_IN_TAR_BUDGET:-$((TAR_HARD_CAP - 32 * 1024 * 1024))}"
statsize() { stat -f%z "$1" 2>/dev/null || stat -c%s "$1"; }
for d in "$WORK"/packs/*/; do
  id=$(basename "$d")
  tar_path="$WORK/assets/ghostroute_${id}_tiles.tar"
  addr_tar="$WORK/assets/ghostroute_${id}_addr.tar"
  adb="$d/addr-$id.sqlite"
  addr_bytes=0; [ -f "$adb" ] && addr_bytes=$(statsize "$adb")

  # 1) Base tar = tiles + camera sidecar + POI db (NO addr yet). Explicit roots (NOT
  #    `.`): `tar cf … .` prefixes `./`, which TileTarExtractor rejects.
  ( cd "$d" && shopt -s nullglob && tar cf "$tar_path" \
      [0-9] [0-9][0-9] cameras-edges-*.json pois-*.sqlite )
  if ! tar tf "$tar_path" | head -1 >/dev/null 2>&1; then
    echo "::error::pack $id produced an empty/unreadable tar"; exit 1; fi
  base_sz=$(statsize "$tar_path")

  # 2) Decide: addr IN-TAR (fits the budget) vs a SEPARATE addr TAR (too big).
  #    The fallback is an UNCOMPRESSED tar (not gzip) on purpose: the device installs
  #    it through the EXISTING native TileTarExtractor + installTilePack path (extracts
  #    addr-<id>.sqlite into tilesDir, manifest-tracked) — no on-device gunzip, so no
  #    new decompression dependency and no native change. The addr db lands in the SAME
  #    tilesDir either way; addressSearchService.discover() can't tell them apart.
  if [ "$addr_bytes" -gt 0 ] && [ $((base_sz + addr_bytes)) -le "$ADDR_IN_TAR_BUDGET" ]; then
    # Re-tar WITH the addr db appended (PHASE-1 FAST PATH — no catalog/download plumbing).
    ( cd "$d" && shopt -s nullglob && tar cf "$tar_path" \
        [0-9] [0-9][0-9] cameras-edges-*.json pois-*.sqlite addr-*.sqlite )
    log "  $id: addr in-tar ($((addr_bytes/1024/1024)) MB; tar $((base_sz/1024/1024))→$(($(statsize "$tar_path")/1024/1024)) MB)"
  elif [ "$addr_bytes" -gt 0 ]; then
    # AUTO-FALLBACK: too big for the tile tar — ship addr as its OWN tar asset.
    ( cd "$d" && shopt -s nullglob && tar cf "$addr_tar" addr-*.sqlite )
    if ! tar tf "$addr_tar" | head -1 >/dev/null 2>&1; then
      echo "::error::pack $id produced an empty/unreadable addr tar"; exit 1; fi
    asz=$(statsize "$addr_tar")
    if [ "$asz" -gt "$TAR_HARD_CAP" ]; then
      echo "::error::addr tar for $id is $asz bytes > 2 GB — split this state's addr db"; exit 1; fi
    log "  $id: addr SEPARATE tar (tile tar $((base_sz/1024/1024)) MB + addr $((addr_bytes/1024/1024)) MB > budget → ghostroute_${id}_addr.tar $((asz/1024/1024)) MB)"
  fi

  # 3) Hard cap guard (should never trip now — the budget check keeps addr out when big).
  sz=$(statsize "$tar_path")
  if [ "$sz" -gt "$TAR_HARD_CAP" ]; then
    echo "::error::pack $id tile tar is $sz bytes > 2 GB even WITHOUT the addr db —"
    echo "::error::  the L2 tiles + POI db alone overflow. Split this state's L2 (slice_tiles)."
    exit 1
  fi
done

log "generate catalog.json (tileSchema:2)"
python3 "$HERE/gen-catalog.py" \
  --packs-dir "$WORK/packs" --assets-dir "$WORK/assets" \
  --states "$HERE/states.json" \
  --repo "$REPO" --tag "$TAG" --out "$WORK/assets/catalog.json"

# ── 8. Publish ────────────────────────────────────────────────────────────────
if [ "$PUBLISH" = 1 ]; then
  log "gh release upload → $REPO $TAG"
  gh release view "$TAG" -R "$REPO" >/dev/null 2>&1 || gh release create "$TAG" -R "$REPO" --title "Tile packs (national)" --notes "Coherent national build, base bands + per-state L2."
  # Upload ALL tars + catalog. `*.tar` covers both the per-state tile tars
  # (ghostroute_<id>_tiles.tar) AND any separate addr tars (ghostroute_<id>_addr.tar)
  # the giant states produced — small/medium states ship addr in-tar and have no addr
  # tar. So one glob uploads everything.
  gh release upload "$TAG" -R "$REPO" "$WORK"/assets/*.tar "$WORK/assets/catalog.json" --clobber
  log "published $(ls "$WORK"/assets/*_tiles.tar 2>/dev/null | wc -l | tr -d ' ') tile packs + $(ls "$WORK"/assets/*_addr.tar 2>/dev/null | wc -l | tr -d ' ') separate addr tars + catalog"
else
  log "DRY RUN (--no-publish): assets in $WORK/assets/ ($(ls "$WORK"/assets/*_tiles.tar 2>/dev/null | wc -l | tr -d ' ') tile packs + $(ls "$WORK"/assets/*_addr.tar 2>/dev/null | wc -l | tr -d ' ') addr tars)"
fi
log "DONE"
