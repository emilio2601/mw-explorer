import json
import math
import pickle
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from mw_explorer.antenna import Antenna
from mw_explorer.build import CELL_BYTES, CELL_FORMAT
from mw_explorer.geo import solar
from mw_explorer.home import levels_at
from mw_explorer.model import field_at, hourly_loss, path_terms, takeoff_blend
from mw_explorer import config
from mw_explorer.server import BadRequest, Gone, Store, _db, _hours
from mw_explorer.stations import base_call, build_stations, move_to_licensed_freq, mx_state


def tower(F, psi, S, orient, G, A=0.0, B=0.0, ref=0):
    return {"F": F, "psi": psi, "S": S, "orient": orient, "G": G, "ref": ref, "A": A, "B": B}


# WFLF 540 night, from the FCC AM Query (BL-20090619ADT); FCC RMS Standard is 2012 mV/m.
WFLF_NIGHT = {"hours": "N", "kw": 46.0, "directional": True, "aux": False, "file": "BL-20090619ADT",
              "rms_std": 2012.0, "rms_theo": 1915.0, "towers": [
                  tower(1.000, 0.00, 159.60, 348.70, 0.00, 72.70, 24.20),
                  tower(0.213, 20.80, 322.80, 347.40, 71.10),
                  tower(0.518, -84.20, 70.80, 150.50, 71.10),
                  tower(0.888, -130.00, 99.50, 12.40, 71.10),
                  tower(0.525, -136.10, 260.20, 355.40, 71.10),
                  tower(0.664, 0.30, 63.90, 251.90, 0.00, 71.10, 3.50)]}


def station(call, lat, lon, kw=50.0, country="US"):
    ant = Antenna(kw, None)
    return {"call": call, "city": "X", "state": "", "country": country, "lat": lat, "lon": lon,
            "ant": {"D": ant, "N": ant}, "site": {"D": (lat, lon), "N": (lat, lon)}}


def row(call, hours, lat, lon, kw=1.0, country="US", city="X", file="F-1"):
    return {"call": call, "freq": 540, "hours": hours, "status": "LIC", "city": city, "state": "", "country": country,
            "file": file, "kw": kw, "da": False, "facid": "1", "lat": lat, "lon": lon, "rec": None}


class AntennaTests(unittest.TestCase):
    def test_directional_pattern_reproduces_fcc_rms(self):
        ant = Antenna(46.0, WFLF_NIGHT)
        computed, fcc = ant.rms_check
        self.assertLess(abs(computed / fcc - 1), 0.01)

    def test_sectionalized_tower_stays_finite(self):
        # WHO 1040: A=120, B=300 describe tower sections; read as top loading the formula divides by zero.
        rec = {"directional": False, "rms_theo": 471.54, "towers": [tower(1.0, 0, 0, 0, 0.0, 120.0, 300.0)]}
        ant = Antenna(50.0, rec)
        for el in (0, 5, 15, 30, 60):
            f = ant.field(0, el)
            self.assertTrue(math.isfinite(f))
            self.assertLess(f, 2 * 471.54 * math.sqrt(50))

    def test_full_power_nondirectional_rms_is_not_rescaled(self):
        ant = Antenna(20.0, {"directional": False, "rms_theo": 1377.45, "towers": []})
        self.assertAlmostEqual(ant.field(0, 0), 1377.45, delta=1)

    def test_per_kw_nondirectional_rms_is_scaled(self):
        ant = Antenna(50.0, {"directional": False, "rms_theo": 317.04, "towers": []})
        self.assertAlmostEqual(ant.field(0, 0), 317.04 * math.sqrt(50), delta=1)


class PropagationTests(unittest.TestCase):
    def test_no_absorption_cliff_at_sunrise(self):
        sol = solar(datetime(2026, 10, 14, 11, 0, tzinfo=timezone.utc))
        losses = [hourly_loss(40, lon / 4, sol) for lon in range(-90 * 4, -40 * 4)]
        self.assertLess(max(abs(a - b) for a, b in zip(losses, losses[1:])), 3)

    def test_hop_blend_weights_sum_to_one(self):
        for d in range(1500, 4500, 37):
            self.assertAlmostEqual(sum(w for _, w in takeoff_blend(d)), 1.0)

    def test_skywave_continuous_across_hop_boundary(self):
        st = station("WAAA", 40.0, -100.0)
        sol = solar(datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc))
        levels = []
        for km in range(1900, 2101, 20):
            lon = -100.0 + km / (111.32 * math.cos(math.radians(40)))
            levels.append(field_at(path_terms(st, 40.0, lon, 100, 1000), "N", sol)[1])
        self.assertLess(max(abs(a - b) for a, b in zip(levels, levels[1:])), 3)

    def test_levels_at_returns_each_hour(self):
        sts = [station("WAAA", 40.0, -80.0), station("WBBB", 30.0, -90.0)]
        rows = levels_at(sts, 1000, 40.7, -74.0, 100, ["2026101400", "2026101401", "2026101402"])
        self.assertEqual({r["i"] for r in rows}, {0, 1})
        for r in rows:
            self.assertEqual(len(r["t"]), 3)
            self.assertEqual(len(r["m"]), 3)


class StationTests(unittest.TestCase):
    def test_mexican_suffix_merges(self):
        self.assertEqual(base_call({"call": "XEBV1", "country": "MX"}), "XEBV")
        self.assertEqual(base_call({"call": "CBKF2", "country": "CA"}), "CBKF2")

    def test_day_and_night_sites_become_one_station(self):
        out = build_stations([row("WRDT", "DAY", 41.89, -83.43), row("WRDT", "NIG", 42.45, -83.16)], set(), 560)
        self.assertEqual(len(out), 1)
        self.assertEqual(set(out[0]["site"]), {"D", "N"})
        self.assertNotEqual(out[0]["site"]["D"], out[0]["site"]["N"])

    def test_cosited_calls_merge_to_wellformed_call(self):
        out = build_stations([row("CINF", "UNL", 45.3, -73.7, country="CA"),
                              row("C690", "UNL", 45.3, -73.7, country="CA")], set(), 690)
        self.assertEqual([s["call"] for s in out], ["CINF"])

    def test_mexican_call_keeps_licensed_city(self):
        rows = [row("XEW", "UNL", 19.32, -99.12, 250, "MX", "Col.Ex-Hacienda De C"),
                row("XEW", "UNL", 23.2, -106.42, 0.5, "MX", "Mazatlan")]
        out = build_stations(rows, set(), 900, crt_cities={("XEW", 900): ["Ciudad De México"]})
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]["lat"], 19.32)


    def test_stale_mexican_frequency_moves_only_when_unambiguous(self):
        crt = [{"call": "XEAI", "freq": 1470}, {"call": "XEHS", "freq": 540}]
        rows = [dict(row("XEAI", "UNL", 19.4, -99.1, 20, "MX"), freq=1500),
                dict(row("XEHS", "DAY", 25.8, -109.0, 5, "MX"), freq=540),
                dict(row("XEHS", "UNL", 20.0, -100.0, 50, "MX"), freq=1280),
                dict(row("XEHS", "UNL", 21.0, -101.0, 1, "MX"), freq=740)]
        move_to_licensed_freq(rows, crt)
        self.assertEqual([r["freq"] for r in rows], [1470, 540, 1280, 740])

    def test_foreign_station_uses_its_newest_filing_for_day_and_night(self):
        rows = [row("XEUU", "DAY", 19.25, -103.707, 1.0, "MX", file="MX_309301"),
                row("XEUU", "NIG", 19.25, -103.707, 0.5, "MX", file="MX_309301"),
                row("XEUU", "DAY", 19.181, -103.672, 1.0, "MX", file="MX_1806921"),
                row("XEUU", "NIG", 19.181, -103.672, 0.13, "MX", file="MX_1806921")]
        st = build_stations(rows, set(), 1080)[0]
        self.assertEqual(st["site"]["D"], st["site"]["N"])
        self.assertAlmostEqual(st["ant"]["N"].kw, 0.13)
        self.assertIsNone(st["note"])

    def test_mexican_states_use_common_abbreviations(self):
        self.assertEqual(mx_state("MC", 19.1, "MICHOACÁN DE OCAMPO"), "Mich.")
        self.assertEqual(mx_state("DF", 24.9, "DURANGO"), "Dgo.")  # CRT beats the FCC's mislabeled code
        self.assertEqual(mx_state("DF", 19.4, "CIUDAD DE MÉXICO"), "CDMX")
        self.assertEqual(mx_state("MC", 19.1), "Mich.")
        self.assertEqual(mx_state("CH", 16.2), "Chis.")
        self.assertEqual(mx_state("CH", 28.6), "Chih.")
        self.assertEqual(mx_state("BN", 24.1), "BCS")
        self.assertEqual(mx_state("BN", 32.5), "BC")


class OutputTests(unittest.TestCase):
    def test_cell_roundtrip(self):
        buf = bytearray(CELL_BYTES)
        struct.pack_into(CELL_FORMAT, buf, 0, 7, 12, 7, 55, 3, 40, 0xFFFF, 0)
        self.assertEqual(struct.unpack(CELL_FORMAT, buf), (7, 12, 7, 55, 3, 40, 0xFFFF, 0))

    def test_api_hours_validation(self):
        now = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        self.assertEqual(len(_hours({"start": [now], "n": ["5"]})), 5)
        with self.assertRaises(BadRequest):
            _hours({"start": ["1999010100"]})
        with self.assertRaises(BadRequest):
            _hours({"start": [now], "n": ["500"]})


class BuildSwitchTests(unittest.TestCase):
    def setUp(self):
        self.prev = config.DATA_DIR
        self.tmp = Path(tempfile.mkdtemp())
        config.DATA_DIR = self.tmp
        for b in ("2026101400-1", "2026101400-2"):
            d = self.tmp / "out" / "builds" / b
            d.mkdir(parents=True)
            (d / "db.pickle").write_bytes(pickle.dumps({"build": b, "ssn": 90, "stations": {}}))
        (self.tmp / "out" / "index.json").write_text(json.dumps({"build": "2026101400-2"}))

    def tearDown(self):
        config.DATA_DIR = self.prev
        shutil.rmtree(self.tmp)

    def test_serves_the_build_the_page_loaded(self):
        store = Store()
        self.assertEqual(store.get()["build"], "2026101400-2")
        self.assertEqual(store.get("2026101400-1")["build"], "2026101400-1")

    def test_pruned_build_is_gone_even_if_cached(self):
        import mw_explorer.server as server
        store = server.STORE = Store()
        store.get("2026101400-1")
        shutil.rmtree(self.tmp / "out" / "builds" / "2026101400-1")
        with self.assertRaises(Gone):
            _db({"build": ["2026101400-1"]})


if __name__ == "__main__":
    unittest.main()
