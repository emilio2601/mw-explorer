"""Assembling one station list per channel: license checks, de-duplication, day/night sites, overrides."""
import json
import math
import re
import sys
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import config
from .antenna import Antenna
from .geo import gc_km
from .sources import (FCC_BAND_URL, FCC_CACHE_DAYS, FCC_FAC_URL, fetch_crt, fetch_ised, fresh, http_get,
                      load_cache, parse_fcc_facility, parse_fcc_list, save_cache)

DETAIL_MIN_KW = 10
SAME_STATION_KM = 30
# ISO 3166-2:MX codes, keyed by state name as the CRT license list writes it (accents and spaces stripped).
MX_STATES = {
    "AGUASCALIENTES": "AGU", "BAJACALIFORNIA": "BCN", "BAJACALIFORNIASUR": "BCS", "CAMPECHE": "CAM",
    "CHIAPAS": "CHP", "CHIHUAHUA": "CHH", "CIUDADDEMEXICO": "CMX", "COAHUILADEZARAGOZA": "COA", "COLIMA": "COL",
    "DURANGO": "DUR", "GUANAJUATO": "GUA", "GUERRERO": "GRO", "HIDALGO": "HID", "JALISCO": "JAL",
    "MEXICO": "MEX", "MICHOACANDEOCAMPO": "MIC", "MORELOS": "MOR", "NAYARIT": "NAY", "NUEVOLEON": "NLE",
    "OAXACA": "OAX", "PUEBLA": "PUE", "QUERETARO": "QUE", "QUINTANAROO": "ROO", "SANLUISPOTOSI": "SLP",
    "SINALOA": "SIN", "SONORA": "SON", "TABASCO": "TAB", "TAMAULIPAS": "TAM", "TLAXCALA": "TLA",
    "VERACRUZDEIGNACIODELALLAVE": "VER", "YUCATAN": "YUC", "ZACATECAS": "ZAC",
}
# Fallback for Mexican stations without a CRT match. The FCC's own codes are inconsistent (several per
# state, some shared), so CH and BC/BN are split by latitude: Chiapas vs Chihuahua, and Baja California
# vs Baja California Sur at 28 N.
FCC_MX_STATES = {
    "AG": "AGU", "BS": "BCS", "CA": "CAM", "CM": "CAM", "CP": "CAM", "CS": "CHP", "CI": "CHH", "CO": "COA",
    "CL": "COL", "DF": "CMX", "DU": "DUR", "DG": "DUR", "GT": "GUA", "GJ": "GUA", "GR": "GRO", "GE": "GRO",
    "HG": "HID", "HD": "HID", "JA": "JAL", "ME": "MEX", "MX": "MEX", "MC": "MIC", "MI": "MIC", "ML": "MOR",
    "MO": "MOR", "NA": "NAY", "NL": "NLE", "OA": "OAX", "PU": "PUE", "QE": "QUE", "QR": "ROO", "QI": "ROO",
    "SL": "SLP", "SI": "SIN", "SO": "SON", "TB": "TAB", "TA": "TAM", "TL": "TLA", "VC": "VER", "VE": "VER",
    "YC": "YUC", "YU": "YUC", "VA": "YUC", "ZA": "ZAC",
}


# How Mexican state names are usually abbreviated in everyday writing, which is what the explorer shows.
MX_ABBR = {
    "AGU": "Ags.", "BCN": "BC", "BCS": "BCS", "CAM": "Camp.", "CHP": "Chis.", "CHH": "Chih.", "CMX": "CDMX",
    "COA": "Coah.", "COL": "Col.", "DUR": "Dgo.", "GUA": "Gto.", "GRO": "Gro.", "HID": "Hgo.", "JAL": "Jal.",
    "MEX": "Edomex", "MIC": "Mich.", "MOR": "Mor.", "NAY": "Nay.", "NLE": "NL", "OAX": "Oax.", "PUE": "Pue.",
    "QUE": "Qro.", "ROO": "Q. Roo", "SLP": "SLP", "SIN": "Sin.", "SON": "Son.", "TAB": "Tab.", "TAM": "Tamps.",
    "TLA": "Tlax.", "VER": "Ver.", "YUC": "Yuc.", "ZAC": "Zac.",
}


def mx_state_iso(fcc_code, lat, crt_name=None):
    if crt_name and norm_place(crt_name) in MX_STATES:
        return MX_STATES[norm_place(crt_name)]
    if fcc_code == "CH":
        return "CHP" if lat < 21 else "CHH"
    if fcc_code in ("BC", "BN"):
        return "BCN" if lat >= 28 else "BCS"
    return FCC_MX_STATES.get(fcc_code, "")


def mx_state(fcc_code, lat, crt_name=None):
    iso = mx_state_iso(fcc_code, lat, crt_name)
    return MX_ABBR.get(iso, iso)


REGIONS = {
    "na": {"grid": (14, 70, -168, -52), "stations": (5, 75, -180, -45), "label": "in North America & the Caribbean"},
}


def ised_operating(r, ised_by_freq):
    """True if ISED shows an operating Canadian station on this channel at this site for r's hours."""
    slots = {"DAY": ("day",), "NIG": ("night",), "UNL": ("day", "night")}[r["hours"]]
    return any(x["banner"] == "O" and any(x[s] == "OP" for s in slots)
               and gc_km(x["lat"], x["lon"], r["lat"], r["lon"]) < SAME_STATION_KM
               for x in ised_by_freq.get(r["freq"], ()))


def norm_place(text):
    return re.sub(r"[^A-Z]", "", unicodedata.normalize("NFKD", (text or "").upper()).encode("ascii", "ignore").decode())


def same_place(a, b):
    a, b = norm_place(a), norm_place(b)
    return bool(a and b) and (a[:6] in b or b[:6] in a)


def move_to_licensed_freq(rows, crt_rows):
    """Move Mexican FCC entries listed on a stale frequency to the one CRT licenses, in place.

    Only when it's unambiguous: the call is licensed on one frequency, the FCC has nothing under that call
    there, and the FCC lists the call on just one other frequency. Otherwise the stray entries are other
    stations wearing the label (several "XEHS" on unrelated channels) and are left to fail the license check.
    """
    licensed = {(x["call"], x["freq"]) for x in crt_rows}
    lic_freqs, fcc_freqs = defaultdict(set), defaultdict(set)
    for x in crt_rows:
        lic_freqs[x["call"]].add(x["freq"])
    for r in rows:
        if r["country"] == "MX":
            fcc_freqs[base_call(r)].add(r["freq"])
    for r in rows:
        call = base_call(r)
        lic = lic_freqs.get(call, set())
        if (r["country"] == "MX" and (call, r["freq"]) not in licensed and len(lic) == 1
                and not lic & fcc_freqs[call] and len(fcc_freqs[call]) == 1):
            new = next(iter(lic))
            r["note"] = f"FCC lists it on {r['freq']} kHz; the CRT license is for {new}."
            r["freq"] = new


def discover(home, max_km, freqs, refresh, exclude=(), use_ised=True, box=None, use_crt=True):
    """Stations per channel: {freq: [station, ...]} plus {freq: {"CALL (CC)", ...} dropped as unlicensed}."""
    cache = load_cache()
    if not fresh(cache.get("band"), refresh):
        print("Fetching FCC station list for 530-1700 kHz...", file=sys.stderr)
        cache["band"] = {"fetched": time.time(), "rows": parse_fcc_list(http_get(FCC_BAND_URL, timeout=180))}
    wanted = set(freqs)

    def in_area(lat, lon):
        return (box[0] <= lat <= box[1] and box[2] <= lon <= box[3] if box
                else gc_km(home[0], home[1], lat, lon) <= max_km)

    pool = []
    for r in cache["band"]["rows"]:
        if (r["status"] == "LIC" if r["country"] == "US" else r["status"] == "CP") and in_area(r["lat"], r["lon"]):
            pool.append(dict(r, country="MX") if r["country"] == "ME" else dict(r))
    dropped = defaultdict(set)

    crt = None
    if use_crt and any(r["country"] == "MX" for r in pool):
        if not fresh(cache.get("crt"), refresh) or "state" not in (cache["crt"]["rows"] or [{}])[0]:
            try:
                cache["crt"] = dict(fetch_crt(), fetched=time.time())
            except Exception as e:
                print(f"CRT fetch failed ({e}); " + ("using the cached license list." if cache.get("crt")
                      else "keeping all Mexican notifications."), file=sys.stderr)
        crt = cache.get("crt")
    if crt:
        licensed = {(x["call"], x["freq"]) for x in crt["rows"]}
        move_to_licensed_freq(pool, crt["rows"])

    cands = [r for r in pool if r["freq"] in wanted]

    if use_ised and any(r["country"] == "CA" for r in cands):
        if not fresh(cache.get("ised"), refresh) or "pday" not in (cache["ised"]["rows"] or [{}])[0]:
            try:
                cache["ised"] = {"fetched": time.time(), "rows": fetch_ised()}
            except Exception as e:
                print(f"ISED fetch failed ({e}); " + ("using the cached station data." if cache.get("ised")
                      else "keeping all Canadian notifications."), file=sys.stderr)
        if cache.get("ised"):
            ised_by_freq = defaultdict(list)
            for x in cache["ised"]["rows"]:
                ised_by_freq[round(x["freq"])].append(x)
            keep = []
            for r in cands:
                if r["country"] == "CA" and not ised_operating(r, ised_by_freq):
                    dropped[r["freq"]].add(f"{r['call']} (CA)")
                else:
                    keep.append(r)
            cands = keep
            # Operating Canadian stations the FCC never received (mostly low-power CBC relays).
            for f in wanted:
                for x in ised_by_freq.get(f, ()):
                    if x["banner"] != "O" or not in_area(x["lat"], x["lon"]) or any(
                            r["country"] == "CA" and r["freq"] == f
                            and gc_km(r["lat"], r["lon"], x["lat"], x["lon"]) < SAME_STATION_KM for r in cands):
                        continue
                    for hours, status, kw in (("DAY", x["day"], x["pday"]), ("NIG", x["night"], x["pnight"])):
                        if status == "OP" and kw > 0:
                            cands.append({"call": x["call"], "freq": f, "hours": hours, "status": "CP",
                                          "city": x["city"], "state": x["province"], "country": "CA",
                                          "file": "ISED", "kw": kw, "da": False, "facid": None,
                                          "lat": x["lat"], "lon": x["lon"],
                                          "note": "From ISED only (not in FCC data); antenna modeled as typical."})

    if crt:
        keep_calls = {k for k, v in load_overrides().get("stations", {}).items() if v.get("keep")}
        keep = []
        for r in cands:
            call = base_call(r)
            if (r["country"] != "MX" or (call, r["freq"]) in licensed
                    or call in keep_calls or f"{call}@{r['freq']}" in keep_calls):
                keep.append(r)
            else:
                dropped[r["freq"]].add(f"{call or '?'} (MX)")
        cands = keep

    # Foreign entries with no call sign are channel-plan allotments (mostly the 1981 Region 2 plan), not stations.
    for r in cands:
        if r["country"] != "US" and r["call"] in ("-", "NEW", ""):
            dropped[r["freq"]].add(f"{r['city']}? ({r['country']})")
    cands = [r for r in cands if r["country"] == "US" or r["call"] not in ("-", "NEW", "")]
    # The FCC marks a deleted call with a leading "D" (DKSTA), even where the record still says licensed.
    for r in cands:
        if r["country"] == "US" and re.match(r"^D[KW][A-Z]{2,3}$", r["call"]):
            dropped[r["freq"]].add(f"{r['call']} (US, deleted)")
    cands = [r for r in cands if not (r["country"] == "US" and re.match(r"^D[KW][A-Z]{2,3}$", r["call"]))]

    need = sorted({r["facid"] for r in cands if r["facid"] and (r["da"] or r["kw"] >= DETAIL_MIN_KW)
                   and not fresh(cache["facilities"].get(r["facid"]), refresh)})
    if need:
        print(f"Fetching FCC antenna data for {len(need)} facilities "
              f"(one-time, cached {FCC_CACHE_DAYS} days)...", file=sys.stderr)

        def get(fid):
            try:
                return fid, parse_fcc_facility(http_get(FCC_FAC_URL.format(fid)))
            except Exception as e:
                return fid, e
        with ThreadPoolExecutor(max_workers=4) as pool_:
            for n, (fid, recs) in enumerate(pool_.map(get, need), 1):
                if isinstance(recs, Exception):
                    print(f"  facility {fid}: {recs}", file=sys.stderr)
                else:
                    cache["facilities"][fid] = {"fetched": time.time(), "records": recs}
                if n % 100 == 0 or n == len(need):
                    print(f"  {n}/{len(need)}", file=sys.stderr)
                    save_cache(cache)
    save_cache(cache)

    by_freq = defaultdict(list)
    for r in cands:
        recs = cache["facilities"].get(r["facid"], {}).get("records", []) if r["facid"] else []
        h = {"DAY": "D", "NIG": "N", "UNL": "U"}[r["hours"]]
        match = [x for x in recs if x["hours"] == h and abs(x["kw"] - r["kw"]) < 1e-6
                 and (x["file"] is None or x["file"] == r["file"])]
        r = dict(r, rec=match[0] if match else None)
        # Every notified foreign record is labeled auxiliary, so the flag only means something for US licenses.
        if r["country"] == "US" and r["rec"] and r["rec"]["aux"]:
            continue
        by_freq[r["freq"]].append(r)

    ised_ops = defaultdict(list)
    for x in (cache.get("ised") or {}).get("rows", []):
        if x["banner"] == "O":
            ised_ops[round(x["freq"])].append(x)
    crt_cities, crt_states = defaultdict(list), {}
    for x in (crt or {}).get("rows", []):
        crt_cities[(x["call"], x["freq"])].append(x["city"])
        if x.get("state"):
            crt_states[(x["call"], x["freq"])] = x["state"]
    overrides = load_overrides()
    return {f: apply_overrides(build_stations(rows, exclude, f, ised_ops.get(f, []), crt_cities, crt_states),
                               f, overrides)
            for f, rows in by_freq.items()}, dropped


def load_overrides():
    try:
        return json.loads(config.OVERRIDES.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        sys.exit(f"{config.OVERRIDES.name} is not valid JSON: {e}")


def apply_overrides(stations, freq, overrides):
    out = []
    for st in stations:
        rule = (overrides.get("stations", {}).get(f"{st['call']}@{freq}")
                or overrides.get("stations", {}).get(st["call"])
                or overrides.get("countries", {}).get(st["country"]))
        if not rule:
            out.append(st)
            continue
        if rule.get("off"):
            continue
        if rule.get("site"):
            lat, lon = rule["site"]
            st["site"] = {slot: (lat, lon) for slot in st["site"]}
            st["lat"], st["lon"] = lat, lon
            st["note"] = re.sub(r"\s*Separate day and night sites[^.]*\.", "", st.get("note") or "").strip() or None
        kw = rule.get("kw")
        for slot, ant in st["ant"].items():
            adj = rule.get("adjust_db", 0)
            slot_kw = kw.get(slot) if isinstance(kw, dict) else kw
            if slot_kw:
                ant.label = ant.label.replace(f"{ant.kw:g} kW", f"{slot_kw:g} kW", 1)
                ant.adjust_db = adj + 10 * math.log10(slot_kw / ant.kw)
            else:
                ant.adjust_db = adj
            if adj:
                ant.label += f" ({adj:+.0f} dB)"
        if rule.get("note"):
            st["note"] = " ".join(filter(None, [rule["note"], st.get("note")]))
        out.append(st)
    for add in overrides.get("add", []):
        if add["freq"] != freq:
            continue
        lat, lon = add["site"]
        out.append({"call": add["call"], "city": add["city"], "state": add.get("state", ""), "country": add["country"],
                    "lat": lat, "lon": lon, "site": {slot: (lat, lon) for slot in add["kw"]},
                    "ant": {slot: Antenna(kw, None) for slot, kw in add["kw"].items()}, "note": add.get("note")})
    return out


def base_call(r):
    # Mexico files updated notifications under the call plus a digit (XEBV -> XEBV1) at the same site.
    return re.sub(r"\d+$", "", r["call"]) if r["country"] == "MX" else r["call"]


CALL_FORMS = {"US": r"^[KW][A-Z]{2,3}$", "CA": r"^(C[A-Z]{2,3}(-\d+)?|VO[A-Z]{2})$", "MX": r"^X[EH][A-Z]{1,5}$"}


def build_stations(cands, exclude, freq, ised_ops=(), crt_cities=None, crt_states=None):
    crt_cities, crt_states = crt_cities or {}, crt_states or {}

    def close(a, b, km):
        return gc_km(a["lat"], a["lon"], b["lat"], b["lon"]) < km

    groups = []
    for r in cands:
        for g in groups:
            if g[0]["country"] == r["country"] and base_call(g[0]) == base_call(r) and close(g[0], r, SAME_STATION_KM):
                g.append(r)
                break
        else:
            groups.append([r])

    # The same transmitter notified under two calls (old and new, or a placeholder like "C690").
    merged = []
    for g in groups:
        for m in merged:
            if m[0]["country"] == g[0]["country"] and close(m[0], g[0], 2):
                m.extend(g)
                break
        else:
            merged.append(g)
    groups = merged

    # Day-only and night-only entries under one call are one station with separate day and night sites.
    paired, used = [], set()
    for i, g in enumerate(groups):
        if i in used:
            continue
        hours = {r["hours"] for r in g}
        for j in range(i + 1, len(groups)):
            h = groups[j]
            if j in used or h[0]["country"] != g[0]["country"] or base_call(h[0]) != base_call(g[0]):
                continue
            other = {r["hours"] for r in h}
            if (hours == {"DAY"} and other == {"NIG"}) or (hours == {"NIG"} and other == {"DAY"}):
                g = g + h
                used.add(j)
                break
        paired.append(g)
    groups = paired

    # A Mexican call licensed in one city but notified at several sites: keep the licensed one.
    by_call = defaultdict(list)
    for g in groups:
        if g[0]["country"] == "MX":
            by_call[base_call(g[0])].append(g)
    for call, gs in by_call.items():
        cities = crt_cities.get((call, freq))
        if len(gs) < 2 or not cities:
            continue
        match = [g for g in gs if any(same_place(r["city"], c) for r in g for c in cities)] or gs
        keep = [max(match, key=lambda g: (not any(r.get("note") for r in g), max(r["kw"] for r in g)))]
        groups = [g for g in groups if g not in gs or g in keep]

    # Each operating ISED station confirms one Canadian group: an exact call match first, else the nearest.
    # Other Canadian groups near it are dead stations sharing the area (CINW next to CFNV on 940).
    if ised_ops:
        ca = [g for g in groups if g[0]["country"] == "CA"]
        claimed = {}
        pairs = sorted(((base_call(g[0]) != x["call"], gc_km(x["lat"], x["lon"], g[0]["lat"], g[0]["lon"]), gi, xi)
                        for gi, g in enumerate(ca) for xi, x in enumerate(ised_ops)
                        if gc_km(x["lat"], x["lon"], g[0]["lat"], g[0]["lon"]) < SAME_STATION_KM))
        used_x = set()
        for _, _, gi, xi in pairs:
            if gi not in claimed and xi not in used_x:
                claimed[gi] = ised_ops[xi]["call"]
                used_x.add(xi)
        for gi, g in enumerate(ca):
            if gi in claimed:
                for r in g:
                    r["ised_call"] = claimed[gi]
        groups = [g for g in groups if g[0]["country"] != "CA" or "ised_call" in g[0]]

    groups = [g for g in groups if not any(r["call"].upper() in exclude or base_call(r).upper() in exclude
                                           for r in g)]

    def newest(r):
        digits = re.sub(r"\D", "", r["file"])
        return (r["kw"], int(digits) if digits else 0)

    def call_rank(r):
        c = base_call(r)
        form = CALL_FORMS.get(r["country"])
        return (bool(form and re.match(form, c)), newest(r))

    stations = []
    for g in groups:
        lead = max(g, key=call_rank)
        st = {"call": base_call(lead), "city": lead["city"], "state": lead["state"],
              "country": lead["country"], "ant": {}, "site": {}}
        if lead.get("ised_call"):
            st["call"] = lead["ised_call"]
        if st["country"] == "MX":
            st["state"] = mx_state(lead["state"], lead["lat"], crt_states.get((st["call"], freq)))
        # Foreign stations pile up notifications over the years; each filing describes one setup, so take the
        # newest filing's day and night records together (older ones only for a mode it lacks). Picking each
        # mode separately mixed filings: XEUU got a newer filing's day site and an old filing's 0.5 kW night.
        # US licenses are all current, and separate day and night licenses are real separate sites.
        by_file = sorted({r["file"] for r in g}, key=lambda f: newest({"kw": 0, "file": f})[1], reverse=True)
        for slot, codes in (("D", ("DAY", "UNL")), ("N", ("NIG", "UNL"))):
            opts = [r for r in g if r["hours"] in codes]
            if opts and lead["country"] != "US":
                opts = next([r for r in opts if r["file"] == f] for f in by_file if any(r["file"] == f for r in opts))
            if opts:
                best = max(opts, key=newest)
                st["ant"][slot] = Antenna(best["kw"], best["rec"])
                st["site"][slot] = (best["lat"], best["lon"])
        if not st["ant"]:
            continue
        st["lat"], st["lon"] = st["site"].get("N") or st["site"]["D"]
        notes = sorted({r["note"] for r in g if r.get("note")})
        if "D" in st["site"] and "N" in st["site"]:
            sep = gc_km(*st["site"]["D"], *st["site"]["N"])
            if sep >= 2:
                notes.append(f"Separate day and night sites, {sep:.0f} km apart.")
        st["note"] = " ".join(notes) or None
        stations.append(st)
    return stations
