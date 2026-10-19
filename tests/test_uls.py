import gzip
import io
import json
import math
import pickle
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mw_explorer import config
from mw_explorer.antenna import TISAntenna
from mw_explorer.build import CELL_BYTES, CELL_FORMAT, build_dir, compute_channel
from mw_explorer.home import levels_at
from mw_explorer.stations import apply_overrides
from mw_explorer.uls import CACHE_VERSION, current, load_uls, parse_uls, tis_stations


def record(kind, values):
    row = [''] * 60
    row[0] = kind
    row[1] = '1234'
    row[4] = 'WQAB123'
    for index, value in values.items():
        row[index] = str(value)
    return row


def fixture():
    return {
        'HD': [record('HD', {5: 'A', 6: 'PW', 7: '01/01/2020', 8: '01/01/2099'})],
        'FR': [record('FR', {6: 1, 7: 1, 8: 'FB', 10: '.53000000', 15: 10, 16: 10})],
        'LO': [record('LO', {6: 'F', 8: 1, 12: 'Test City', 14: 'NY',
                             19: 40, 20: 42, 21: 36, 22: 'N', 23: 74, 24: 0, 25: 0, 26: 'W'})],
        'AN': [record('AN', {6: 1, 7: 1, 9: 'T', 10: 15, 15: 'V', 16: 360, 17: -10, 33: 0})],
        'EN': [record('EN', {5: 'L', 7: 'Test County'})],
        'EM': [record('EM', {5: 1, 6: 1, 7: '.53000000', 9: '6K00A3E'})],
    }


def archive(data):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as z:
        for kind, rows in data.items():
            z.writestr(kind + '.dat', ''.join('|'.join(row) + '\n' for row in rows))
    result.seek(0)
    return result


class ULSImportTests(unittest.TestCase):
    def test_units_coordinates_and_exact_antenna_join(self):
        data = fixture()
        wrong = data['AN'][0].copy()
        wrong[7], wrong[17] = '2', '6'
        data['AN'].insert(0, wrong)
        row = parse_uls(archive(data))[0]
        self.assertEqual(row['freq'], 530)
        self.assertAlmostEqual(row['lat'], 40.71)
        self.assertEqual(row['lon'], -74)
        self.assertEqual(row['licensee'], 'Test County')
        self.assertEqual(row['antennas'][0]['gain_db'], -10)
        self.assertEqual(row['antennas'][0]['power_w'], 10)
        station = tis_stations([row])[0]
        self.assertAlmostEqual(station['ant']['D'].kw, .01)
        self.assertAlmostEqual(station['ant']['D'].field(0, 0), math.sqrt(49.2))

    def test_rejects_non_tis_and_unusable_records(self):
        changes = [('HD', 5, 'E'), ('HD', 6, 'IG'), ('HD', 9, '01/01/2025'),
                   ('FR', 8, 'FBT'), ('FR', 8, 'MO'), ('FR', 10, '.535'),
                   ('FR', 10, '530'), ('FR', 10, 'NaN'), ('FR', 27, 'D'),
                   ('LO', 6, 'T'), ('LO', 22, ''), ('LO', 20, '60'),
                   ('LO', 19, 'NaN'), ('LO', 48, 'D'), ('EM', 9, '11K2F3E')]
        for kind, index, value in changes:
            with self.subTest(kind=kind, index=index, value=value):
                data = fixture()
                data[kind][0][index] = value
                self.assertEqual(parse_uls(archive(data)), [])

    def test_retains_sites_deduplicates_frequencies_and_keeps_alternative_antennas(self):
        data = fixture()
        data['FR'].append(data['FR'][0].copy())
        extra = data['FR'][0].copy()
        extra[7] = '2'
        data['FR'].append(extra)
        other = data['FR'][0].copy()
        other[6] = '2'
        data['FR'].append(other)
        location = data['LO'][0].copy()
        location[8], location[21] = '2', '37'  # Adjacent sites under one call must stay separate.
        data['LO'].append(location)
        rows = parse_uls(archive(data))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]['antennas']), 2)
        stations = tis_stations(rows)
        self.assertNotEqual(stations[0]['id'], stations[1]['id'])
        self.assertEqual(stations[0]['call'], stations[1]['call'])
        self.assertEqual(tis_stations(rows, {'WQAB123'}), [])
        self.assertEqual(len(tis_stations(rows, {stations[0]['id']})), 1)

    def test_power_fallback_and_no_invented_coordinates(self):
        data = fixture()
        data['FR'][0][15] = ''
        self.assertEqual(parse_uls(archive(data))[0]['antennas'][0]['erp_w'], 10)
        data['FR'][0][16] = 'NaN'
        self.assertEqual(parse_uls(archive(data)), [])
        data = fixture()
        data['AN'] = []
        self.assertEqual(len(parse_uls(archive(data))), 1)
        data['LO'] = []
        self.assertEqual(parse_uls(archive(data)), [])

    def test_day_override_preserves_night_power(self):
        stations = tis_stations(parse_uls(archive(fixture())))
        base = stations[0]['ant']['N'].field(0, 0)
        apply_overrides(stations, 530, {'stations': {'WQAB123': {'kw': {'D': .04}}}})
        self.assertEqual(stations[0]['ant']['D'].label, '40 W TIS')
        self.assertEqual(stations[0]['ant']['N'].label, '10 W TIS')
        self.assertAlmostEqual(stations[0]['ant']['D'].field(0, 0), 2 * base)
        self.assertAlmostEqual(stations[0]['ant']['N'].field(0, 0), base)

    def test_expiry_is_rechecked_without_a_refresh(self):
        row = parse_uls(archive(fixture()))[0]
        self.assertTrue(current(row, '2098-12-31'))
        self.assertFalse(current(row, '2099-01-02'))
        self.assertFalse(current(row, '2019-12-31'))


class TISAntennaTests(unittest.TestCase):
    def test_gain_loss_erp_cap_and_power_scaling(self):
        a = TISAntenna([{'power_w': 10, 'gain_db': -6, 'line_loss_db': 2, 'erp_w': 10}])
        self.assertAlmostEqual(a.field(0, 0), math.sqrt(49.2 * 10 * 10 ** (-.8)))
        capped = TISAntenna([{'power_w': 10, 'gain_db': 6, 'erp_w': 2}])
        self.assertAlmostEqual(capped.field(0, 0), math.sqrt(49.2 * 2))
        stronger = TISAntenna([{'power_w': 40, 'gain_db': -6, 'line_loss_db': 2}])
        self.assertAlmostEqual(stronger.field(0, 0) / a.field(0, 0), 2)
        self.assertAlmostEqual(a.field(0, 60) / a.field(0, 0), .5)

    def test_missing_antenna_does_not_invent_electrical_height(self):
        base = TISAntenna([{'power_w': 10, 'height_tip_m': 15}])
        tall = TISAntenna([{'power_w': 10, 'height_tip_m': 100}])
        self.assertEqual(base.field(0, 45), tall.field(0, 45))
        self.assertTrue(any('ideal short vertical' in n for n in base.notes))
        erp = TISAntenna([{'power_w': None, 'erp_w': 2}])
        self.assertAlmostEqual(erp.field(0, 0), math.sqrt(98.4))
        self.assertIn('ERP', erp.label)

    def test_alternatives_are_not_double_counted_and_bad_gain_is_ignored(self):
        ant = {'power_w': 10, 'erp_w': 2, 'gain_db': 41.3}
        one = TISAntenna([ant])
        duplicate = TISAntenna([ant, ant])
        self.assertEqual(one.field(0, 0), duplicate.field(0, 0))
        self.assertTrue(any('Implausible' in n for n in one.notes))


class ULSCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'DATA_DIR', Path(self.tmp.name))
        self.patch.start()
        self.path = config.DATA_DIR / 'cache' / 'uls_tis.json'

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def response(self):
        response = archive(fixture())
        response.headers = {'Last-Modified': 'Sun, 18 Oct 2026 09:25:55 GMT'}
        return response

    def test_download_cache_hit_and_forced_refresh(self):
        with patch('mw_explorer.uls.urllib.request.urlopen', side_effect=lambda *a, **k: self.response()) as fetch:
            first = load_uls()
            self.assertEqual(load_uls(), first)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0].get_header('Accept'), '*/*')
            self.assertEqual(load_uls(refresh=True), first)
            self.assertEqual(fetch.call_count, 2)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['version'], CACHE_VERSION)
        self.assertIn('18 Oct 2026', saved['source_modified'])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ['uls_tis.json'])

    def test_failed_refresh_retains_cache_and_filters_expiration(self):
        with patch('mw_explorer.uls.urllib.request.urlopen', return_value=self.response()):
            load_uls()
        saved = json.loads(self.path.read_text())
        saved['fetched'] = time.time() - 8 * 86400
        expired = dict(saved['rows'][0], expires='2000-01-01')
        saved['rows'].append(expired)
        self.path.write_text(json.dumps(saved))
        before = self.path.read_bytes()
        with patch('mw_explorer.uls.urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertEqual(len(load_uls()), 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_download_failure_and_invalid_zip_do_not_publish_cache(self):
        for error in (OSError('offline'), zipfile.BadZipFile('truncated')):
            with patch('mw_explorer.uls.urllib.request.urlopen', side_effect=error):
                with self.assertRaises(RuntimeError):
                    load_uls()
            self.assertFalse(self.path.exists())


class ULSBuildTests(unittest.TestCase):
    def test_build_metadata_and_grid_match_home_station_indices(self):
        row = parse_uls(archive(fixture()))[0]
        stations = tis_stations([row, dict(row, location='2', lon=-73.8)])
        stations = pickle.loads(pickle.dumps(stations))
        ctx = {'build': '2026101918-1234', 'ssn': 100, 'hours': ['2026101918'],
               'grid': {'lat0': 40.70, 'lon0': -74.01, 'step': .02, 'nlat': 1, 'nlon': 1}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, 'DATA_DIR', Path(tmp)):
            result = compute_channel((530, stations, ctx))
            self.assertEqual(result[:2], (530, 2))
            root = build_dir(ctx['build']) / 'ch' / '530'
            metadata = json.loads((root / 'stations.json').read_text())['stations']
            self.assertEqual([s['id'] for s in metadata], [s['id'] for s in stations])
            self.assertEqual(metadata[1]['uls']['location'], '2')
            self.assertEqual(metadata[0]['ant']['D'], '10 W TIS')
            self.assertEqual(metadata[0]['uls']['antennas'][0]['height_tip_m'], 15)
            grid = gzip.decompress((root / '2026101918.bin.gz').read_bytes())
            self.assertEqual(len(grid), CELL_BYTES)
            best, _, first, level, *_ = struct.unpack(CELL_FORMAT, grid)
            home = levels_at(stations, 530, 40.71, -74, 100, ctx['hours'])
            strongest = max(home, key=lambda r: r['t'][0])
            self.assertEqual(best, strongest['i'])
            self.assertEqual(first, strongest['i'])
            self.assertEqual(level, strongest['t'][0])
