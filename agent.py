#!/usr/bin/env python3
"""The Agent tab: the editing that separate Claude sessions used to do on a reel
through the HTTP API and the project files (rewrite voice-over lines, re-cut the
timeline, write new HTML clips), done in the app as changes the user approves.

propose() asks the configured AI (publik, your own key or the claude CLI) for a
list of changes; apply() checks each one against the project and applies the
ones the user ticked. Nothing renders and nothing posts: Update renders, as it
does after any edit.

    python3 agent.py     # self-check with a fake model, in a temp project
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path, PurePosixPath

import ai
import discovery
import pipeline
import scripts

SYSTEM = """You are the editor inside ReelDeck, an app that cuts short vertical videos (1080x1920 Reels).
You see one project: its timeline (segments, played in order), its voice-over lines, the footage and
HTML clip templates it has, its notes, and the user's ask. Propose concrete changes that do what the
user asked. Answer JSON only:
{"summary": str, "changes": [change, ...]}
Every change has "why": str (one short sentence) and exactly one of these shapes:
  {"op": "set", "seg": name, "fields": {...}}
      fields, any of: start, dur (seconds into the source, and how long to play it), note, xfade
      (crossfade seconds into this segment, 0 = hard cut), fit ("cover" | "blur" | "contain"),
      secs (a template segment's render length), params (template params to change; null removes one)
  {"op": "add", "seg": {"name": str, "src": footage file, "start": s, "dur": s, "note": str}, "after": name}
  {"op": "add", "seg": {"name": str, "render": {"html": template file, "params": {...}, "secs": s}}, "after": name}
  {"op": "remove", "seg": name}
  {"op": "move", "seg": name, "after": name}
      "after" is a segment name or "first"; leave it out to put the segment at the end.
  {"op": "line", "i": n, "text": str}         rewrite voice-over line n; "" removes it
  {"op": "line", "after": n, "text": str, "beat": segment name}
      insert a voice-over line after line n ("first" for the top, leave it out for the end)
  {"op": "clip", "file": "<clips_dir>/<new-name>.html", "html": str, "request": request id or null}
      a new HTML clip template; then "add" a segment whose render.html is that file to use it
Rules:
- Footage and template files only from the lists given. Line numbers are the ones shown, before any change.
- Voice-over is spoken: short lines, one idea each, no emojis, hashtags or stage directions.
- A new clip is one self-contained HTML page, 1080x1920, that sets window.DURATION (seconds) and
  window.seek(t), which draws the frame at time t with no timers or wall clock. It reads params with
  new URLSearchParams(location.search). It may load files that sit beside it in the clips dir (see
  the example), and nothing from the network. Clips can't be edited in place: a changed clip is a new file.
- Few, purposeful changes. If nothing should change, return no changes and say why in the summary."""

FITS = ("cover", "blur", "contain")
SLUG = re.compile(r"[^a-z0-9_-]+")
CLIP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
# ponytail: a denylist, not a sandbox. A clip runs in render.mjs's headless
# Chromium; this refuses the usual ways a page reaches the network, and the user
# reads the source before approving it. A real sandbox is Chromium launched offline.
NETWORK = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|RTCPeerConnection)\b"
                     r"|https?:(?!//www\.w3\.org/)|[\"'(=]\s*//", re.I)


# ---------- what the model sees ----------
def _library(project: Path, m: dict, disc: dict) -> list[str]:
    """Every video a segment may cut from: what the timeline already uses, the
    library (out/*.mp4, uploads land there) and the rendered clips."""
    files = {s["src"] for s in m.get("segments", []) if s.get("src") and not s.get("render")}
    files |= {f["file"] for f in disc["static"]}
    cdir = discovery.clips_dir(project)
    if cdir:
        files |= {str(f.relative_to(project)) for f in (cdir / "out").glob("*.mp4")}
    return sorted(f for f in files if (project / f).is_file())


def context(project: Path, m: dict) -> str:
    disc = discovery.discover(project, m)
    footage = []
    for f in _library(project, m, disc)[:60]:   # ponytail: first 60 by name; past that it wants search, not a longer prompt
        try:
            footage.append({"file": f, "secs": round(pipeline.duration(project / f), 2)})
        except (RuntimeError, ValueError, OSError):
            pass
    view = {
        "reel": {k: m.get(k) for k in ("name", "width", "height", "fps", "xfade", "notes")},
        "segments": [{k: s[k] for k in ("name", "src", "start", "dur", "xfade", "fit", "note", "render") if k in s}
                     for s in m.get("segments", [])],
        "voiceover": [{"i": i, "text": l.get("text", ""), "beat": l.get("beat", "")}
                      for i, l in enumerate((m.get("vo") or {}).get("lines", []))],
        "footage": footage,
        "templates": [{"file": t["file"], "params": t["params"], "retimes": t["retimes"]}
                      for t in disc["html_templates"]],
        "clips_dir": disc["clips_dir"],
        "clip_requests": [{k: r.get(k) for k in ("id", "prompt", "duration_hint", "notes", "target")}
                          for r in discovery.list_requests(project) if r.get("status") == "pending"],
    }
    parts = [json.dumps(view, indent=1)]
    draft = scripts.load(project)
    if any(l.get("text", "").strip() for b in draft.get("beats", []) for l in b.get("lines", [])):
        parts.append("SCRIPT TAB DRAFT:\n" + ai._render(draft))
    for doc in ("SCRIPT.md", "SHOTS.md"):
        if (project / doc).is_file():
            parts.append(f"{doc}:\n" + (project / doc).read_text(errors="replace")[:4000])
    if disc["html_templates"]:
        t = min(disc["html_templates"], key=lambda t: (project / t["file"]).stat().st_size)
        parts.append(f"EXAMPLE TEMPLATE {t['file']}:\n" + (project / t["file"]).read_text(errors="replace")[:6000])
    if not disc["clips_dir"]:
        parts.append("This project has no clip pipeline (no render.mjs), so it can't take new clips.")
    return "\n\n".join(parts)


def propose(project: Path, m: dict, ask: str) -> dict:
    if not ask.strip():
        raise ValueError("say what you want changed")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": context(project, m) + "\n\nASK: " + ask.strip()}]
    out = ai.parse_json(ai.chat(msgs, "smart", max_tokens=12000))
    changes = [c for c in out.get("changes") or [] if isinstance(c, dict)]
    for c, err in zip(changes, apply(project, m, changes)[3]):
        if err:
            c["error"] = err     # shown, and its box can't be ticked
    return {"summary": str(out.get("summary") or ""), "changes": changes}


# ---------- applying ----------
def _num(v, what: str, positive: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or (positive and v == 0):
        raise ValueError(f"{what} must be a {'positive' if positive else 'non-negative'} number of seconds")
    return round(float(v), 3)


def _set(seg: dict, fields: dict) -> None:
    """Validate everything first, then write, so a bad field leaves the segment untouched."""
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields is an object of what to change")
    new, r = dict(seg), dict(seg.get("render") or {})
    for k, v in fields.items():
        if k in ("start", "dur", "xfade"):
            if v is None:
                new.pop(k, None)
            else:
                new[k] = _num(v, k, positive=k == "dur")
        elif k == "note":
            new["note"] = str(v or "")
        elif k == "fit":
            if v not in FITS:
                raise ValueError(f"fit is one of {', '.join(FITS)}")
            new["fit"] = v
        elif k in ("secs", "params"):
            if not seg.get("render"):
                raise ValueError(f"{seg['name']} is footage, not a template clip, so it has no {k}")
            if k == "secs":
                r["secs"] = _num(v, "secs", positive=True)
            elif not isinstance(v, dict):
                raise ValueError("params is an object")
            else:
                p = dict(r.get("params") or {})
                for pk, pv in v.items():
                    if pv is None:
                        p.pop(pk, None)
                    else:
                        p[str(pk)] = pv if isinstance(pv, (int, float)) and not isinstance(pv, bool) else str(pv)
                r["params"] = p
        else:
            raise ValueError(f"can't set {k}")
    if seg.get("render"):
        new["render"] = r
    seg.clear()
    seg.update(new)


def _where(segs: list, after) -> int:
    if after in (None, ""):
        return len(segs)
    if after == "first":
        return 0
    for i, s in enumerate(segs):
        if s.get("name") == after:
            return i + 1
    raise ValueError(f'no segment named "{after}"')


def _find(segs: list, name) -> int:
    for i, s in enumerate(segs):
        if s.get("name") == name:
            return i
    raise ValueError(f'no segment named "{name}"')


def apply(project: Path, m: dict, changes: list) -> tuple[dict, dict, dict, list]:
    """Apply `changes` in order to a copy of the manifest.

    Returns (manifest, clip files to write {path: html}, clip requests they
    fulfil {id: path}, an error per change or None). A change that fails is
    skipped, so one bad proposal doesn't sink the rest; a change that depends on
    it (an "add" of a clip that wasn't written) then fails its own check.
    """
    m = copy.deepcopy(m)
    segs = m.setdefault("segments", [])
    disc = discovery.discover(project, m)
    cdir = discovery.clips_dir(project)
    crel = disc["clips_dir"]
    footage = set(_library(project, m, disc))
    templates = {t["file"] for t in disc["html_templates"]}
    requests = {r["id"] for r in discovery.list_requests(project)}
    # Lines are addressed by their number before any change, so removing one
    # doesn't shift the next. slots[0] holds lines inserted at the top.
    lines = (m.get("vo") or {}).get("lines") or []
    slots = [[]] + [[l] for l in lines]
    touched_lines = False
    files, done, errors = {}, {}, []

    for c in changes:
        try:
            op = c.get("op") if isinstance(c, dict) else None
            if op == "set":
                _set(segs[_find(segs, c.get("seg"))], c.get("fields"))
            elif op == "add":
                s = c.get("seg") if isinstance(c.get("seg"), dict) else {}
                base = SLUG.sub("-", str(s.get("name") or "clip").lower()).strip("-") or "clip"
                name, n = base, 2
                taken = {x.get("name") for x in segs}
                while name in taken or (cdir and (cdir / "out" / f"{name}.mp4").exists()):
                    name, n = f"{base}-{n}", n + 1
                if isinstance(s.get("render"), dict):
                    r = s["render"]
                    if r.get("html") not in templates and r.get("html") not in files:
                        raise ValueError(f"{r.get('html')} is not one of this project's clip templates")
                    new = {"name": name, "src": f"{crel}/out/{name}.mp4",
                           "render": {"kind": "html", "html": r["html"]}}
                    rp = {k: r[k] for k in ("params", "secs") if r.get(k) is not None}
                    if rp:
                        _set(new, rp)
                else:
                    if s.get("src") not in footage:
                        raise ValueError(f"{s.get('src')} is not footage in this project")
                    new = {"name": name, "src": s["src"]}
                extra = {k: s[k] for k in ("start", "dur", "xfade", "fit", "note") if s.get(k) is not None}
                if extra:
                    _set(new, extra)
                segs.insert(_where(segs, c.get("after")), new)
            elif op == "remove":
                segs.pop(_find(segs, c.get("seg")))
            elif op == "move":
                i = _find(segs, c.get("seg"))
                if c.get("after") == c.get("seg"):
                    raise ValueError("a segment can't go after itself")
                seg = segs.pop(i)
                try:
                    segs.insert(_where(segs, c.get("after")), seg)
                except ValueError:
                    segs.insert(i, seg)
                    raise
            elif op == "line":
                text = str(c.get("text") or "").strip()
                if "i" in c:
                    i = c["i"]
                    if not isinstance(i, int) or not 0 <= i < len(lines) or slots[i + 1][0] is None:
                        raise ValueError(f"no voice-over line {i}")
                    slots[i + 1][0] = {**slots[i + 1][0], "text": text} if text else None
                else:
                    if not text:
                        raise ValueError("a new line needs text")
                    a = c.get("after")
                    at = 0 if a == "first" else len(lines) if a in (None, "") else a + 1 if (
                        isinstance(a, int) and 0 <= a < len(lines)) else None
                    if at is None:
                        raise ValueError(f"no voice-over line {a}")
                    slots[at].append({"text": text, "beat": str(c.get("beat") or "")})
                touched_lines = True
            elif op == "clip":
                if not cdir:
                    raise ValueError("this project has no clip pipeline (no render.mjs), so it can't render a new clip")
                f, html = str(c.get("file") or ""), str(c.get("html") or "")
                p = PurePosixPath(f)
                if str(p.parent) != crel or p.suffix != ".html" or not CLIP_NAME.match(p.stem):
                    raise ValueError(f"a new clip is {crel}/<lowercase-name>.html")
                if (project / f).exists() or f in files:
                    raise ValueError(f"{f} already exists; a changed clip gets a new name")
                if "seek" not in html or "DURATION" not in html:
                    raise ValueError("a clip must set window.DURATION and window.seek(t)")
                bad = NETWORK.search(html)
                if bad:
                    raise ValueError(f'the clip reaches for the network ("{bad.group(0).strip()}"); clips render offline')
                rid = c.get("request")
                if rid and rid not in requests:
                    raise ValueError(f"no clip request {rid}")
                files[f] = html
                if rid:
                    done[rid] = f
            else:
                raise ValueError(f"unknown change {op!r}")
            errors.append(None)
        except (ValueError, TypeError, KeyError) as e:
            errors.append(str(e))

    if touched_lines:
        m.setdefault("vo", {})["lines"] = [l for slot in slots for l in slot if l is not None]
    return m, files, done, errors


# ---------- self-check ----------
def _check() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "clips" / "out").mkdir(parents=True)
        (p / "clips" / "render.mjs").write_text("// stub\n")
        (p / "clips" / "hook.html").write_text(
            "<script>const Q=new URLSearchParams(location.search);Q.get('words');"
            "window.DURATION=2;window.seek=t=>{}</script>")
        for n in ("a", "b"):
            (p / "clips" / "out" / f"{n}.mp4").write_bytes(b"not really video")   # apply() only checks it exists
        m = {"name": "t", "out": "out", "segments": [
                {"name": "hook", "src": "clips/out/hook.mp4", "render": {"kind": "html", "html": "clips/hook.html", "params": {"words": "x"}}},
                {"name": "a", "src": "clips/out/a.mp4", "dur": 1}],
             "vo": {"lines": [{"text": "one", "beat": "hook", "pause": 200}, {"text": "two"}, {"text": "three"}]}}
        pipeline.save(p, m)
        rid = discovery.add_request(p, "a counter", 2, "")["id"]
        good = "<script>window.DURATION=2;window.seek=t=>{document.body.textContent=t}</script>"
        changes = [
            {"op": "set", "seg": "hook", "fields": {"secs": 3, "params": {"words": "a|b", "extra": None}}},
            {"op": "set", "seg": "a", "fields": {"dur": -1}},                                  # bad, skipped
            {"op": "add", "seg": {"name": "B roll", "src": "clips/out/b.mp4", "dur": 0.5}, "after": "first"},
            {"op": "add", "seg": {"name": "x", "src": "../etc/passwd"}},                       # bad
            {"op": "clip", "file": "clips/count.html", "html": good, "request": rid},
            {"op": "clip", "file": "clips/evil.html", "html": good.replace("{", "{fetch('/x');")},  # bad
            {"op": "clip", "file": "clips/hook.html", "html": good},                           # exists, bad
            {"op": "add", "seg": {"name": "count", "render": {"html": "clips/count.html", "secs": 2}}, "after": "hook"},
            {"op": "move", "seg": "a", "after": "first"},
            {"op": "remove", "seg": "nope"},                                                   # bad
            {"op": "line", "i": 1, "text": ""},
            {"op": "line", "i": 0, "text": "ONE"},
            {"op": "line", "after": 1, "text": "two and a half", "beat": "a"},
            {"op": "line", "after": "first", "text": "zero"},
            {"op": "line", "i": 9, "text": "?"},                                               # bad
            {"op": "wat"},                                                                     # bad
        ]
        out, files, done, errors = apply(p, m, changes)
        bad = [i for i, e in enumerate(errors) if e]
        assert bad == [1, 3, 5, 6, 9, 14, 15], (bad, errors)
        assert [s["name"] for s in out["segments"]] == ["a", "b-roll", "hook", "count"], out["segments"]
        hook = out["segments"][2]["render"]
        assert hook["secs"] == 3 and hook["params"] == {"words": "a|b"}, hook
        assert out["segments"][3] == {"name": "count", "src": "clips/out/count.mp4",
                                      "render": {"kind": "html", "html": "clips/count.html", "secs": 2.0}}, out["segments"][3]
        assert [l["text"] for l in out["vo"]["lines"]] == ["zero", "ONE", "two and a half", "three"]
        assert out["vo"]["lines"][1]["pause"] == 200, "a rewrite kept the line's settings"
        assert files == {"clips/count.html": good} and done == {rid: "clips/count.html"}
        assert m["segments"][0]["render"]["params"] == {"words": "x"}, "the input manifest was mutated"
        assert NETWORK.search('<svg xmlns="http://www.w3.org/2000/svg">') is None
        assert NETWORK.search('<img src="//evil.example/x.png">') and NETWORK.search("a // b") is None

        # propose(): the model's changes come back with their errors marked
        saved = ai.chat
        ai.chat = lambda msgs, tier, max_tokens: (
            '{"summary": "s", "changes": [{"op": "remove", "seg": "a", "why": "w"}, {"op": "remove", "seg": "zz"}]}')
        try:
            r = propose(p, m, "cut it")
        finally:
            ai.chat = saved
        assert r["summary"] == "s" and "error" not in r["changes"][0] and "zz" in r["changes"][1]["error"], r
    print("agent: ok")


if __name__ == "__main__":
    _check()
