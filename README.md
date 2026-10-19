# MW Explorer

Explore predicted AM radio reception on every broadcast channel (530–1700 kHz) across North America
and the Caribbean, hour by hour. Compare stations at your location and see how a loop antenna's
null affects interference.

## How it works

- **Stations** come from the FCC AM Query (US licenses plus foreign notifications), checked against
  [ISED](https://www.ic.gc.ca/engineering/BC_DBF_FILES/baserad.zip) for Canada and the
  [CRT license registry](https://rpc.crt.gob.mx/vrpc/visor/downloads) for Mexico. Unnamed plan
  allotments, deleted calls and duplicate notifications are dropped; day/night transmitter sites
  are merged. Manual corrections to station data and power estimates are recorded in
  `station_overrides.json`.
- **Antennas** use the licensed day/night patterns. Directional arrays use the FCC standard
  pattern (47 CFR 73.150/73.160) computed in 3D from the tower data.
- **Skywave** approximates ITU-R P.1147 using sunspot number, geomagnetic latitude and the hour
  relative to sunset/sunrise at the path midpoint, with increased absorption during daylight.
  **Groundwave** uses a flat-earth Norton approximation with an approximate curvature loss.
- **Live correction:** for the current hour, WSPR 630 m/160 m paths that resemble each station's
  path are compared with their own nighttime baseline; the median change adjusts skywave.

The P.1147 coefficients were reconstructed from memory, and daytime absorption is an estimate.
Absolute field strengths may be off by several dB. Comparisons between stations on the same
channel are more reliable.

## Running locally

Requires Python 3.9+ with no third-party packages.

```bash
python3 -m mw_explorer build          # station database + 48 hours of grids for every channel
python3 -m mw_explorer serve --open   # http://127.0.0.1:8766/
python3 -m mw_explorer report --freq 540 --home 40.71,-74.01 --target XEWA --wspr
python3 -m unittest                   # tests
```

The first build fetches about 3,000 FCC facility pages plus the ISED and CRT files. Downloads are
cached for 30 days in `data/cache/`. A full build uses a 0.5° grid covering 48 hours, writes about
400 MB and peaks around 0.3 GB of memory per worker. Build time depends on the available CPU
cores; use `--workers` to limit parallelism. Two builds are kept.

For a smaller build, use `--freq 540` to select a channel or `--step 1.0` for a coarser grid.
The coarser grid can miss small stations' local coverage.

## Layout

| Path | What |
|---|---|
| `mw_explorer/sources.py` | FCC / ISED / CRT fetching and parsing, cache |
| `mw_explorer/stations.py` | per-channel station lists: license checks, de-duplication, overrides |
| `mw_explorer/antenna.py` | antenna patterns |
| `mw_explorer/model.py` | skywave and groundwave field strength |
| `mw_explorer/build.py` | daily build: grid frames per channel and hour |
| `mw_explorer/home.py` | levels at any location (used by the API and `report`) |
| `mw_explorer/live.py` | Kp, sunspot number, WSPR |
| `mw_explorer/server.py` | HTTP server and API |
| `static/index.html` | the explorer page |

Build output is stored in `$MW_DATA_DIR/out` (default `./data/out`). Each build gets its own directory,
`builds/<id>/`, holding `db.pickle` (the station database the server loads) and `ch/<kHz>/stations.json`
plus one gzipped grid frame per UTC hour. `index.json` names the current build and is switched only
once a build is complete; the previous build is kept so open pages keep working, and the page tells the
API which build it loaded so station numbers always line up.

## API

- `GET /api/home?freq=540&lat=40.71&lon=-74.01&start=YYYYMMDDHH&n=13` — sky, ground and total
  dBµV/m from every station within 5,000 km, per hour, plus current Kp.
- `GET /api/wspr?freq=540&lat=40.71&lon=-74.01&i=1,4` — live WSPR skywave correction for those
  station indices (current hour). Cached 15 minutes.
- `GET /api/health`

Per-client rate limits apply (60 home / 10 WSPR requests per minute).

## Hosting

The included `Dockerfile` uses Python 3.12 and starts the web server. Mount persistent storage at
`/data`, writable by UID/GID 1000, to keep the cache and build output. Set `PORT` to the port your
host expects; the server defaults to 8766.

Run `python3 -m mw_explorer build` before using the explorer, then schedule it daily. The build
process and web server must share the same data directory. Each build covers 48 hours by default,
allowing the 24-hour slider to stay populated between daily runs.

For Dokku, `Procfile` defines the web process and `app.json` provides a daily build schedule and
startup health check. Adjust the schedule and worker count for your host.
