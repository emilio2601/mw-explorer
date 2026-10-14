"""python -m mw_explorer {build,serve,report}"""
import argparse
import os
import pickle
import sys
import webbrowser
from datetime import datetime, timedelta, timezone

from . import config
from .home import HOUR_FMT, levels_at, wspr_deltas
from .model import power_sum


def parse_latlon(s):
    lat, lon = (float(x) for x in s.split(","))
    return lat, lon


def cmd_build(args):
    from .build import build
    exclude = {c.strip().upper() for c in (args.exclude or "").split(",") if c.strip()}
    build(args.freq, args.hours, args.step, args.workers, args.refresh, exclude, not args.no_ised, not args.no_crt)


def cmd_relabel(args):
    from .build import relabel
    relabel(not args.no_ised, not args.no_crt)


def cmd_serve(args):
    from .server import serve
    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}/")
    serve(args.host, args.port)


def cmd_report(args):
    try:
        db = pickle.loads((config.out_dir() / "db.pickle").read_bytes())
    except FileNotFoundError:
        sys.exit("No build yet. Run: python -m mw_explorer build")
    stations = db["stations"].get(args.freq)
    if not stations:
        sys.exit(f"{args.freq} kHz is not in the current build.")
    lat, lon = args.home
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hours = [(now + timedelta(hours=i)).strftime(HOUR_FMT) for i in range(args.hours)]
    deltas = {}
    if args.wspr:
        loud = levels_at(stations, args.freq, lat, lon, db["ssn"], hours[:1])
        pick = [r["i"] for r in sorted(loud, key=lambda r: -(r["t"][0] or -999))[:5]]
        pick += [k for k, st in enumerate(stations) if args.target and st["call"] == args.target.upper()]
        res = wspr_deltas(stations, args.freq, lat, lon, db["ssn"], set(pick))
        deltas = {k: v["delta"] for k, v in res.items() if v["delta"] is not None}
        print("WSPR corrections now: " + (", ".join(f"{stations[k]['call']} {d:+.1f} dB" for k, d in deltas.items())
                                          or "none (no dark proxy paths with a baseline)"))
    rows = levels_at(stations, args.freq, lat, lon, db["ssn"], hours, deltas)
    target = args.target.upper() if args.target else None
    print(f"\n{args.freq} kHz at {lat:.2f},{lon:.2f} — loudest now (dBµV/m):")
    print(f"  {'station':16s}{'km':>6s}{'brg':>6s}{'antenna':>16s}{'sky':>6s}{'ground':>8s}{'total':>7s}")
    for r in sorted((r for r in rows if r["t"][0] is not None), key=lambda r: -r["t"][0])[:10]:
        st = stations[r["i"]]
        print(f" {'*' if st['call'] == target else ' '}{st['call'] + ' ' + st['country']:16s}{r['km']:6d}"
              f"{r['brg']:5.0f}°{st['ant'][r['m'][0]].label:>16s}{r['sky'][0]:6d}{r['gw'][0]:8d}{r['t'][0]:7d}")
    print("\nNext hours:")
    for fi, h in enumerate(hours):
        now_rows = sorted(((r["t"][fi], r) for r in rows if r["t"][fi] is not None), key=lambda x: -x[0])
        if not now_rows:
            continue
        lead_t, lead = now_rows[0]
        rest = power_sum(*(t for t, _ in now_rows[1:]))
        line = f"  {h[8:]}:00 UTC  loudest {stations[lead['i']]['call']:>8s} {lead_t:4d}"
        line += f" ({lead_t - rest:+.0f} dB)" if rest is not None else ""
        tr = next((r for t, r in now_rows if stations[r["i"]]["call"] == target), None)
        if target:
            others = power_sum(*(t for t, r in now_rows if r is not tr))
            line += (f"   {target} {tr['t'][fi]:4d}, vs rest {tr['t'][fi] - others:+.0f} dB"
                     if tr and others is not None else f"   {target} not heard")
        print(line)


def main():
    ap = argparse.ArgumentParser(prog="python -m mw_explorer", description=__doc__)
    ap.add_argument("--data", help="data directory (cache + build output); default $MW_DATA_DIR or ./data")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="refresh the station database and compute the grid for every channel")
    b.add_argument("--freq", type=int, nargs="*", help="only these channels (kHz)")
    b.add_argument("--hours", type=int, default=48, help="hours to compute from the current hour (default 48)")
    b.add_argument("--step", type=float, default=0.5, help="grid step in degrees (default 0.5)")
    b.add_argument("--workers", type=int, default=None, help="processes (default: CPU count)")
    b.add_argument("--refresh", action="store_true", help="re-download FCC/ISED/CRT data even if cached")
    b.add_argument("--exclude", help="comma-separated call signs to drop")
    b.add_argument("--no-ised", action="store_true", help="skip the ISED check for Canadian stations")
    b.add_argument("--no-crt", action="store_true", help="skip the CRT license check for Mexican stations")
    b.set_defaults(func=cmd_build)

    lb = sub.add_parser("relabel", help="refresh station names and states in the current build, no recompute")
    lb.add_argument("--no-ised", action="store_true")
    lb.add_argument("--no-crt", action="store_true")
    lb.set_defaults(func=cmd_relabel)

    s = sub.add_parser("serve", help="serve the explorer and its API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8766)))
    s.add_argument("--open", action="store_true", help="open a browser")
    s.set_defaults(func=cmd_serve)

    r = sub.add_parser("report", help="terminal report for one channel at one location")
    r.add_argument("--freq", type=int, required=True)
    r.add_argument("--home", type=parse_latlon, default=(40.71, -74.01), help="lat,lon (default NYC)")
    r.add_argument("--target", help="call sign to compare against everything else")
    r.add_argument("--hours", type=int, default=25)
    r.add_argument("--wspr", action="store_true", help="apply the live WSPR correction to the current hour")
    r.set_defaults(func=cmd_report)

    args = ap.parse_args()
    if args.data:
        config.set_data_dir(args.data)
    args.func(args)


if __name__ == "__main__":
    main()
