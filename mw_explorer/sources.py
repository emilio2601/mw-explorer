"""Fetching and parsing the FCC AM Query, ISED broadcast database and Mexico's CRT license registry."""
import html
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from . import config

FCC_BAND_URL = "https://transition.fcc.gov/fcc-bin/amq?freq=530&fre2=1700&list=4"
FCC_FAC_URL = "https://transition.fcc.gov/fcc-bin/amq?list=0&facid={}"
FCC_CACHE_DAYS = 30
ISED_URL = "https://www.ic.gc.ca/engineering/BC_DBF_FILES/baserad.zip"
CRT_PAGE = "https://rpc.crt.gob.mx/vrpc/visor/downloads"
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


def http_get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("latin-1")


def dms(deg, mins, secs, hemi):
    v = float(deg) + float(mins) / 60 + float(secs) / 3600
    return -v if hemi.strip() in ("S", "W") else v


def parse_fcc_list(text):
    rows = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split("|")]
        if len(f) < 28 or f[5] not in ("DAY", "NIG", "UNL"):
            continue
        try:
            rows.append({"call": f[1], "freq": int(float(f[2].split()[0])), "hours": f[5], "status": f[9],
                         "city": f[10].title(), "state": f[11], "country": f[12], "file": f[13],
                         "kw": float(f[14].split()[0]), "da": f[15] == "Directional", "facid": f[18],
                         "lat": dms(f[20], f[21], f[22], f[19]), "lon": dms(f[24], f[25], f[26], f[23])})
        except (ValueError, IndexError):
            continue
    return rows


TOWER_ROW = re.compile(
    r"(\d+) (\d+\.\d+) (-?\d+\.\d+) (\d+\.\d+) (\d+\.\d+) (\d+\.\d+) (\d)(#?) ?([\x00-\x03])? ?"
    r"(\d+\.\d+) (\d+\.\d+) (\d+\.\d+) (\d+\.\d+)")
HOURS = {"Daytime": "D", "Nighttime": "N", "Unlimited": "U", "Critical": "C"}


def parse_fcc_facility(page):
    t = re.sub(r"(?is)<(script|style).*?</\1>", "", page)
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))
    starts = [m.start() for m in re.finditer(r"Power: [\d.]+ kilowatts", t)]
    records, prev = [], 0
    for k, s in enumerate(starts):
        head, body = t[prev:s], t[s:starts[k + 1] if k + 1 < len(starts) else len(t)]
        prev = s
        m = re.match(r"Power: ([\d.]+) kilowatts \(kW\) \((\w+)[^)]*\) (\S+)", body)
        hdr = list(re.finditer(r" AM (Daytime|Nighttime|Unlimited|Critical Hours)( -- Auxiliary)?", head))
        files = re.findall(r"File No: (\S+)", head)
        i = body.find("Tower information:")
        towers = []
        end = re.search(r"(\d+) towers? (in the directional array|LMS)", body[i:]) if i >= 0 else None
        if end:
            for r in TOWER_ROW.finditer(body[i:i + end.start()]):
                g = r.groups()
                towers.append({"F": float(g[1]), "psi": float(g[2]), "S": float(g[3]),
                               "orient": float(g[4]), "G": float(g[5]), "ref": int(g[6]),
                               "A": float(g[9]), "B": float(g[10])})
            towers = towers[:int(end.group(1))]

        def rms(label):
            mm = re.search(label + r": ([\d.]+) mV/m", body)
            return float(mm.group(1)) if mm else None
        records.append({"hours": HOURS.get(m.group(2), "?"), "kw": float(m.group(1)),
                        "directional": m.group(3) == "Directional",
                        "aux": bool(hdr and hdr[-1].group(2)), "file": files[-1] if files else None,
                        "rms_std": rms("RMS Standard"), "rms_theo": rms("RMS Theoretical"), "towers": towers})
    return records


def read_dbf(data):
    n = int.from_bytes(data[4:8], "little")
    hlen, rlen = int.from_bytes(data[8:10], "little"), int.from_bytes(data[10:12], "little")
    fields, off = [], 32
    while data[off] != 0x0D:
        fields.append((data[off:off + 11].split(b"\0")[0].decode(), data[off + 16]))
        off += 32
    rows = []
    for i in range(n):
        rec, pos, row = data[hlen + i * rlen: hlen + (i + 1) * rlen], 1, {}
        for name, ln in fields:
            row[name] = rec[pos:pos + ln].decode("latin-1").strip()
            pos += ln
        rows.append(row)
    return rows


def ised_dms(v, width):
    v = v.zfill(width)
    return float(v[:-4]) + float(v[-4:-2]) / 60 + float(v[-2:]) / 3600


def fetch_ised():
    """Canadian AM records from ISED's broadcast database: call, freq, location, day/night status."""
    print("Fetching ISED broadcast database (15 MB)...", file=sys.stderr)
    with urllib.request.urlopen(ISED_URL, timeout=120) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    out = []
    for row in read_dbf(z.read("amstatio.dbf")):
        if row["PROVINCE"] not in CA_PROVINCES or not row["LATITUDE"].strip("0"):
            continue
        out.append({"call": row["CALL_SIGN"], "freq": float(row["FREQUENCY"] or 0),
                    "lat": ised_dms(row["LATITUDE"], 6), "lon": -ised_dms(row["LONGITUDE"], 7),
                    "banner": row["BANNER"], "day": row["STATUS1"], "night": row["STATUS2"],
                    "pday": float(row["POWERDAY"] or 0) / 1000, "pnight": float(row["POWERNIGHT"] or 0) / 1000,
                    "city": row["CITY"].title(), "province": row["PROVINCE"]})
    return out


def read_xlsx_rows(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared = ([''.join(t.text or "" for t in si.iter(ns + "t"))
               for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(ns + "si")]
              if "xl/sharedStrings.xml" in z.namelist() else [])
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(ns + "row"):
        out = {}
        for c in row.findall(ns + "c"):
            v = c.find(ns + "v")
            val = v.text if v is not None else ""
            if c.get("t") == "s":
                val = shared[int(val)]
            out[re.match(r"[A-Z]+", c.get("r")).group()] = val
        yield out


def fetch_crt():
    """Licensed Mexican AM stations (call, kHz, city, use) from the CRT/IFT public concession registry."""
    page = http_get(CRT_PAGE)
    m = re.search(r'href="([^"]*infraestructura_AM_FM[^"]*\.xlsx)"', page)
    if not m:
        raise RuntimeError("AM/FM infrastructure file not linked from the registry page")
    url = urllib.parse.urljoin(CRT_PAGE, m.group(1))
    print(f"Fetching Mexican AM license list ({url.rsplit('/', 1)[-1]})...", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    out = []
    for row in read_xlsx_rows(data):
        if row.get("I") != "AM":
            continue
        try:
            out.append({"call": row.get("H", "").strip().upper(), "freq": round(float(row["J"])),
                        "city": row.get("C", "").title(), "use": row.get("F", "")})
        except (KeyError, ValueError):
            continue
    return {"file": url.rsplit("/", 1)[-1], "rows": out}


def load_cache():
    try:
        cache = json.loads(config.cache_file().read_text())
    except (OSError, ValueError):
        cache = {}
    cache.setdefault("facilities", {})
    return cache


def fresh(entry, refresh):
    return bool(entry) and not refresh and time.time() - entry["fetched"] < FCC_CACHE_DAYS * 86400


def save_cache(cache):
    path = config.cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(path)
