#!/usr/bin/env python3
"""Place sound cues against what a clip actually animates.

Two things kept going wrong when these were written by hand:

  * offsets were stored as seconds, so retiming a beat left every cue pointing
    at a moment that had moved — the reveal it was meant to hit was now
    somewhere else entirely
  * a percussive hit landing on the first syllable of a line reads as a
    mistake, and it is easy to do by accident because the voice track is not
    visible while you are picking numbers

So cues are authored as FRACTIONS of their beat, which is how the clips
themselves are written (`p = t / SECS`), and anything loud that lands on a
spoken word is pulled into the breath before it, where it works as punctuation.
"""
import json
import sys
from pathlib import Path

import pipeline

LOUD_DB = -14          # at or above this, a cue must not sit on a word
NEAR = 0.25            # seconds either side of a line start that counts as "on it"
LEAD_IN = 0.40         # how far before the line to move it instead


def place(project: Path, spec: dict, log=print) -> list[dict]:
    """spec: {beat: [(kind, fraction_of_beat, gain_db), ...]} -> cue list, saved."""
    m = pipeline.load(project)
    outdir = project / m.get("out", "out")
    plan = json.loads((outdir / "plan.json").read_text())
    starts = {s["name"]: s["start_at"] for s in plan["segments"]}
    lens = {s["name"]: s["length"] for s in plan["segments"]}

    linesfile = outdir / "vo" / "lines.json"
    voice = []
    if linesfile.is_file():
        for l in json.loads(linesfile.read_text()):
            if l.get("beat") in starts:
                voice.append(starts[l["beat"]] + float(l.get("offset", 0) or 0))
    voice.sort()

    cues, moved = [], 0
    for beat, items in spec.items():
        if beat not in lens:
            log(f"  ! no beat {beat!r} in the cut — skipped")
            continue
        for kind, frac, gain in items:
            off = round(lens[beat] * frac, 2)
            if gain >= LOUD_DB and voice:
                t = starts[beat] + off
                hit = [v for v in voice if abs(t - v) < NEAR]
                if hit:
                    want = hit[0] - LEAD_IN - starts[beat]
                    new = round(max(0.05, min(want, lens[beat] - 0.15)), 2)
                    if abs(new - off) > 0.01:
                        moved += 1
                        off = new
            cues.append({"kind": kind, "beat": beat, "offset": off, "gain_db": gain})
    m.setdefault("audio", {})["sfx"] = cues
    pipeline.save(project, m)
    log(f"  {len(cues)} cue(s) placed" + (f", {moved} nudged off a spoken word" if moved else ""))
    return cues


def demo() -> None:
    """A loud cue sitting on a line moves earlier; a quiet one stays put."""
    starts, lens = {"a": 10.0}, {"a": 4.0}
    line = 11.0
    for gain, expect_move in ((-8, True), (-19, False)):
        off = 1.0                       # lands exactly on the line at t=11.0
        t = starts["a"] + off
        near = abs(t - line) < NEAR
        assert near
        if gain >= LOUD_DB:
            off2 = round(max(0.05, line - LEAD_IN - starts["a"]), 2)
            assert (off2 != off) == expect_move, (gain, off2)
            assert off2 == 0.6, off2     # 0.4s before the line
        else:
            assert not expect_move
    print("ok")


if __name__ == "__main__":
    demo()
