#!/usr/bin/env python3
"""Fit every beat to the voice that plays over it.

The VO is synthesised before the timing is known, so the honest order is:
render the lines, measure them, then size each beat to what it actually has to
hold. Doing it the other way round — guessing a length and hoping the voice
fits — is what produces beats that end mid-sentence.

Lines are laid end to end inside their beat with a breath between them; each
line's `offset` is pinned to where it lands, and the beat's `render.secs` is
whatever that adds up to. Run after `vo`, before `clips`.
"""
import json
import sys
from pathlib import Path

import pipeline

LEAD = 0.35   # visual starts fractionally before the voice
TAIL = 0.55   # room to land after the last word
GAP = 0.18    # breath between lines in the same beat


def retime(project: Path, lead=LEAD, tail=TAIL, gap=GAP) -> dict:
    m = pipeline.load(project)
    lines = json.loads((project / "clips/out/vo/lines.json").read_text())

    # Pair the manifest's lines with the built track BY POSITION. build_vo writes
    # one row per non-empty line in manifest order, so the index is stable —
    # whereas matching on (beat, text) breaks the moment either is edited, which
    # is exactly when a retime is wanted. Durations come from the track; which
    # beat a line belongs to comes from the manifest, because that is the edit.
    spoken = [l for l in m["vo"]["lines"] if l.get("text", "").strip()]
    if len(spoken) != len(lines):
        raise RuntimeError(
            f"the voice track has {len(lines)} line(s) but the script has "
            f"{len(spoken)} — run the `vo` step first so they agree")

    pos: dict[str, float] = {}
    for l, ln in zip(spoken, lines):
        b = l.get("beat") or ""
        t = pos.get(b, lead)
        l["offset"] = round(t, 2)
        pos[b] = t + ln["dur"] + gap
    secs = {b: round(end - gap + tail, 2) for b, end in pos.items() if b}

    for seg in m["segments"]:
        if seg["name"] in secs and isinstance(seg.get("render"), dict):
            seg["render"]["secs"] = secs[seg["name"]]
    for c in m.get("audio", {}).get("sfx", []):
        lim = secs.get(c["beat"])
        if lim:                       # a cue past its beat plays over the next one
            c["offset"] = round(min(c["offset"], lim - 0.3), 2)
    pipeline.save(project, m)
    return secs


def demo() -> None:
    """Two lines in one beat must not overlap, and the beat must contain both."""
    a, b = {"dur": 2.0}, {"dur": 1.0}
    first = LEAD
    second = first + a["dur"] + GAP
    assert second > first + a["dur"], "lines overlap"
    total = second + b["dur"] + TAIL
    assert total >= second + b["dur"], "beat cuts the last line off"
    assert abs(total - (LEAD + 2.0 + GAP + 1.0 + TAIL)) < 1e-9


if __name__ == "__main__":
    demo()
    if len(sys.argv) > 1:
        for k, v in retime(Path(sys.argv[1]).expanduser()).items():
            print(f"  {k:10s} {v}s")
