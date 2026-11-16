#!/usr/bin/env python3
"""Delivery: how a line was actually said — by the TTS, and by you.

The same few numbers for both reads, so the gap between what the TTS predicted
and what you did is a subtraction:

  pace      syllables a second over the whole line (rate) and with the pauses
            taken out (artic), and the pauses themselves
  pitch     how far the voice moves: the spread (st_sd) and the range (p10-p90,
            st_range) in semitones, and where the line ends up (end: + rises
            like a question, - falls like a statement)
  emphasis  the loud peaks (emph) and where in the line they land (emph_at,
            0-1), and how much the level moves overall (loud_sd)

Every take appends one row to data/delivery.jsonl — all reels, one file —
with the pitch and loudness contours too, so the gap can be studied, and a
model tuned on it, without the wavs.

Stdlib only, like the server: one ffmpeg decode to 8kHz, then a normalized
autocorrelation per 20ms frame. ponytail: no pitch library, so a ~0.3s/line
pure-python tracker; swap in pyin/crepe if octave errors start to matter.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from array import array
from operator import mul
from pathlib import Path

import pipeline

SR, HOP, WIN = 8000, 160, 320       # 20ms frames over a 40ms window
LO, HI = 70, 400                     # f0 search range, Hz
FPS = SR // HOP
DATA = Path(os.environ.get("REELDECK_DATA") or Path(__file__).resolve().parent) / "data" / "delivery.jsonl"
_LOCK = threading.Lock()


def syllables(text: str) -> int:
    n = 0
    for w in re.findall(r"[A-Za-z']+|\d+", text):
        if w.isdigit():
            n += 2 * len(w)    # ponytail: "37" as "thir-ty se-ven"; scripts spell numbers out anyway
            continue
        w = w.lower()
        g = len(re.findall(r"[aeiouy]+", w))
        if w.endswith("e") and not w.endswith(("le", "ee")) and g > 1:
            g -= 1             # the silent e of "disguise"
        n += max(1, g)         # "D" of "I.D." is a syllable too
    return n


def pcm(path: Path) -> array:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-af",
                          "highpass=f=60,lowpass=f=1000", "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return array("h", raw)


def _f0(x: array, i: int) -> float | None:
    """Pitch of the window at sample i, Hz, or None when it is not voiced."""
    a = x[i:i + WIN]
    e0 = sum(map(mul, a, a))
    lo, hi = SR // HI, SR // LO
    seg = x[i + lo:i + hi + WIN]
    eL = sum(map(mul, seg[:WIN], seg[:WIN]))
    r = []
    for k in range(hi - lo + 1):
        if k:
            eL += seg[k + WIN - 1] ** 2 - seg[k - 1] ** 2
        r.append(sum(map(mul, a, seg[k:k + WIN])) / math.sqrt(e0 * eL) if e0 and eL > 0 else 0.0)
    best = max(r)
    if best < 0.6:
        return None
    # every multiple of the period peaks too, and the double often scores a hair
    # higher: take the first peak that is nearly as good (no octave drop)
    k = next(k for k in range(len(r)) if r[k] >= 0.9 * best
             and (k == 0 or r[k] >= r[k - 1]) and (k + 1 == len(r) or r[k] >= r[k + 1]))
    d = 0.0
    if 0 < k < len(r) - 1 and (den := r[k - 1] - 2 * r[k] + r[k + 1]):
        d = 0.5 * (r[k - 1] - r[k + 1]) / den
    return SR / (lo + k + d)


def _pct(v: list[float], q: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(q * len(s)))]


def _sd(v: list[float]) -> float:
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))


def measure(path: Path, text: str) -> dict | None:
    """The delivery numbers of one read; None if no voice is in it."""
    x = pcm(path)
    n = max(0, (len(x) - WIN - SR // LO) // HOP)
    env = []
    for f in range(n):
        a = x[f * HOP:f * HOP + WIN]
        env.append(10 * math.log10(max(sum(map(mul, a, a)) / WIN, 1) / 32768 ** 2))
    thr = pipeline.voice_threshold(env)
    if thr is None:
        return None
    on = [v > thr for v in env]
    a, b = on.index(True), n - 1 - on[::-1].index(True)
    span = (b - a + 1) / FPS
    # ponytail: a quiet run of 0.2s or more is a pause — a stop consonant's
    # closure is under 0.1s; the lowpass hides "s"/"f" so they read as quiet
    pauses, run = [], 0
    for v in on[a:b + 1]:
        if v:
            if run / FPS >= 0.2:
                pauses.append(run / FPS)
            run = 0
        else:
            run += 1
    syl = syllables(text)
    talk = span - sum(pauses)

    f0 = [_f0(x, f * HOP) if on[f] else None for f in range(a, b + 1)]
    hz = [v for v in f0 if v]
    med = _pct(hz, 0.5) if hz else None
    st = [None if not v else 12 * math.log2(v / med) for v in f0] if med else [None] * len(f0)
    st = [None if v is None or abs(v) > 9 else v for v in st]   # octave slips
    voiced = [v for v in st if v is not None]
    tail = [v for v in st[-round(0.4 * FPS):] if v is not None]

    # the level contour, smoothed over 60ms; a peak stands 4dB over the line's
    # typical level and rules the 0.12s either side of it
    lv = env[a:b + 1]
    sm = [sum(lv[max(0, k - 1):k + 2]) / len(lv[max(0, k - 1):k + 2]) for k in range(len(lv))]
    talking = [v for v, o in zip(sm, on[a:b + 1]) if o]
    mid = _pct(talking, 0.5)
    peaks = []
    for k, v in enumerate(sm):
        if v >= mid + 4 and v == max(sm[max(0, k - 6):k + 7]) \
                and (not peaks or k - peaks[-1] >= 0.25 * FPS):
            peaks.append(k)
    return {
        "dur": round(len(x) / SR, 3), "span": round(span, 3),
        "words": len(re.findall(r"[A-Za-z']+|\d+", text)), "syl": syl,
        "rate": round(syl / span, 2), "artic": round(syl / max(talk, 0.1), 2),
        "pauses": len(pauses), "pause_s": round(sum(pauses), 2),
        "f0": round(med, 1) if med else None,
        "st_sd": round(_sd(voiced), 2) if voiced else None,
        "st_range": round(_pct(voiced, 0.9) - _pct(voiced, 0.1), 2) if voiced else None,
        "end": round(_pct(tail, 0.5), 2) if tail else None,
        "loud_sd": round(_sd(talking), 2),
        "emph": len(peaks), "emph_at": [round(k / len(sm), 2) for k in peaks],
        # contours at FPS: semitones off the median (null = unvoiced), dB off the typical level
        "fps": FPS, "st": [None if v is None else round(v, 1) for v in st],
        "db": [round(v - mid, 1) for v in sm],
    }


def tts_file(m: dict, line: dict) -> str:
    return f"{m.get('out', 'out')}/vo/{pipeline.line_key(m, line)}.wav"


def record(project: Path, pid: str, m: dict, i: int, take: str) -> dict:
    """Measure line i's TTS read and your take of it, and keep the pair."""
    l, vo = m["vo"]["lines"][i], m["vo"]
    tts = tts_file(m, l)
    old = latest(pid).get(take)
    if old and old["tts"]["file"] == tts and old["text"] == l["text"]:
        return old    # asked again (the page measures takes it has no row for)
    row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": pid, "line": i, "text": l["text"],
           "tts": {"file": tts, "voice": vo.get("voice"), "temp": vo.get("temperature", 0.8),
                   "seed": l.get("seed", vo.get("seed", 0)), "exag": l.get("exag", 0.5),
                   "cfg": l.get("cfg", 0.5),
                   **((project / tts).is_file() and measure(project / tts, l["text"]) or {})},
           "you": {"file": take, **(measure(project / take, l["text"]) or {})}}
    with _LOCK:
        DATA.parent.mkdir(exist_ok=True)
        with DATA.open("a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def latest(pid: str) -> dict:
    """take file -> its newest row, for one reel."""
    out = {}
    if DATA.is_file():
        for s in DATA.read_text().splitlines():
            r = json.loads(s)
            if r["pid"] == pid:
                out[r["you"]["file"]] = r
    return out


if __name__ == "__main__":
    # a fake read: room, a 1s vowel gliding 150 -> 200Hz, a 0.4s pause, a 1s
    # vowel at 150Hz with a 6dB-louder stress in the middle, room
    import random
    import tempfile
    import wave
    random.seed(1)
    s, ph = [], 0.0

    def room(t):
        s.extend(random.randint(-30, 30) for _ in range(int(t * SR)))

    def vowel(t, f0, f1, amp=lambda u: 1.0):
        global ph
        k = int(t * SR)
        for j in range(k):
            ph += 2 * math.pi * (f0 + (f1 - f0) * j / k) / SR
            s.append(int(3000 * amp(j / k) * sum(math.sin(h * ph) / h for h in (1, 2, 3))))

    room(0.3); vowel(1.0, 150, 200); room(0.4)
    vowel(1.0, 150, 150, lambda u: 2.0 if 0.45 < u < 0.55 else 1.0); room(0.3)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(array("h", s).tobytes())
        t0 = time.time()
        r = measure(p, "Thirty seven wear a disguise. Their I.D. keeps changing.")
        took = time.time() - t0
    assert syllables("Thirty seven wear a disguise. Their I.D. keeps changing.") == 14
    assert r["syl"] == 14 and r["words"] == 10, r
    assert abs(r["span"] - 2.4) < 0.15, r["span"]
    assert r["pauses"] == 1 and abs(r["pause_s"] - 0.4) < 0.1, (r["pauses"], r["pause_s"])
    assert 145 < r["f0"] < 175, r["f0"]
    assert 3 < r["st_range"] < 6, r["st_range"]     # the glide spans 5 semitones
    assert r["emph"] >= 1 and any(0.6 < a < 0.85 for a in r["emph_at"]), r["emph_at"]
    assert abs(r["end"]) < 1.5, r["end"]             # ends flat, on the median
    print(f"ok · {took:.2f}s for a {r['dur']}s read", {k: r[k] for k in r if k not in ("st", "db")})
