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

1. **The daily job stays ENABLED.** `build-cameras.yml` runs on `cron: '23 3 * * *'` (off-peak odd minute since `2d0ab43`, 2026-09-05 — the old `0 8` slot drifted 3–12 h). If the workflow
   is disabled (or GitHub auto-disables the schedule after 60 days of repo inactivity), cameras FREEZE
   — the layer silently goes stale even though the app is healthy. Scheduled workflows only run on the
   default branch.
2. **Every release producer sets `make_latest: false`.** The repo has THREE producers on rolling tags
   (`tiles-latest`, `cameras-latest`, `basemap-latest`). GitHub's "Latest" flag auto-moves to the
   newest release, so any producer that omits `make_latest: false` can steal "Latest" from
   `tiles-latest` (the 2026-06-30 hijack: `/releases/latest` resolved to the camera release, which has
   no `catalog.json`, so fresh installs 404'd → "Offline — waiting for connection"). Since
   2026-09-14 (`22e7681`) `build-cameras.yml` publishes with `gh release upload --clobber`, which
   never touches the "Latest" flag; its release-create fallback MUST keep `--latest=false`. Any
   producer still on `action-gh-release` (tile and basemap workflows) MUST carry `make_latest: false`.
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
opens/updates a GitHub issue when the **served** `cameras-catalog.json` is >30 h old (26 → 30 on 2026-09-05 to absorb schedule drift; and never while a `build-cameras` run is queued/in progress), and warns when
an individual state's `generatedAt` has not moved in >60 h (2× threshold — the Wyoming shape). It is a SEPARATE
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

## Runbook addendum 2026-09-07/08 — a `cron` change may not take effect, and cycling did NOT fix it

`2d0ab43` moved the daily build from `0 8 * * *` to `23 3 * * *` (off-peak odd minute, to cut the observed
3-12 h schedule drift). The live file on the default branch has carried `23 3 * * *` since 2026-09-05
(single `cron:` entry, default branch `main` - both verified via the contents API).

**On 2026-09-07 I cycled the workflow (`gh workflow disable` then `enable`, state `active`) and recorded
here that this was the remedy. That claim was NOT verified and the evidence since does not support it.**
Scheduled fire times, UTC: 08:00 (09-06), 08:30 (09-07), 08:12 (09-08). Over the 15 scheduled runs on
record the minimum fire time is 08:00 and **no run has ever fired before 08:00** - neither before nor
after the cycle.

The evidence is genuinely AMBIGUOUS and I am not going to guess: 08:12 is equally consistent with the OLD
`0 8` slot at +0.2 h drift and with the NEW `23 3` slot at +4.8 h drift (this repo has shown 0-11.7 h
drift). Two samples cannot separate them.

**DECISIVE TEST — a single run that fires before 08:00 UTC proves the new schedule is live; if the minimum
stays >= 08:00 over ~5 more days, the schedule did not re-register and the cycle is not the remedy.**
Check with:
`gh run list -R <repo> --workflow=build-cameras.yml -L 20 --json createdAt,event --jq '.[]|select(.event=="schedule")|.createdAt' | cut -c12-16 | sort | head -1`

**This is cosmetic, not a correctness risk.** Freshness does not depend on the slot: every run since
2026-09-04 succeeded, the watchdog is green on every tick with no issue opened, and its 30 h threshold plus
in-flight suppression already absorbs the full observed drift range from either slot. **Invariant 8 stands
as "after a `cron` edit, CONFIRM a run at the new slot" - but cycling the workflow is NOT a proven remedy,
and the runbook must not claim it is.**

## Runbook addendum 2026-09-14/15 — publish-step retry (GitHub API 5xx) + the cron test is now answered

**Incident.** Run `34747670892` (scheduled, 2026-09-13 08:26 UTC) failed: 16 of 52 `build` jobs died at
"Publish to cameras-latest release" with GitHub `Server Error` / `HttpError fetching GitHub release` —
a transient GitHub API outage (08:43–08:51 UTC), not our data or guard. `softprops/action-gh-release`
makes ONE attempt. Those 16 states kept their prior-day assets, the `catalog` job (`if: always()`) still
ran and passed the 90 % floor, the watchdog stayed green, and the 09-14 run succeeded 52/52 — so devices
saw at most a one-day-old asset for 16 states and never a stale catalog. The owner's failure e-mail was
this run.

**Fix (`22e7681`, mirror `9eaf1f26`).** Both publish steps now run `gh release upload cameras-latest
<file> --clobber` in a 5-attempt backoff loop (30/60/90/120 s), the pattern `build-tiles.yml` already
uses; a create fallback pins `--latest=false`. Uploading an asset never touches the release's "Latest"
flag, so the 2026-06-30 hijack cannot recur from here. **Proof:** `workflow_dispatch` run `34913798068`
(`states=vermont,wyoming`, 2026-09-15 00:35 UTC) — both state publishes and the catalog publish
succeeded on the new steps; assets `cameras-vermont.json` / `cameras-wyoming.json` /
`cameras-catalog.json` updated 00:36–00:37 UTC; the catalog still lists 52 states (50 dated 09-14,
2 dated 09-15) — a subset dispatch does NOT shrink the catalog. "Latest" is still `basemap-latest`.

> **⚠️ RETRACTED 2026-09-20 — the paragraph below overclaims. Its conclusion ("ANSWERED: did NOT
> re-register") rests on a sentence I never measured: "the same repo's watchdog fires within minutes of
> its slot". That is FALSE — see the 2026-09-20 addendum for the measured drift. The question is OPEN and
> the "new workflow filename" escalation must NOT be done on this evidence. Kept as the record.**

**The cron decisive test (addendum above) is ANSWERED: the `23 3 * * *` schedule did NOT re-register.**
Scheduled fire times since the cycle, UTC: 08:16 (09-09), 08:15 (09-10), 08:10 (09-11), 08:01 (09-12),
08:26 (09-13), 09:07 (09-14). Over all 21 scheduled runs on record the minimum is 08:00 and none fired
before it — 9 days after the cron edit, 7 after the cycle. That is the OLD `0 8` slot; +4.8 h drift on
every single day is not credible when the same repo's watchdog fires within minutes of its slot.
Cycling the workflow is not a remedy (confirmed). Today's push (`22e7681`) changed the workflow file
again — the next decisive check is the 2026-09-15 scheduled run: fired ≈03:23–05:00 UTC ⇒ the push
re-registered it; fired ≥08:00 UTC again ⇒ escalate by moving the schedule to a NEW workflow filename
(fresh registration), which is the documented community workaround. Still cosmetic (watchdog margin
covers either slot); Invariant 8 stands.

## Runbook addendum 2026-09-20 — fire times CANNOT answer the cron question; the workflow now reports it

**Correction of my own 2026-09-14/15 claim.** I wrote that the `23 3` schedule "did NOT re-register"
because a +4.8 h drift "is not credible when the same repo's watchdog fires within minutes of its slot".
I had not measured the watchdog. Measured now (28 scheduled watchdog runs, `0 */6 * * *`):

| watchdog slot (UTC) | n | drift, hours (min–max) |
|---|---|---|
| 00:00 | 7 | 1.92 – 2.18 |
| 06:00 | 7 | 3.85 – 5.12 |
| 12:00 | 7 | 2.95 – 5.15 |
| 18:00 | 7 | 1.70 – 3.12 |

Every scheduled run in this repo is 1.7–5.2 h late, and early-morning slots are the worst. A LIVE
`23 3` cron + 4.6–5.4 h lands at 08:00–08:49 UTC — exactly where the camera build fires (six more runs
since: 08:49, 08:43, 08:46, 08:22, 08:10, 08:42 on 09-15…09-20, all after the `22e7681` file push). A
STALE `0 8` cron + 0–0.8 h lands in the same window. **The two hypotheses predict the same fire times, so
no number of further runs separates them.** Points each way, neither decisive: two near-exact 08:00 hits
(08:00:20 on 09-06, 08:01:05 on 09-12) favour the old slot; the repo-wide multi-hour drift favours the
new one. The earlier "AMBIGUOUS, I am not going to guess" addendum was right and I should not have
overridden it.

**What actually decides it (`build-cameras.yml`, setup job, step "Which schedule fired this run?").**
`github.event.schedule` is the cron string GitHub used to fire the run. The step prints it, writes it to
the run summary, and raises `::warning::STALE CRON` when it differs from the declared `23 3 * * *`
(`EXPECTED` in that step must be kept equal to the `cron:` line). Read it off the next scheduled run:
`gh run view <id> -R <repo> --log | grep "fired by cron"`.
- prints `23 3 * * *` ⇒ the schedule IS live; the ~5 h lateness is GitHub's queue, not a registration
  bug, and renaming the workflow would fix nothing. If the lateness matters, the lever is a different
  trigger (e.g. an external dispatcher), not the cron line.
- prints `0 8 * * *` ⇒ stale registration confirmed; only THEN move the schedule to a new workflow
  filename (and update the watchdog's `--workflow=build-cameras.yml` in-flight lookup in the same commit).

Still cosmetic either way: the watchdog's 30 h threshold + in-flight suppression covers both slots, and
there have been no failures and no alarm issues since the 09-13 GitHub outage.

## 2026-09-21 — VERDICT: the `23 3 * * *` schedule IS live; the lateness is GitHub's queue

First instrumented scheduled run `35581627825` (head `00ee4b3`, job started 09:08 UTC):
`fired by cron '23 3 * * *' | file declares '23 3 * * *'` — no STALE CRON warning. The schedule
re-registered on the 2026-09-05 edit after all; every 08:00–09:08 UTC fire time since was the `23 3`
slot plus **4.6–5.8 h of GitHub scheduling delay**, in line with the watchdog's measured 3.9–5.1 h on
its 06:00 slot. Closed: cycling was never needed (the 09-07/08 addendum was right to doubt it), the
09-14/15 "did NOT re-register" claim was wrong (retracted above), and the workflow-rename escalation
is withdrawn for good. The cron line stays; the "Which schedule fired this run?" step stays as the
permanent instrument (Invariant 8: after any cron edit, read `fired by cron` off the next scheduled
run instead of inferring from timestamps). If a ~5 h delay ever matters, the lever is an external
dispatcher (`gh workflow run` from a timer we control), not the cron string.
