#!/usr/bin/env python3
"""The Script tab's document: script.json at the project root, beside reel.json,
and named versions of it in script-versions/.

A script is beats of lines, written before anything is voiced:

    {"title": str, "brief": str, "target": 30,
     "beats": [{"name": "hook", "lines": [{"text": "...", "note": ""}]}]}

It is the writer's draft, not the pipeline's input: "Send to Voice" in the tab
copies it into reel.json's vo.lines, where the Voice tab and the render take over.

    python3 scripts.py     # self-check in a temp dir
"""
from __future__ import annotations

import json
import time
from pathlib import Path

FILE, DIR, KEEP = "script.json", "script-versions", 60   # ponytail: oldest versions past KEEP are dropped


def empty() -> dict:
    return {"title": "", "brief": "", "target": 30, "beats": [{"name": "Hook", "lines": [{"text": "", "note": ""}]}]}


def load(project: Path) -> dict:
    f = project / FILE
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return empty()


def _clean(doc: dict) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("beats"), list):
        raise ValueError("a script is {title, beats: [{name, lines: [{text}]}]}")
    beats = [{"name": str(b.get("name") or ""),
              "lines": [{"text": str(l.get("text") or ""), "note": str(l.get("note") or "")}
                        for l in b.get("lines") or [] if isinstance(l, dict)]}
             for b in doc["beats"] if isinstance(b, dict)]
    target = doc.get("target")
    return {"title": str(doc.get("title") or ""), "brief": str(doc.get("brief") or ""),
            "target": int(target) if isinstance(target, (int, float)) and 5 <= target <= 600 else 30,
            "beats": beats}


def save(project: Path, doc: dict) -> dict:
    doc = {**_clean(doc), "updated": time.time()}
    tmp = project / (FILE + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n")
    tmp.replace(project / FILE)
    return doc


def versions(project: Path) -> list[dict]:
    """Newest first: id, when, label and a word count, enough to pick one."""
    out = []
    for f in sorted((project / DIR).glob("*.json"), reverse=True):
        try:
            v = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        words = sum(len(l["text"].split()) for b in v["doc"]["beats"] for l in b["lines"])
        out.append({"id": f.stem, "at": v["at"], "label": v.get("label", ""), "words": words})
    return out


def snapshot(project: Path, label: str = "") -> dict:
    d = project / DIR
    d.mkdir(exist_ok=True)
    at = time.time()
    vid = f"{int(at * 1000)}"
    (d / f"{vid}.json").write_text(json.dumps({"at": at, "label": str(label)[:80], "doc": load(project)}, indent=1) + "\n")
    for old in sorted(d.glob("*.json"))[:-KEEP]:
        old.unlink()
    return {"id": vid, "at": at, "label": label}


def version(project: Path, vid: str) -> dict:
    if not vid.isdigit():
        raise KeyError(vid)
    f = project / DIR / f"{vid}.json"
    if not f.is_file():
        raise KeyError(vid)
    return json.loads(f.read_text())["doc"]


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        assert load(p) == empty()
        save(p, {"title": "T", "target": 45, "beats": [{"name": "Hook", "lines": [{"text": "one two"}]}]})
        a = snapshot(p, "first")
        time.sleep(0.002)
        save(p, {"title": "T2", "beats": [{"name": "Hook", "lines": [{"text": "x"}, "junk"]}]})
        assert load(p)["target"] == 30 and load(p)["beats"][0]["lines"] == [{"text": "x", "note": ""}]
        snapshot(p)
        vs = versions(p)
        assert [v["label"] for v in vs] == ["", "first"] and vs[1]["words"] == 2, vs
        assert version(p, a["id"])["title"] == "T"
        for bad in ("../x", "nope"):
            try:
                version(p, bad)
                raise AssertionError(bad)
            except KeyError:
                pass
        try:
            save(p, {"beats": "no"})
            raise AssertionError("bad doc saved")
        except ValueError:
            pass
    print("scripts: ok")
