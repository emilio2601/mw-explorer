"""Paths and run-wide settings. MW_DATA_DIR points the cache and build output at a persistent volume."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
OVERRIDES = Path(os.environ.get("MW_OVERRIDES", ROOT / "station_overrides.json"))
CHANNELS = range(530, 1701, 10)

DATA_DIR = Path(os.environ.get("MW_DATA_DIR", ROOT / "data"))


def set_data_dir(path):
    global DATA_DIR
    DATA_DIR = Path(path).resolve()
    os.environ["MW_DATA_DIR"] = str(DATA_DIR)  # build worker processes re-import this module


def cache_file():
    return DATA_DIR / "cache" / "fcc_cache.json"


def out_dir():
    return DATA_DIR / "out"
