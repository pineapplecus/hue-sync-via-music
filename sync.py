#!/usr/bin/env python3
"""Sync Philips Hue lights to live audio (e.g. a DJ mixer line-in).

Usage:
  python sync.py pair                 # one-time: press bridge button (also fetches the streaming key)
  python sync.py setup-area           # create an Entertainment Area (needed for streaming)
  python sync.py areas                # list Entertainment Areas
  python sync.py devices              # list audio input devices
  python sync.py groups               # list rooms/zones
  python sync.py lights               # list Hue lights
  python sync.py run [--device N] [--lights name1,name2] [--preset melissa-party|noe-default] [--groups home]
"""
import argparse
import json
import math
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

import numpy as np
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)  # bridge cert is self-signed

CONFIG = Path.home() / ".hue-music-sync.json"
SAMPLE_RATE = 44100
BLOCK = 1024  # ~23 ms per block


# ---------------------------------------------------------------- Hue bridge
def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def resolve_device(value):
    """--device may be an index, part of a device name, or omitted (then BlackHole is used if installed)."""
    import sounddevice as sd
    devs = sd.query_devices()
    if value is None:
        for i, d in enumerate(devs):
            if "blackhole" in d["name"].lower() and d["max_input_channels"] > 0:
                print(f"Using input: {d['name']}")
                return i
        return None
    if str(value).lstrip("-").isdigit():
        return int(value)
    for i, d in enumerate(devs):
        if str(value).lower() in d["name"].lower() and d["max_input_channels"] > 0:
            print(f"Using input: {d['name']}")
            return i
    sys.exit(f"No audio input matching '{value}'. See: python sync.py devices")


def discover_bridge():
    r = requests.get("https://discovery.meethue.com", timeout=10)
    r.raise_for_status()
    bridges = r.json()
    if not bridges:
        sys.exit("No Hue bridge found. Is it on the same network?")
    return bridges[0]["internalipaddress"]


def pair():
    ip = discover_bridge()
    print(f"Bridge at {ip}. Press the round link button on top of the bridge now (you have 60 seconds)...")
    for attempt in range(30):
        try:
            r = requests.post(f"https://{ip}/api", verify=False, timeout=10,
                              json={"devicetype": "hue-music-sync#party", "generateclientkey": True}).json()[0]
        except Exception as e:
            print(f"  attempt {attempt + 1}: could not reach the bridge: {e}")
            time.sleep(2)
            continue
        if "success" in r:
            cfg = {"ip": ip, "key": r["success"]["username"], "clientkey": r["success"].get("clientkey")}
            CONFIG.write_text(json.dumps(cfg))        # save first, so a later failure can't lose the pairing
            CONFIG.chmod(0o600)
            print("Button pressed, paired.")
            try:
                cfg["app_id"] = Hue(ip, cfg["key"]).app_id()
                CONFIG.write_text(json.dumps(cfg))
            except Exception as e:
                print(f"  Paired, but could not fetch the application id yet ({e}). Run `pair` is NOT needed; "
                      "`run` will fetch it.")
            print(f"Saved to {CONFIG}" + ("" if cfg.get("clientkey") else "  (no streaming key returned)"))
            return
        err = r.get("error", {})
        print(f"  attempt {attempt + 1}: bridge says: {err.get('description', r)}")   # 101 = button not pressed yet
        time.sleep(2)
    sys.exit("Timed out waiting for the link button.")


class Hue:
    def __init__(self, ip, key):
        self.ip = ip
        self.base = f"https://{ip}/clip/v2/resource"
        self.s = requests.Session()
        self.s.verify = False
        self.s.headers["hue-application-key"] = key

    def app_id(self):
        """The id the bridge expects as the DTLS PSK identity."""
        r = self.s.get(f"https://{self.ip}/auth/v1", timeout=10)
        r.raise_for_status()
        return r.headers["hue-application-id"]

    def areas(self):
        r = self.s.get(f"{self.base}/entertainment_configuration", timeout=10)
        r.raise_for_status()
        return [{"id": a["id"], "name": a["metadata"]["name"], "channels": len(a.get("channels", [])),
                 "status": a.get("status")} for a in r.json()["data"]]

    def create_area(self, name, lights):
        """Entertainment Area containing the given lights, spread around a ring (positions are arbitrary)."""
        ent = {e["owner"]["rid"]: e["id"] for e in
               self.s.get(f"{self.base}/entertainment", timeout=10).json()["data"]}
        locs = []
        usable = [l for l in lights if l["owner"] in ent]
        for i, l in enumerate(usable):
            a = 2 * math.pi * i / max(1, len(usable))
            p = {"x": round(0.9 * math.cos(a), 3), "y": round(0.9 * math.sin(a), 3), "z": 0.0}
            locs.append({"service": {"rid": ent[l["owner"]], "rtype": "entertainment"},
                         "position": p, "positions": [p]})
        if not locs:
            raise RuntimeError("none of the lights expose an entertainment service")
        body = {"metadata": {"name": name}, "configuration_type": "music",
                "stream_proxy": {"mode": "auto"}, "locations": {"service_locations": locs}}
        r = self.s.post(f"{self.base}/entertainment_configuration", json=body, timeout=10)
        if r.status_code >= 300 or r.json().get("errors"):
            raise RuntimeError(f"bridge refused the area: {r.status_code} {r.text[:300]}")
        return r.json()["data"][0]["rid"], len(locs)

    def stream_action(self, area_id, action):
        r = self.s.put(f"{self.base}/entertainment_configuration/{area_id}", json={"action": action}, timeout=10)
        if r.status_code >= 300 or r.json().get("errors"):
            raise RuntimeError(f"bridge refused '{action}': {r.status_code} {r.text[:300]}")

    def lights(self):
        r = self.s.get(f"{self.base}/light", timeout=10)
        r.raise_for_status()
        conn = self.s.get(f"{self.base}/zigbee_connectivity", timeout=10).json()["data"]
        ok = {c["owner"]["rid"]: c["status"] == "connected" for c in conn}
        return [{"id": l["id"], "name": l["metadata"]["name"], "color": "color" in l,
                 "reachable": ok.get(l["owner"]["rid"], True), "owner": l["owner"]["rid"]}
                for l in r.json()["data"]]

    def rooms(self):
        """Rooms with their grouped_light id and member device ids (one command hits every light)."""
        r = self.s.get(f"{self.base}/room", timeout=10)
        r.raise_for_status()
        out = []
        for room in r.json()["data"]:
            gl = [x["rid"] for x in room["services"] if x["rtype"] == "grouped_light"]
            if gl:
                out.append({"name": room["metadata"]["name"], "id": gl[0],
                            "devices": {c["rid"] for c in room["children"]}})
        return out

    def groups(self):
        """Every controllable group by name: rooms, zones, and 'home' (all lights, one command)."""
        out = {}
        for kind in ("room", "zone", "bridge_home"):
            r = self.s.get(f"{self.base}/{kind}", timeout=10)
            r.raise_for_status()
            for g in r.json()["data"]:
                gl = [x["rid"] for x in g["services"] if x["rtype"] == "grouped_light"]
                if gl:
                    name = "home" if kind == "bridge_home" else g["metadata"]["name"]
                    out[name.lower()] = {"name": name, "id": gl[0]}
        return out

    def set(self, light_id, xy, bri, duration_ms, kind="light"):
        bri = float(bri)
        body = {"on": {"on": bri > 0.5},
                "dimming": {"brightness": max(bri, 1.0)},
                "color": {"xy": {"x": float(xy[0]), "y": float(xy[1])}},
                "dynamics": {"duration": duration_ms}}
        r = self.s.put(f"{self.base}/{kind}/{light_id}", json=body, timeout=2)
        if r.status_code >= 300 or r.json().get("errors"):
            raise RuntimeError(f"bridge said {r.status_code}: {r.text[:200]}")


def rgb_to_xy(r, g, b):
    """sRGB (0-1) -> CIE xy using the Hue wide-gamut conversion."""
    f = lambda c: ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92
    r, g, b = f(r), f(g), f(b)
    X = r * 0.664511 + g * 0.154324 + b * 0.162028
    Y = r * 0.283881 + g * 0.668433 + b * 0.047685
    Z = r * 0.000088 + g * 0.072310 + b * 0.986039
    s = X + Y + Z
    return (0.3127, 0.3290) if s == 0 else (X / s, Y / s)


class Budget:
    """Token bucket for bridge commands. The bridge accepts commands far faster than it can
    deliver them to the lights; anything over its real throughput just queues up and the
    show runs seconds behind the music. So we drop beats we can't afford instead."""

    def __init__(self, rate, capacity):
        self.rate, self.cap = rate, capacity
        self.tokens = capacity
        self.t = time.monotonic()

    def take(self, n, keep=0.0):
        """Spend n tokens if at least n + keep are available (keep = reserve for higher priorities)."""
        now = time.monotonic()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n + keep:
            self.tokens -= n
            return True
        return False


# ------------------------------------------------------------------- presets
PRESETS = {
    "melissa-party": {
        "style": "pulse",    # soft swells and slow color blends, no quick flashes
        # pink, lavender, champagne white (sRGB)
        "palette": [(1.0, 0.0, 0.32), (0.35, 0.05, 0.9), (1.0, 0.65, 0.35)],
        "sens": 1.1,         # beat threshold = mean_flux * sens + std_weight * std
        "std_weight": 0.4,
        "min_gap": 0.15,     # seconds between beats
        "flash_gap": 0.12,   # min seconds between flashes (REST engine rate limit)
        "dip_bri": 12,       # REST engine: brightness the lights drop to after a pulse (1-100)
        "bpm": 129,
        # streaming engine:
        "base_bri": 55,      # resting brightness between pulses (1-100)
        "attack": 0.19,      # seconds to swell up to the peak (no instant flash)
        "pulse_tau": 0.52,   # how slowly a pulse fades (s)
        "color_tau": 0.16,   # how long colors take to change (s); short = each color reaches full strength
        "peak_plain": 0.85,  # swell height for an ordinary beat (0-1)
        "peak_deep": 0.95,   # ...and for a deep bass hit
    },
}
PRESETS["noe-default"] = {
    "style": "strobe",       # red pulses on every beat, white blips only on deep bass hits
    "flash_color": (1.0, 0.0, 0.0),
    "strobe_color": (1.0, 1.0, 1.0),
    "sens": 1.1,
    "std_weight": 0.4,
    "min_gap": 0.15,
    "flash_gap": 0.12,
    "base_bri": 45,          # resting red brightness (never goes dark between beats)
    "deep_ratio": 1.4,       # low-bass energy must rise to this many times the dip just before it
    "strobe_flashes": 1,     # white blips per deep hit
    "bpm": 129,              # tempo hint: beats/deep hits closer than ~0.7 beats apart are ignored
    "strobe_cooldown": 0.5,  # min seconds between white bursts
    "strobe_on": 0.0,        # extra seconds white is held beyond the command round trip
    "strobe_off": 0.07,      # seconds of bright red between white blips
    "white_max": 0.25,       # white never stays on longer than this (seconds)
    # streaming engine (no command budget, so these can be much tighter):
    "white_ms": 70,          # each white blip (hard-capped at 200 ms in code)
    "white_gap_ms": 50,      # bright red between blips (never dark)
    "stream_flashes": 2,
    "pulse_tau": 0.14,       # red pulse fade time constant (s)
}
DEFAULT_PRESET = "melissa-party"


# ------------------------------------------------------------ audio analysis
class TempoTracker:
    """Live tempo from the autocorrelation of the onset signal over the last ~8 s.
    Scores each candidate BPM by how well it repeats at 1x, 2x and 4x the beat period (the comb
    trick, so half-time and double-time patterns don't fool it), with a mild prior toward
    dance-music tempos. A new tempo is only adopted once two estimates in a row agree."""

    def __init__(self, lo=85, hi=175, prior=128, hist_s=8.0):
        self.lo, self.hi, self.prior = lo, hi, prior
        self.maxlen = int(hist_s * SAMPLE_RATE / BLOCK)
        self.hist = []
        self.bpm = None
        self.conf = 0.0
        self._cand, self._cnt, self._n = None, 0, 0

    def update(self, onset):
        self.hist = (self.hist + [float(onset)])[-self.maxlen:]
        self._n += 1
        if len(self.hist) >= int(4.0 * SAMPLE_RATE / BLOCK) and self._n % 22 == 0:   # every ~0.5 s
            self._estimate()

    def _estimate(self):
        x = np.array(self.hist)
        x = x - x.mean()
        ac = np.correlate(x, x, "full")[len(x) - 1:]
        if ac[0] <= 1e-9:
            return
        ac = ac / ac[0]
        dt = BLOCK / SAMPLE_RATE
        lags = np.arange(len(ac))
        bpms = np.arange(self.lo, self.hi + 1, dtype=float)
        score = np.zeros(len(bpms))
        for i, b in enumerate(bpms):
            lag = 60.0 / b / dt
            score[i] = (np.interp(lag, lags, ac) + 0.5 * np.interp(2 * lag, lags, ac)
                        + 0.25 * np.interp(4 * lag, lags, ac))
        prior = np.exp(-0.5 * (np.log2(bpms / self.prior) / 0.5) ** 2)
        weighted = score * (0.6 + 0.4 * prior)
        best = int(np.argmax(weighted))
        self.conf = float(weighted[best] / (np.median(np.abs(weighted)) + 1e-9))
        cand = float(bpms[best])
        if self._cand and abs(cand - self._cand) / self._cand < 0.03:
            self._cnt += 1
        else:
            self._cand, self._cnt = cand, 1
        if self._cnt >= 2 and self.conf > 1.5:
            if self.bpm is None or abs(cand - self.bpm) / self.bpm > 0.05:
                self.bpm = cand                      # tempo change (new track)
            else:
                self.bpm = 0.7 * self.bpm + 0.3 * cand   # smooth small drift



class Analyzer:
    """Tracks bass energy, spectral-flux beats, and overall loudness."""

    def __init__(self, sens=1.5, std_weight=1.0, min_gap=0.25, deep_ratio=1.4, deep_gap=0.2):
        self.deep_ratio, self.deep_gap = deep_ratio, deep_gap
        self._lp_len = 192                      # moving average ~ low-pass below ~100 Hz, 2 passes
        self._lp_tail = [np.zeros(self._lp_len - 1), np.zeros(self._lp_len - 1)]
        self._env_hist = []
        self._env_prev = 0.0
        self._env_peak = 1e-6
        self._last_deep = -1.0
        self.sub = 0.0     # 0-1 low-bass energy relative to its recent peak
        self.deep = False  # True for one block when a deep bass hit starts
        self.sens, self.std_weight, self.min_gap = sens, std_weight, min_gap
        self.window = np.hanning(BLOCK)
        self.freqs = np.fft.rfftfreq(BLOCK, 1 / SAMPLE_RATE)
        self.prev = None
        self.flux_hist = []
        self.bass_peak = 1e-6
        self.level = 0.0   # 0-1 smoothed loudness driving brightness
        self.bass = 0.0    # 0-1 bass
        self.high = 0.0    # 0-1 treble
        self.beat = False
        self.peak = 0.0
        self.strength = 0.0  # last beat's flux relative to the threshold
        self.last_beat = -1.0
        self.clock = 0.0   # audio time in seconds (driven by samples, not wall time)
        self.tempo = TempoTracker()

    def process(self, block):
        self.peak = float(np.abs(block).max())
        mag = np.abs(np.fft.rfft(block * self.window))
        band = lambda lo, hi: mag[(self.freqs >= lo) & (self.freqs < hi)].mean()
        bass, high = band(30, 150), band(2000, 10000)

        # Deep bass: low-pass the raw signal (time domain, so no FFT-bin or windowing delay),
        # then flag a sudden jump in its energy versus the last second.
        y = block
        for k in range(2):
            x = np.concatenate([self._lp_tail[k], y])
            self._lp_tail[k] = x[-(self._lp_len - 1):]
            y = np.convolve(x, np.ones(self._lp_len) / self._lp_len, mode="valid")
        env = float(np.sqrt(np.mean(y * y)))
        # Compare with the quietest point of the last ~190 ms, not a 1 s average: sustained
        # bass keeps the average high, but each hit still jumps up from the dip before it.
        lo_env = min(self._env_hist) if self._env_hist else env
        self._env_hist = (self._env_hist + [env])[-8:]
        self._env_peak = max(self._env_peak * 0.9995, env)
        self.sub = min(env / self._env_peak, 1.0)
        t = self.clock + BLOCK / SAMPLE_RATE
        self.deep = bool(env > lo_env * self.deep_ratio and env > self._env_prev * 1.05
                         and env > 0.5 * self._env_peak and self._env_peak > 0.003
                         and env > 0.35 * float(np.sqrt(np.mean(block * block)))  # bass must dominate the hit
                         and t - self._last_deep > self.deep_gap)
        if self.deep:
            self._last_deep = t
        self._env_prev = env

        self.bass_peak = max(self.bass_peak * 0.9995, bass)   # slow-decay auto-gain
        self.bass = min(bass / self.bass_peak, 1.0)
        self.high = min(high / (self.bass_peak * 0.15), 1.0)
        self.level = max(self.bass, self.level * 0.85)        # fast attack, smooth release

        # Spectral flux over low/mid bands -> beat when it jumps over its recent average.
        low = mag[self.freqs < 4000]
        flux = 0.0 if self.prev is None else np.maximum(low - self.prev, 0).sum()
        self.prev = low
        self.tempo.update(flux)
        self.flux_hist = (self.flux_hist + [flux])[-43:]      # ~1 second
        self.clock += BLOCK / SAMPLE_RATE
        now = self.clock
        thresh = np.mean(self.flux_hist) * self.sens + np.std(self.flux_hist) * self.std_weight
        self.beat = bool(flux > thresh and now - self.last_beat > self.min_gap
                         and self.bass_peak > 0.5)  # ignore silence
        if self.beat:
            self.last_beat = now
            self.strength = float(flux / max(thresh, 1e-9))


# ------------------------------------------------------- streaming (Entertainment API)
HELPER_SRC = Path(__file__).with_name("hue_stream.swift")
HELPER_BIN = Path(__file__).with_name("bin") / "hue_stream"


def ensure_helper():
    if not HELPER_BIN.exists() or HELPER_BIN.stat().st_mtime < HELPER_SRC.stat().st_mtime:
        print("Building the streaming helper (one time)...")
        HELPER_BIN.parent.mkdir(exist_ok=True)
        r = subprocess.run(["swiftc", "-O", "-swift-version", "5", str(HELPER_SRC), "-o", str(HELPER_BIN)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not HELPER_BIN.exists():
            sys.exit("Could not build the helper (need Xcode command line tools):\n" + r.stderr[-1500:])
    return str(HELPER_BIN)


class Show:
    """Turns audio events into one RGB frame per channel, 50x a second. No commands, no queue:
    each frame is just the show's state at that instant, so it can never fall behind the music."""

    def __init__(self, pr, n_channels, period, every=1):
        self.pr, self.n = pr, n_channels
        self.every, self.plain_count = max(1, every), 0
        self.strobe_style = pr.get("style") == "strobe"
        self.palette = pr.get("palette") or [pr.get("flash_color", (1.0, 0.0, 0.0))]
        self.base = (pr.get("base_bri", 30)) / 100.0
        self.tau = pr.get("pulse_tau", 0.14)               # how fast a pulse fades (s)
        self.attack = pr.get("attack", 0.0)                # swell-in time; 0 = instant flash
        self.color_tau = pr.get("color_tau", 0.0)          # 0 = colors snap, >0 = blend
        self.peak_plain = pr.get("peak_plain", 1.0)
        self.peak_deep = pr.get("peak_deep", 1.0)
        self.white_s = min(pr.get("white_ms", 70) / 1000.0, 0.2)        # hard cap: always < 0.25 s
        self.gap_s = pr.get("white_gap_ms", 50) / 1000.0
        self.blips = pr.get("stream_flashes", pr.get("strobe_flashes", 1))
        self.cooldown = pr.get("strobe_cooldown", 0.5)
        self.flash_gap = pr.get("flash_gap", 0.1)
        self.plain_gap = max(self.flash_gap, 0.8 * period)
        self.flash_t = -9.0
        self.flash_amp = 1.0
        self.last_strobe = -9.0
        self.strobe_t = -9.0
        self.idx = 0
        self.cur = None            # current (blended) color per channel
        self.last_t = None

    def set_period(self, period):
        self.plain_gap = max(self.flash_gap, 0.8 * period)

    def trigger(self, now, beat, deep, idx):
        self.idx = idx
        if beat and not deep:
            self.plain_count += 1
            if self.plain_count % self.every:        # --every N: skip the beats in between
                return
        if deep and self.strobe_style and now - self.last_strobe >= self.cooldown:
            self.last_strobe = self.strobe_t = self.flash_t = now
            self.flash_amp = 1.0
        elif deep or (beat and now - self.flash_t >= self.plain_gap):
            self.flash_t = now
            self.flash_amp = self.peak_deep if deep else self.peak_plain

    def white_now(self, now):
        t = now - self.strobe_t
        span = self.white_s + self.gap_s
        return 0 <= t < self.blips * span and (t % span) < self.white_s

    def envelope(self, now):
        """0-1 pulse shape: smooth swell over `attack`, then exponential fade."""
        t = max(0.0, now - self.flash_t)
        if t < self.attack:
            x = t / self.attack
            return x * x * (3 - 2 * x)                     # smoothstep
        return math.exp(-(t - self.attack) / self.tau)

    def frame(self, now, level):
        if self.white_now(now):
            return bytes([255, 255, 255] * self.n)
        swell = self.base + (1 - self.base) * 0.35 * level if self.strobe_style else self.base
        bri = min(1.0, swell + (self.flash_amp - swell) * self.envelope(now)) if self.flash_amp > swell else swell
        dt = 0.02 if self.last_t is None else max(0.0, now - self.last_t)
        self.last_t = now
        k = 1.0 if self.color_tau <= 0 else 1.0 - math.exp(-dt / self.color_tau)
        if self.cur is None:
            self.cur = [list(self.palette[j % len(self.palette)]) for j in range(self.n)]
        out = bytearray()
        for j in range(self.n):
            tgt = self.palette[(self.idx + j) % len(self.palette)]
            c = self.cur[j]
            for i in range(3):
                c[i] += (tgt[i] - c[i]) * k
            out += bytes([int(255 * c[0] * bri), int(255 * c[1] * bri), int(255 * c[2] * bri)])
        return bytes(out)


def predicted_beat(state, lock, period, lead, now, last_fire):
    """True when it's time to start the next pulse: `lead` seconds before the next expected beat,
    locked to the phase of the most recent detected beats. Only when the recent beats are steady."""
    with lock:
        bt = list(state["beat_times"])
    if len(bt) < 5 or now - bt[-1] > 2.0:
        return False
    d = np.diff(bt)
    if abs(float(np.median(d)) - period) > 0.15 * period or np.median(np.abs(d - period)) > 0.15 * period:
        return False                                               # beats don't match the tempo: stay reactive
    n = max(1, int((last_fire + 0.5 * period + lead - bt[-1]) // period) + 1)
    return now >= bt[-1] + n * period - lead


def pick_area(hue, name):
    areas = hue.areas()
    if name:
        for a in areas:
            if a["name"].lower() == name.lower():
                return a
        sys.exit(f"No area named '{name}'. Existing: {', '.join(a['name'] for a in areas) or 'none'}")
    if not areas:
        sys.exit("No Entertainment Area yet. Run: python sync.py setup-area  (or create one in the Hue app)")
    return areas[0]


def start_stream(hue, cfg, area, n):
    """Start the area's stream on the bridge and launch the DTLS helper. Returns the helper process."""
    helper = ensure_helper()
    try:                                   # an unclean earlier exit can leave the area "active"; start would be refused
        hue.stream_action(area["id"], "stop")
        time.sleep(0.3)
    except Exception:
        pass
    hue.stream_action(area["id"], "start")
    # Own session: Ctrl+C reaches only Python, which then sends the final frames itself.
    # (The helper still exits on its own when our end of the pipe closes.)
    proc = subprocess.Popen([helper, cfg["ip"], "2100", cfg["app_id"], cfg["clientkey"], area["id"], str(n), "50"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False, start_new_session=True)
    if proc.stdout.readline().decode().strip() != "READY":
        raise RuntimeError("streaming helper did not connect (see the error above)")
    return proc


def stop_stream(hue, area, proc, final_frame):
    try:
        if proc and proc.poll() is None:
            try:
                for _ in range(10):
                    proc.stdin.write(final_frame)
                    proc.stdin.flush()
                    time.sleep(0.03)
                proc.stdin.close()
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except KeyboardInterrupt:              # a second Ctrl+C must not skip the bridge-side stop
        if proc:
            proc.kill()
    try:
        hue.stream_action(area["id"], "stop")
    except (Exception, KeyboardInterrupt) as e:
        print("Could not stop the stream:", e, file=sys.stderr)


def stream_test(args):
    cfg = load_config()
    if not cfg.get("clientkey"):
        sys.exit("No streaming key. Run: python sync.py pair   (press the bridge button)")
    hue = Hue(cfg["ip"], cfg["key"])
    area = pick_area(hue, args.area)
    n = area["channels"]
    warm = bytes([255, 170, 90] * n)
    proc = None
    try:
        proc = start_stream(hue, cfg, area, n)
        print(f"Connected. Area '{area['name']}', {n} channels. Red, green, blue, then white pulses (~8 s)...")
        t0 = time.monotonic()
        while (t := time.monotonic() - t0) < 8:
            if t < 6:
                col = [(255, 0, 0), (0, 255, 0), (0, 0, 255)][int(t / 2)]
            else:
                col = (255, 255, 255) if int(t * 5) % 2 == 0 else (255, 0, 0)
            proc.stdin.write(bytes(col * n))
            proc.stdin.flush()
            time.sleep(0.02)
        print("Done. If the lights followed that pattern, streaming works.")
    except KeyboardInterrupt:
        pass
    finally:
        stop_stream(hue, area, proc, warm)


def run_stream(args, cfg, hue, pr, period, an, state, lock, callback):
    import sounddevice as sd

    area = pick_area(hue, args.area)
    n = area["channels"]
    if n < 1:
        sys.exit(f"Area '{area['name']}' has no channels. Add lights to it in the Hue app.")
    print(f"Streaming to area '{area['name']}' ({n} channels) at 50 fps")
    show = Show(pr, n, period, args.every)
    live_bpm = None
    last_fire = 0.0
    warm = bytes([255, 170, 90] * n)
    proc = None
    try:
        proc = start_stream(hue, cfg, area, n)
        proc.stdin.write(warm)
        proc.stdin.flush()
        with sd.InputStream(device=args.device, channels=1, samplerate=SAMPLE_RATE,
                            blocksize=BLOCK, latency="low", callback=callback):
            print("Listening. Ctrl+C to stop.")
            tick = 0
            nxt = time.monotonic()
            while True:
                now = time.monotonic()
                with lock:
                    pb, pd = state["pend_beat"], state["pend_deep"]
                    state["pend_beat"] = state["pend_deep"] = False
                    idx, lvl = state["beats"], an.level
                bpm = an.tempo.bpm if not args.no_track else None
                if bpm and (live_bpm is None or abs(bpm - live_bpm) > 0.5):
                    live_bpm = bpm
                    p = 60.0 / bpm
                    show.set_period(p)                               # beat filter follows the live tempo
                    an.min_gap = max(pr["min_gap"], 0.7 * p)
                    an.deep_gap = max(0.2, 0.7 * p)
                if args.predict and live_bpm and not pb:
                    pb = predicted_beat(state, lock, 60.0 / live_bpm, args.lead / 1000.0, now, last_fire)
                    if pb:
                        last_fire = now
                elif args.predict and live_bpm and pb:
                    pb = False                                       # predicted pulses replace detected ones
                if pb or pd:
                    state["strobes"] += show.strobe_style and pd and now - show.last_strobe >= show.cooldown
                    show.trigger(now, pb, pd, idx)
                try:
                    proc.stdin.write(show.frame(now, lvl))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    print("\nStreaming helper stopped unexpectedly.")
                    break
                tick += 1
                if tick % 5 == 0:
                    bpm_txt = f"{live_bpm:.0f}" if live_bpm else "--"
                    print("\r\033[K" + f"level {'#' * int(an.level * 20):<20} peak {an.peak:.2f} "
                          f"beats {state['beats']} deep {state['deeps']} white {state['strobes']} bpm {bpm_txt}",
                          end="", flush=True)
                nxt += 0.02
                time.sleep(max(0.0, nxt - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopped. Resetting lights to warm white...")
        stop_stream(hue, area, proc, warm)


# --------------------------------------------------------------------- main
def run(args):
    import sounddevice as sd

    cfg = load_config()
    if not cfg:
        sys.exit("Not paired. Run: python sync.py pair")
    hue = Hue(cfg["ip"], cfg["key"])
    if cfg.get("clientkey") and not cfg.get("app_id"):
        try:
            cfg["app_id"] = hue.app_id()
            CONFIG.write_text(json.dumps(cfg))
        except Exception as e:
            print(f"Could not fetch the application id: {e}")
    use_stream = args.engine != "rest" and bool(cfg.get("clientkey") and cfg.get("app_id"))
    if args.engine == "stream" and not use_stream:
        sys.exit("Streaming needs the entertainment key. Run: python sync.py pair   (press the bridge button)")
    if args.engine == "auto" and not use_stream:
        print("Note: no streaming key saved, using the slower command mode. Run `pair` again for streaming.")
    lights = [l for l in hue.lights() if l["color"]]
    for l in lights:
        if not l["reachable"]:
            print(f"Skipping unreachable light: {l['name']} (check power)")
    lights = [l for l in lights if l["reachable"]]
    if args.lights:
        wanted = {n.strip().lower() for n in args.lights.split(",")}
        lights = [l for l in lights if l["name"].lower() in wanted]
    if not lights:
        sys.exit("No matching color lights found. See: python sync.py lights")
    print("Syncing:", ", ".join(l["name"] for l in lights))

    if use_stream:
        targets = []
    elif args.groups:
        # Explicit groups: "home" = every light on the bridge, or room/zone names (e.g. upstairs,downstairs).
        avail = hue.groups()
        names = [n.strip().lower() for n in args.groups.split(",")]
        missing = [n for n in names if n not in avail]
        if missing:
            sys.exit(f"Unknown group(s): {', '.join(missing)}. Available: {', '.join(g['name'] for g in avail.values())}")
        targets = [(avail[n]["id"], "grouped_light") for n in names]
        print("Group mode. Groups:", ", ".join(avail[n]["name"] for n in names))
    elif args.mode == "group":
        chosen = {l["owner"] for l in lights}
        rooms = [r for r in hue.rooms() if r["devices"] & chosen]
        targets = [(r["id"], "grouped_light") for r in rooms]
        print("Group mode (all lights flash together). Rooms:", ", ".join(r["name"] for r in rooms))
        if args.lights:
            print("  Note: --lights is ignored in group mode; whole rooms flash. Use --mode lights to pick lights.")
    else:
        targets = [(l["id"], "light") for l in lights]
        print("Per-light mode (lights update one after another, not in sync)")

    pr = dict(PRESETS[args.preset])
    if getattr(args, "blend", None) is not None:
        pr["color_tau"] = args.blend        # --blend: slow color-to-color blending
    strobe_style = pr.get("style") == "strobe"
    palette = [rgb_to_xy(*c) for c in pr.get("palette", [pr.get("flash_color", (1, 0, 0))])]
    bpm = args.bpm if args.bpm is not None else pr.get("bpm", 0)
    period = 60.0 / bpm if bpm else 0.0
    if period:
        print(f"Tempo hint: {bpm:g} BPM (ignoring beats closer than {0.7 * period:.2f}s)")
    an = Analyzer(pr["sens"], pr["std_weight"], max(pr["min_gap"], 0.7 * period),
                  args.deep if args.deep is not None else pr.get("deep_ratio", 1.4),
                  max(0.2, 0.7 * period))
    state = {"beats": 0, "deeps": 0, "sent": 0, "strobes": 0, "rtt": 0.1, "errors": 0, "last_err": "", "dropped": 0,
             "pend_beat": False, "pend_deep": False, "beat_times": []}
    lock = threading.Lock()
    wake = threading.Event()

    def callback(indata, frames, t, status):
        with lock:
            an.process(indata[:, 0].astype(np.float32))
            if an.beat:
                state["beats"] += 1
                state["beat_times"] = (state["beat_times"] + [time.monotonic()])[-9:]
                state["pend_beat"] = True
                wake.set()
            if an.deep:
                state["deeps"] += 1
                state["pend_deep"] = True
                wake.set()

    if use_stream:
        return run_stream(args, cfg, hue, pr, period, an, state, lock, callback)

    n_t = len(targets)
    white_cost = n_t * (2 * pr.get("strobe_flashes", 1) + 1)   # white+red per blip, plus the dip
    budget = Budget(args.max_rate, max(white_cost, 2 * n_t))
    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=max(1, len(targets)))

    def burst(color_for, bri, duration):
        """Update every target at once; color_for(j) picks target j's color."""
        def one(j):
            try:
                t0 = time.monotonic()
                hue.set(targets[j][0], color_for(j), bri, duration, targets[j][1])
                state["rtt"] = 0.8 * state["rtt"] + 0.2 * (time.monotonic() - t0)
            except Exception as e:
                state["errors"] += 1
                state["last_err"] = str(e).replace("\n", " ")[:90]
        list(pool.map(one, range(len(targets))))
        state["sent"] += 1

    def sender():
        last_flash = last_strobe = 0.0
        dip_at = None
        idx = 0
        strobe_xy = rgb_to_xy(*pr.get("strobe_color", (1, 1, 1)))
        while not stop.is_set():
            woke = wake.wait(timeout=0.01)
            now = time.monotonic()
            pb = pd = False
            if woke:
                with lock:
                    wake.clear()
                    pb, pd = state["pend_beat"], state["pend_deep"]
                    state["pend_beat"] = state["pend_deep"] = False
                    idx = state["beats"]
            cycle = lambda j: palette[(idx + j) % len(palette)]
            # Deep hits always get a chance; ordinary beats (hi-hats etc.) are spaced out so they
            # can't eat the whole command budget, and must leave a reserve for the next deep hit.
            plain_gap = max(pr["flash_gap"], 0.8 * period, 2 * n_t / args.max_rate)
            flash_wanted = pd or (pb and now - last_flash >= plain_gap)
            white_ok = pd and strobe_style and now - last_strobe >= pr["strobe_cooldown"]
            if white_ok and not budget.take(white_cost):
                white_ok = False
                state["dropped"] += 1
            if white_ok:
                last_strobe = last_flash = now
                state["strobes"] += 1
                for _ in range(pr["strobe_flashes"]):      # white blip, straight back to red
                    t0 = time.monotonic()
                    try:
                        burst(lambda j: strobe_xy, 100, 0)
                    finally:
                        # Red always follows immediately; hold white no longer than white_max.
                        held = time.monotonic() - t0
                        stop.wait(max(0.0, min(pr["strobe_on"], pr["white_max"] - held - 0.05)))
                        burst(cycle, 100, 0)
                    stop.wait(pr["strobe_off"])
                dip_at = time.monotonic() + 0.08
            elif flash_wanted and budget.take(2 * n_t, keep=0 if pd else 2 * n_t):
                last_flash = now
                burst(cycle, 100, 0 if strobe_style else 40)   # red/palette flash, instantly
                dip_at = time.monotonic() + 0.08
            else:
                if flash_wanted:
                    state["dropped"] += 1                       # can't afford it; skip, don't queue
                if dip_at and now >= dip_at and not wake.is_set():
                    dip_at = None
                    with lock:
                        lvl = an.level
                    if strobe_style:   # settle to a solid red that swells a little with the bass
                        bri = pr["base_bri"] + (100 - pr["base_bri"]) * 0.35 * lvl
                    else:
                        bri = pr["dip_bri"] + (args.min_bri - pr["dip_bri"]) * lvl
                    burst(cycle, bri, 200)  # fade back down

    sender_thread = threading.Thread(target=sender, daemon=True)
    sender_thread.start()
    try:
        with sd.InputStream(device=args.device, channels=1, samplerate=SAMPLE_RATE,
                            blocksize=BLOCK, latency="low", callback=callback):
            print("Listening. Ctrl+C to stop.")
            shown_err = ""
            while True:
                time.sleep(0.2)
                line = (f"level {'#' * int(an.level * 20):<20} peak {an.peak:.2f} beats {state['beats']} "
                        f"deep {state['deeps']} white {state['strobes']} dropped {state['dropped']} "
                        f"hue {state['rtt'] * 1000:.0f}ms errors {state['errors']}")
                print("\r\033[K" + line, end="", flush=True)
                if state["errors"] and state["last_err"] != shown_err:
                    shown_err = state["last_err"]
                    print(f"\n  last Hue error: {shown_err}")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sender_thread.join(timeout=3)          # let any in-flight flash finish, send nothing new
        pool.shutdown(wait=True, cancel_futures=True)
        print("\nStopped. Resetting lights to warm white...")
        warm = (0.4573, 0.4100)                # ~2700K warm white
        for tid, kind in targets:
            try:
                hue.set(tid, warm, 60, 400, kind)
            except Exception as e:
                print("Could not reset:", e, file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pair")
    sub.add_parser("devices")
    sub.add_parser("lights")
    sub.add_parser("areas", help="list Entertainment Areas")
    st = sub.add_parser("stream-test", help="stream a red/green/blue/white test pattern (no audio)")
    st.add_argument("--area")
    sub.add_parser("setup-area", help="create an Entertainment Area with all your color lights")
    sub.add_parser("groups", help="list rooms/zones you can target with --groups")
    m = sub.add_parser("meter", help="print audio levels for 5 seconds (no lights)")
    m.add_argument("--device", help="input device index or name (default: BlackHole if installed)")
    rec = sub.add_parser("record", help="record N seconds, save a WAV, print a beat/deep-bass timeline")
    rec.add_argument("--device", help="input device index or name (default: BlackHole if installed)")
    rec.add_argument("--seconds", type=float, default=10)
    rec.add_argument("--out", default="capture.wav")
    sub.add_parser("test", help="flash every color light red then blue")
    r = sub.add_parser("run")
    r.add_argument("--device", help="audio input: device index or part of its name (default: BlackHole if installed; see 'devices')")
    r.add_argument("--lights", help="comma-separated light names (default: all color lights)")
    r.add_argument("--engine", choices=["auto", "stream", "rest"], default="auto",
                   help="stream: Entertainment API, 50 fps, no queue (needs `pair` + an area); "
                        "rest: one HTTP command per change (laggy); auto: stream if possible")
    r.add_argument("--area", help="Entertainment Area name (default: the first one)")
    r.add_argument("--max-rate", type=float, default=5,
                   help="max Hue commands/sec (default 5). The bridge queues anything above what it can "
                        "deliver, making the lights lag the music; lower this if lights lag or keep going after you quit")
    r.add_argument("--bpm", type=float, help="tempo hint (preset default 129; 0 = off). Beats faster than "
                                             "~0.7 beats apart (hi-hats, ghost notes) are ignored")
    r.add_argument("--blend", type=float, nargs="?", const=0.8, default=None, metavar="SECONDS",
                   help="blend slowly from color to color (default 0.8 s when given with no value); "
                        "without it each color reaches full strength quickly (streaming engine)")
    r.add_argument("--no-track", action="store_true", help="don't estimate tempo live (use --bpm / the preset hint only)")
    r.add_argument("--every", type=int, default=1, help="pulse on every Nth beat (2 = calmer); deep hits always pulse")
    r.add_argument("--predict", action="store_true",
                   help="streaming only: when the beat is steady, start pulses slightly early (see --lead)")
    r.add_argument("--lead", type=float, default=60, help="ms to fire ahead of the predicted beat (default 60)")
    r.add_argument("--deep", type=float, help="deep-bass sensitivity for white flashes: rise ratio, default 1.4 (lower = more flashes, try 1.2-2.0)")
    r.add_argument("--groups", help="comma-separated room/zone names, or 'home' for every light "
                                    "(e.g. --groups upstairs,downstairs). See: python sync.py groups")
    r.add_argument("--mode", choices=["group", "lights"], default="group",
                   help="group: one command per room, all lights in sync (default); lights: per-light")
    r.add_argument("--preset", default=DEFAULT_PRESET, choices=PRESETS, help=f"(default {DEFAULT_PRESET})")
    r.add_argument("--min-bri", type=float, default=30, help="resting brightness between beats 1-100 (default 30)")
    args = p.parse_args()
    if hasattr(args, "device"):
        args.device = resolve_device(args.device)

    if args.cmd == "pair":
        pair()
    elif args.cmd == "devices":
        import sounddevice as sd
        print(sd.query_devices())
    elif args.cmd == "stream-test":
        stream_test(args)
    elif args.cmd == "areas":
        cfg = load_config() or sys.exit("Not paired. Run: python sync.py pair")
        for a in Hue(cfg["ip"], cfg["key"]).areas():
            print(f"{a['name']}  ({a['channels']} channels)")
    elif args.cmd == "setup-area":
        cfg = load_config() or sys.exit("Not paired. Run: python sync.py pair")
        hue = Hue(cfg["ip"], cfg["key"])
        lights = [l for l in hue.lights() if l["color"] and l["reachable"]]
        rid, n = hue.create_area("Party Sync", lights)
        print(f"Created Entertainment Area 'Party Sync' with {n} lights ({rid}).")
    elif args.cmd == "groups":
        cfg = load_config() or sys.exit("Not paired. Run: python sync.py pair")
        for g in Hue(cfg["ip"], cfg["key"]).groups().values():
            print(g["name"])
    elif args.cmd == "lights":
        cfg = load_config() or sys.exit("Not paired. Run: python sync.py pair")
        for l in Hue(cfg["ip"], cfg["key"]).lights():
            print(f"{l['name']}  ({'color' if l['color'] else 'white only'}"
                  f"{'' if l['reachable'] else ', UNREACHABLE'})")
    elif args.cmd == "meter":
        import sounddevice as sd
        an = Analyzer()
        beats = [0]
        deeps = [0]
        def cb(indata, frames, t, status):
            an.process(indata[:, 0].astype(np.float32))
            beats[0] += an.beat
            deeps[0] += an.deep
        info = sd.query_devices(args.device if args.device is not None else sd.default.device[0])
        print("Using input:", info["name"])
        with sd.InputStream(device=args.device, channels=1, samplerate=SAMPLE_RATE,
                            blocksize=BLOCK, latency="low", callback=cb):
            for sec in range(1, 6):
                time.sleep(1)
                print(f"{sec}s  peak={an.peak:.4f}  bass={an.bass:.2f}  level={an.level:.2f}  beats so far={beats[0]}  sub={an.sub:.2f}  deep hits={deeps[0]}")
        if an.bass_peak <= 1e-3:
            print("\nNo signal. Audio isn't reaching this input (routing or microphone permission).")
    elif args.cmd == "record":
        import sounddevice as sd, wave
        print(f"Recording {args.seconds:.0f}s... play your song now.")
        rec = sd.rec(int(args.seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1,
                     device=args.device, latency="low")
        sd.wait()
        x = rec[:, 0]
        with wave.open(args.out, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
        print(f"Saved {args.out}  peak={np.abs(x).max():.4f}")
        an = Analyzer(1.1, 0.4, 0.15)
        print("time  low-bass level                beat deep")
        for i in range(0, len(x) - BLOCK, BLOCK):
            an.process(x[i:i + BLOCK].astype(np.float32))
            if an.beat or an.deep or (i // BLOCK) % 4 == 0:
                print(f"{i / SAMPLE_RATE:5.2f} {'#' * int(an.sub * 30):<30}  {'BEAT' if an.beat else '    '} {'DEEP' if an.deep else ''}")
    elif args.cmd == "test":
        cfg = load_config() or sys.exit("Not paired. Run: python sync.py pair")
        hue = Hue(cfg["ip"], cfg["key"])
        for name, rgb in (("red", (1, 0, 0)), ("blue", (0, 0, 1)), ("green", (0, 1, 0))):
            print(name)
            for l in hue.lights():
                if l["color"]:
                    try:
                        hue.set(l["id"], rgb_to_xy(*rgb), 100, 200)
                    except Exception as e:
                        print(f"  {l['name']}: {e}"[:160])
            time.sleep(1.5)
    else:
        run(args)


if __name__ == "__main__":
    main()
