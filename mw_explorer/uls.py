"""Cached FCC ULS import for fixed travelers' information transmitters.

Record layouts: https://wireless.fcc.gov/wtbfiles/pa_ddef51.pdf
The archive is streamed to disk; only AM-band records survive the joins.
"""
import json
import math
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .antenna import TISAntenna

ULS_URL = 'https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip'
CACHE_DAYS = 7
CACHE_VERSION = 1


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def positive(value):
    n = number(value)
    return n if n is not None and n > 0 else None


def date_key(value):
    try:
        return datetime.strptime(value, '%m/%d/%Y').date().isoformat()
    except ValueError:
        return None


def current(row, today):
    return (row['expires'] >= today and (not row.get('granted') or row['granted'] <= today))


def records(archive, name, ids=None):
    with archive.open(name + '.dat') as source:
        for line in source:
            # Filter before decoding unrelated records in the large land-mobile tables.
            if ids is not None and (line.split(b'|', 2) + [b''])[1] not in ids:
                continue
            row = line.decode('latin-1').rstrip('\r\n').split('|')
            yield row + [''] * max(0, 60 - len(row))


def coordinate(parts, limit, hemispheres):
    deg, minute, second = (number(x) for x in parts[:3])
    if (None in (deg, minute, second) or parts[3] not in set(hemispheres)
            or not 0 <= deg <= limit or not 0 <= minute < 60 or not 0 <= second < 60):
        return None
    result = deg + minute / 60 + second / 3600
    if result > limit:
        return None
    return -result if parts[3] in ('S', 'W') else result


def parse_uls(source):
    """Return one row per license/location/channel; retain alternative antenna authorizations."""
    with zipfile.ZipFile(source) as archive:
        frequencies = []
        for r in records(archive, 'FR'):
            mhz = number(r[10])
            if mhz is None or not 0.53 <= mhz <= 1.7:
                continue
            khz = round(mhz * 1000)
            if (khz not in config.CHANNELS or abs(mhz * 1000 - khz) > 0.001
                    or not r[6].isdigit() or not r[7].isdigit() or r[8] != 'FB' or r[5] == 'D' or r[27] not in ('', 'A')):
                continue
            power, erp = positive(r[15]), positive(r[16])
            if power is not None or erp is not None:
                frequencies.append((r, khz, power, erp))
        ids = {r[1].encode() for r, *_ in frequencies}
        headers = {}
        for r in records(archive, 'HD', ids):
            if (r[1].isdigit() and r[5] == 'A' and r[6] == 'PW' and not r[9] and date_key(r[8])
                    and re.fullmatch(r'[A-Z][A-Z0-9]{2,9}', r[4])):
                headers[r[1]] = {'call': r[4], 'expires': date_key(r[8]), 'granted': date_key(r[7])}
        ids = {uid.encode() for uid in headers}
        locations = {}
        for r in records(archive, 'LO', ids):
            if not r[8].isdigit() or r[6] != 'F' or r[5] == 'D' or r[48] not in ('', 'A'):
                continue
            lat, lon = coordinate(r[19:23], 90, 'NS'), coordinate(r[23:27], 180, 'EW')
            if lat is None or lon is None or (lat == 0 and lon == 0):
                continue
            locations[(r[1], r[8])] = {
                'lat': lat, 'lon': lon, 'city': r[12].title(), 'state': r[14],
                'structure_height_m': positive(r[39]), 'location': r[8], 'location_name': r[42],
            }
        antennas = {}
        for r in records(archive, 'AN', ids):
            if r[5] == 'D' or r[34] not in ('', 'A') or r[9] not in ('', 'T'):
                continue
            antennas[(r[1], r[7], r[6])] = {
                'number': r[6], 'height_tip_m': positive(r[10]), 'height_center_m': positive(r[11]),
                'make': r[12], 'model': r[13], 'polarization': r[15],
                'beamwidth_deg': positive(r[16]), 'gain_db': number(r[17]),
                'azimuth_deg': number(r[18]), 'line_loss_db': number(r[33]),
            }
        emissions = defaultdict(set)
        for r in records(archive, 'EM', ids):
            if r[8] != 'D' and r[13] in ('', 'A'):
                emissions[(r[1], r[5], r[6], number(r[7]))].add(r[9])
        names = {}
        for r in records(archive, 'EN', ids):
            if r[5] == 'L':
                names[r[1]] = r[7]
        sites = {}
        for r, freq, power, erp in frequencies:
            uid, loc, antno = r[1], r[6], r[7]
            header, location = headers.get(uid), locations.get((uid, loc))
            if not header or not location:
                continue
            modes = emissions.get((uid, loc, antno, number(r[10])), set())
            if modes and not any(mode.endswith('A3E') for mode in modes):
                continue
            key = (uid, loc, freq)
            if key not in sites:
                sites[key] = dict(header, **location, uid=uid, freq=freq, licensee=names.get(uid, ''), antennas=[])
            antenna = dict(antennas.get((uid, loc, antno), {}), number=antno,
                           power_w=power, erp_w=erp)
            if antenna not in sites[key]['antennas']:
                sites[key]['antennas'].append(antenna)
    return sorted(sites.values(), key=lambda s: (s['freq'], s['call'], int(s['location']), s['uid']))


def load_uls(refresh=False):
    """Cache the compact parsed data for a week; retain the last good cache after a failed refresh."""
    path = config.DATA_DIR / 'cache' / 'uls_tis.json'
    cached = None
    try:
        value = json.loads(path.read_text())
        if (value['version'] == CACHE_VERSION and isinstance(value['rows'], list)
                and value['rows'] and number(value['fetched']) is not None):
            cached = value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if refresh or cached is None or time.time() - cached['fetched'] >= CACHE_DAYS * 86400:
        print('Fetching FCC ULS travelers’ information licenses (about 424 MB)...', file=sys.stderr)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # TemporaryFile cleans up even after an interrupted download or parsing failure.
            with tempfile.TemporaryFile(dir=path.parent) as archive:
                request = urllib.request.Request(ULS_URL, headers={'User-Agent': 'mw-explorer/1.0', 'Accept': '*/*'})
                with urllib.request.urlopen(request, timeout=120) as response:
                    modified = response.headers.get('Last-Modified')
                    shutil.copyfileobj(response, archive, length=1024 * 1024)
                archive.seek(0)
                rows = parse_uls(archive)
            if not rows:
                raise ValueError('ULS archive contained no usable fixed TIS licenses')
            value = {'version': CACHE_VERSION, 'fetched': time.time(), 'source': ULS_URL,
                     'source_modified': modified, 'rows': rows}
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as tmp:
                temp_path = Path(tmp.name)
                try:
                    json.dump(value, tmp, separators=(',', ':'), allow_nan=False)
                    tmp.close()
                    temp_path.replace(path)
                finally:
                    temp_path.unlink(missing_ok=True)
            cached = value
        except (OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile) as error:
            if cached is None:
                raise RuntimeError(f'ULS import failed: {error}. Retry or use --no-uls.') from error
            print(f'ULS refresh failed ({error}); using the cached licenses.', file=sys.stderr)
    today = datetime.now(timezone.utc).date().isoformat()
    return [r for r in cached['rows'] if current(r, today)]


def tis_stations(rows, exclude=()):
    """Bypass broadcast callsign/site merging: a TIS license can cover many nearby transmitters."""
    result = []
    for row in rows:
        identity = f"{row['call']}~{row['uid']}~{row['location']}"
        if row['call'] in exclude or identity in exclude:
            continue
        ant = TISAntenna(row['antennas'])
        site = (row['lat'], row['lon'])
        note = ' '.join(ant.notes)
        result.append({'id': identity, 'call': row['call'], 'city': row['city'], 'state': row['state'],
                       'country': 'US', 'lat': site[0], 'lon': site[1], 'site': {'D': site, 'N': site},
                       'ant': {'D': ant, 'N': TISAntenna(row['antennas'])}, 'note': note, 'service': 'TIS',
                       'uls': {k: row[k] for k in ('uid', 'location', 'location_name', 'licensee',
                                                  'expires', 'structure_height_m', 'antennas')}})
    return result
