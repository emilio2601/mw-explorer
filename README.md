# AM channel explorer

Who is heard where on every AM broadcast channel (530–1700 kHz) across North America and the
Caribbean, hour by hour, and whether a station you want to catch can beat everything else at your
location with a loop antenna null.

## How it works

- **Stations** come from the FCC AM Query (US licenses plus foreign notifications), checked against
  [ISED](https://www.ic.gc.ca/engineering/BC_DBF_FILES/baserad.zip) for Canada and the
  [CRT license registry](https://rpc.crt.gob.mx/vrpc/visor/downloads) for Mexico. Unnamed plan
  allotments, deleted calls and duplicate notifications are dropped; day/night transmitter sites
  are merged. Judgment calls the databases can't make (phantom Cuban half-megawatts, derating)
  live in `station_overrides.json`.
- **Antennas** use the licensed day/night patterns. Directional arrays use the FCC standard
  pattern (47 CFR 73.150/73.160) computed in 3D from the tower data.
- **Skywave** follows the shape of ITU-R P.1147 (sunspot number, geomagnetic latitude, hour
  relative to sunset/sunrise at the path midpoint), with absorption ramping up through daylight.
  **Groundwave** is a flat-earth Norton approximation with a crude curvature loss.
- **Live correction:** for the current hour, WSPR 630 m/160 m paths that resemble each station's
  path are compared with their own nighttime baseline; the median change adjusts skywave.

Model coefficients were written from memory of P.1147 rather than transcribed from it, and the
daytime absorption ramp is an estimate. Treat absolute levels as ±several dB; comparisons between
stations on the same channel are more trustworthy.

## Running locally

Python 3.9+ and nothing else (the Docker image uses 3.12).

```bash
python3 -m mw_explorer build          # station database + 48 hours of grids for every channel
python3 -m mw_explorer serve --open   # http://127.0.0.1:8766/
python3 -m mw_explorer report --freq 540 --home 40.71,-74.01 --target XEWA --wspr
python3 -m unittest                   # tests
```

The first build fetches about 3,000 FCC facility pages plus the ISED and CRT files (about 10
minutes); they're cached for 30 days in `data/cache/`. A full build afterwards (0.5° grid, 48 hours)
takes about 15 minutes on 16 Apple-silicon cores and about 2 hours on a 4-vCPU ARM VPS with 3 workers
(measured), peaks around 0.3 GB of memory per worker, and writes about 400 MB; two builds are kept. `--step 1.0` is about 4× faster and 2.5× smaller but hides small stations' local coverage.

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

Build output lives in `$MW_DATA_DIR/out` (default `./data/out`). Each build gets its own directory,
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

## Deploying on Dokku

Built from the `Dockerfile` (like airline-stats on the same host). One app: the web process serves
the page and API; a daily cron job (`app.json`, 05:15 UTC) rebuilds. Both use a persistent volume at
`/data` for the cache and build output, owned by the container's uid 1000.

Everything below runs through Dokku's SSH interface (`ssh dokku@vps1 <command>`, the same access
`git push` uses), so no `sudo` is needed. `vps1` is the server over Tailscale; SSH isn't open publicly.

```bash
ssh dokku@vps1 apps:create mw-explorer
ssh dokku@vps1 storage:ensure-directory --chown heroku mw-explorer-data   # owned by uid 1000, the container user
ssh dokku@vps1 storage:mount mw-explorer /var/lib/dokku/data/storage/mw-explorer-data:/data
ssh dokku@vps1 domains:set mw-explorer mw.emilio2601.dev
```

DNS (Cloudflare, zone `emilio2601.dev`, SSL/TLS mode Full): one `AAAA mw → 2a01:4f8:1c1e:bfc6::1`
record, **proxied**. The VPS is IPv6-only; with the proxy on, Cloudflare serves IPv4 and IPv6 visitors
and reaches the origin over IPv6.

Deploy from here:

```bash
git remote add dokku dokku@vps1:mw-explorer
git push dokku main
ssh dokku@vps1 letsencrypt:enable mw-explorer
```

The station-data cache has to come from a machine with IPv4: ISED (Canada) and the CRT (Mexico) are
IPv4-only, while the FCC, NOAA and wspr.live work over IPv6. Build it locally (any build refreshes it
when it's older than 30 days) and pipe it into the app's volume; repeat monthly:

```bash
python3 -m mw_explorer build --freq 540
ssh dokku@vps1 run --no-tty mw-explorer sh -c 'mkdir -p /data/cache && cat > /data/cache/fcc_cache.json' < data/cache/fcc_cache.json
```

(If `dokku run` doesn't pass stdin through on your Dokku version, `scp` the file to `vps1` and
`sudo install -o 1000 -g 1000` it into `/var/lib/dokku/data/storage/mw-explorer-data/cache/`.)

Then start the first build (about 2 hours); afterwards the daily cron in `app.json` runs it:

```bash
ssh dokku@vps1 run --detach mw-explorer nice -n 19 python3 -m mw_explorer build --workers 3
```

Each build covers 48 hours, so the 24-hour slider stays full until the next daily run, and a missed run still leaves most of a day. For a private
deployment, `dokku http-auth:enable mw-explorer <user> <password>` (http-auth plugin).
