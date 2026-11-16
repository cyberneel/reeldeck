#!/usr/bin/env python3
"""reeldeck — an end-to-end reel-making suite.

    ./server.py                     # http://<tailnet-ip>:8810, manages projects.json

Multiple reels, each its own directory with a reel.json (see projects.py). Every
project gets: a Clips library (discovered from the project's own files — HTML
templates, manim scenes, Chrome-capture beats, already-rendered footage), a
Script tab (writing, with AI), a Voice tab (TTS and recorded takes), and a Timeline tab (drag/trim/reorder,
live composited preview, transitions, sfx/music).

Stdlib only. One background job per project (rendering is real CPU/GPU time and
should stay visible and serialised per-project); different projects can run jobs
concurrently. The UI polls /api/p/<id>/status for progress.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePath
from urllib.parse import parse_qs, urlsplit

import ai
import delivery
import discovery
import pipeline
import projects
import publish
import scripts

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("REELDECK_PORT", 8810))
HOST = os.environ.get("REELDECK_HOST", "")
VERSION = (HERE / "VERSION").read_text().strip() if (HERE / "VERSION").is_file() else "dev"

# ---------- 720p preview copies ----------
# The in-app player asks for ?proxy=1: a rendered reel is ~45 Mbps at 1080x1920,
# more than a tailnet link sustains, so play waited on buffering and seeks
# stalled. The copy is ~2.5 Mbps with a keyframe every second (scrubs land
# fast) and moov first. Built on first ask, kept per source mtime, outside the
# project dirs so discovery never lists it. The master itself is untouched.
PROXY = projects.DATA / "data" / "proxy"
PROXY_LOCK = threading.Lock()   # ponytail: one encode at a time (~5 s per 24 s reel); per-file locks if that queues


def proxy_for(pid: str, rel: str, src: Path) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", f"{pid}--{rel}")
    out = PROXY / f"{stem}--{src.stat().st_mtime_ns}.mp4"
    with PROXY_LOCK:
        if not out.is_file():
            PROXY.mkdir(parents=True, exist_ok=True)
            for old in PROXY.glob(stem + "--*"):   # older renders of the same file
                old.unlink()
            tmp = out.with_suffix(".tmp.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", "scale='min(720,iw)':-2",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                            "-force_key_frames", "expr:gte(t,n_forced)", "-c:a", "copy",
                            "-movflags", "+faststart", str(tmp)], check=True, capture_output=True)
            os.replace(tmp, out)
    return out

# ---------- per-project job state ----------
# One entry per project id; a lock per project would be more "correct" but this
# app has at most a handful of projects open by one person, and a single lock
# guarding dict access (not job *execution*, which happens in its own thread)
# costs nothing and is impossible to deadlock.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _job(pid: str) -> dict:
    with JOBS_LOCK:
        return JOBS.setdefault(pid, {"running": False, "step": "", "log": [],
                                     "result": {}, "error": ""})


def _log(pid: str, msg: str) -> None:
    with JOBS_LOCK:
        # not every logger holds the job slot — saving a voice take writes a
        # line without claiming one, because a take must not be refused just
        # because a render happens to be running
        j = JOBS.setdefault(pid, {"running": False, "step": "", "log": [],
                                  "result": {}, "error": ""})
        j["log"].append(msg)
        del j["log"][:-300]


def _begin(pid: str) -> bool:
    """Claim the per-project job slot. False if one is already running."""
    with JOBS_LOCK:
        j = JOBS.setdefault(pid, {"running": False, "step": "", "log": [],
                                  "result": {}, "error": ""})
        if j["running"]:
            return False
        j.update(running=True, step="", log=[], result={}, error="")
        return True


def _end(pid: str) -> None:
    with JOBS_LOCK:
        JOBS[pid].update(running=False, step="")


def _set_step(pid: str, step: str) -> None:
    with JOBS_LOCK:
        JOBS[pid]["step"] = step


def _set_result(pid: str, key: str, val) -> None:
    with JOBS_LOCK:
        JOBS[pid]["result"][key] = val


def _fail(pid: str, e: Exception) -> None:
    with JOBS_LOCK:
        JOBS[pid]["error"] = str(e)
    _log(pid, f"✗ {e}")
    traceback.print_exc()


# ---------- job bodies (run in a background thread) ----------
def run_steps(pid: str, project: Path, steps: list[str]) -> None:
    try:
        # ["auto"] is resolved here rather than in the browser: the manifest was
        # just saved, so this sees the real current state instead of whatever the
        # UI last labelled the button with
        if steps == ["auto"]:
            steps = pipeline.stale(project, pipeline.load(project))
            if not steps:
                _log(pid, "everything is already current")
                return
            _log(pid, "update: " + " → ".join(steps))
        for step in steps:
            _set_step(pid, step)
            _log(pid, f"▶ {step}")
            # reloaded per step because `fit` rewrites reel.json
            m = pipeline.load(project)
            _set_result(pid, step,
                        pipeline.do_step(project, m, step, log=lambda s: _log(pid, s)))
    except Exception as e:
        _fail(pid, e)
    finally:
        _end(pid)


def run_line(pid: str, project: Path, i: int, force: bool) -> None:
    """Re-render one VO line, rebuild the voiceover, and re-mux — see the note
    in pipeline.py: this never writes reel.json, the browser owns that file."""
    try:
        m = pipeline.load(project)
        line = m["vo"]["lines"][i]
        _set_step(pid, f"line {i + 1}")
        _log(pid, f"▶ line {i + 1} · seed {line.get('seed', m['vo'].get('seed', 0))}")
        n = pipeline.render_lines(project, m, only=i, force=force,
                                  log=lambda s: _log(pid, s))
        r = pipeline.build_vo(project, m)
        # build_vo raises unless every line has audio, so reaching here means the
        # voice really is current — mark it, or Update would offer to redo it
        pipeline.mark_step(project, m, "vo")
        _log(pid, f"  {n} rendered · voiceover now {r['duration']}s")
        _set_result(pid, "vo", r)
        try:
            if pipeline.wants_mix(project, m):
                pipeline.do_step(project, m, "mix", log=lambda s: _log(pid, s))
            pipeline.do_step(project, m, "mux", log=lambda s: _log(pid, s))
        except Exception as e:
            _log(pid, f"  (not muxed: {e})")
        _set_result(pid, "align", pipeline.align(project, m))
    except Exception as e:
        _fail(pid, e)
    finally:
        _end(pid)


def run_generate(pid: str, project: Path, spec: dict) -> None:
    """Produce one library clip (not tied to any segment) from a Clips-tab
    'generate' form: kind + its params + the filename to land at."""
    try:
        _set_step(pid, "generate")
        _log(pid, f"▶ generate ({spec.get('kind', 'html')})")
        m = pipeline.load(project)
        cdir = discovery.clips_dir(project)
        if cdir is None:
            raise RuntimeError("no render.mjs found — this project has no clip pipeline")
        name = re.sub(r"[^a-zA-Z0-9_-]+", "-", spec.get("filename") or "clip").strip("-")
        out = cdir / "out" / f"{name}.mp4"
        if out.is_file() and not spec.get("overwrite"):
            base, n = name, 2
            while out.is_file():
                name = f"{base}-{n}"
                out = cdir / "out" / f"{name}.mp4"
                n += 1
        produced = pipeline.generate_one(project, spec, out, m.get("fps", 60),
                                         log=lambda s: _log(pid, s))
        rel = str(produced.relative_to(project))
        dur = pipeline.duration(produced)
        _log(pid, f"  {rel} · {dur:.2f}s")
        _set_result(pid, "generate", {"file": rel, "duration": round(dur, 3)})
    except Exception as e:
        _fail(pid, e)
    finally:
        _end(pid)


def _vo_ready(project: Path, m: dict) -> list[bool]:
    """Whether each line's audio exists for its current text and settings.

    Editing a line or rerolling it changes its cache key, so the old take stops
    counting — without this the Script tab cannot show which lines still owe a
    render, and a reroll looks like it did nothing.
    """
    vod = project / m.get("out", "out") / "vo"
    out = []
    for l in (m.get("vo") or {}).get("lines", []):
        if not l.get("text", "").strip():
            out.append(True)
            continue
        out.append((vod / (pipeline.line_key(m, l) + ".wav")).is_file())
    return out


def _versions(project: Path, m: dict) -> dict:
    """mtime per media file the preview loads.

    The browser keys its <video> elements on these: regenerating a clip leaves
    the path identical, so without a changing URL the player happily keeps
    playing the frames it already decoded and the retime looks like it did
    nothing.
    """
    out = project / m.get("out", "out")
    rels = [s["src"] for s in m["segments"]]
    rels += [str((out / n).relative_to(project))
             for n in ("preview.mp4", "voiceover.wav", "mixdown.wav", "reel.mp4")]
    v = {}
    for rel in rels:
        f = project / rel
        if f.is_file():
            v[rel] = round(f.stat().st_mtime, 3)
    return v


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive sounded like a win for video scrubbing (many small
    # Range requests), but it interacts badly with this minimal server: Chrome's
    # <video> HEAD-probe requests were hanging indefinitely on a kept-alive
    # connection in a way plain curl couldn't reproduce, and it was not worth
    # chasing further. This is a single local user on loopback/tailnet — a new
    # TCP handshake per request costs nothing that matters here, and "works" beats
    # "technically more efficient but occasionally hangs the whole preview."
    protocol_version = "HTTP/1.0"

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")


    def _upload(self, pid: str, project: Path):
        """Take an uploaded file into the project's clip library.

        The body is the file itself rather than a multipart form: the browser
        can post a File straight through with fetch, and this side never has to
        parse MIME boundaries. It is streamed to a temp file in chunks because
        a minute of phone video is comfortably larger than anything worth
        holding in memory. The temp file sits in the project folder, on disk:
        /tmp is RAM here, and a long 4K take filled it (HTTP 400).
        """
        name = (parse_qs(urlsplit(self.path).query).get("name") or ["clip"])[0]
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "empty upload"}, 400)
        if not _begin(pid):
            return self._json({"error": "a job is already running"}, 409)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=PurePath(name).suffix or ".bin",
                                             prefix=".upload-", dir=project, delete=False) as f:
                tmp = Path(f.name)
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            m = pipeline.load(project)
            row = discovery.import_footage(project, m, name, tmp,
                                           log=lambda s: _log(pid, s))
            _log(pid, f"  imported {row['name']}")
            return self._json(row)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip().split("\n")[-1] or "ffmpeg failed"
            return self._json({"error": f"could not read that file: {err}"}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        finally:
            _end(pid)
            if tmp and tmp.exists():
                tmp.unlink()

    def _take(self, pid: str, project: Path):
        """Save a voice take recorded over the picture in the browser.

        The body is the raw MediaRecorder blob (webm/opus). It comes back as a
        row rather than being written into the manifest here: the editor
        autosaves reel.json on every keystroke, so a take appended server-side
        would be erased by the next autosave carrying the browser's older copy.
        The browser owns the manifest; this owns the file.
        """
        q = parse_qs(urlsplit(self.path).query)
        at = max(0.0, float((q.get("at") or ["0"])[0]))
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "empty recording"}, 400)
        m = pipeline.load(project)
        takes = project / m.get("out", "out") / "vo" / "takes"
        takes.mkdir(parents=True, exist_ok=True)
        i = 1 + max([int(f.stem.split("-")[-1]) for f in takes.glob("take-*.wav")
                     if f.stem.split("-")[-1].isdigit()] or [0])
        dest = takes / f"take-{i}.wav"
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
                tmp = Path(f.name)
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            # match the voiceover bus exactly, so the mix never resamples
            pipeline._run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp),
                           "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dest)])
            row = {"file": str(dest.relative_to(project)), "at": at}
            # a per-line read is placed on its line, not on a playhead, so the
            # second you spent finding the button must go: trim it to the voice
            if (q.get("trim") or [""])[0] == "1":
                row["voice"] = pipeline.trim_to_voice(dest)
            row["dur"] = pipeline.duration(dest)
            _log(pid, f"  take {i} · {row['dur']:.2f}s at {at:.2f}s" +
                 {True: " · trimmed to the voice", False: " · no voice heard"}.get(row.get("voice"), ""))
            return self._json(row)
        except subprocess.CalledProcessError as e:
            err = (e.stderr or "").strip().split("\n")[-1] or "ffmpeg failed"
            return self._json({"error": f"could not read that recording: {err}"}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        finally:
            if tmp and tmp.exists():
                tmp.unlink()

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _file(self, path: Path, download_name: str | None = None):
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = {
            ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
            ".mp4": "video/mp4", ".wav": "audio/wav", ".mp3": "audio/mpeg",
            ".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json", ".svg": "image/svg+xml",
        }.get(path.suffix, "application/octet-stream")
        st = path.stat()
        size, etag = st.st_size, f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m[1] or m[2]) and self.headers.get("If-Range", etag) == etag:
            if m[1]:
                start, end = int(m[1]), min(int(m[2]), size - 1) if m[2] else size - 1
            else:
                start = max(0, size - int(m[2]))      # bytes=-N: the last N bytes
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        # ?v=<mtime> URLs are new whenever the bytes are, so the browser may keep
        # them (replays and re-scrubs come from its cache); bare paths stay no-store
        versioned = "v" in parse_qs(urlsplit(self.path).query)
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if versioned else "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f'inline; filename="{download_name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        # stream it: reading the whole range first held a 138 MB reel in RAM
        # per request before the first byte left
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0 and (b := f.read(min(left, 1 << 20))):
                self.wfile.write(b)
                left -= len(b)

    def _proj_or_404(self, pid: str) -> Path | None:
        try:
            return projects.get_dir(pid)
        except KeyError:
            self._json({"error": f"no such project: {pid}"}, 404)
            return None

    # ---- routing ----
    # Chrome preflights <video src> with a HEAD request before ever issuing the
    # real GET/Range fetch; BaseHTTPRequestHandler has no do_HEAD by default, so
    # every video in the preview silently sat at readyState 0 forever with no
    # visible error anywhere except the network log. _send() already special-
    # cases `self.command != "HEAD"` to skip the body — this alias is the only
    # piece that was missing.
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlsplit(self.path)
        p, q = u.path, parse_qs(u.query)
        if p in ("/", "/index.html"):
            return self._file(HERE / "index.html")
        if p == "/master":
            return self._file(HERE / "master.html")
        if p.startswith("/static/"):
            return self._file(HERE / p[1:])

        if p == "/api/app":   # the shell's boot call, polled while the sign-in gate is up
            return self._json({"version": VERSION, "taildrop": bool(shutil.which("tailscale")),
                               "ai": ai.state(wallet="wallet" in q)})
        if p == "/api/projects":
            # ?detail=1 costs a manifest read and a staleness pass per project;
            # the plain list stays cheap for the switcher that only needs names
            if (q.get("detail") or [""])[0]:
                return self._json({"projects": [projects.summarise(r)
                                                for r in projects.list_projects()]})
            return self._json({"projects": projects.list_projects()})
        if p == "/api/publish":
            return self._json(publish.listing())
        if p == "/api/publish/quota":
            return self._pub(publish.quota)
        if p == "/api/comments":
            return self._json(publish.remembered()) if "seen" in q else self._pub(publish.inbox, "fresh" in q)
        mc = re.match(r"^/api/comments/(\d+)$", p)
        if mc:
            return self._pub(lambda: {**publish.reel(mc.group(1)), "auto": publish.auto_view()})
        # an agent's reads (publish_mcp.py): the newest comments, and Instagram's numbers
        if p == "/api/comments/recent":
            return self._pub(publish.recent, (q.get("limit") or [20])[0], "unanswered" in q)
        if p == "/api/metrics":
            return self._pub(publish.metrics, (q.get("reel") or [""])[0])
        if p == "/api/insights":
            return self._pub(publish.insights, (q.get("days") or [7])[0])

        m3 = re.match(r"^/api/p/([^/]+)/(.+)$", p)
        if m3:
            pid, rest = m3.groups()
            project = self._proj_or_404(pid)
            if project is None:
                return
            return self._api_get(pid, project, rest, q)

        m4 = re.match(r"^/media/p/([^/]+)/(.+)$", p)
        if m4:
            pid, rel = m4.groups()
            project = self._proj_or_404(pid)
            if project is None:
                return
            if rel == "__thumb__":
                return self._thumb(project, q)
            # project-root-relative, matching every path the manifest and
            # discovery already use (segment `src`, static footage, templates) —
            # resolving against `out` instead was a mismatch: a segment's own
            # src is "clips/out/x.mp4" already, not relative to out a second time
            target = (project / rel).resolve()
            base = project.resolve()
            if not str(target).startswith(str(base)):
                return self._json({"error": "nope"}, 403)
            if "proxy" in q and target.suffix == ".mp4" and target.is_file():
                try:
                    target = proxy_for(pid, rel, target)
                except (subprocess.CalledProcessError, OSError):
                    traceback.print_exc()   # the master still plays, just heavier
            return self._file(target)

        return self._json({"error": "not found"}, 404)

    def _clean(self, pid: str, project: Path, q: dict):
        """One take through the Script tab's sliders, to hear before a mix.
        The sliders ride in the query, so an unsaved drag previews too."""
        if _job(pid)["running"]:
            return self._json({"error": "something is rendering — preview after it"}, 409)
        m = pipeline.load(project)
        base = project.resolve()
        raw = (base / (q.get("file") or [""])[0]).resolve()
        if raw.parent != base / m.get("out", "out") / "vo" / "takes" or raw.suffix != ".wav" \
                or not raw.is_file():
            return self._json({"error": "not a take"}, 400)
        try:
            tone = {k: float(q[k][0]) for k in pipeline.TONE if k in q}
        except ValueError:
            return self._json({"error": "bad slider value"}, 400)
        m.setdefault("vo", {})["tone"] = tone
        out = pipeline.clean_take(base, m, {"file": str(raw.relative_to(base))})
        if out is None:
            return self._json({"error": "no voice in this take"})
        return self._json({"file": str(out.relative_to(base)), "v": round(out.stat().st_mtime, 3)})

    def _delivery(self, pid: str, project: Path):
        """Measure a take against its line's TTS read and keep the pair
        (delivery.py). Not a job: it only reads audio and appends a row."""
        b = self._body()
        m = pipeline.load(project)
        base = project.resolve()
        take = (base / str(b.get("file", ""))).resolve()
        i = b.get("i")
        if take.parent != base / m.get("out", "out") / "vo" / "takes" or take.suffix != ".wav" \
                or not take.is_file():
            return self._json({"error": "not a take"}, 400)
        if not isinstance(i, int) or not 0 <= i < len(m["vo"]["lines"]):
            return self._json({"error": "no such line"}, 400)
        try:
            return self._json(delivery.record(base, pid, m, i, str(take.relative_to(base))))
        except Exception as e:
            return self._json({"error": f"could not measure it: {e}"}, 400)

    def _ai(self, act: str):
        """Settings → AI, the sign-in gate's buttons, and the Script tab's assistant."""
        b = self._body()
        try:
            if act == "press":
                return self._json({"url": ai.UI.press(str(b.get("action", "")))})
            if act == "settings":
                return self._json(ai.configure(b))
            if act == "choose-again":
                return self._json(ai.ask_again())
            if act == "assist":
                return self._json(ai.assist(b))
        except ai.AIError as e:
            return self._json({"error": str(e), "link": e.link}, 502)
        except (ValueError, TypeError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def _pub(self, fn, *a):
        """A Publish tab / MCP call: bad input is the caller's, a Graph error is Meta's."""
        try:
            return self._json(fn(*a))
        except KeyError as e:
            return self._json({"error": f"no such reel: {e}"}, 404)
        except (ValueError, TypeError) as e:
            return self._json({"error": str(e)}, 400)
        except RuntimeError as e:
            return self._json({"error": str(e)}, 502)

    def _thumb(self, project: Path, q: dict):
        src = (q.get("src") or [""])[0]
        if not src:
            return self._json({"error": "?src required"}, 400)
        m = pipeline.load(project)
        # ?at= picks the frame: the Publish tab shows the cover thumb_offset lands on
        t = discovery.thumbnail(project, m, src, float((q.get("at") or ["0.3"])[0]))
        if t is None:
            return self._json({"error": "could not make a thumbnail"}, 404)
        return self._file(t)

    def _api_get(self, pid: str, project: Path, rest: str, q: dict):
        if rest == "project":
            m = pipeline.load(project)
            try:
                voices = pipeline.voices(m)
            except Exception:
                voices = []
            out = project / m.get("out", "out")
            planfile = out / "plan.json"
            cached = json.loads(planfile.read_text()) if planfile.is_file() else {}
            lj = out / "vo" / "lines.json"
            voinfo = json.loads(lj.read_text()) if lj.is_file() else []
            mf = project / pipeline.MANIFEST
            return self._json({
                "manifest": m, "voices": voices, "project": str(project),
                "rev": round(mf.stat().st_mtime, 3),
                "plan": cached.get("segments", []),
                "vo_lines": voinfo,
                "has_preview": (out / "preview.mp4").is_file(),
                "has_vo": (out / "voiceover.wav").is_file(),
                "has_reel": (out / "reel.mp4").is_file(),
                "has_mix": (out / "mixdown.wav").is_file(),
                "stale": pipeline.stale(project, m),
                "srcv": _versions(project, m),
                "vo_ready": _vo_ready(project, m),
                "discovery": discovery.discover(project, m),
                "requests": discovery.list_requests(project),
            })
        if rest == "status":
            return self._json(_job(pid))
        if rest == "clean":
            return self._clean(pid, project, q)
        if rest == "align":
            return self._json(pipeline.align(project))
        if rest == "delivery":
            return self._json({"rows": delivery.latest(pid)})
        if rest == "discovery":
            return self._json(discovery.discover(project, pipeline.load(project)))
        if rest == "script":
            return self._json({"script": scripts.load(project), "versions": scripts.versions(project)})
        mv = re.match(r"^script/versions/(\w+)$", rest)
        if mv:
            try:
                return self._json({"script": scripts.version(project, mv.group(1))})
            except KeyError:
                return self._json({"error": "no such version"}, 404)
        if rest == "taildrop":
            return self._json({"targets": taildrop_targets(),
                               "files": exports(project, pipeline.load(project))})
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        m = re.match(r"^/api/publish/([^/]+)$", self.path)
        if m:
            return self._pub(publish.edit, m.group(1), self._body())
        m = re.match(r"^/api/p/([^/]+)/project$", self.path)
        if m:
            pid = m.group(1)
            project = self._proj_or_404(pid)
            if project is None:
                return
            body = self._body()
            mf = project / pipeline.MANIFEST
            seen = body.pop("_rev", None)
            rev = round(mf.stat().st_mtime, 3) if mf.is_file() else None
            if seen is not None and rev is not None and abs(seen - rev) > 0.002:
                return self._json({"error": "stale", "rev": rev}, 409)
            pipeline.save(project, body)
            # staleness comes back with the save so the Update button can
            # relabel itself as you edit, without polling for it
            return self._json({"ok": True, "rev": round(mf.stat().st_mtime, 3),
                               "stale": pipeline.stale(project, body)})
        m = re.match(r"^/api/p/([^/]+)/script$", self.path)
        if m:
            project = self._proj_or_404(m.group(1))
            if project is None:
                return
            try:
                return self._json({"ok": True, "script": scripts.save(project, self._body())})
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
        m = re.match(r"^/api/projects/([^/]+)$", self.path)
        if m:
            body = self._body()
            try:
                row = projects.edit(m.group(1), body.get("name"), body.get("folder"))
            except KeyError:
                return self._json({"error": "not found"}, 404)
            return self._json(row)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        # match on the path alone: self.path still carries the query string, and
        # a route that takes parameters (upload?name=...) never matched without
        # this
        path = urlsplit(self.path).path
        if path == "/api/projects":
            body = self._body()
            try:
                if body.get("dir"):
                    row = projects.register(Path(body["dir"]), body.get("name"), body.get("folder") or "")
                else:
                    row = projects.create(body.get("name") or "untitled reel", body.get("folder") or "")
            except (ValueError, OSError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json(row)

        if path.startswith("/api/ai/"):
            return self._ai(path[8:])

        if path == "/api/publish/token":   # the tab's paste box; the token is never sent back
            return self._pub(publish.save_token, self._body().get("token", ""))
        m = re.match(r"^/api/publish/([^/]+)/(schedule|unschedule|stage)$", path)
        if m:
            pid, act = m.groups()
            if act == "schedule":
                return self._pub(lambda: publish.schedule(pid, float(self._body().get("at", ""))))
            return self._pub(getattr(publish, {"unschedule": "unschedule", "stage": "stage_now"}[act]), pid)
        if path == "/api/comments/auto":   # the tab's bank of replies, and a reel's auto answer switched on or off
            return self._pub(publish.auto_set, self._body())
        m = re.match(r"^/api/comments/(\d+)/reply$", path)
        if m:
            b = self._body()
            return self._pub(publish.reply, m.group(1), b.get("text", ""), b.get("dm", ""), b.get("need_dm") is True)

        m = re.match(r"^/api/p/([^/]+)/(.+)$", path)
        if not m:
            return self._json({"error": "not found"}, 404)
        pid, rest = m.groups()
        project = self._proj_or_404(pid)
        if project is None:
            return

        if rest == "run":
            if not _begin(pid):
                return self._json({"error": "a job is already running"}, 409)
            steps = self._body().get("steps") or ["assemble"]
            threading.Thread(target=run_steps, args=(pid, project, steps),
                             daemon=True).start()
            return self._json({"ok": True, "steps": steps})

        if rest == "line":
            if not _begin(pid):
                return self._json({"error": "a job is already running"}, 409)
            b = self._body()
            threading.Thread(target=run_line, daemon=True,
                             args=(pid, project, int(b.get("i", 0)),
                                   bool(b.get("force", True)))).start()
            return self._json({"ok": True})

        if rest == "generate":
            if not _begin(pid):
                return self._json({"error": "a job is already running"}, 409)
            spec = self._body()
            threading.Thread(target=run_generate, daemon=True,
                             args=(pid, project, spec)).start()
            return self._json({"ok": True})

        if rest == "upload":
            return self._upload(pid, project)

        if rest == "script/versions":
            return self._json(scripts.snapshot(project, self._body().get("label", "")))

        if rest == "taildrop":
            b = self._body()
            rel, target = b.get("file", ""), b.get("target", "")
            # both come off a menu the server itself built; anything else is
            # refused rather than handed to a subprocess
            if rel not in exports(project, pipeline.load(project)):
                return self._json({"error": f"not an export: {rel}"}, 400)
            if target not in {t["name"] for t in taildrop_targets()}:
                return self._json({"error": f"not a taildrop target: {target}"}, 400)
            # every project's cut is called reel.mp4 — prefix the project so
            # they don't overwrite each other on the phone
            name = f"{pid}-{Path(rel).name}"
            r = subprocess.run(["tailscale", "file", "cp", "--update-interval=0",
                                f"--name={name}", str(project / rel), f"{target}:"],
                               capture_output=True, text=True, timeout=600)
            if r.returncode:
                err = (r.stderr or r.stdout).strip().split("\n")[-1] or "tailscale file cp failed"
                return self._json({"error": err}, 502)
            _log(pid, f"  taildrop {name} → {target}")
            return self._json({"ok": True, "name": name})

        if rest == "take":
            return self._take(pid, project)

        if rest == "delivery":
            return self._delivery(pid, project)

        if rest == "requests":
            b = self._body()
            row = discovery.add_request(project, b.get("prompt", ""),
                                        b.get("duration_hint"), b.get("notes", ""),
                                        b.get("target", ""))
            return self._json(row)

        m2 = re.match(r"^requests/([^/]+)/fulfill$", rest)
        if m2:
            b = self._body()
            row = discovery.fulfill_request(project, m2.group(1), b.get("file", ""))
            return self._json(row)

        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        m = re.match(r"^/api/projects/([^/]+)$", self.path)
        if m:
            projects.unregister(m.group(1))
            return self._json({"ok": True})
        m = re.match(r"^/api/p/([^/]+)/requests/([^/]+)$", self.path)
        if m:
            pid, rid = m.groups()
            project = self._proj_or_404(pid)
            if project is None:
                return
            discovery.remove_request(project, rid)
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def taildrop_targets() -> list[dict]:
    """Devices `tailscale file cp` can send to, online ones first."""
    try:
        out = subprocess.run(["tailscale", "file", "cp", "--targets"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():   # "ip<TAB>name[<TAB>offline; last seen …]"
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append({"name": parts[1], "online": "offline" not in "".join(parts[2:])})
    return sorted(rows, key=lambda r: not r["online"])


def exports(project: Path, m: dict) -> list[str]:
    """What's worth sending to a phone: the cut, plus finished files kept at
    the project root (a music-only cut, its poster, the caption)."""
    files = [project / m.get("out", "out") / "reel.mp4"]
    files += sorted(p for p in project.iterdir() if p.suffix in (".mp4", ".jpg", ".txt"))
    return [str(p.relative_to(project)) for p in files if p.is_file()]


def tailnet_ip(wait: int = 20) -> str:
    deadline = time.time() + wait
    while True:
        try:
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                text=True, timeout=5).stdout.split("\n")[0].strip()
            if ip:
                return ip
        except Exception:
            pass
        if time.time() >= deadline:
            return "127.0.0.1"
        time.sleep(2)


if __name__ == "__main__":
    import sys
    if os.environ.get("PUBLIK_SELF_TEST"):   # builder check: prints publik's self-test line, then exits
        ai.publik.ensure_signed_in()
    # backward compat: `server.py <dir>` auto-registers that project instead of
    # locking the whole server onto it, then starts normally
    if len(sys.argv) > 1:
        d = Path(sys.argv[1]).expanduser().resolve()
        if (d / pipeline.MANIFEST).is_file() and not any(
                Path(r["dir"]).resolve() == d for r in projects.list_projects()):
            projects.register(d)

    publish.start()   # the Instagram queue (dry run until there's a token)
    ai.start_gate()   # Sign in with publik, drawn by the page (REELDECK_PUBLIK=off skips it)
    print(f"reeldeck {VERSION}  ·  {len(projects.list_projects())} project(s) registered", flush=True)
    if HOST:   # Docker / LAN: one explicit bind, e.g. REELDECK_HOST=0.0.0.0
        print(f"  http://{HOST}:{PORT}/", flush=True)
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    # Default: loopback, plus the tailnet address as a second socket when
    # tailscale is installed — never 0.0.0.0, so nothing is reachable from the
    # LAN. Loopback matters because the browser refuses getUserMedia on an
    # insecure origin: recording a voice take only works at http://localhost.
    loop = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"  http://localhost:{PORT}/", flush=True)
    ip = tailnet_ip() if shutil.which("tailscale") else ""
    if not ip or ip == "127.0.0.1":
        loop.serve_forever()
    threading.Thread(target=loop.serve_forever, daemon=True).start()
    print(f"  http://{ip}:{PORT}/  (tailnet; a mic from another device needs `tailscale serve`)", flush=True)
    ThreadingHTTPServer((ip, PORT), Handler).serve_forever()
