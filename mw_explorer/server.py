"""HTTP server: the explorer page, the daily build output, and per-location APIs.

  GET /                          explorer page (static/index.html)
  GET /out/...                   build output (index.json, ch/<kHz>/stations.json, ch/<kHz>/<hour>.bin.gz)
  GET /api/home?freq&lat&lon&start&n   levels from every station at that location, per hour
  GET /api/wspr?freq&lat&lon&i=1,2     live WSPR skywave correction for those stations, for the current hour
  GET /api/health
"""
import json
import mimetypes
import pickle
import re
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .home import HOUR_FMT, levels_at, wspr_deltas
from .live import fetch_kp

MAX_HOURS = 48
KP_TTL = 30 * 60
WSPR_TTL = 15 * 60
RATE = {"home": (60, 60), "wspr": (10, 60)}  # requests per window (s) per client


class Store:
    """Station databases by build id. The current build comes from index.json; the page names the build it
    loaded so its station indices line up with ours even while a newer build is switching in."""

    def __init__(self):
        self.lock = threading.Lock()
        self.dbs = {}

    def current(self):
        try:
            return json.loads((config.out_dir() / "index.json").read_text()).get("build")
        except (OSError, ValueError):
            return None

    def get(self, build=None):
        build = build or self.current()
        if not build or not re.fullmatch(r"[0-9]{10}-[0-9]+", build):
            return None
        with self.lock:
            path = config.out_dir() / "builds" / build / "db.pickle"
            if build in self.dbs and not path.exists():
                del self.dbs[build]  # pruned by a newer build
            if build not in self.dbs:
                try:
                    # db.pickle is written only by our own build job into our own data volume.
                    self.dbs[build] = pickle.loads(path.read_bytes())
                except FileNotFoundError:
                    return None
                for old in sorted(self.dbs)[:-2]:
                    del self.dbs[old]
            return self.dbs[build]


class TTLCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def get(self, key, ttl, make):
        with self.lock:
            hit = self.items.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        value = make()
        with self.lock:
            self.items[key] = (time.time(), value)
            if len(self.items) > 5000:
                for k in sorted(self.items, key=lambda k: self.items[k][0])[:1000]:
                    del self.items[k]
        return value


class RateLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits = defaultdict(deque)

    def allow(self, client, kind):
        limit, window = RATE[kind]
        now = time.time()
        with self.lock:
            q = self.hits[(client, kind)]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


STORE, CACHE, LIMITS = Store(), TTLCache(), RateLimiter()


class BadRequest(Exception):
    status = 400


class Gone(BadRequest):
    status = 410


def _num(q, name, lo, hi, cast=float):
    try:
        v = cast(q[name][0])
    except (KeyError, IndexError, ValueError):
        raise BadRequest(f"missing or invalid {name}")
    if not lo <= v <= hi:
        raise BadRequest(f"{name} out of range")
    return v


def _hours(q):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = q.get("start", [now.strftime(HOUR_FMT)])[0]
    try:
        t0 = datetime.strptime(start, HOUR_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise BadRequest("start must be YYYYMMDDHH")
    if abs((t0 - now).total_seconds()) > 48 * 3600:
        raise BadRequest("start must be within two days of now")
    n = _num(q, "n", 1, MAX_HOURS, int) if "n" in q else 25
    return [(t0 + timedelta(hours=i)).strftime(HOUR_FMT) for i in range(n)]


def _channel(db, q):
    freq = _num(q, "freq", 530, 1700, int)
    stations = db["stations"].get(freq)
    if stations is None:
        raise BadRequest(f"{freq} kHz is not in the current build")
    return freq, stations


def _db(q):
    build = q.get("build", [None])[0]
    db = STORE.get(build)
    if db is None:
        raise Gone("that build is no longer available; reload") if build else BadRequest("no build yet")
    return db


def api_home(q):
    db = _db(q)
    freq, stations = _channel(db, q)
    lat, lon = _num(q, "lat", -90, 90), _num(q, "lon", -180, 180)
    hours = _hours(q)
    kp, kp_time = CACHE.get("kp", KP_TTL, fetch_kp)
    return {"freq": freq, "build": db["build"], "ssn": db["ssn"], "kp": kp, "kp_time": kp_time,
            "hours": hours, "stations": levels_at(stations, freq, lat, lon, db["ssn"], hours)}


def api_wspr(q):
    db = _db(q)
    freq, stations = _channel(db, q)
    lat, lon = _num(q, "lat", -90, 90), _num(q, "lon", -180, 180)
    try:
        idx = sorted({int(x) for x in q.get("i", [""])[0].split(",") if x})[:6]
    except ValueError:
        raise BadRequest("i must be station indices")
    if any(not 0 <= k < len(stations) for k in idx):
        raise BadRequest("station index out of range")
    key = ("wspr", db["build"], freq, round(lat, 1), round(lon, 1), tuple(idx))
    res = CACHE.get(key, WSPR_TTL, lambda: wspr_deltas(stations, freq, lat, lon, db["ssn"], idx))
    return {"freq": freq, "build": db["build"], "deltas": {str(k): v for k, v in res.items()}}


class Handler(BaseHTTPRequestHandler):
    server_version = "mw-explorer"

    def client(self):
        fwd = self.headers.get("X-Forwarded-For")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, code, obj, cache="no-store"):
        self.send(code, json.dumps(obj, separators=(",", ":")), headers={"Cache-Control": cache})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                return self.send_file(config.STATIC_DIR / "index.html", "no-cache")
            if url.path == "/api/health":
                db = STORE.get()
                return self.send_json(200, {"ok": db is not None, "build": db and db["build"]})
            if url.path in ("/api/home", "/api/wspr"):
                kind = url.path.rsplit("/", 1)[1]
                if not LIMITS.allow(self.client(), kind):
                    return self.send_json(429, {"error": "too many requests, slow down"})
                return self.send_json(200, api_home(q) if kind == "home" else api_wspr(q))
            if url.path.startswith("/out/"):
                return self.send_output(url.path[len("/out/"):])
            return self.send_json(404, {"error": "not found"})
        except BadRequest as e:
            return self.send_json(e.status, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            return  # the browser left before the answer arrived (reload, channel switch)
        except Exception as e:  # keep the server up; report the failure to the client
            self.log_error("%s failed: %r", url.path, e)
            return self.send_json(500, {"error": "internal error"})

    def send_output(self, rel):
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", rel) or ".." in rel:
            return self.send_json(404, {"error": "not found"})
        path = (config.out_dir() / rel).resolve()
        if config.out_dir().resolve() not in path.parents or not path.is_file() or path.suffix == ".pickle":
            return self.send_json(404, {"error": "not found"})
        if rel.endswith(".bin.gz"):
            return self.send(200, path.read_bytes(), "application/octet-stream",
                             {"Content-Encoding": "gzip", "Cache-Control": "public, max-age=86400, immutable"})
        return self.send_file(path, "no-cache")

    def send_file(self, path, cache):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return self.send_json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        return self.send(200, data, ctype, {"Cache-Control": cache})

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else "") or (len(args) > 1 and str(args[1]) >= "400"):
            super().log_message(fmt, *args)


def serve(host="127.0.0.1", port=8766):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"Serving on http://{host}:{port}/  (data: {config.DATA_DIR})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
