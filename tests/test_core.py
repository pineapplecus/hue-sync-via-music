"""Offline checks for the audio analysis and the light show (no bridge, no audio device needed).

    .venv/bin/python tests/test_core.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402

SR = sync.SAMPLE_RATE


def feed(an, sig):
    deep, beats = [], []
    for i in range(0, len(sig) - sync.BLOCK, sync.BLOCK):
        an.process(sig[i:i + sync.BLOCK].astype(np.float32))
        if an.deep:
            deep.append(i / SR)
        if an.beat:
            beats.append(i / SR)
    return beats, deep


def house(bpm, secs=20, hats=True, synco=False):
    """Kick on every beat, optional off-beat hats and a syncopated bass note."""
    t = np.arange(SR * secs) / SR
    per, sig = 60 / bpm, np.zeros(SR * secs)
    for b in np.arange(0, secs, per):
        m = (t >= b) & (t < b + 0.25)
        sig[m] += np.sin(2 * np.pi * 55 * (t[m] - b)) * np.exp(-(t[m] - b) * 14) * 0.5
        if hats:
            m = (t >= b + per / 2) & (t < b + per / 2 + 0.03)
            sig[m] += np.random.default_rng(int(b * 100)).standard_normal(m.sum()) * 0.12
        if synco:
            h = b + per * 0.75
            m = (t >= h) & (t < h + 0.2)
            sig[m] += np.sin(2 * np.pi * 55 * (t[m] - h)) * np.exp(-(t[m] - h) * 14) * 0.3
    return sig


def test_deep_bass_found_and_no_false_positives():
    _, deep = feed(sync.Analyzer(), house(120, secs=10, hats=False))
    assert len(deep) >= 15, f"expected ~20 kicks, found {len(deep)}"
    t = np.arange(SR * 8) / SR
    rng = np.random.default_rng(1)
    no_bass = 0.2 * np.sin(2 * np.pi * 1000 * t) * (np.sin(2 * np.pi * 3 * t) > 0) + 0.05 * rng.standard_normal(len(t))
    assert len(feed(sync.Analyzer(), no_bass)[1]) == 0, "mids/hats must not count as deep bass"
    assert len(feed(sync.Analyzer(), 0.3 * np.sin(2 * np.pi * 50 * t))[1]) == 0, "a steady bass tone is not a hit"


def test_tempo_tracker():
    for bpm in (120, 129, 140):
        an = sync.Analyzer()
        feed(an, house(bpm))
        assert an.tempo.bpm and abs(an.tempo.bpm - bpm) <= 2, f"{bpm} BPM read as {an.tempo.bpm}"
    an = sync.Analyzer()
    feed(an, house(129, synco=True))
    assert an.tempo.bpm and abs(an.tempo.bpm - 129) <= 2, f"syncopated 129 read as {an.tempo.bpm}"
    an = sync.Analyzer()
    feed(an, np.concatenate([house(124, 12), house(132, 16)]))
    assert abs(an.tempo.bpm - 132) <= 2, f"tempo change not followed: {an.tempo.bpm}"


def test_show_white_never_exceeds_cap_and_frames_are_well_formed():
    show = sync.Show(sync.PRESETS["noe-default"], 9, 60 / 129)
    longest, run, t = 0.0, 0.0, 0.0
    for step in range(1500):                     # 30 s at 50 fps, a deep hit every 0.4 s
        t = step * 0.02
        if step % 20 == 0:
            show.trigger(t, True, True, step)
        f = show.frame(t, 0.5)
        assert len(f) == 27
        if f[:3] == bytes([255, 255, 255]):
            run += 0.02
            longest = max(longest, run)
        else:
            run = 0.0
    assert 0 < longest < 0.25, f"white lasted {longest:.2f}s"


def test_pulse_is_smooth_and_colors_hold():
    show = sync.Show(sync.PRESETS["melissa-party"], 3, 60 / 129)
    prev, worst = None, 0
    for step in range(750):
        t = step * 0.02
        if step % 23 == 0:
            show.trigger(t, True, False, step // 23)
        b = max(show.frame(t, 0.3)[:3])
        if prev is not None:
            worst = max(worst, abs(b - prev))
        prev = b
    assert worst < 90, f"brightness jumped {worst}/255 in one frame (should swell, not flash)"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"{len(tests)} passed")
