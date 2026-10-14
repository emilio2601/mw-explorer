"""Daily build: the station database plus a who-wins-where grid for every channel and upcoming hour.

Output (under config.out_dir()):
  index.json                 the current build: channels, call-sign index, grid geometry, hours, sunspot number
  builds/<id>/db.pickle      station lists with antenna models, loaded by the server for per-location queries
  builds/<id>/ch/<kHz>/stations.json     station metadata and map colors for that channel
  builds/<id>/ch/<kHz>/<YYYYMMDDHH>.bin.gz  one packed grid frame per UTC hour (see CELL_FORMAT)

Station indices in frames and API responses only mean something within one build, so each build gets its
own directory and index.json switches to it only once it is complete. The previous build is kept so pages
loaded before the switch keep working.
"""
import gzip
from array import array
import heapq
import json
import math
import os
import pickle
import shutil
import struct
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone

from . import config
from .geo import gc_km, solar
from .live import fetch_ssn
from .model import MAX_PATH_KM, NOISE_DBU, hourly_loss, path_terms, station_mode
from .stations import REGIONS, discover

COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
# Per cell: winner index, winner's margin (dB), then the top three (index, dBuV/m). 0xFFFF = none.
CELL_FORMAT = "<Hb" + "Hb" * 3
CELL_BYTES = struct.calcsize(CELL_FORMAT)
HOUR_FMT = "%Y%m%d%H"


def hour_key(t):
    return t.strftime(HOUR_FMT)


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, path)


def grid_cells(g):
    return [(g["lat0"] + (i + 0.5) * g["step"], g["lon0"] + (j + 0.5) * g["step"])
            for i in range(g["nlat"]) for j in range(g["nlon"])]


def assign_colors(wins, bests, nlat, nlon):
    """Give every winning station a palette color, reusing colors only between areas that never touch.

    Like coloring a political map: bigger winners choose first, so they keep the same colors run to run,
    and each later station takes a color no neighboring winner has (or the least-shared one if all are taken).
    """
    touch = defaultdict(lambda: defaultdict(int))
    for best in bests:
        for i in range(nlat):
            for j in range(nlon):
                a = best[i * nlon + j]
                if a < 0:
                    continue
                for b in ((best[i * nlon + j + 1] if j + 1 < nlon else -1),
                          (best[(i + 1) * nlon + j] if i + 1 < nlat else -1)):
                    if b >= 0 and b != a:
                        touch[a][b] += 1
                        touch[b][a] += 1
    color_of = {}
    for k in sorted(wins, key=lambda k: -wins[k]):
        clash = defaultdict(int)
        for nb, n in touch[k].items():
            if nb in color_of:
                clash[color_of[nb]] += n
        color_of[k] = min(COLORS, key=lambda c: (clash[c], COLORS.index(c)))
    return color_of


def compute_channel(task):
    """Grid frames for one channel; writes the hour files and returns the station metadata."""
    freq, stations, ctx = task
    t0 = time.time()
    g = ctx["grid"]
    cells = grid_cells(g)
    # Per station and mode, only what each hour needs, in flat arrays: a dict of path terms per station and
    # cell takes gigabytes on busy channels (over 4 GB for ~215 stations at 0.5 degrees).
    per = []
    for st in stations:
        arrs = {m: (array("I"), array("d"), array("d"), array("d"), array("d")) for m in st["ant"]}
        for c, (la, lo) in enumerate(cells):
            if gc_km(st["lat"], st["lon"], la, lo) > MAX_PATH_KM:
                continue
            terms = path_terms(st, la, lo, ctx["ssn"], freq)
            for m, (ci, sky, gw, mla, mlo) in arrs.items():
                ci.append(c)
                sky.append(terms["sky" + m])
                gw.append(terms["gw" + m])
                mla.append(terms["mid" + m][0])
                mlo.append(terms["mid" + m][1])
        per.append(arrs)
    out = build_dir(ctx["build"]) / "ch" / str(freq)
    wins, bests = defaultdict(int), []
    for key in ctx["hours"]:
        sol = solar(datetime.strptime(key, HOUR_FMT).replace(tzinfo=timezone.utc))
        total = [0.0] * len(cells)
        tops = [[] for _ in cells]
        for k, st in enumerate(stations):
            arrs = per[k].get(station_mode(st, sol))
            if not arrs:
                continue
            ci, sky, gw, mla, mlo = arrs
            for j in range(len(ci)):
                v = 10 * math.log10(10 ** ((sky[j] - hourly_loss(mla[j], mlo[j], sol)) / 10) + 10 ** (gw[j] / 10))
                c = ci[j]
                total[c] += 10 ** (v / 10)
                t = tops[c]
                if len(t) < 3:
                    t.append((v, k))
                    t.sort(reverse=True)
                elif (v, k) > t[-1]:
                    t[-1] = (v, k)
                    t.sort(reverse=True)
        buf = bytearray(CELL_BYTES * len(cells))
        frame_best = [-1] * len(cells)
        bests.append(frame_best)
        for c in range(len(cells)):
            top = tops[c]
            if top and top[0][0] >= NOISE_DBU:
                rest_mw = total[c] - 10 ** (top[0][0] / 10)
                rest = 10 * math.log10(rest_mw) if rest_mw > 0 else -99
                best, du = top[0][1], top[0][0] - max(rest, NOISE_DBU)
                wins[best] += 1
                frame_best[c] = best
            else:
                best, du = 0xFFFF, 0
            top = top + [(0, 0xFFFF)] * (3 - len(top))
            struct.pack_into(CELL_FORMAT, buf, c * CELL_BYTES, best, max(-128, min(127, round(du))),
                             *(x for v, k in top for x in (k, max(-128, min(127, round(v))))))
        write_atomic(out / f"{key}.bin.gz", gzip.compress(bytes(buf), 6))

    color_of = assign_colors(wins, bests, g["nlat"], g["nlon"])
    meta = [{"call": st["call"], "city": st["city"], "state": st["state"], "country": st["country"],
             "lat": round(st["lat"], 4), "lon": round(st["lon"], 4),
             "sites": {h: [round(x, 4) for x in site] for h, site in st["site"].items()},
             "ant": {h: a.label for h, a in st["ant"].items()}, "note": st.get("note"),
             "color": color_of.get(k), "wins": wins.get(k, 0)} for k, st in enumerate(stations)]
    write_atomic(out / "stations.json", json.dumps({"freq": freq, "build": ctx["build"], "ssn": ctx["ssn"],
                                                    "hours": ctx["hours"], "stations": meta},
                                                   separators=(",", ":")))
    das = [a for st in stations for a in st["ant"].values() if a.rms_check]
    ok = sum(1 for a in das if abs(a.rms_check[0] / a.rms_check[1] - 1) <= 0.02)
    return freq, len(stations), time.time() - t0, (len(das), ok)


def build_dir(build_id):
    return config.out_dir() / "builds" / build_id


def current_build():
    try:
        return json.loads((config.out_dir() / "index.json").read_text()).get("build")
    except (OSError, ValueError):
        return None


def carry_over(prev, new, freqs):
    """Hard-link channels a partial build didn't recompute from the previous build into the new one."""
    src = build_dir(prev) / "ch"
    for ch in (src.iterdir() if src.is_dir() else ()):
        if int(ch.name) in freqs:
            continue
        dst = build_dir(new) / "ch" / ch.name
        dst.mkdir(parents=True, exist_ok=True)
        for f in ch.iterdir():
            try:
                os.link(f, dst / f.name)
            except OSError:
                shutil.copy2(f, dst / f.name)


def prune(keep):
    root = config.out_dir() / "builds"
    for d in (root.iterdir() if root.is_dir() else ()):
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)


def build(freqs=None, hours=48, step=0.5, workers=None, refresh=False, exclude=(), use_ised=True, use_crt=True):
    freqs = list(freqs or config.CHANNELS)
    region = REGIONS["na"]
    by_freq, dropped = discover(None, None, freqs, refresh, set(exclude), use_ised, region["stations"], use_crt)
    by_freq = {f: s for f, s in by_freq.items() if s}
    print(f"Dropped {sum(len(v) for v in dropped.values())} notifications with no current license "
          f"(ISED for Canada, CRT for Mexico), no call sign, or a deleted call.", file=sys.stderr)

    ssn, ssn_src = fetch_ssn()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    lat0, lat1, lon0, lon1 = region["grid"]
    grid = {"lat0": lat0, "lon0": lon0, "step": step,
            "nlat": int((lat1 - lat0) / step), "nlon": int((lon1 - lon0) / step)}
    build_id = now.strftime(HOUR_FMT) + "-" + str(int(time.time()))
    ctx = {"ssn": ssn, "grid": grid, "build": build_id,
           "hours": [hour_key(now + timedelta(hours=i)) for i in range(hours)]}

    # Cap disk at two builds: clear anything older than the current one (and any crashed build) before
    # writing the new one. Pages still on an older build get a 410 and move to the current build.
    prune({current_build()} - {None})

    total = sum(len(s) for s in by_freq.values())
    print(f"{len(by_freq)} channels, {total} stations {region['label']}; SSN {ssn:.0f} ({ssn_src}); "
          f"{hours} hours from {ctx['hours'][0]} UTC with {workers or os.cpu_count()} processes...", file=sys.stderr)
    t0 = time.time()
    checks = [0, 0]
    tasks = sorted(((f, s, ctx) for f, s in by_freq.items()), key=lambda t: -len(t[1]))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for n, (f, ns, dt, (nda, ok)) in enumerate(pool.map(compute_channel, tasks), 1):
            checks[0] += nda
            checks[1] += ok
            print(f"  [{n}/{len(tasks)}] {f} kHz: {ns} stations, {dt:.0f}s", file=sys.stderr)

    out = config.out_dir()
    prev = current_build()
    partial = len(freqs) < len(config.CHANNELS) and bool(prev) and build_dir(prev).is_dir()
    db = {"stations": {}}
    if partial:
        try:
            db = pickle.loads((build_dir(prev) / "db.pickle").read_bytes())
        except (OSError, pickle.UnpicklingError, EOFError):
            pass
        carry_over(prev, build_id, set(by_freq))
    db["stations"].update(by_freq)
    db.update(build=build_id, ssn=ssn)
    write_atomic(build_dir(build_id) / "db.pickle", pickle.dumps(db))
    old = {}
    try:
        old = json.loads((out / "index.json").read_text())
    except (OSError, ValueError):
        pass
    channels = {str(f): len(s) for f, s in by_freq.items()}
    if partial:
        channels = {**old.get("channels", {}), **channels}
    calls = sorted({(st["call"], f, f"{st['city']} {st['state']}".strip()) for f, s in by_freq.items() for st in s})
    old_calls = [c for c in old.get("calls", []) if partial and str(c[1]) not in {str(f) for f in by_freq}]
    write_atomic(out / "index.json", json.dumps({
        "build": build_id, "generated": datetime.now(timezone.utc).isoformat(), "ssn": ssn, "ssn_src": ssn_src,
        "grid": grid, "hours": ctx["hours"], "noise": NOISE_DBU, "max_path_km": MAX_PATH_KM,
        "scope": region["label"], "channels": channels,
        "calls": sorted(old_calls + [list(c) for c in calls]),
    }, separators=(",", ":")))
    prune({build_id, prev} - {None})
    for legacy in (out / "ch", out / "db.pickle"):
        shutil.rmtree(legacy, ignore_errors=True) if legacy.is_dir() else legacy.unlink(missing_ok=True)
    print(f"Done in {time.time() - t0:.0f}s. {checks[1]}/{checks[0]} directional patterns match FCC RMS "
          f"within 2%.", file=sys.stderr)


def relabel(use_ised=True, use_crt=True):
    """Refresh station names and states in the current build without recomputing its grid.

    Labels don't affect the grid, but frames refer to stations by index, so a channel is only touched
    when the database still yields the same stations in the same order.
    """
    build_id = current_build()
    if not build_id:
        sys.exit("No build yet.")
    out = config.out_dir()
    root = build_dir(build_id)
    by_freq, _ = discover(None, None, list(config.CHANNELS), False, set(), use_ised, REGIONS["na"]["stations"],
                          use_crt)
    db = pickle.loads((root / "db.pickle").read_bytes())
    changed = skipped = 0
    for freq, new in by_freq.items():
        path = root / "ch" / str(freq) / "stations.json"
        if not path.exists():
            continue
        ch = json.loads(path.read_text())
        old = ch["stations"]
        if [(m["call"], m["lat"], m["lon"]) for m in old] != [(st["call"], round(st["lat"], 4), round(st["lon"], 4))
                                                             for st in new]:
            skipped += 1
            continue
        diff = 0
        for m, st in zip(old, new):
            if (m["city"], m["state"]) != (st["city"], st["state"]):
                m["city"], m["state"] = st["city"], st["state"]
                diff += 1
        for st_old, st in zip(db["stations"].get(freq, []), new):
            st_old["city"], st_old["state"] = st["city"], st["state"]
        if diff:
            write_atomic(path, json.dumps(ch, separators=(",", ":")))
            changed += 1
    idx = json.loads((out / "index.json").read_text())
    idx["calls"] = sorted({(st["call"], f, f"{st['city']} {st['state']}".strip())
                           for f, sts in db["stations"].items() for st in sts})
    write_atomic(root / "db.pickle", pickle.dumps(db))
    write_atomic(out / "index.json", json.dumps(idx, separators=(",", ":")))
    print(f"Relabeled {changed} channels in build {build_id}; {skipped} skipped because their station list "
          f"changed (the next build picks those up).", file=sys.stderr)
