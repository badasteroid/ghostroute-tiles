# Camera pipeline — health invariants & check (keep cameras fresh + correct)

The ALPR camera layer is **decoupled from the map/tile bake on purpose**: cameras are crowd-added
daily, so they refresh on their own fast cadence and must NEVER require a tile rebake. This doc lists
the invariants that keep that true and a 60-second health check. Complements
`RECOVERY-RUNBOOK-CAMERA-SIDECARS.md` (which covers the separate empty-sidecar precompute outage).

## Architecture (one line)

`build-cameras.yml` (DAILY cron) → `build-camera-extract.py` per state from fresh Geofabrik OSM →
tiny `cameras-<state>.json` + `cameras-catalog.json` on the **`cameras-latest`** GitHub Release →
the app polls the catalog and downloads only changed states. Tiles (`build-tiles.yml` → `tiles-latest`)
and basemaps (`basemap-latest`) are SEPARATE producers on their own cadences. Repo:
`badasteroid/ghostroute-tiles`.

## INVARIANTS — do not break these

1. **The daily job stays ENABLED.** `build-cameras.yml` runs on `cron: '0 8 * * *'`. If the workflow
   is disabled (or GitHub auto-disables the schedule after 60 days of repo inactivity), cameras FREEZE
   — the layer silently goes stale even though the app is healthy. Scheduled workflows only run on the
   default branch.
2. **Every release producer sets `make_latest: false`.** The repo has THREE producers on rolling tags
   (`tiles-latest`, `cameras-latest`, `basemap-latest`). GitHub's "Latest" flag auto-moves to the
   newest release, so any producer that omits `make_latest: false` can steal "Latest" from
   `tiles-latest` (the 2026-06-30 hijack: `/releases/latest` resolved to the camera release, which has
   no `catalog.json`, so fresh installs 404'd → "Offline — waiting for connection"). Both
   `action-gh-release` steps in `build-cameras.yml` MUST carry `make_latest: false` (restored
   2026-07-17). The tile and basemap workflows must too.
3. **The app pins FIXED tags, never `/releases/latest`.** This is the load-bearing defense (invariant
   2 is defense-in-depth). Verify these stay pinned to `releases/download/<tag>/…`:
   - routing: `src/services/routingTilePackService.ts` `GITHUB_CATALOG_URL` → `tiles-latest/catalog.json`
   - cameras: `src/services/cameraCatalogService.ts` `CAMERAS_BASE` → `cameras-latest`
   - basemap: `src/services/basemapAssetService.ts` → `basemap-latest`
4. **Publish guards in `build-cameras.yml` stay intact** (they self-audit each state before publish):
   - vandalism/deletion guard: refuse if a state's count drops > 30% vs the released file;
   - id-format gate: every `id` MUST start with `overpass-` (prevents the `node/` double-count schism);
   - schema gate: `schemaVersion == 2` (typed tiers + canonical ids); the app's `ACCEPTED_SCHEMA` = {1,2}.
5. **Precompute needs a healthy local Valhalla** (`localhost:8002`) or sidecars resolve 0 edges — the
   2026-06-16 empty-sidecar outage. Detection + fix: `RECOVERY-RUNBOOK-CAMERA-SIDECARS.md`.

## 60-second health check (run any time; needs `gh` authed to the tiles repo)

```
R=badasteroid/ghostroute-tiles
# 1) daily job ENABLED + running? (state must be 'active'; last run within ~24h, success)
gh api repos/$R/actions/workflows --jq '.workflows[] | select(.path|test("build-cameras")) | {name,state}'
gh run list -R $R --workflow=build-cameras.yml -L 3
# 2) cameras-latest FRESH? (a state's asset updated within ~24h)
gh api repos/$R/releases/tags/cameras-latest --jq '.assets[] | select(.name=="cameras-california.json") | .updated_at'
# 3) "Latest" is NOT held by a camera/basemap release (should be tiles-latest, or nothing app-relevant)
gh release list -R $R | grep -i latest
gh api repos/$R/releases/latest --jq '.tag_name'   # app doesn't use this, but it must not 404 routing for anything that does
# 4) app still pins fixed tags (no /releases/latest)
grep -rn "releases/latest" src/services/*.ts   # expect ZERO matches
```

PASS = workflow `active` + a run within ~24h + a state asset `updated_at` within ~24h + no
`src/services/*.ts` match for `/releases/latest`.

## Findings 2026-09-03 — 14-day SILENT freeze, root-caused and fixed

Cameras were frozen 2026-08-20..09-02 (last good catalog 2026-08-19). **Two independent bugs**,
both now fixed (tiles-repo `f4a083f`, `8d8724d`):

1. **Publish-guard DEADLOCK.** Wyoming tripped `count 143 < 70% of previous 546` every day. The
   drop was REAL (counts stable 138–144 across 14 runs; no tiles-repo code change since
   2026-07-03 → upstream OSM removal of a bulk import). But the guard compares against the
   **released** file, so blocking the publish froze that baseline at 546 forever — it could never
   self-heal. FIX: `camera-publish-guard.py` (unit-tested, 12/12) publishes a >30% drop iff the new
   count is within ±10% of a **ratified** entry in `camera-count-baselines.json`. Wyoming ratified
   @143. Ratifications >180 days old warn.
2. **One state froze all 53.** The `catalog` job was `needs: [setup, build]` with no `if:`, so ANY
   failed state skipped it. 51/53 per-state assets published fine on 2026-09-02 — but the app gates
   every download on the catalog, so devices saw **nothing** for 14 days. FIX: `if: always() &&
   needs.setup.result == 'success'` plus a **90% coverage floor** (refuse a shrunken catalog, keep
   the last-good one).

Also fixed: Geofabrik 502/503 refresh-window overload (failed 11 states on 2026-08-28) now survives
an outer retry loop; `make_latest: false` restored on the catalog publish (it had drifted out of the
tiles-repo copy — the sync hazard invariant 2 warns about, realised).

**RECEIPT:** run `33811617073` SUCCESS, 53/53 jobs, 0 failures; `cameras-wyoming.json` count=143;
`cameras-catalog.json` refreshed 2026-09-03T22:31Z with 52 states.

### The real lesson: it was SILENT

Nothing watched. Invariant 1 assumed a disabled workflow was the only freeze mode; this freeze
happened with the workflow **enabled and green-ish** (per-state jobs succeeded). So:

**INVARIANT 6 — a watchdog must check the OUTCOME, not the job.** `cameras-freshness-watch.yml`
(cron every 6 h, decision in `camera-freshness-check.py`, 7/7 unit tests) fails loudly and
opens/updates a GitHub issue when the **served** `cameras-catalog.json` is >26 h old, and warns when
an individual state's `generatedAt` has not moved in >52 h (the Wyoming shape). It is a SEPARATE
workflow on purpose: a watchdog inside the thing it watches cannot report that thing failing to run
at all. Verified by dispatch: run `33829144617` SUCCESS.

**INVARIANT 7 — publish-blocking guards must have a ratification path.** Any guard that refuses to
publish while comparing against the last published artifact is a latent deadlock. Give it an
explicit, auditable, human-committed override (`camera-count-baselines.json`) or it will one day
freeze the layer permanently.

## Findings 2026-07-17 (earlier check — superseded above)

- App side CORRECT: all three services pin fixed tags (immune to the Latest-flag hijack).
- Extract/guards CORRECT: vandalism 30%, `overpass-` id gate, schema 2 all present in `build-cameras.yml`.
- **ISSUE — daily job is DISABLED** (`disabled_manually`); last successful run 2026-07-11, so cameras
  are frozen ~6 days. Re-enable to restore freshness: `gh workflow enable build-cameras.yml -R
  badasteroid/ghostroute-tiles` (or in the repo Actions UI). Confirm it was not disabled intentionally.
- **FIXED — `make_latest: false`** was absent from both `build-cameras.yml` publish steps (lost in the
  typed-extract-v2 rewrite; `git log -S make_latest` shows it was never in the app-repo copy). Restored
  here 2026-07-17. The authoritative CI copy in the tiles repo needs the same change synced. "Latest"
  is currently held by `basemap-latest` (harmless only because the app pins tags — but the basemap
  producer should also set `make_latest: false`).
