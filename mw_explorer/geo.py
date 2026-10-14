"""Great-circle geometry, geomagnetic latitude and solar position."""
import math

GEOMAG_POLE = (80.8, -72.7)


def rad(*xs):
    return [math.radians(x) for x in xs]


def gc_km(lat1, lon1, lat2, lon2):
    p1, l1, p2, l2 = rad(lat1, lon1, lat2, lon2)
    c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(l2 - l1)
    return 6371 * math.acos(max(-1.0, min(1.0, c)))


def bearing(lat1, lon1, lat2, lon2):
    p1, l1, p2, l2 = rad(lat1, lon1, lat2, lon2)
    y = math.sin(l2 - l1) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(l2 - l1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def gc_points(lat1, lon1, lat2, lon2, n=48):
    p1, l1, p2, l2 = rad(lat1, lon1, lat2, lon2)
    d = gc_km(lat1, lon1, lat2, lon2) / 6371
    if d == 0:
        return [[lat1, lon1]]
    pts = []
    for i in range(n + 1):
        f = i / n
        a = math.sin((1 - f) * d) / math.sin(d)
        b = math.sin(f * d) / math.sin(d)
        x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
        y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
        z = a * math.sin(p1) + b * math.sin(p2)
        pts.append([round(math.degrees(math.atan2(z, math.hypot(x, y))), 3),
                    round(math.degrees(math.atan2(y, x)), 3)])
    return pts


def midpoint(lat1, lon1, lat2, lon2):
    p1, l1, p2, l2 = rad(lat1, lon1, lat2, lon2)
    bx = math.cos(p2) * math.cos(l2 - l1)
    by = math.cos(p2) * math.sin(l2 - l1)
    lat = math.atan2(math.sin(p1) + math.sin(p2), math.hypot(math.cos(p1) + bx, by))
    lon = l1 + math.atan2(by, math.cos(p1) + bx)
    return math.degrees(lat), (math.degrees(lon) + 540) % 360 - 180


def geomag_lat(lat, lon):
    p, l, pp, lp = rad(lat, lon, *GEOMAG_POLE)
    s = math.sin(p) * math.sin(pp) + math.cos(p) * math.cos(pp) * math.cos(l - lp)
    return math.degrees(math.asin(s))


def solar(when):
    """(declination rad, equation of time min, UTC minutes) — NOAA low-precision formulas."""
    doy = when.timetuple().tm_yday
    minutes = when.hour * 60 + when.minute + when.second / 60
    g = 2 * math.pi / 365 * (doy - 1 + (minutes / 60 - 12) / 24)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    return decl, eqt, minutes


def hour_angle(lon, sol):
    _, eqt, minutes = sol
    return ((minutes + eqt + 4 * lon) % 1440) / 4 - 180


def sun_elevation(lat, lon, sol):
    la, ha = math.radians(lat), math.radians(hour_angle(lon, sol))
    s = math.sin(la) * math.sin(sol[0]) + math.cos(la) * math.cos(sol[0]) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def interp(table, x):
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]
