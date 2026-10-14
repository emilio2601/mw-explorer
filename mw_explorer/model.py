"""Skywave (ITU-R P.1147 style) and groundwave field strength for one station at one receiver."""
import math

from .geo import bearing, gc_km, geomag_lat, hour_angle, interp, midpoint, sun_elevation

NORTH_AMERICA_BR = 4
E_LAYER_KM = 100
GROUND_SIGMA = 0.005
# Crude stand-in for earth-curvature diffraction loss, which flat-earth Norton ignores.
CURVATURE_START_KM = 400
CURVATURE_DB_PER_KM = 0.015
NOISE_DBU = 15
MAX_PATH_KM = 5000

# Approximation of P.1147 hourly loss L_t (dB) vs hours after midpoint sunset / before sunrise.
LT_AFTER_SUNSET = [(0, 9.0), (1, 5.0), (2, 2.5), (3, 1.0), (4, 0.0)]
LT_BEFORE_SUNRISE = [(0, 6.0), (1, 2.5), (2, 1.0), (3, 0.0)]
LT_DAY = 60.0
# Daytime D-layer absorption builds with the sun's elevation at the path midpoint; starts from the night
# value at the terminator so skywave fades out over the first hours of daylight instead of switching off.
LT_DAY_BY_ELEV = [(3, 14.0), (6, 22.0), (10, 30.0), (15, 38.0), (25, 50.0), (40, LT_DAY)]
HOP_KM = 2000
HOP_BLEND_KM = 300


def hourly_loss(lat, lon, sol):
    """P.1147 L_t at a path midpoint, from hours since sunset / until sunrise there; ramps up through daylight."""
    c = -math.tan(math.radians(lat)) * math.tan(sol[0])
    ha = hour_angle(lon, sol)
    h0 = 180.0 if c <= -1 else 0.0 if c >= 1 else math.degrees(math.acos(c))
    if abs(ha) < h0:
        edge = LT_BEFORE_SUNRISE[0][1] if ha < 0 else LT_AFTER_SUNSET[0][1]
        return interp([(0, edge)] + LT_DAY_BY_ELEV, max(0.0, sun_elevation(lat, lon, sol)))
    since = (ha - h0 if ha > 0 else ha + 360 - h0) / 15
    until = (360 - h0 - ha if ha > 0 else -h0 - ha) / 15
    return max(interp(LT_AFTER_SUNSET, since), interp(LT_BEFORE_SUNRISE, until))


def takeoff_angle(d_km, hops=None):
    hops = hops or max(1, math.ceil(d_km / HOP_KM))
    x = d_km / hops / 2 / 6371
    r = 6371 / (6371 + E_LAYER_KM)
    return max(0.0, math.degrees(math.atan2(math.cos(x) - r, math.sin(x))))


def takeoff_blend(d_km):
    """[(elevation, weight)]: near a hop-count boundary, mix the two hop geometries instead of jumping."""
    hops = max(1, math.ceil(d_km / HOP_KM))
    lower = (hops - 1) * HOP_KM
    if hops > 1 and d_km - lower < HOP_BLEND_KM:
        w = 0.5 + 0.5 * (d_km - lower) / HOP_BLEND_KM
        return [(takeoff_angle(d_km, hops), w), (takeoff_angle(d_km, hops - 1), 1 - w)]
    if hops * HOP_KM - d_km < HOP_BLEND_KM:
        w = 0.5 + 0.5 * (hops * HOP_KM - d_km) / HOP_BLEND_KM
        return [(takeoff_angle(d_km, hops), w), (takeoff_angle(d_km, hops + 1), 1 - w)]
    return [(takeoff_angle(d_km, hops), 1.0)]

def path_terms(st, rx_lat, rx_lon, ssn, freq):
    """Time-independent pieces of the field from st at (rx_lat, rx_lon), per day/night antenna and site."""
    out, geo = {}, {}
    for h, ant in st["ant"].items():
        site = st["site"][h]
        if site not in geo:
            d = max(1.0, gc_km(*site, rx_lat, rx_lon))
            az = bearing(*site, rx_lat, rx_lon)
            mla, mlo = midpoint(*site, rx_lat, rx_lon)
            phi = geomag_lat(mla, mlo)
            p = math.hypot(max(d, 50), 200)
            a = 106.6 - 2 * math.sin(math.radians(phi))
            k = 3.2 + 0.19 * freq ** 0.4 * math.tan(math.radians(min(phi, 60) + 3)) ** 2
            kr = k + 0.01 * NORTH_AMERICA_BR * ssn
            wavelength_km = 299792.458 / freq / 1000
            x = 1.8e4 * GROUND_SIGMA / (freq / 1000)
            pn = math.pi * d / (wavelength_km * x)
            geo[site] = {"d": d, "az": az, "mid": (mla, mlo), "el": takeoff_angle(d), "els": takeoff_blend(d),
                         "sky_geo": a - 20 * math.log10(p) - kr * p / 1000,
                         "atten": (2 + 0.3 * pn) / (2 + pn + 0.6 * pn ** 2),
                         "curv": CURVATURE_DB_PER_KM * max(0.0, d - CURVATURE_START_KM)}
        g = geo[site]
        e_sky = math.sqrt(sum(w * ant.field(g["az"], el) ** 2 for el, w in g["els"]))
        out["sky" + h] = 20 * math.log10(e_sky / 300) + g["sky_geo"]
        out["gw" + h] = 20 * math.log10(max(1e-9, ant.field(g["az"], 0) * g["atten"] / g["d"] * 1000)) - g["curv"]
        out["mid" + h] = g["mid"]
    main = geo[st["site"].get("N") or st["site"]["D"]]
    out.update(d=main["d"], az=main["az"], mid=main["mid"], el=main["el"])
    return out


def power_sum(*dbs):
    vals = [10 ** (v / 10) for v in dbs if v is not None]
    return 10 * math.log10(sum(vals)) if vals else None


def station_mode(st, sol):
    return "D" if sun_elevation(st["lat"], st["lon"], sol) > 0 else "N"


def field_at(terms, mode, sol, sky_delta=0.0):
    if "sky" + mode not in terms:
        return None, None, None
    sky = terms["sky" + mode] - hourly_loss(*terms["mid" + mode], sol) + sky_delta
    gw = terms["gw" + mode]
    return power_sum(sky, gw), sky, gw
