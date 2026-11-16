#!/usr/bin/env python3
"""Find out what a project can generate, by looking at its own files.

No per-project config schema to maintain: point this at a directory and it finds
whatever is actually there. A project with no capture-*.mjs simply has no capture
beats to offer; one with no scenes.py has no manim scenes. This is what makes the
Clips tab work unmodified across different reels with different asset pipelines.

    discover(project) -> {
        "clips_dir": "clips" | None,          # where render.mjs lives, if any
        "html_templates": [ {file, name, params: [...], retimes: bool}, ... ],
        "manim_scenes":   [ {file, scene}, ... ],
        "capture_beats":  [ {script, beat}, ... ],
        "static":         [ {file, name}, ... ],   # already-rendered footage, browseable
    }
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path, PurePath

BUILD_PRODUCTS ={"preview.mp4", "reel.mp4", "mixdown.wav", "voiceover.wav"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "manim", ".clips", ".thumbs",
            "vo", "sfx", "partial_movie_files"}


def _iter_files(root: Path, suffix: str, max_depth: int = 4):
    root = root.resolve()
    for p in root.rglob(f"*{suffix}"):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if len(rel.parts) > max_depth:
            continue
        yield p


def clips_dir(project: Path) -> Path | None:
    """The directory holding render.mjs, if this project has one."""
    for p in _iter_files(project, "render.mjs"):
        return p.parent
    return None


_clips_dir = clips_dir  # internal alias kept for brevity below


def _html_templates(clips_dir: Path, project: Path) -> list[dict]:
    out = []
    for f in sorted(clips_dir.glob("*.html")):
        src = f.read_text(errors="ignore")
        params = sorted(set(re.findall(r"Q\.get\(\s*['\"](\w+)['\"]", src)))
        out.append({
            "file": str(f.relative_to(project)),
            "name": f.stem,
            "params": params,
            # only true if window.DURATION itself is a function of ?secs — every
            # other clip's `secs` just trims/holds the frozen last frame, it
            # does not reflow the animation to fit
            "retimes": "secs" in params and "window.DURATION" in src
                       and "Q.get" in src.split("window.DURATION")[1].split("\n")[0]
                       if "window.DURATION" in src else False,
        })
    return out


def _scene_classes(project: Path) -> list[dict]:
    out = []
    for f in _iter_files(project, ".py"):
        src = f.read_text(errors="ignore")
        for scene in re.findall(r"class\s+(\w+)\(Scene\)", src):
            out.append({"file": str(f.relative_to(project)), "scene": scene})
    return out


def _beats_object(src: str) -> str | None:
    """The body of `const BEATS = { ... }`, brace-matched (not just grepped) so
    a nested object deeper in the file can't be mistaken for the beat list."""
    m = re.search(r"\bBEATS\s*=\s*\{", src)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return None


def _capture_beats(project: Path) -> list[dict]:
    out = []
    for f in _iter_files(project, ".mjs"):
        if not re.match(r"capture.*\.mjs$", f.name, re.I):
            continue
        body = _beats_object(f.read_text(errors="ignore"))
        if body is None:
            continue
        # top-level keys only: 2-space indent, immediately followed by ':'
        for beat in re.findall(r"^\s{2}(\w+)\s*:", body, re.M):
            out.append({"script": str(f.relative_to(project)), "beat": beat})
    return out


def _static_footage(project: Path, m: dict) -> list[dict]:
    outdir = project / m.get("out", "out")
    if not outdir.is_dir():
        return []

    # Matched on filename as well as full path: a manim segment points at the
    # nested render (out/manim/videos/scenes/1920p60/McpFlow.mp4) while the
    # library lists the copy generate_one lands in out/. Comparing paths alone
    # meant a clip that is very much in use showed as unused.
    by_path: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for s in m.get("segments", []):
        src = s.get("src") or ""
        by_path.setdefault(src, []).append(s.get("name", "?"))
        by_name.setdefault(PurePath(src).name, []).append(s.get("name", "?"))

    out = []
    for f in sorted(outdir.glob("*.mp4")):
        if f.name in BUILD_PRODUCTS:
            continue
        rel = str(f.relative_to(project))
        users = by_path.get(rel) or by_name.get(f.name) or []
        st = f.stat()
        out.append({"file": rel, "name": f.stem, "used": bool(users),
                    "used_by": users, "bytes": st.st_size, "mtime": round(st.st_mtime, 3)})
    return out



# ---------- uploaded footage ----------
_SAFE = re.compile(r"[^a-z0-9]+")


def _unique(outdir: Path, stem: str) -> Path:
    """A free filename, so a second upload of the same clip does not silently
    replace the first — the earlier one may already be on the timeline."""
    target = outdir / f"{stem}.mp4"
    n = 2
    while target.exists():
        target = outdir / f"{stem}-{n}.mp4"
        n += 1
    return target


def import_footage(project: Path, m: dict, filename: str, src: Path,
                   log=lambda s: None) -> dict:
    """Bring a file the user uploaded into the clips library.

    The library is just `out/*.mp4`, so importing is mostly a matter of landing
    the file there — but not by copying it as-is. Phone footage arrives as .mov,
    often HEVC and almost always variable frame rate, and VFR is exactly what
    silently truncated a cut once before. Re-encoding to the project's own fps
    with the same seekable GOP settings the generated clips use means an
    uploaded clip behaves like every other clip in the library: it scrubs, it
    concatenates, and its duration is what it says it is.
    """
    outdir = project / m.get("out", "out")
    outdir.mkdir(parents=True, exist_ok=True)
    stem = _SAFE.sub("-", PurePath(filename).stem.lower()).strip("-") or "footage"
    target = _unique(outdir, stem)
    fps = int(m.get("fps", 60))

    import pipeline
    log(f"  transcoding {filename} -> {target.name}")
    args = ["ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-map", "0:v:0", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-vsync", "cfr", "-r", str(fps),
            *pipeline._SEEKABLE(fps)]
    # keep audio when there is any: b-roll with room tone is worth having, and
    # the mixer can always ignore it
    args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "192k", str(target)]
    subprocess.run(args, check=True, capture_output=True, text=True, timeout=1800)

    st = target.stat()
    return {"file": str(target.relative_to(project)), "name": target.stem,
            "used": False, "used_by": [], "bytes": st.st_size,
            "mtime": round(st.st_mtime, 3)}

def discover(project: Path, m: dict) -> dict:
    clips_dir = _clips_dir(project)
    return {
        "clips_dir": str(clips_dir.relative_to(project)) if clips_dir else None,
        "html_templates": _html_templates(clips_dir, project) if clips_dir else [],
        "manim_scenes": _scene_classes(project),
        "capture_beats": _capture_beats(project),
        "static": _static_footage(project, m),
    }


# ---------- generation (dispatch used by pipeline.render_clips) ----------
def _snapshot(outdir: Path) -> dict[Path, float]:
    if not outdir.is_dir():
        return {}
    return {f: f.stat().st_mtime for f in outdir.glob("*.mp4")}


def run_capture(project: Path, script_rel: str, beat: str, log) -> Path:
    """Run a capture beat and return the file it produced.

    The script's own output-naming convention (out/pwa-<beat>.mp4 here) is not
    something reeldeck should need to know — instead, snapshot out/*.mp4 before
    and after, and whatever changed is the answer. Works for any project's own
    naming scheme with zero configuration.
    """
    script = project / script_rel
    outdir = script.parent / "out"
    before = _snapshot(outdir)
    p = subprocess.run(["node", script.name, beat], cwd=str(script.parent),
                       capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().split("\n")[-1])
    after = _snapshot(outdir)
    changed = [f for f, t in after.items() if before.get(f) != t]
    if not changed:
        raise RuntimeError(f"capture beat {beat!r} ran but produced no new file")
    changed.sort(key=lambda f: after[f], reverse=True)
    log(f"  captured {changed[0].name}")
    return changed[0]


def run_manim(project: Path, file_rel: str, scene: str, fps: int, log) -> Path:
    """Render a manim scene and return the file it produced, via the same
    before/after snapshot trick — manim's own quality-folder naming
    (e.g. 1920p60) is derived from config set inside the scene file, which
    reeldeck should not have to parse to find the output."""
    scene_file = project / file_rel
    outdir = scene_file.parent / "out"
    media = outdir / "manim"
    before = {p: p.stat().st_mtime for p in media.rglob(f"{scene}.mp4")} if media.is_dir() else {}
    try:
        p = subprocess.run(
            ["manim", "render", "--fps", str(fps), "--media_dir", "out/manim",
             scene_file.name, scene],
            cwd=str(scene_file.parent), capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise RuntimeError(
            "manim is not on PATH — it is usually in ~/.local/bin, which systemd "
            "leaves out of a user service's PATH (see reeldeck.service)") from None
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().split("\n")[-1])
    after = {p: p.stat().st_mtime for p in media.rglob(f"{scene}.mp4")} if media.is_dir() else {}
    changed = [f for f, t in after.items() if before.get(f) != t]
    if not changed:
        raise RuntimeError(f"manim rendered {scene!r} but the output file was not found")
    # copy to a stable, predictable path — out/<Scene>.mp4 — rather than making
    # every segment reference manim's versioned quality-folder path directly
    stable = outdir / f"{scene}.mp4"
    stable.write_bytes(changed[0].read_bytes())
    log(f"  rendered {scene} -> {stable.relative_to(project)}")
    return stable


# ---------- thumbnails ----------
def thumbnail(project: Path, m: dict, rel_src: str, at: float = 0.3) -> Path | None:
    src = project / rel_src
    if not src.is_file():
        return None
    d = project / m.get("out", "out") / ".thumbs"
    d.mkdir(parents=True, exist_ok=True)
    stamp = hashlib.md5(f"{rel_src}|{src.stat().st_mtime}|{at}".encode()).hexdigest()[:12]
    out = d / f"{stamp}.jpg"
    if out.is_file():
        return out
    dur = 0.0
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True, timeout=10)
        dur = float(r.stdout.strip() or 0)
    except Exception:
        pass
    ss = min(at, max(0.0, dur - 0.05)) if dur else 0.0
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{ss:.2f}", "-i", str(src),
         "-frames:v", "1", "-vf", "scale=240:-2", str(out)],
        capture_output=True, text=True, timeout=20)
    return out if p.returncode == 0 and out.is_file() else None


# ---------- clip requests: a prompt queue, fulfilled by a human/agent session ----------
# This is deliberately NOT a live codegen pipeline. Standing up "prompt in,
# arbitrary generated code executed in a real browser out" unattended is a code
# execution surface this app should not open on its own. A request just parks
# the ask; a build session (a person, or Claude Code working in this project)
# writes the actual clip file and calls fulfill().
REQUESTS_FILE = "clip_requests.json"


def list_requests(project: Path) -> list[dict]:
    f = project / REQUESTS_FILE
    return json.loads(f.read_text()) if f.is_file() else []


def add_request(project: Path, prompt: str, duration_hint: float | None, notes: str,
                target: str = "") -> dict:
    """Queue a clip ask. `target` names an existing clip to change, empty for a new one."""
    reqs = list_requests(project)
    rid = time.strftime("%Y%m%d-%H%M%S") + f"-{len(reqs):02d}"
    row = {"id": rid, "prompt": prompt.strip(), "duration_hint": duration_hint,
          "notes": notes.strip(), "target": (target or "").strip(),
          "status": "pending", "created": time.time()}
    reqs.append(row)
    (project / REQUESTS_FILE).write_text(json.dumps(reqs, indent=1))
    return row


def remove_request(project: Path, rid: str) -> None:
    reqs = [r for r in list_requests(project) if r["id"] != rid]
    (project / REQUESTS_FILE).write_text(json.dumps(reqs, indent=1))


def fulfill_request(project: Path, rid: str, file_rel: str) -> dict:
    reqs = list_requests(project)
    for r in reqs:
        if r["id"] == rid:
            r["status"] = "fulfilled"
            r["file"] = file_rel
    (project / REQUESTS_FILE).write_text(json.dumps(reqs, indent=1))
    return next(r for r in reqs if r["id"] == rid)
