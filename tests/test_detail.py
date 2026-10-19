import gzip
import json
import pickle
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mw_explorer import config
from mw_explorer.build import CELL_BYTES, CELL_FORMAT, build, build_dir, compute_channel
from mw_explorer.detail import STEP, detail_cells, detail_edges, detail_rows, lon_step, refine, refine_channel
from mw_explorer.geo import gc_km
from mw_explorer.home import levels_at
from tests.test_model import station


def tis(lat=44.5, lon=-69):
    return dict(station('WTIS', lat, lon, kw=.01), service='TIS')


class GeometryTests(unittest.TestCase):
    def test_cell_sizes_and_site_coverage_at_different_latitudes(self):
        for lat in (18, 44.5, 65):
            rows = detail_rows([tis(lat)])
            points = detail_cells(rows)
            self.assertLess(min(gc_km(lat, -69, a, b) for a, b in points), 1.4)
            self.assertEqual(sum(r[2] for r in rows), len(points))
            for index, (a, b) in enumerate(points):
                i = int(a // STEP)
                self.assertAlmostEqual(gc_km(a, b, a + STEP, b), 1.85325, delta=.001)
                self.assertAlmostEqual(gc_km(a, b, a, b + lon_step(i)), 1.85325, delta=.001)
                j = int((b + 180) // lon_step(i))
                run = next(r for r in rows if r[0] == i and r[1] <= j < r[1] + r[2])
                self.assertEqual(run[3] + j - run[1], index)

    def test_overlaps_are_deduplicated_and_broadcast_sites_do_not_add_detail(self):
        first, second = tis(), tis(lon=-68.9)
        rows = detail_rows([first])
        self.assertEqual(detail_rows([first, first]), rows)
        combined = detail_cells(detail_rows([first, second]))
        self.assertEqual(len(set(combined)), len(combined))
        self.assertLess(len(combined), len(detail_cells(rows)) + len(detail_cells(detail_rows([second]))))
        self.assertEqual(detail_rows([station('AM', 40, -70)]), [])

    def test_neighbors_stay_adjacent_across_disconnected_runs(self):
        rows = detail_rows([tis(), tis(lon=-75)])
        points = detail_cells(rows)
        edges = detail_edges(rows)
        self.assertTrue(edges)
        self.assertEqual(len(edges), len(set(edges)))
        self.assertTrue(all(gc_km(*points[a], *points[b]) < 2.7 for a, b in edges))


class DetailBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = patch.object(config, 'DATA_DIR', Path(self.temp.name))
        self.data.start()
        self.ctx = {'build': '2026101918-12345', 'ssn': 100,
                    'hours': ['2026101918', '2026102006'],
                    'grid': {'lat0': 44, 'lon0': -70, 'step': .5, 'nlat': 2, 'nlon': 3}}
        self.stations = [tis(), station('BROADCAST', 44.55, -68.8, kw=1)]

    def tearDown(self):
        self.data.stop()
        self.temp.cleanup()

    def build(self):
        with patch('mw_explorer.detail.BATCH_CELLS', 37):
            compute_channel((530, self.stations, self.ctx))
        root = build_dir(self.ctx['build']) / 'ch' / '530'
        return root, json.loads((root / 'stations.json').read_text())

    def test_detail_frames_include_all_interferers_and_match_exact_location_levels(self):
        root, ch = self.build()
        points = detail_cells(ch['detail']['rows'])
        winners = set()
        for hour in self.ctx['hours']:
            data = gzip.decompress((root / (hour + '.detail.bin.gz')).read_bytes())
            self.assertEqual(len(data), len(points) * CELL_BYTES)
            for index in range(0, len(points), 31):
                values = struct.unpack_from(CELL_FORMAT, data, index * CELL_BYTES)
                rows = levels_at(self.stations, 530, *points[index], 100, [hour])
                loudest = max(rows, key=lambda r: r['t'][0])
                self.assertEqual(values[2], loudest['i'])
                self.assertEqual(values[3], loudest['t'][0])
                winners.add(values[0])
                self.assertEqual(set(values[2::2]) - {65535}, {0, 1})
        self.assertEqual(winners, {0, 1})
        self.assertTrue(all(s['color'] for s in ch['stations']))
        self.assertNotEqual(ch['stations'][0]['color'], ch['stations'][1]['color'])

    def test_refine_publishes_atomically_and_preserves_previous_files_and_hours(self):
        root, ch = self.build()
        old_meta = (root / 'stations.json').read_bytes()
        old_frame = (root / (self.ctx['hours'][0] + '.bin.gz')).read_bytes()
        index = dict(self.ctx, channels={'530': 2}, generated='original')
        (config.out_dir() / 'index.json').write_text(json.dumps(index))
        (build_dir(index['build']) / 'db.pickle').write_bytes(pickle.dumps(
            {'build': index['build'], 'ssn': 100, 'stations': {530: self.stations}}))
        with patch('mw_explorer.detail.ProcessPoolExecutor') as executor:
            executor.return_value.__enter__.return_value.map.side_effect = lambda fn, tasks: map(fn, tasks)
            refine(workers=1)
        new = json.loads((config.out_dir() / 'index.json').read_text())
        self.assertNotEqual(new['build'], index['build'])
        self.assertEqual(new['hours'], index['hours'])
        self.assertEqual((root / 'stations.json').read_bytes(), old_meta)
        self.assertEqual((build_dir(new['build']) / 'ch' / '530' / (index['hours'][0] + '.bin.gz')).read_bytes(), old_frame)
        new_ch = json.loads((build_dir(new['build']) / 'ch' / '530' / 'stations.json').read_text())
        self.assertEqual([s['wins'] for s in new_ch['stations']], [s['wins'] for s in ch['stations']])
        before = (config.out_dir() / 'index.json').read_bytes()
        with patch('mw_explorer.detail.ProcessPoolExecutor') as executor:
            executor.return_value.__enter__.return_value.map.side_effect = RuntimeError('failed computation')
            with self.assertRaises(RuntimeError):
                refine(workers=1)
        self.assertEqual((config.out_dir() / 'index.json').read_bytes(), before)

    def test_regular_build_includes_detail_before_publishing(self):
        region = {'label': 'test region', 'stations': (44, 45, -70, -68.5), 'grid': (44, 45, -70, -68.5)}
        with patch('mw_explorer.build.discover', return_value=({530: self.stations}, {})), \
             patch('mw_explorer.build.fetch_ssn', return_value=(100, 'test')), \
             patch.dict('mw_explorer.build.REGIONS', {'na': region}), \
             patch('mw_explorer.build.ProcessPoolExecutor') as coarse, \
             patch('mw_explorer.detail.ProcessPoolExecutor') as fine:
            for executor in (coarse, fine):
                executor.return_value.__enter__.return_value.map.side_effect = lambda fn, tasks: map(fn, tasks)
            build(freqs=[530], hours=2, workers=1)
        index = json.loads((config.out_dir() / 'index.json').read_text())
        root = build_dir(index['build']) / 'ch' / '530'
        ch = json.loads((root / 'stations.json').read_text())
        self.assertGreater(ch['detail']['cells'], 0)
        self.assertEqual(ch['hours'], index['hours'])
        self.assertTrue(all((root / (h + '.detail.bin.gz')).exists() for h in ch['hours']))

    def test_already_finer_base_grid_does_not_get_coarser_detail(self):
        ctx = dict(self.ctx, grid=dict(self.ctx['grid'], step=.01))
        self.assertEqual(refine_channel((530, self.stations, ctx)), (530, 0))
