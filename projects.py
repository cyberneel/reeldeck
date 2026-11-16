#!/usr/bin/env python3
"""The project registry: which reels reeldeck knows about.

A project is just a directory with a reel.json in it — reeldeck never owns or
moves your files, it only remembers where they are. `projects.json` (next to
this file) is the registry: id, display name, absolute path, and an optional
folder. A folder is only a label the browser groups by (a series, say): moving a
reel between folders never moves its directory.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pipeline

HERE = Path(__file__).resolve().parent
# REELDECK_DATA holds the registry and caches (a volume in Docker); the repo by default.
DATA = Path(os.environ.get("REELDECK_DATA") or HERE)
REGISTRY = DATA / "projects.json"
# New projects land beside the app (../reeldeck-projects) unless REELDECK_PROJECTS says otherwise.
DEFAULT_ROOT = Path(os.environ.get("REELDECK_PROJECTS") or HERE.parent / "reeldeck-projects")
TTS_URL = os.environ.get("REELDECK_TTS_URL", "http://127.0.0.1:7860")


def _read() -> list[dict]:
    return json.loads(REGISTRY.read_text())["projects"] if REGISTRY.is_file() else []


def _write(rows: list[dict]) -> None:
    REGISTRY.write_text(json.dumps({"projects": rows}, indent=1) + "\n")


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def list_projects() -> list[dict]:
    """Registered projects, each annotated with whether its reel.json is still
    actually there — a moved or deleted directory shows up rather than 404s."""
    out = []
    for row in _read():
        d = Path(row["dir"])
        out.append({**row, "missing": not (d / pipeline.MANIFEST).is_file()})
    return out


def summarise(row: dict) -> dict:
    """Everything the browser needs to show a project without opening it.

    Cheap on purpose — one manifest read, one staleness pass, and a duration
    lifted from the cut plan rather than by probing the file. A gallery that
    shells out to ffprobe once per card would take longer to draw the more
    reels there are, which is exactly backwards.
    """
    d = Path(row["dir"])
    out = {**row, "missing": not (d / pipeline.MANIFEST).is_file()}
    if out["missing"]:
        return out
    try:
        m = pipeline.load(d)
    except (OSError, ValueError):
        out["missing"] = True
        return out
    outdir = d / m.get("out", "out")
    reel = outdir / "reel.mp4"
    plan = outdir / "plan.json"
    length = None
    if plan.is_file():
        try:
            segs = json.loads(plan.read_text())["segments"]
            if segs:
                length = round(segs[-1]["start_at"] + segs[-1]["length"], 2)
        except (OSError, ValueError, KeyError, IndexError):
            length = None
    lines = [l for l in (m.get("vo") or {}).get("lines", []) if l.get("text", "").strip()]
    slates = [s["name"] for s in m.get("segments", [])
              if "slate" in str((s.get("render") or {}).get("html", ""))]
    out.update({
        "title": m.get("name") or row.get("name"),
        "segments": len(m.get("segments", [])),
        "lines": len(lines),
        "effects": len(m.get("effects") or []),
        "length": length,
        "has_reel": reel.is_file(),
        "reel_mtime": round(reel.stat().st_mtime, 3) if reel.is_file() else None,
        "stale": pipeline.stale(d, m),
        "needs_footage": slates,
    })
    return out


def get_dir(pid: str) -> Path:
    for row in _read():
        if row["id"] == pid:
            return Path(row["dir"])
    raise KeyError(f"no such project: {pid!r}")


def register(directory: Path, name: str | None = None, folder: str = "") -> dict:
    """Point reeldeck at an existing directory. Scaffolds a minimal reel.json
    only if the directory is empty of one — never overwrites."""
    directory = directory.expanduser().resolve()
    rows = _read()
    if any(Path(r["dir"]).resolve() == directory for r in rows):
        raise ValueError(f"already registered: {directory}")
    if not (directory / pipeline.MANIFEST).is_file():
        directory.mkdir(parents=True, exist_ok=True)
        pipeline.save(directory, _skeleton(name or directory.name))
    display = name or (pipeline.load(directory).get("name") or directory.name)
    row = {"id": _slug(display, {r["id"] for r in rows}), "name": display,
          "dir": str(directory), "folder": folder.strip(), "created": time.time()}
    rows.append(row)
    _write(rows)
    return row


def create(name: str, folder: str = "") -> dict:
    """Scaffold a brand new project under the default root."""
    rows = _read()
    slug = _slug(name, {r["id"] for r in rows})
    directory = DEFAULT_ROOT / slug
    if directory.exists():
        raise ValueError(f"{directory} already exists")
    directory.mkdir(parents=True)
    (directory / "clips").mkdir()
    (directory / "clips" / "out").mkdir()
    pipeline.save(directory, _skeleton(name))
    row = {"id": slug, "name": name, "dir": str(directory), "folder": folder.strip(),
           "created": time.time()}
    rows.append(row)
    _write(rows)
    return row


def unregister(pid: str) -> None:
    """Forget a project. Never touches its files — this is the registry
    equivalent of closing a tab, not deleting anything."""
    rows = [r for r in _read() if r["id"] != pid]
    _write(rows)


def edit(pid: str, name: str | None = None, folder: str | None = None) -> dict:
    """Rename a project and/or file it under a folder ("" = unfiled)."""
    rows = _read()
    for r in rows:
        if r["id"] == pid:
            if name:
                r["name"] = name
            if folder is not None:
                r["folder"] = folder.strip()
            _write(rows)
            return r
    raise KeyError(pid)


def _skeleton(name: str) -> dict:
    return {
        "name": name, "out": "clips/out", "width": 1080, "height": 1920,
        "fps": 60, "bg": "#0d0d0d", "xfade": 0.12, "notes": "",
        "segments": [],
        "vo": {"server": TTS_URL, "voice": "", "lines": []},
        "audio": {"sfx": [], "music": {}},
    }
