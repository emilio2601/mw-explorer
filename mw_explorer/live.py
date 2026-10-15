"""Live inputs: planetary Kp, smoothed sunspot number, and WSPR 630m/160m path conditions."""
import json
import statistics
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .geo import gc_km, gc_points, midpoint, solar, sun_elevation
from .sources import http_get

BANDS = {0: "630m", 1: "160m"}
WSPR_URL = "https://db1.wspr.live/"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
SSN_URL = "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"


def fetch_spots(days, mid_lat, mid_lon, dist_km):
    """Hourly median power-normalized SNR per tx/rx pair on paths near this midpoint and length.

    Coarse SQL prefilter (arithmetic-mean midpoint box); exact geometry is filtered in Python. Pairs report
    every couple of minutes, so summarizing per hour in the database cuts the rows ~30x (raw spots ran to
    300k+ rows and took seconds to parse) while moving corrections by well under 1 dB.
    """
    q = f"""
        SELECT toUnixTimestamp(toStartOfHour(time)) + 1800, band, tx_sign, rx_sign,
               any(tx_lat), any(tx_lon), any(rx_lat), any(rx_lon), any(distance), 0, median(snr - power)
        FROM wspr.rx
        WHERE time > now() - INTERVAL {int(days)} DAY
          AND band IN ({",".join(str(b) for b in BANDS)})
          AND distance BETWEEN {int(dist_km * 0.55)} AND {int(dist_km * 1.45)}
          AND (rx_lat + tx_lat) / 2 BETWEEN {mid_lat - 9} AND {mid_lat + 9}
          AND (rx_lon + tx_lon) / 2 BETWEEN {mid_lon - 12} AND {mid_lon + 12}
        GROUP BY 1, band, tx_sign, rx_sign
        FORMAT JSONCompact
    """
    data = json.loads(http_get(WSPR_URL + "?" + urllib.parse.urlencode({"query": q})))
    cols = ("t", "band", "tx", "rx", "tx_lat", "tx_lon", "rx_lat", "rx_lon", "dist", "pwr", "snr")
    return [dict(zip(cols, row)) for row in data["data"]]


def fetch_kp():
    try:
        last = json.loads(http_get(KP_URL, timeout=20))[-1]
        if isinstance(last, dict):
            return float(last.get("Kp", last.get("kp_index"))), last.get("time_tag")
        return float(last[1]), last[0]
    except Exception as e:  # Kp is informational only
        return None, str(e)


def fetch_ssn(default=100.0):
    try:
        rows = json.loads(http_get(SSN_URL, timeout=20))
        smoothed = [r for r in rows if r.get("smoothed_ssn", -1) >= 0]
        if smoothed:
            return float(smoothed[-1]["smoothed_ssn"]), smoothed[-1]["time-tag"] + " smoothed"
        return float(rows[-1]["ssn"]), rows[-1]["time-tag"] + " monthly"
    except Exception as e:
        return default, f"fallback ({e})"


def wspr_proxy(call, mid, path_km, now, hours=3, baseline_days=14, radius=700):
    """Median SNR change (dB) on WSPR paths resembling this one, now vs. each pair's own nighttime baseline."""
    cutoff = (now - timedelta(hours=hours)).timestamp()
    spots = fetch_spots(baseline_days, mid[0], mid[1], path_km)
    by_pair = defaultdict(lambda: {"base": [], "now": []})
    for s in spots:
        smla, smlo = midpoint(s["tx_lat"], s["tx_lon"], s["rx_lat"], s["rx_lon"])
        if gc_km(smla, smlo, *mid) > radius:
            continue
        # Both windows only count spots with a dark midpoint, so day spots never get compared to a night baseline.
        if sun_elevation(smla, smlo, solar(datetime.fromtimestamp(s["t"], timezone.utc))) > -6:
            continue
        d = by_pair[(s["band"], s["tx"], s["rx"])]
        d["now" if s["t"] >= cutoff else "base"].append(s["snr"] - s["pwr"] + 37)
        d["dist"] = s["dist"]
        d["coords"] = (s["tx_lat"], s["tx_lon"], s["rx_lat"], s["rx_lon"])

    pairs, deltas = [], []
    for (band, tx, rx), d in by_pair.items():
        if not d["now"]:
            continue
        nmed = statistics.median(d["now"])
        delta = None
        if len(d["base"]) >= 5:
            delta = nmed - statistics.median(d["base"])
            deltas.append(delta)
        pairs.append({"station": call, "band": BANDS[band], "tx": tx, "rx": rx, "dist": d["dist"],
                      "snr": round(nmed), "delta": None if delta is None else round(delta, 1),
                      "line": gc_points(*d["coords"], n=16)})
    return {"delta": statistics.median(deltas) if deltas else None, "pairs": pairs}
