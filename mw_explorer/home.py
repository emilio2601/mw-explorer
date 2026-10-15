"""Signal levels from every station on a channel at one receiver location, hour by hour."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .geo import bearing, gc_km, solar
from .live import wspr_proxy
from .model import MAX_PATH_KM, field_at, path_terms, station_mode

HOUR_FMT = "%Y%m%d%H"
WSPR_MIN_KM = 400


def _short(v):
    return None if v is None else round(v)


def levels_at(stations, freq, lat, lon, ssn, hours, deltas=None):
    """Per station within range: distance, bearing from the receiver, and sky/ground/total dBuV/m per hour.

    deltas ({station index: dB}) shifts skywave in the first hour only (the live WSPR correction).
    """
    deltas = deltas or {}
    sols = [solar(datetime.strptime(h, HOUR_FMT).replace(tzinfo=timezone.utc)) for h in hours]
    out = []
    for k, st in enumerate(stations):
        if gc_km(st["lat"], st["lon"], lat, lon) > MAX_PATH_KM:
            continue
        terms = path_terms(st, lat, lon, ssn, freq)
        row = {"i": k, "km": round(terms["d"]), "brg": round(bearing(lat, lon, st["lat"], st["lon"]), 1),
               "t": [], "sky": [], "gw": [], "m": ""}
        for fi, sol in enumerate(sols):
            mode = station_mode(st, sol)
            tot, sky, gw = field_at(terms, mode, sol, (deltas.get(k) or 0.0) if fi == 0 else 0.0)
            row["t"].append(_short(tot))
            row["sky"].append(_short(sky))
            row["gw"].append(_short(gw))
            row["m"] += mode
        if any(v is not None for v in row["t"]):
            out.append(row)
    return out


def wspr_deltas(stations, freq, lat, lon, ssn, indices, now=None):
    """Live WSPR skywave correction for the given station indices: {index: {"delta": dB or None, "pairs": n}}."""
    now = now or datetime.now(timezone.utc)
    paths = {}
    for k in indices:
        terms = path_terms(stations[k], lat, lon, ssn, freq)
        if terms["d"] >= WSPR_MIN_KM:
            paths[k] = terms

    def one(k):
        px = wspr_proxy(stations[k]["call"], paths[k]["mid"], paths[k]["d"], now)
        return k, {"delta": None if px["delta"] is None else round(px["delta"], 1),
                   "pairs": sum(1 for p in px["pairs"] if p["delta"] is not None)}
    # One wspr.live query per path; run them together so the answer takes about as long as the slowest.
    with ThreadPoolExecutor(max_workers=max(1, len(paths))) as pool:
        return dict(pool.map(one, paths))
