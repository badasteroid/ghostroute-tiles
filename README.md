# GhostRoute Valhalla Tiles

Valhalla routing tiles for GhostRoute's on-device navigation engine.

## How it works

1. GitHub Actions downloads the latest OSM data from Geofabrik
2. Builds Valhalla tiles using `valhalla_build_tiles` in Docker
3. Deploys the tiles to GitHub Pages as static files
4. GhostRoute's iOS app lazy-loads tiles on demand via HTTP

## Tile URL pattern

```
https://badasteroid.github.io/ghostroute-tiles/{tilePath}
```

Valhalla replaces `{tilePath}` with paths like `2/000/756/000.gph`.

## Building manually

Trigger a build from the Actions tab, or push to main. Default region is NorCal (SF Bay Area).

## Tile structure

- Level 0: Highway network (4° tiles)
- Level 1: Arterial roads (1° tiles)
- Level 2: Local streets (0.25° tiles)
