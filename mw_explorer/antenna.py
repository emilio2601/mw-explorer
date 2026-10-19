"""AM antenna patterns: FCC standard pattern for directional arrays (47 CFR 73.150/73.160), simple towers otherwise."""
import cmath
import math


HALF_WAVE = {"G": 180.0}


def simple_tower(g_deg, th):
    g = math.radians(g_deg)
    return (math.cos(g * math.sin(th)) - math.cos(g)) / ((1 - math.cos(g)) * math.cos(th))


def vertical_factor(t, th):
    if t["G"] > 0:
        f = simple_tower(t["G"], th)
    elif t["B"] > t["A"] or t["A"] + t["B"] > 200:
        # Sectionalized tower (A/B are section heights, not top loading). These are anti-fading designs;
        # a half-wave tower approximates their suppressed high-angle radiation.
        f = simple_tower(180.0, th)
    else:
        s, c = math.sin(th), math.cos(th)
        a, b = math.radians(t["A"]), math.radians(t["B"])
        den = c * (math.cos(b) - math.cos(a + b))
        f = ((math.cos(b) * math.cos(a * s) - s * math.sin(b) * math.sin(a * s) - math.cos(a + b)) / den
             if abs(den) > 1e-3 else float("nan"))
    if not math.isfinite(f) or abs(f) > 2:
        f = simple_tower(180.0, th)
    return f


class Antenna:
    """Field in mV/m at 1 km (= cymomotive force in volts) toward azimuth/elevation."""

    def __init__(self, kw, rec):
        self.kw = kw
        self.adjust_db = 0.0
        self.rms_check = None
        towers = (rec or {}).get("towers") or []
        rms_theo = (rec or {}).get("rms_theo")
        if rec and rec["directional"] and len(towers) > 1 and rms_theo:
            self._init_da(rec, towers)
            return
        self.towers = None
        tower = towers[0] if towers and (towers[0]["G"] > 0 or towers[0]["A"] > 0) else {"G": 90.0}
        # Non-directional RMS is normally notified per kW, but some records give it at full power;
        # no real tower exceeds ~520 mV/m per kW, so a larger value must already be the full-power field.
        if rms_theo and not (rec or {}).get("directional"):
            self.k_omni = rms_theo * math.sqrt(kw) if rms_theo <= 520 else rms_theo
        elif rms_theo:
            self.k_omni = rms_theo
        else:
            self.k_omni = 306 * math.sqrt(kw)
        self.omni = tower
        self.label = f"{kw:g} kW DA?" if (rec or {}).get("directional") else f"{kw:g} kW ND"

    def _init_da(self, rec, towers):
        self.towers = towers
        pos, prev = [], (0.0, 0.0)
        for t in towers:
            o = math.radians(t["orient"])
            base = prev if t["ref"] == 1 else (0.0, 0.0)
            prev = (base[0] + t["S"] * math.sin(o), base[1] + t["S"] * math.cos(o))
            pos.append(prev)
        self.pos = pos
        r0 = math.sqrt(sum(self._unscaled(az, 0) ** 2 for az in range(0, 360, 2)) / 180)
        self.k = rec["rms_theo"] / r0
        self.e_rss = self.k * math.sqrt(sum(t["F"] ** 2 for t in towers))
        self.shortest = min(towers, key=lambda t: t["G"] if t["G"] > 0 else t["A"] + t["B"])
        self.label = f"{self.kw:g} kW DA-{len(towers)}"
        if rec.get("rms_std"):
            computed = math.sqrt(sum(self.field(az, 0) ** 2 for az in range(0, 360, 2)) / 180)
            self.rms_check = (computed, rec["rms_std"])

    def _unscaled(self, az, el):
        th, a = math.radians(el), math.radians(az)
        sa, ca, cth = math.sin(a), math.cos(a), math.cos(th)
        tot = 0j
        for t, (x, y) in zip(self.towers, self.pos):
            ph = math.radians(cth * (x * sa + y * ca) + t["psi"])
            tot += t["F"] * vertical_factor(t, th) * cmath.exp(1j * ph)
        return abs(tot)

    def field(self, az, el):
        scale = 10 ** (self.adjust_db / 20)
        if not self.towers:
            return max(1e-6, scale * self.k_omni * abs(vertical_factor(self.omni, math.radians(el))))
        g = vertical_factor(self.shortest, math.radians(el))
        q = max(0.025 * g * self.e_rss, 10 * g * math.sqrt(self.kw))
        return max(1e-6, scale * 1.05 * math.hypot(self.k * self._unscaled(az, el), q))


class TISAntenna:
    """Approximate short-vertical pattern scaled by ULS power and antenna data.

    ULS tip/center heights describe installation height, not radiating length.
    They must not be substituted for the electrical tower height in Antenna.
    Multiple authorizations at a site are treated as alternatives, not summed.
    """

    def __init__(self, antennas):
        self.adjust_db = 0.0
        self.rms_check = None
        self.notes = []
        fields, powers = [], []
        for ant in antennas:
            power, erp = ant.get('power_w'), ant.get('erp_w')
            gain, loss = ant.get('gain_db'), ant.get('line_loss_db')
            if power:
                powers.append(power)
            if gain is not None and not -60 <= gain <= 20:
                gain = None
                self.notes.append('Implausible antenna gain ignored.')
            if loss is not None and not 0 <= loss <= 60:
                loss = None
                self.notes.append('Invalid line loss ignored.')
            if power and gain is not None:
                # Land-mobile gain is interpreted relative to a dipole for ERP estimation.
                effective = power * 10 ** ((gain - (loss or 0)) / 10)
                if erp:
                    effective = min(effective, erp)
                field = math.sqrt(30 * 1.64 * effective)
                self.notes.append('Power modeled from output and gain (dBd).')
                if loss is None:
                    self.notes.append('Default feedline loss: 0 dB.')
            elif erp:
                field = math.sqrt(30 * 1.64 * erp)
                self.notes.append('Using listed ERP.')
            else:
                # Ideal short monopole over perfect ground, directivity 3; efficiency unknown.
                field = math.sqrt(90 * power * 10 ** (-(loss or 0) / 10))
                self.notes.append('Power modeled with an ideal short vertical.')
            fields.append(field)
            beam = ant.get('beamwidth_deg')
            if beam is not None and 0 < beam < 360:
                self.notes.append('Omnidirectional model.')
            if ant.get('polarization') not in (None, '', 'V'):
                self.notes.append('Vertical polarization model.')
        self.k_omni = max(fields)
        self.kw = max(powers, default=max(a.get('erp_w') or 0 for a in antennas)) / 1000
        self.label = f'{self.kw * 1000:g} W TIS' + (' (ERP)' if not powers else '')
        self.notes = list(dict.fromkeys(self.notes))

    def field(self, az, el):
        return max(1e-6, self.k_omni * abs(math.cos(math.radians(el))) * 10 ** (self.adjust_db / 20))
