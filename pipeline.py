#!/usr/bin/env python3
"""Reel pipeline: manifest -> cut -> voiceover -> mux, plus alignment measurement.

One JSON manifest per reel project is the single source of truth. Everything the
editor does is a function in here, so the UI and the command line can never drift
apart.

    python3 pipeline.py <project-dir> clips|assemble|vo|mux|align|all

The manifest (reel.json) lives in the project dir; every path inside it is
relative to that dir, so a project is self-contained and movable.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST = "reel.json"


# ---------- manifest ----------
def load(project: Path) -> dict:
    return json.loads((project / MANIFEST).read_text())


def save(project: Path, m: dict) -> None:
    (project / MANIFEST).write_text(json.dumps(m, indent=2) + "\n")


def _run(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(_first_error((p.stderr or p.stdout).strip()))
    return p.stdout


def duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path)])
    return float(out.strip())


# ---------- generated clips ----------
# A segment may carry a `render` block, meaning its source is produced rather
# than being fixed footage. Three kinds, all dispatched from the same field:
#
#   html:    { "kind": "html", "html": "clips/04-punchline.html", "secs": 2.0,
#              "params": { "words": "Claude|can|be|better" } }
#            (kind defaults to "html" when omitted, for manifests written before
#            "kind" existed — the punchline segment predates this field)
#   manim:   { "kind": "manim", "file": "clips/scenes.py", "scene": "TmuxPersistence" }
#   capture: { "kind": "capture", "script": "clips/capture-xvfb.mjs", "beat": "grid" }
#
# For html clips, `secs` genuinely retimes the animation only when the template
# itself reads ?secs into its own pacing math (discovery.py's `retimes` flag) —
# otherwise it trims or freeze-holds the last frame, same as any fixed clip.
def clip_sig(project: Path, r: dict) -> str | None:
    """A signature that changes whenever re-rendering would change the output.

    Capture clips have no signature: they record a live app, so "unchanged
    inputs" is not a concept that applies to them. They render only when the
    output is missing or a render is explicitly forced.
    """
    kind = r.get("kind", "html" if "html" in r else None)
    if kind == "manim":
        src = (project / r["file"]).read_bytes()
        payload = [hashlib.md5(src).hexdigest(), r["scene"]]
    elif kind == "capture":
        return None
    else:
        path = project / r["html"]
        html = path.read_bytes()
        payload = [hashlib.md5(html).hexdigest(), r.get("secs"), r.get("params", {})]
        # the local scripts it loads (data, a music RMS track) change the picture
        # too; ponytail: one level deep, a script's own imports aren't followed
        deps = sorted({d for d in re.findall(rb"""(?:src=|from\s*)["']\.?/?([\w./-]+\.js)["']""", html)})
        payload += [hashlib.md5((path.parent / d.decode()).read_bytes()).hexdigest()
                    for d in deps if (path.parent / d.decode()).is_file()]
    return hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def generate_one(project: Path, r: dict, out: Path, fps: int, log=lambda s: None) -> Path:
    """Run whatever `r` describes and land the result at `out`.

    The single dispatch point for all three generator kinds. render_clips()
    (segment-bound: out = seg["src"]) and the Clips-tab "generate a variation"
    endpoint (library-bound: out = a filename the user picked) both call this,
    so the html/manim/capture logic exists exactly once.
    """
    import discovery
    kind = r.get("kind", "html" if "html" in r else None)
    if kind == "manim":
        log(f"  rendering (manim: {r['scene']})")
        produced = discovery.run_manim(project, r["file"], r["scene"], fps, log)
    elif kind == "capture":
        log(f"  capturing (beat: {r['beat']})")
        produced = discovery.run_capture(project, r["script"], r["beat"], log)
    else:
        html = project / r["html"]
        query = dict(r.get("params", {}))
        if r.get("secs") is not None:
            query["secs"] = r["secs"]
        target = html.name + ("?" + urllib.parse.urlencode(query) if query else "")
        # secs may be "" — render.mjs then reads the clip's own DURATION
        args = ["node", "render.mjs", target, str(out.resolve()),
                str(r["secs"]) if r.get("secs") is not None else "", str(fps)]
        log(f"  rendering ({r.get('secs', 'auto')}s)")
        _run_in(html.parent, args)
        produced = out

    if produced.resolve() != out.resolve():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(produced.read_bytes())
    return out


def clips_pending(project: Path, m: dict) -> list[str]:
    """Names of generated segments whose source would change if re-rendered.

    Same skip test render_clips() uses, asked without rendering anything, so the
    editor can say what an update would actually do before you press it.
    """
    sigdir = project / m.get("out", "out") / ".clips"
    out = []
    for seg in m["segments"]:
        r = seg.get("render")
        if not r:
            continue
        try:
            sig = clip_sig(project, r)
        except OSError:
            out.append(seg["name"])       # template gone missing — let the render report it
            continue
        sigfile = sigdir / (seg["name"] + ".sig")
        current = (project / seg["src"]).is_file() and (
            sig is None or (sigfile.is_file() and sigfile.read_text().strip() == sig))
        if not current:
            out.append(seg["name"])
    return out


def render_clips(project: Path, m: dict, force: bool = False,
                 log=lambda s: None) -> int:
    made = 0
    sigdir = project / m.get("out", "out") / ".clips"
    sigdir.mkdir(parents=True, exist_ok=True)
    for seg in m["segments"]:
        r = seg.get("render")
        if not r:
            continue
        out = project / seg["src"]
        sigfile = sigdir / (seg["name"] + ".sig")
        sig = clip_sig(project, r)
        skip = not force and out.is_file() and (
            sig is None or (sigfile.is_file() and sigfile.read_text().strip() == sig))
        if skip:
            continue
        generate_one(project, r, out, m.get("fps", 60),
                    lambda s, n=seg["name"]: log(f"  [{n}]{s}"))
        if sig is not None:
            sigfile.write_text(sig)
        made += 1
    if not made:
        log("  every generated clip is current")
    return made


def _first_error(text: str) -> str:
    """The line worth reading out of a subprocess failure.

    Taking the last line seemed reasonable until node reported a clip that threw
    as "Node.js v24.14.1" — its version banner is the last thing it prints, and
    the actual TypeError is near the top. Prefer the first line that looks like
    an error and keep a little context after it.
    """
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return "failed with no output"
    for i, l in enumerate(lines):
        if re.search(r"(?:\w*Error|Exception|error:|failed)\b", l):
            return "\n".join(lines[i:i + 3])
    return lines[-1]


def _run_in(cwd: Path, args: list[str]) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, cwd=str(cwd))
    except FileNotFoundError:
        raise RuntimeError(
            f"{args[0]!r} is not on PATH — install it, or add its directory to the "
            f"service's PATH (pip tools usually land in ~/.local/bin)") from None
    if p.returncode != 0:
        raise RuntimeError(_first_error((p.stderr or p.stdout).strip()))
    return p.stdout


# ---------- assemble ----------
# x264 defaults to a ~250-frame GOP, which at 60fps means a keyframe every four
# seconds — every scrub in between has to decode forward from one, so dragging
# the playhead crawls. Half-second keyframes cost a little size on a 40-second
# reel and make it seekable; faststart puts the index at the front so the
# browser can start playing (and seeking) before the whole file has arrived.
def _SEEKABLE(fps: int) -> list[str]:
    g = max(1, int(fps) // 2)
    return ["-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0",
            "-movflags", "+faststart"]


def round_mask(project: Path, m: dict, w: int, h: int, radius: int) -> Path:
    """A rounded-rectangle alpha mask, generated once and cached.

    Built with a per-pixel `geq` expression, which is far too slow to run on
    every frame of every render — so it runs on one frame and the result is
    reused as a plain image via alphamerge.
    """
    d = project / m.get("out", "out") / ".masks"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"round-{w}x{h}-{radius}.png"
    if not f.is_file():
        # distance from the nearest corner centre; inside the radius -> opaque
        expr = (f"if(lte(hypot("
                f"max(0\\,max({radius}-X\\,X-({w}-1-{radius})))\\,"
                f"max(0\\,max({radius}-Y\\,Y-({h}-1-{radius}))))\\,{radius}),255,0)")
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
              "-i", f"color=black:s={w}x{h}:d=1",
              "-vf", f"format=gray,geq=lum='{expr}'", "-frames:v", "1", str(f)])
    return f


def _normalise(project: Path, seg: dict, tmp: Path, i: int, m: dict,
               freeze: float = 0.0) -> Path:
    """Cut a segment out of its source and force it to the reel's format.

    `freeze` holds the last frame for that many seconds, for when the segment is
    asked to run longer than its source can provide.

    A segment may carry `layers`: further clips composited over it, each scaled,
    positioned and optionally corner-rounded. That is how a phone screen and a
    watch end up in one shot instead of two consecutive ones.
    """
    args = ["ffmpeg", "-v", "error", "-y"]
    if seg.get("start") not in (None, ""):
        args += ["-ss", str(seg["start"])]
    args += ["-i", str(project / seg["src"])]
    # `-t` goes on the OUTPUT, not here. As an input option it binds to whatever
    # -i comes next, so once layers started appending their own inputs it began
    # trimming the overlay instead of the segment — and with a `-loop 1` mask
    # input (an infinite stream) nothing bounded the encode at all and it ran
    # until killed.
    want = None
    if seg.get("dur") not in (None, ""):
        want = float(seg["dur"])
    w, h, fps = m.get("width", 1080), m.get("height", 1920), m.get("fps", 60)
    bg = m.get("bg", "#0d0d0d")
    out = tmp / f"{i:02d}-{seg['name']}.mp4"
    # How a source that isn't the reel's aspect ratio gets fitted.
    #
    #   cover   (default) scale up until the frame is full, then centre-crop.
    #   blur    show the whole frame, and fill the leftover margin with a
    #           blurred, darkened copy of the clip itself. Nothing is cropped
    #           and there are no flat bars — the right choice when the edges of
    #           the source matter (a Dynamic Island, a keyboard, a status bar).
    #   contain show the whole frame, pad the margin with the reel's flat bg.
    fit = seg.get("fit") or m.get("fit") or "cover"
    # setpts first: screen recordings off a phone are variable-frame-rate with
    # wall-clock timestamps, and a source whose PTS does not start at zero
    # makes the offsets in assemble()'s crossfade chain mean the wrong thing.
    pre = f"setpts=PTS-STARTPTS,fps={fps}"
    if fit == "blur":
        # One filtergraph, single in / single out, so it still fits in -vf.
        #
        # The backdrop is deliberately overscaled (1.35x) before blurring: fill
        # the frame exactly and the blurred copy still lines up with the sharp
        # one, so the seam reads as a doubled, mismatched edge. Pushing it out
        # first breaks that alignment, and a wide sigma plus real darkening and
        # desaturation stops it competing with the frame it is framing.
        zw, zh = int(w * 1.35), int(h * 1.35)
        chain = (
            f"{pre},split[bg][fg];"
            f"[bg]scale={zw}:{zh}:force_original_aspect_ratio=increase,crop={zw}:{zh},"
            f"gblur=sigma=64,eq=brightness=-0.17:saturation=0.55,"
            f"scale={w}:{h}[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    elif fit == "cover":
        chain = (f"{pre},scale={w}:{h}:force_original_aspect_ratio=increase,"
                 f"crop={w}:{h},setsar=1")
    else:
        chain = (f"{pre},scale={w}:{h}:force_original_aspect_ratio=decrease,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1")
    if freeze > 0:
        # Hold the last frame instead of handing back a short clip. `-t` still
        # trims to the requested length afterwards, so the result is exactly the
        # duration asked for.
        chain += f",tpad=stop_mode=clone:stop_duration={freeze:.3f}"

    layers = seg.get("layers") or []
    if not layers:
        args += ["-vf", chain]
    else:
        # Each layer is an extra input, plus a cached mask input when it wants
        # rounded corners. Indices are assigned as inputs are appended — working
        # them out afterwards is how you end up compositing the wrong stream.
        fc = [f"[0:v]{chain}[base]"]
        prev, idx = "[base]", 1
        for n, L in enumerate(layers):
            if L.get("start") not in (None, ""):
                args += ["-ss", str(L["start"])]
            args += ["-i", str(project / L["src"])]
            lin, idx = f"[{idx}:v]", idx + 1

            lw = int(w * float(L.get("scale", 0.5)))
            lw -= lw % 2                       # libx264 needs even dimensions
            lh = int(round(lw * duration_ratio(project / L["src"])))
            lh -= lh % 2
            # A delayed layer is shifted in time rather than started late: shift
            # its PTS so it begins at frame 0 exactly when it appears, instead of
            # playing unseen behind the base and cutting in mid-shot.
            delay = float(L.get("delay", 0) or 0)
            shift = f",setpts=PTS+{delay}/TB" if delay else ""
            fc.append(f"{lin}setpts=PTS-STARTPTS{shift},scale={lw}:{lh}[l{n}]")
            src_lab = f"[l{n}]"

            # `mask` is an image whose luma becomes the layer's alpha — the way
            # to cut out a shape a rounded rectangle cannot describe, like a
            # watch with a crown and buttons sticking out of one side.
            maskfile = None
            if L.get("mask"):
                maskfile = project / L["mask"]
            elif float(L.get("round", 0) or 0) > 0:
                maskfile = round_mask(project, m, lw, lh, int(lw * float(L["round"])))
            if maskfile:
                args += ["-loop", "1", "-i", str(maskfile)]
                fc.append(f"[{idx}:v]scale={lw}:{lh},format=gray[m{n}]")
                idx += 1
                fc.append(f"{src_lab}format=yuva420p[l{n}f]")
                fc.append(f"[l{n}f][m{n}]alphamerge[l{n}a]")
                src_lab = f"[l{n}a]"

            fade = float(L.get("fade", 0.3) or 0)
            if fade > 0:
                fc.append(f"{src_lab}fade=t=in:st={delay}:d={fade}:alpha=1[l{n}v]")
                src_lab = f"[l{n}v]"

            x = L.get("x", "center")
            y = L.get("y", "center")
            xe = "(W-w)/2" if x == "center" else str(x)
            ye = "(H-h)/2" if y == "center" else str(y)
            en = f":enable='gte(t,{delay})'" if delay else ""
            out_lab = "[comp]" if n == len(layers) - 1 else f"[c{n}]"
            fc.append(f"{prev}{src_lab}overlay={xe}:{ye}:shortest=0{en}{out_lab}")
            prev = out_lab
        args += ["-filter_complex", ";".join(fc), "-map", prev]

    if want is None and layers:
        # a looped mask never ends, so an unbounded output would never finish;
        # fall back to however much source this segment actually has
        want = max(0.1, duration(project / seg["src"]) - float(seg.get("start") or 0)) + freeze
    if want is not None:
        args += ["-t", f"{want:.3f}"]

    args += ["-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
             "-pix_fmt", "yuv420p", *_SEEKABLE(fps), str(out)]
    _run(args)
    return out


def duration_ratio(path: Path) -> float:
    """height / width of a video, so a layer's mask matches its real shape."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)])
    vw, vh = (int(x) for x in out.strip().split("x")[:2])
    return vh / vw


def plan(project: Path, m: dict, durs: list[float]) -> list[dict]:
    """Where each segment lands in the finished cut.

    Segments with xfade 0 are concatenated onto the previous group with no blend
    (a fade into black leaves a dimmed frame, which reads as a glitch); the
    groups themselves are cross-faded, so each group start pulls back by its own
    fade length.
    """
    default = float(m.get("xfade", 0.12))
    fades = [0.0] + [default if s.get("xfade") in (None, "") else float(s["xfade"])
                     for s in m["segments"][1:]]

    groups: list[list[int]] = [[0]]
    gfade = [0.0]
    for i in range(1, len(m["segments"])):
        if fades[i] == 0:
            groups[-1].append(i)
        else:
            groups.append([i])
            gfade.append(fades[i])

    out = [dict(s) for s in m["segments"]]
    clock = 0.0
    for k, members in enumerate(groups):
        clock = clock - gfade[k] if k else 0.0
        for idx in members:
            out[idx]["start_at"] = round(clock, 3)
            out[idx]["length"] = round(durs[idx], 3)
            clock += durs[idx]
    return out


def assemble(project: Path, m: dict | None = None, log=lambda s: None) -> dict:
    m = m or load(project)
    outdir = project / m.get("out", "out")
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / "preview.mp4"

    tmp = Path(tempfile.mkdtemp(prefix="reeldeck-"))
    try:
        segs, durs, freezes = [], [], []
        for i, s in enumerate(m["segments"]):
            src = project / s["src"]
            if not src.is_file():
                raise RuntimeError(f"missing source: {s['src']}")

            # Asking for more than the source holds used to truncate silently,
            # which shifted every beat after it — sfx and voice then landed at
            # times the timeline never showed. Hold the last frame instead.
            freeze, asked = 0.0, s.get("dur")
            if asked not in (None, ""):
                avail = duration(src) - float(s.get("start") or 0)
                if avail <= 0:
                    raise RuntimeError(
                        f"{s['name']}: in-point {s.get('start')}s is at or past the "
                        f"end of {s['src']} ({duration(src):.2f}s)")
                if float(asked) - avail > 0.02:
                    freeze = float(asked) - avail
                    log(f"  ! {s['name']}: source runs out at {avail:.2f}s — "
                        f"holding the last frame for {freeze:.2f}s")

            p = _normalise(project, s, tmp, i, m, freeze)
            got = duration(p)
            secs = (s.get("render") or {}).get("secs")
            if s.get("dur") in (None, "") and secs is not None and abs(got - float(secs)) > 0.05:
                log(f"  ! {s['name']}: secs={secs} but this generator does not retime — "
                    f"the clip is {got:.2f}s")
            segs.append(p)
            durs.append(got)
            freezes.append(freeze)

        laid = plan(project, m, durs)
        # so the editor can mark which clips end on a held frame
        for i, f in enumerate(freezes):
            if f > 0:
                laid[i]["frozen"] = round(f, 3)

        # rebuild the same grouping the plan used
        default = float(m.get("xfade", 0.12))
        fades = [0.0] + [default if s.get("xfade") in (None, "") else float(s["xfade"])
                         for s in m["segments"][1:]]
        groups, gfade = [[0]], [0.0]
        for i in range(1, len(segs)):
            if fades[i] == 0:
                groups[-1].append(i)
            else:
                groups.append([i])
                gfade.append(fades[i])

        # Groups are joined with the concat FILTER, not the concat demuxer.
        # The demuxer muxes packets without decoding, so it needs every part to
        # be bit-compatible; a phone screen recording normalised alongside
        # rendered clips is close enough to mux but not to decode, and playback
        # stopped dead partway through the joined stream. `-c copy` hid that
        # (the container duration still looked right) and only surfaced later as
        # a crossfade that silently dropped two thirds of the reel. The filter
        # decodes each part independently, so compatibility stops mattering.
        fps_out = m.get("fps", 60)
        gfiles, gdurs = [], []
        for k, members in enumerate(groups):
            gp = tmp / f"group{k}.mp4"
            args = ["ffmpeg", "-v", "error", "-y"]
            for i in members:
                args += ["-i", str(segs[i])]
            labels = "".join(f"[{n}:v]" for n in range(len(members)))
            args += ["-filter_complex",
                     f"{labels}concat=n={len(members)}:v=1:a=0,setpts=N/{fps_out}/TB[g]",
                     "-map", "[g]", "-r", str(fps_out),
                     "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                     "-pix_fmt", "yuv420p", str(gp)]
            _run(args)
            gfiles.append(gp)
            gdurs.append(sum(durs[i] for i in members))

        if len(gfiles) == 1:
            # remux rather than copy, so this path gets faststart too
            _run(["ffmpeg", "-v", "error", "-y", "-i", str(gfiles[0]),
                  "-c", "copy", "-movflags", "+faststart", str(target)])
        else:
            args = ["ffmpeg", "-v", "error", "-y"]
            for g in gfiles:
                args += ["-i", str(g)]
            # Rebase every group to a zero start before it reaches xfade.
            # `concat -c copy` carries the timestamps of whatever it joined, so a
            # single variable-frame-rate source (a phone screen recording) gives
            # the group file a timeline xfade's absolute `offset` cannot be read
            # against — it then emits the first group alone and exits 0.
            # fps= too: ffmpeg 7's xfade refuses inputs with no constant rate.
            chain = "".join(f"[{k}:v]setpts=PTS-STARTPTS,fps={m.get('fps', 60)}[g{k}];"
                            for k in range(len(gfiles)))
            prev, acc = "[g0]", gdurs[0]
            for k in range(1, len(gfiles)):
                x = gfade[k]
                chain += (f"{prev}[g{k}]xfade=transition=fade:"
                          f"duration={x}:offset={acc - x:.4f}[v{k}];")
                prev = f"[v{k}]"
                acc += gdurs[k] - x
            args += ["-filter_complex", chain.rstrip(";"), "-map", prev,
                     "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                     "-pix_fmt", "yuv420p", "-r", str(m.get("fps", 60)),
                     *_SEEKABLE(m.get("fps", 60)), str(target)]
            _run(args)

        got = duration(target)
        # The chained xfade can drop whole groups without failing — one bad
        # source truncated a 45s cut to 21s and still exited 0. plan() already
        # knows how long the result should be, so compare and refuse to pass a
        # short cut off as finished.
        want = max(s["start_at"] + s["length"] for s in laid) if laid else 0.0
        if want and got < want - 0.25:
            raise RuntimeError(
                f"assembled cut is {got:.2f}s but the plan says {want:.2f}s — "
                f"a crossfade dropped part of the timeline (check for "
                f"variable-frame-rate or odd-timestamp sources)")

        result = {"video": str(target.relative_to(project)),
                  "duration": round(got, 3), "segments": laid}
        # cache the layout so the editor can show where each segment lands
        # without re-normalising every source just to answer that question
        (outdir / "plan.json").write_text(json.dumps(result, indent=1))
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- voiceover ----------
# Lines are rendered and cached individually, keyed by their own content. Editing
# one line re-renders one line; changing only a pause re-renders nothing at all,
# because the pauses are applied here rather than by the TTS. Changing a line's
# seed makes a new file and leaves the old one on disk, so flipping the seed back
# gets the previous take instantly.
def line_key(m: dict, line: dict) -> str:
    cfg = m["vo"]
    payload = json.dumps([cfg["voice"], cfg.get("temperature", 0.8),
                          line.get("seed", cfg.get("seed", 0)), line["text"],
                          line.get("exag", 0.5), line.get("cfg", 0.5)],
                         sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:16]


def _vo_dir(project: Path, m: dict) -> Path:
    d = project / m.get("out", "out") / "vo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _submit(m: dict, rows: list[list], title: str, seed: int) -> str:
    base = m["vo"]["server"].rstrip("/")
    body = urllib.parse.urlencode({
        "voice": m["vo"]["voice"], "lines": json.dumps(rows), "title": title,
        "temperature": m["vo"].get("temperature", 0.8), "seed": seed,
    }).encode()
    return json.load(urllib.request.urlopen(
        urllib.request.Request(base + "/api/jobs", data=body), timeout=30))["id"]


def _await(m: dict, jid: str, poll: float, timeout: float) -> None:
    base = m["vo"]["server"].rstrip("/")
    deadline = time.time() + timeout
    while True:
        listing = json.load(urllib.request.urlopen(base + "/api/jobs", timeout=20))
        job = next((j for j in listing["jobs"] if j["id"] == jid), None)
        status = job["status"] if job else "unknown"
        if status == "done":
            return
        if status in ("error", "failed") or time.time() > deadline:
            raise RuntimeError(f"voiceover job {jid}: {status}")
        time.sleep(poll)


def render_lines(project: Path, m: dict, only: int | None = None,
                 force: bool = False, poll: float = 5.0,
                 timeout: float = 1800, log=lambda s: None) -> int:
    """Render whatever line audio is missing. Returns how many were rendered."""
    vod = _vo_dir(project, m)
    lines = m["vo"]["lines"]
    todo = []
    for i, l in enumerate(lines):
        if only is not None and i != only:
            continue
        if not l.get("text", "").strip():
            continue
        wav = vod / (line_key(m, l) + ".wav")
        if force or not wav.is_file():
            todo.append(i)
    if not todo:
        log("  every line already rendered")
        return 0

    # swaram takes one seed per job, so group by the seed each line asks for
    groups: dict[int, list[int]] = {}
    for i in todo:
        groups.setdefault(lines[i].get("seed", m["vo"].get("seed", 0)), []).append(i)

    base = m["vo"]["server"].rstrip("/")
    done = 0
    for seed, idxs in groups.items():
        rows = [[lines[i]["text"], lines[i].get("exag", 0.5),
                 lines[i].get("cfg", 0.5), 0] for i in idxs]
        log(f"  rendering {len(rows)} line(s) at seed {seed}")
        jid = _submit(m, rows, m.get("name", "reel") + " lines", seed)
        _await(m, jid, poll, timeout)
        for n, i in enumerate(idxs, start=1):
            with urllib.request.urlopen(f"{base}/api/jobs/{jid}/line/{n}", timeout=120) as r:
                (vod / (line_key(m, lines[i]) + ".wav")).write_bytes(r.read())
            done += 1
    return done


def build_vo(project: Path, m: dict) -> dict:
    """Concatenate the line audio with its pauses into one voiceover track."""
    vod = _vo_dir(project, m)
    outdir = project / m.get("out", "out")
    lines = [l for l in m["vo"]["lines"] if l.get("text", "").strip()]
    files, missing = [], []
    for i, l in enumerate(lines):
        f = vod / (line_key(m, l) + ".wav")
        files.append(f)
        if not f.is_file():
            missing.append(i)
    if missing:
        detail = "; ".join(f"line {i + 1} ({lines[i]['text'][:30]!r})" for i in missing)
        raise RuntimeError(f"not rendered yet: {detail}")

    args = ["ffmpeg", "-v", "error", "-y"]
    for f in files:
        args += ["-i", str(f)]
    # Silence before the first word. Without it a first line that wants to sit
    # partway into its beat drags the whole track early — every later line is
    # solved against this one, so the shift is uniform and easy to misread as
    # "the voice is just fast".
    lead = float(m["vo"].get("lead", 0) or 0)
    chain, labels = "", []
    for n, l in enumerate(lines):
        pause = float(l.get("pause", 250) or 0) / 1000.0
        pre = f"adelay={int(round(lead * 1000))}:all=1" if (n == 0 and lead > 0) else ""
        post = f"apad=pad_dur={pause}" if pause > 0 else ""
        steps = ",".join(x for x in (pre, post) if x)
        if steps:
            chain += f"[{n}:a]{steps}[p{n}];"
            labels.append(f"[p{n}]")
        else:
            labels.append(f"[{n}:a]")
    chain += "".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]"
    wav = outdir / "voiceover.wav"
    args += ["-filter_complex", chain, "-map", "[out]",
             "-c:a", "pcm_s16le", str(wav)]
    _run(args)

    # exact starts, computed rather than detected
    rows, clock = [], lead
    for l in lines:
        d = duration(vod / (line_key(m, l) + ".wav"))
        rows.append({"text": l["text"], "beat": l.get("beat", ""),
                     "offset": l.get("offset", 0), "file": line_key(m, l) + ".wav",
                     "start": round(clock, 3), "dur": round(d, 3),
                     "pause": float(l.get("pause", 250) or 0) / 1000.0})
        clock += d + float(l.get("pause", 250) or 0) / 1000.0
    (vod / "lines.json").write_text(json.dumps(rows, indent=1))
    return {"wav": str(wav.relative_to(project)), "duration": round(duration(wav), 3),
            "lines": rows}


def vo(project: Path, m: dict | None = None, only: int | None = None,
       force: bool = False, log=lambda s: None, **kw) -> dict:
    m = m or load(project)
    n = render_lines(project, m, only=only, force=force, log=log, **kw)
    r = build_vo(project, m)
    r["rendered"] = n
    return r


_VOICE_CACHE: dict[str, list[str]] = {}


def voices(m: dict) -> list[str]:
    """The voice list, cached per server.

    Opening a project used to block on this call with a 15s timeout, so a
    swaram that was merely slow made the whole editor feel broken — and a
    response that landed late could overwrite state the browser had already
    moved past. It is a rarely-changing list; fetch it once and keep it.
    """
    base = m["vo"]["server"].rstrip("/")
    if base not in _VOICE_CACHE:
        _VOICE_CACHE[base] = json.load(
            urllib.request.urlopen(base + "/api/voices", timeout=4))["voices"]
    return _VOICE_CACHE[base]


# ---------- alignment ----------
def line_starts(wav: Path, floor_db: int = -45, min_gap: float = 0.15) -> list[float]:
    """Where each spoken line begins, from the silences between them."""
    p = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(wav), "-af",
         f"silencedetect=noise={floor_db}dB:d={min_gap}", "-f", "null", "-"],
        capture_output=True, text=True)
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", p.stderr)]
    return [0.0] + ends


def align(project: Path, m: dict | None = None) -> dict:
    """Compare each VO line's start against the beat it is supposed to sit on.

    This is the measurement that used to be done by hand after every change, and
    it is the whole reason a line can drift two seconds early without anyone
    noticing until playback.
    """
    m = m or load(project)
    outdir = project / m.get("out", "out")
    wav, vid = outdir / "voiceover.wav", outdir / "preview.mp4"
    if not wav.is_file():
        return {"error": "no voiceover.wav yet"}

    lj = outdir / "vo" / "lines.json"
    if lj.is_file():
        starts = [r["start"] for r in json.loads(lj.read_text())]
    else:
        starts = line_starts(wav)   # older projects with a single mixdown
    beats: dict[str, float] = {}
    cached = outdir / "plan.json"
    if cached.is_file():
        for s in json.loads(cached.read_text())["segments"]:
            beats[s["name"]] = s["start_at"]

    rows = []
    for i, l in enumerate(m["vo"]["lines"]):
        got = starts[i] if i < len(starts) else None
        # beat + offset, never an absolute time: the punchline lines sit partway
        # into a clip, and an absolute target silently rots the moment any
        # earlier segment changes length
        want = beats.get(l.get("beat", ""))
        if want is not None:
            want += float(l.get("offset", 0) or 0)
        rows.append({
            "i": i + 1, "text": l["text"], "beat": l.get("beat", ""),
            "start": None if got is None else round(got, 2),
            "target": None if want is None else round(want, 2),
            "drift": None if (got is None or want is None) else round(got - want, 2),
        })
    return {"lines": rows, "vo_duration": round(duration(wav), 3),
            "video_duration": round(duration(vid), 3) if vid.is_file() else None,
            "detected": len(starts), "expected": len(m["vo"]["lines"])}


def fit_pauses(project: Path, m: dict) -> dict:
    """Solve the pauses so every line lands on its beat.

    Line starts are a running total of (line duration + pause), so once the
    durations are known this is arithmetic, not trial and error: the gap before a
    line is exactly its target minus where the previous line ended. A line whose
    target has already passed gets pause 0 — it cannot be pulled earlier without
    cutting the line before it.
    """
    # A reel whose segments are fixed footage cannot have its beats stretched to
    # fit the read, so pinning lines to them is a target that can never be met —
    # solving pauses against it only pushes later lines further out. Such a
    # project declares `vo.pinned: false` and keeps its beat labels as notes.
    if (m.get("vo") or {}).get("pinned") is False:
        return {"changed": 0, "late": [], "skipped": "hand-timed (vo.pinned is false)"}

    vod = _vo_dir(project, m)
    outdir = project / m.get("out", "out")
    planfile = outdir / "plan.json"
    if not planfile.is_file():
        raise RuntimeError("assemble first — beat times come from the cut")
    beats = {s["name"]: s["start_at"]
             for s in json.loads(planfile.read_text())["segments"]}

    lines = [l for l in m["vo"]["lines"] if l.get("text", "").strip()]
    for l in lines:
        f = vod / (line_key(m, l) + ".wav")
        if not f.is_file():
            raise RuntimeError("render the voice first")

    changed, clock, late = 0, 0.0, []
    for i, l in enumerate(lines):
        dur = duration(vod / (line_key(m, l) + ".wav"))
        target = beats.get(l.get("beat", ""))
        if target is not None:
            target += float(l.get("offset", 0) or 0)
            gap = target - clock
            if i == 0:
                # no previous line to pad against — the gap becomes lead-in
                # silence on the track itself
                m["vo"]["lead"] = round(max(0.0, target), 3)
                clock = max(clock, target)
            else:
                want = max(0.0, round(gap, 3))
                prev = lines[i - 1]
                if abs(float(prev.get("pause", 250) or 0) / 1000.0 - want) > 0.005:
                    prev["pause"] = int(round(min(want, 5.0) * 1000))
                    changed += 1
                if gap < -0.05:
                    late.append(l["text"][:38])
                clock = max(clock, target)
        clock += dur
        if l is not lines[-1]:
            pass
    # recompute the trailing clock with the pauses we just set
    save(project, m)
    return {"changed": changed, "late": late}


# ---------- sound effects ----------
# Synthesised rather than downloaded: a handful of shapes cover everything this
# kind of reel needs, they cost nothing, and there is no licence attached to a
# sine wave. Referenced by kind, so a manifest can say {"kind": "impact"}.
SFX_KINDS = {
    # Every shape carries mid-band content on purpose. A pure sub-bass thump
    # measures loud and is inaudible on a phone speaker — the first version of
    # these put 20dB of "sub" below what small drivers reproduce at all.
    # weight + body + snap: the ✕ landing, a slam
    "impact": ("0.42*sin(2*PI*t*(95-50*t))*exp(-7*t)"
               "+0.34*sin(2*PI*t*(300-160*t))*exp(-22*t)"
               "+0.30*(random(0)*2-1)*exp(-48*t)"),
    # boom with a mid thump so it reads without a subwoofer
    "sub":    ("0.40*sin(2*PI*t*(62-30*t))*exp(-4*t)"
               "+0.36*sin(2*PI*t*(165-80*t))*exp(-8*t)"
               "+0.18*(random(0)*2-1)*exp(-30*t)"),
    # short high click with a little body
    "tick":   ("0.42*sin(2*PI*2600*t)*exp(-90*t)"
               "+0.22*sin(2*PI*900*t)*exp(-45*t)"
               "+0.16*(random(0)*2-1)*exp(-150*t)"),
    # band-limited noise swell across a transition
    "whoosh": "0.5*(random(0)*2-1)*sin(PI*t/0.42)",
    # rising tone into a payoff
    "riser":  "0.4*sin(2*PI*t*(190+780*t))*(t/0.7)",
}
SFX_LEN = {"impact": 0.6, "sub": 1.0, "tick": 0.12, "whoosh": 0.42, "riser": 0.7}


def sfx_path(project: Path, m: dict, cue: dict) -> Path:
    """Where a cue's audio lives, generating the built-in kinds on demand."""
    if cue.get("src"):
        return project / cue["src"]
    kind = cue.get("kind") or "impact"
    if kind not in SFX_KINDS:
        raise RuntimeError(f"unknown sfx kind {kind!r}; have {sorted(SFX_KINDS)}")
    d = project / m.get("out", "out") / "sfx"
    d.mkdir(parents=True, exist_ok=True)
    stamp = hashlib.md5(f"{SFX_KINDS[kind]}|{SFX_LEN[kind]}".encode()).hexdigest()[:6]
    f = d / f"{kind}-{stamp}.wav"
    if not f.is_file():
        expr, dur = SFX_KINDS[kind], SFX_LEN[kind]
        chain = f"aevalsrc={expr}:s=48000:d={dur}"
        if kind == "whoosh":
            chain += ",highpass=f=320,lowpass=f=5200"
        chain += ",afade=t=out:st=%.3f:d=%.3f" % (max(0.0, dur - 0.06), 0.06)
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", chain,
              "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(f)])
    return f


# ---------- voice takes ----------
# A phone mic in a room: the voice owns 100-700Hz by 25dB+ even when hiss
# swamps everything above 1kHz. So that band decides what is speech — it keys
# the gate that shuts the hiss between words, and finds where a read starts.
VOICE_BAND = "highpass=f=100,lowpass=f=700"
HISS_BAND = "highpass=f=1000"
# afftdn subtracts a flat floor; set it a little over the hiss measured in the
# take's own pauses. Its noise tracking (tn) is off on purpose: on this kind of
# hiss it chases the floor upward and takes almost nothing out.
# DeepFilterNet (bin/deep-filter, the author's v0.5.6 release build, CPU, model
# built in) does it better when it is there: it takes noise and the ring between
# words without touching an "s". It does not dry the room under the words.
#
# The four sliders on Script → Voice, 0-100, 0 = that stage off:
#   denoise  DeepFilterNet's most it may take out (60 = 36dB; afftdn: 24dB)
#   room     an untreated room is boom (125-500Hz piled up) plus words that ring
#            on after they end: cut the low-mids, and gate harder and faster
#            between words. ponytail: an expander, not a de-reverb model — it
#            shortens the tails between words, not the room under them.
#   comp     evens loud and soft words (ratio up to 7:1)
#   crisp    presence at 3.5kHz and air above 9kHz
#   deess    the opposite of crisp, for a read that comes out harsh: a cut at
#            4.5kHz, a shelf down above 8kHz, and a de-esser that only dips "s"
TONE = {"denoise": 60, "room": 50, "comp": 50, "crisp": 50, "deess": 0}
CHAIN = "13"   # bump when tone_filters changes, so cached cleans re-render
DFN = Path(__file__).parent / "bin" / "deep-filter"


def tone_of(m: dict) -> dict:
    t = (m.get("vo") or {}).get("tone") or {}
    return {k: max(0.0, min(100.0, float(t.get(k, d)))) for k, d in TONE.items()}


def tone_filters(t: dict, nf: float | None) -> tuple[str, str | None, str]:
    """(on the raw take, the gate's settings or None, after leveling).
    nf None: DeepFilterNet already took the noise out."""
    pre = ["highpass=f=80"]
    if t["denoise"] and nf is not None:
        pre.append(f"afftdn=nr={0.4 * t['denoise']:.1f}:nf={nf:.1f}")
    if t["room"]:
        pre.append(f"equalizer=f=250:t=o:w=2:g={-0.12 * t['room']:.1f}")
    if t["crisp"]:
        pre += [f"equalizer=f=3500:t=o:w=2:g={0.1 * t['crisp']:.1f}",
                f"highshelf=f=9000:g={0.06 * t['crisp']:.1f}"]
    # never fully shut: dead air between words sounds edited
    gate = (f"ratio=6:range={10 ** (-(6 + 0.36 * t['room']) / 20):.4f}:attack=20"
            f":release={300 - 2.5 * t['room']:.0f}") if t["room"] else None
    post = (f"acompressor=threshold={10 ** ((-12 - 0.16 * t['comp']) / 20):.4f}"
            f":ratio={1 + 0.06 * t['comp']:.2f}:attack=8:release=80:knee=4,") if t["comp"] else ""
    if t["deess"]:
        # after the compressor, which lets sibilants through: it is slow to catch
        # a short "s". deesser's intensity is steep — 0.3 barely dips an "s",
        # 0.5 takes 12dB off it — so the slider spans 0.3-0.5.
        post += (f"equalizer=f=4500:t=o:w=1.3:g={-0.08 * t['deess']:.1f},"
                 f"highshelf=f=8000:g={-0.04 * t['deess']:.1f},deesser=i={0.3 + 0.002 * t['deess']:.2f},")
    return ",".join(pre), gate, post


def voice_envelope(path: Path, win: float = 0.05, band: str = VOICE_BAND) -> list[float]:
    """RMS of the voice band (or `band`) per `win` seconds, dB."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-af",
                        f"{band},asetnsamples=n={round(48000 * win)}:p=0,"
                        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
                        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                        "-f", "null", "-"], capture_output=True, text=True, check=True)
    return [max(-120.0, float(l.split("=", 1)[1]))
            for l in r.stdout.splitlines() if "RMS_level=" in l]


def voice_threshold(env: list[float], frac: float = 0.4) -> float | None:
    """The level splitting speech from room tone, or None when there is no speech.
    `frac` is how far up from the floor toward the speech peaks it sits.

    ponytail: a percentile split, so a take with no pause in it at all reads as
    no voice — every real take has the breath before the first word.
    The floor is the 5th percentile: a take trimmed to its voice keeps only
    ~0.3s of room, and at the 10th the "floor" landed on soft speech, so the
    gate shut on the quiet ends of words ("chang-" with no "-ing").
    """
    if len(env) < 4:
        return None
    s = sorted(env)
    floor, peak = s[len(s) // 20], s[int(len(s) * 0.95)]
    return floor + frac * (peak - floor) if peak - floor >= 12 else None


def voice_span(path: Path, pre: float = 0.08, post: float = 0.2, win: float = 0.05):
    """(start, end) of the speech in a take, padded, or None if there is none.

    Speech is a burst of 0.3s or more (bursts under 0.4s apart merge, so a
    sentence is one burst). A lone click before or after it — the tap on ● or
    ■, the mic waking up — is shorter, and does not move where the line starts.
    """
    env = voice_envelope(path, win)
    thr = voice_threshold(env)
    if thr is None:
        return None
    runs: list[list[int]] = []
    for i, v in enumerate(env):
        if v > thr:
            if runs and (i - runs[-1][1]) * win <= 0.4:
                runs[-1][1] = i
            else:
                runs.append([i, i])
    words = [r for r in runs if (r[1] - r[0] + 1) * win >= 0.3] or [max(runs, key=lambda r: r[1] - r[0])]
    return max(0.0, words[0][0] * win - pre), min(len(env) * win, (words[-1][1] + 1) * win + post)


def trim_to_voice(path: Path) -> bool:
    """Cut a take down to its speech, so it starts where it is placed."""
    span = voice_span(path)
    if not span:
        return False
    tmp = path.with_name(path.stem + ".trim.wav")
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-af",
          f"atrim={span[0]:.3f}:{span[1]:.3f}", "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(path)
    return True


def lufs(path: Path) -> float | None:
    """Integrated loudness; None for a file too short or too quiet to measure."""
    r = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128",
                        "-f", "null", "-"], capture_output=True, text=True)
    hit = re.search(r"I:\s+(-?[\d.]+) LUFS", r.stderr.rsplit("Summary:", 1)[-1])
    return float(hit.group(1)) if hit and float(hit.group(1)) > -69 else None


_CLEAN_LOCK = threading.Lock()


def clean_take(project: Path, m: dict, take: dict, log=lambda s: None) -> Path | None:
    # one at a time: an editor preview and a mix share each take's clean/ folder
    with _CLEAN_LOCK:
        return _clean_take(project, m, take, log)


def _clean_take(project: Path, m: dict, take: dict, log) -> Path | None:
    """The take with the room gated out and its level matched to the TTS read,
    or None when it holds no voice at all (a false start is only hiss).

    Cached beside the raw take, keyed on everything that shapes it, so a mix
    pays for this once per take. The raw recording is never touched.
    """
    raw = project / take["file"]
    voice = project / m.get("out", "out") / "voiceover.wav"
    want = (lufs(voice) if voice.is_file() else None) or -16.0
    t = tone_of(m)
    # audio.voice_headroom: dB the take's peaks may stand over its loudness. 12 squeezed
    # a far-mic read's 18-22dB by up to 7dB at the limiter, which crunched loud words.
    # Only keyed when set, so reels that never set it keep their cached cleans.
    hr = float((m.get("audio") or {}).get("voice_headroom", 12))
    st = raw.stat()
    key = hashlib.md5((f"{CHAIN}|{DFN.is_file()}|{sorted(t.items())}|{VOICE_BAND}|{want:.1f}|{st.st_size}|{st.st_mtime_ns}"
                       + ("" if hr == 12 else f"|h{hr:g}")).encode()).hexdigest()[:8]
    out = raw.parent / "clean" / f"{raw.stem}-{key}.wav"
    if out.is_file():
        return out
    env = voice_envelope(raw)
    thr = voice_threshold(env)
    if thr is None:
        log(f"  ! {raw.name}: no voice in it — left out (turn clean off to hear it raw)")
        return None
    hiss = voice_envelope(raw, band=HISS_BAND)
    quiet = sorted(h for v, h in zip(env, hiss) if v < thr)
    nf = max(-80.0, min(-20.0, quiet[len(quiet) // 2] + 5)) if quiet else -50.0
    out.parent.mkdir(exist_ok=True)
    for old in out.parent.glob(f"{raw.stem}-*.wav"):
        old.unlink()
    src = raw   # what gets cleaned; the gate's key always listens to the raw take
    if t["denoise"] and DFN.is_file():
        # DFN writes 16-bit, and a far-mic take peaks near -18dBFS: the quiet ends
        # of words land on a few sample values, and the +30dB of leveling below
        # turns that staircase into crackle. Fed at -3dBFS they keep ~15dB more.
        r = subprocess.run(["ffmpeg", "-i", str(raw), "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True)
        peak = float(r.stderr.split("max_volume:")[1].split("dB")[0])
        # It also outputs ~0.1s of digital silence while it warms up, then starts
        # with a click, and drops the last 30ms: so it gets the take mirrored 0.3s
        # out at both ends, and the graphs below cut the head back off.
        with tempfile.TemporaryDirectory() as td:
            hot = Path(td) / raw.name
            _run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-filter_complex",
                  f"[0:a]volume={-3 - peak:.2f}dB,asplit=3[a][b][c];[a]atrim=0:0.3,areverse[h];"
                  # It also skips frames under -70dBFS (a pause, once the browser's own
                  # noise suppression has run) and comes back with a burst of what it
                  # last held: a tick 30ms ahead of the next word. A DC offset keeps
                  # every frame over that line; the high-pass below takes it back out.
                  "[c]areverse,atrim=0:0.3[e];[h][b][e]concat=n=3:v=0:a=1,dcshift=0.001",
                  "-c:a", "pcm_f32le", str(hot)])
            # -D keeps it sample-aligned with the raw take, so the key still fits it
            _run([str(DFN), "-D", "-a", f"{0.6 * t['denoise']:.1f}", "-o", str(out.parent), str(hot)])
        src, nf = out.parent / raw.name, None
    cut = "atrim=start=0.3,asetpts=PTS-STARTPTS," if src != raw else ""
    pre, gate, comp = tone_filters(t, nf)
    gated = out.with_name(out.stem + ".gated.wav")
    if gate:
        # "s", "f", "th" barely touch the voice band, so a gate keyed on it alone
        # shut on them. The key adds the band above 1kHz, lifted so its pauses sit
        # at the voice band's: hiss can't open the gate, a fricative can. Never
        # pushed down — a voice band that is near-silent between words would sink
        # the fricatives with it. The bands don't overlap, so their powers add.
        lift = max(0.0, sorted(env)[len(env) // 10] - sorted(hiss)[len(hiss) // 10])
        key = [10 * math.log10(10 ** (v / 10) + 10 ** ((h + lift) / 10)) for v, h in zip(env, hiss)]
        # more room = the gate sits higher up the speech, so it shuts on the tail
        f = 0.4 + 0.004 * t["room"]
        shut = voice_threshold(key, f)
        if shut is None:   # hiss so loud the mixed key has no pauses: voice band alone
            shut, lift = voice_threshold(env, f), -120.0
        # padded, then cut to the raw take's length below: fed from two files,
        # sidechaingate stops early and drops the end of the last word.
        # The voice runs 20ms behind its key, so the gate's soft attack is already
        # open when a word lands — a snap-open in 5ms was a click on every onset.
        graph = (f"[1:a]{cut}{pre},adelay=20,apad[d];[0:a]apad,asplit[k1][k2];[k1]{VOICE_BAND}[kv];"
                 f"[k2]{HISS_BAND},volume={lift:.2f}dB[kh];[kv][kh]amix=inputs=2:normalize=0[key];"
                 f"[d][key]sidechaingate=threshold={10 ** (shut / 20):.6f}:{gate},"
                 "atrim=start=0.02,asetpts=PTS-STARTPTS[o]")
    else:
        graph = f"[1:a]{cut}{pre},apad[o]"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-i", str(src), "-filter_complex", graph,
          "-map", "[o]", "-t", f"{duration(raw):.6f}", "-c:a", "pcm_f32le", str(gated)])
    if src != raw:
        src.unlink()
    have = lufs(gated)
    gain = 0.0 if have is None else max(-12.0, min(24.0, want - have))
    top = 0.0   # after the compressor, which takes loudness off as it works
    # peaks held `hr` dB over the take's loudness: caught here, on the voice alone,
    # they don't reach the mix's limiter, which would dip the music with them
    ceil = min(0.89, 10 ** ((want + hr) / 20))
    for _ in range(3):  # the compressor and limiter shave loudness: measure, top up
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(gated), "-af",
              f"volume={gain:.2f}dB,{comp}volume={top:.2f}dB,alimiter=limit={ceil:.3f}:level=0:latency=1",
              "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out)])
        got = lufs(out) if have is not None else None
        if got is None or abs(want - got) < 0.5:
            break
        top = max(-12.0, min(24.0, top + want - got))
    gated.unlink()
    log(f"  {raw.name}: cleaned (" + ("deepfilternet" if nf is None else f"hiss floor {nf:.0f}dB") + " · " +
        " ".join(f"{k} {v:.0f}" for k, v in t.items()) + f"), {gain + top:+.1f}dB to {want:.1f} LUFS")
    return out


# ---------- final mix ----------
def muted_spans(project: Path, m: dict) -> list[tuple[float, float]]:
    """Where the muted lines sit in voiceover.wav, from the starts build_vo wrote.

    A row only counts if it still holds that line's audio: a lines.json older
    than the script must not silence whatever line now happens to share its slot.
    """
    lj = project / m.get("out", "out") / "vo" / "lines.json"
    if not lj.is_file():
        return []
    lines = [l for l in (m.get("vo") or {}).get("lines", []) if l.get("text", "").strip()]
    rows = json.loads(lj.read_text())
    return [(r["start"], r["start"] + r["dur"]) for l, r in zip(lines, rows)
            if l.get("mute") and r.get("file") == line_key(m, l) + ".wav"]


def mix(project: Path, m: dict, log=lambda s: None) -> dict:
    """Voice + cues + (ducked) music -> mixdown.wav.

    Cues pin to beats the same way voice lines do, so retiming the cut moves them
    with it instead of leaving them stranded on an absolute timestamp.
    """
    outdir = project / m.get("out", "out")
    voice = outdir / "voiceover.wav"
    vo = m.get("vo") or {}
    takes = [t for t in vo.get("takes", []) if not t.get("mute")]
    audio = m.get("audio") or {}
    cues = [c for c in audio.get("sfx", []) if not c.get("mute")]
    music = audio.get("music") or {}
    # a reel carried by music alone (no script, TTS muted, no takes) is still a
    # reel — only refuse when there is nothing at all to put on the track
    if not voice.is_file() and not takes and not music.get("src") and not cues:
        raise RuntimeError("render the voice first")

    beats = {}
    planfile = outdir / "plan.json"
    if planfile.is_file():
        beats = {s["name"]: s["start_at"]
                 for s in json.loads(planfile.read_text())["segments"]}

    target = duration(voice) if voice.is_file() else 0.0
    prev = outdir / "preview.mp4"
    if prev.is_file():
        target = max(target, duration(prev))
    if target <= 0 and takes:
        # nothing but hand-recorded takes yet (no cut, no synthesized read) —
        # without this the mix is trimmed to zero and comes out silent
        target = max(float(t.get("at", 0) or 0) + float(t.get("dur", 0) or 0)
                     for t in takes)
    if target <= 0:
        raise RuntimeError("assemble first — a music-only mix takes its length from the cut")

    # Assign input indices as we go — patching them afterwards is how you end up
    # shifting the same stream twice.
    args: list[str] = []
    chains: list[str] = []
    mixin: list[str] = []
    idx = 0

    music_idx = None
    if music.get("src"):
        mf = project / music["src"]
        if not mf.is_file():
            raise RuntimeError(f"music not found: {music['src']}")
        args += ["-stream_loop", "-1", "-ss", str(music.get("start", 0)), "-i", str(mf)]
        music_idx = idx
        idx += 1

    voice_db = float(audio.get("voice_db", 0) or 0)
    sfx_db = float(audio.get("sfx_db", 0) or 0)

    # The voice bus is the synthesized read plus any takes recorded over the
    # picture, each pinned to the time it was recorded at. Muting the TTS is
    # what you do once the whole reel has been read by hand and the takes
    # carry it alone.
    vparts: list[str] = []
    if voice.is_file() and not vo.get("mute_tts"):
        args += ["-i", str(voice)]
        # A muted line is silenced where it sits rather than cut out, so every
        # later line keeps its place and a take recorded over it slots right in.
        hush = "".join(f",volume=0:enable='between(t,{a:.3f},{b:.3f})'"
                       for a, b in muted_spans(project, m))
        chains.append(f"[{idx}:a]volume={voice_db}dB{hush}[v{idx}]")
        vparts.append(f"[v{idx}]")
        idx += 1
    for t in takes:
        tf = project / t["file"]
        if not tf.is_file():
            log(f"  ! take not found: {t['file']} — skipped")
            continue
        if vo.get("clean", True):
            tf = clean_take(project, m, t, log)
            if tf is None:
                continue
        args += ["-i", str(tf)]
        ms = max(0, _r(float(t.get("at", 0) or 0) * 1000))
        g = float(t.get("gain_db", 0) or 0) + voice_db
        # a take that starts or stops mid-sound clicks against the silence around it
        fo = max(0.0, duration(tf) - 0.015)
        chains.append(f"[{idx}:a]volume={g}dB,afade=t=in:d=0.015,afade=t=out:st={fo:.3f}:d=0.015,"
                      f"adelay={ms}|{ms}[v{idx}]")
        vparts.append(f"[v{idx}]")
        idx += 1
    if not vparts and music_idx is None and not cues:
        raise RuntimeError("nothing on the voice bus — unmute the script or record a take")
    if len(vparts) == 1:
        vsum = vparts[0]
    elif vparts:
        chains.append("".join(vparts) +
                      f"amix=inputs={len(vparts)}:normalize=0:dropout_transition=0[vsum]")
        vsum = "[vsum]"
    if not vparts:
        pass                     # music/cues only: no voice to mix or duck under
    elif music_idx is None:
        chains.append(f"{vsum}anull[vmain]")
        mixin.append("[vmain]")
    else:
        # key the duck off the voice *after* its own trim, so changing the voice
        # bus does not quietly change how hard the music ducks
        # sidechaincompress stops when its key does — pad the key out to the cut
        # or the bed goes silent the moment the last line ends. The voice too: a
        # bus of takes alone ends early, and the limiter then drops the mix's last 5ms
        chains.append(f"{vsum}apad=whole_dur={target:.3f},asplit=2[vmain][vkey]")
        mixin.append("[vmain]")

    kept = 0
    for c in cues:
        at = float(c.get("at", 0) or 0)
        if c.get("beat"):
            if c["beat"] not in beats:
                log(f"  ! cue on unknown beat {c['beat']!r} — skipped")
                continue
            at = beats[c["beat"]] + float(c.get("offset", 0) or 0)
        # one malformed cue must not take the whole mix down with it — and with
        # mix in the middle of an Update, that meant nothing downstream rendered
        try:
            f = sfx_path(project, m, c)
        except RuntimeError as e:
            log(f"  ! {e} — cue skipped")
            continue
        args += ["-i", str(f)]
        ms = max(0, int(round(at * 1000)))
        g = float(c.get("gain_db", -8) or 0) + sfx_db
        chains.append(f"[{idx}:a]volume={g}dB,adelay={ms}|{ms}[s{idx}]")
        mixin.append(f"[s{idx}]")
        idx += 1
        kept += 1

    if music_idx is not None:
        fi, fo = float(music.get("fade_in", 0.4)), float(music.get("fade_out", 1.2))
        # hicut (Hz): rolls off a bed whose hats and noise fizz over the voice
        hc = f"lowpass=f={float(music['hicut']):.0f}," if music.get("hicut") else ""
        chains.append(
            f"[{music_idx}:a]{hc}volume={music.get('gain_db', -20)}dB,"
            f"afade=t=in:st=0:d={fi},"
            f"afade=t=out:st={max(0.0, target - fo):.3f}:d={fo}[m0]")
        duck = -6.0 if music.get("duck_db") is None else float(music["duck_db"])   # the editor's default
        if vparts and duck < 0:
            # duck the bed under the voice rather than picking one static level.
            # The key is lifted far over the threshold, so any syllable squashes
            # the bed flat, and `mix` blends that with the dry bed: the dip is
            # duck_db deep however loud the read is, instead of 0-4dB that pumped
            # with each word. The slow release holds it down between words.
            chains.append("[m0][vkey]sidechaincompress=threshold=0.01:ratio=20:level_sc=10:"
                          f"attack=20:release=700:mix={1 - 10 ** (duck / 20):.3f}[mout]")
        else:
            chains.append("[m0]anull[mout]")
            if vparts:
                chains.append("[vkey]anullsink")
        mixin.append("[mout]")

    chains.append("".join(mixin) +
                  f"amix=inputs={len(mixin)}:normalize=0:dropout_transition=0,"
                  # level defaults on: it re-normalises to 0dB and clips; without latency=1 its 5ms lookahead
                  # delays the mix (5ms of silence at the head, the last 5ms trimmed: a gap in a looping reel)
                  f"alimiter=limit=0.95:level=0:latency=1,atrim=0:{target:.3f},"
                  f"apad=whole_dur={target:.3f}[out]")

    out = outdir / "mixdown.wav"
    raw = outdir / "mixdown.raw.wav"
    _run(["ffmpeg", "-v", "error", "-y", *args,
          "-filter_complex", ";".join(chains), "-map", "[out]",
          "-ac", "1", "-ar", "48000", "-c:a", "pcm_f32le", str(raw)])
    # Reels play at -14 to -15 LUFS; a mix left at -21 sounds thin next to them.
    # One measured lift, its peaks caught 2.5dB under full scale: between-sample
    # peaks and AAC add ~1.1dB, and this lands the reel under -1dBTP.
    # audio.lufs is the target ahead of the limiter: a voice-led mix lands up to ~1.3dB under it.
    have = lufs(raw)
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af",
          f"volume={0 if have is None else float(audio.get('lufs', -15)) - have:.2f}dB,alimiter=limit=0.75:level=0:latency=1",
          "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out)])
    raw.unlink()
    log(f"  {kept} cue(s) at {sfx_db:+g}dB · voice {voice_db:+g}dB" +
        (" + music" if music_idx is not None else "") +
        f" -> mixdown {duration(out):.2f}s")
    return {"mix": str(out.relative_to(project)),
            "duration": round(duration(out), 3), "cues": kept}


# ---------- mux ----------
# ---------- effects ----------
# A region of the picture, on for a window of time. Pinned to a beat the same
# way sound cues are, so retiming the cut carries them along instead of leaving
# them stranded on an absolute timestamp.
#
#   {"kind": "pixelate", "beat": "payoff", "offset": 0.5, "dur": 2.0,
#    "box": [0.30, 0.42, 0.25, 0.14],     x, y, w, h as fractions of the frame
#    "fade": 0.25, "intensity": 0.6}
#
# Fractions rather than pixels so a box drawn in the editor still means the same
# thing if the reel's resolution ever changes.
def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def _r(x: float) -> int:
    """Round half up, explicitly.

    Python's round() is half-to-even and JavaScript's Math.round() is half-up,
    so the editor's live preview and this encoder disagreed on any value landing
    exactly on .5 — intensity 0.65 gives 16.5 blocks, and one side chose 16
    while the other chose 17. A preview you tune against has to agree with the
    render, so both now use this rule.
    """
    import math
    return int(math.floor(x + 0.5))


def _fx_chain(m: dict, effects: list[dict], beats: dict, w: int, h: int):
    """Build the filter graph for every live effect, or None if there are none.

    The mosaic is built the way a broadcast censor is, and the details are what
    separate it from a smear:

      * the box is snapped to a whole number of blocks, so every block is the
        same size — a region that is 6.4 blocks wide upscales to a row of
        uneven, visibly wrong tiles
      * the downscale uses `area`, which is a true box average. The default
        scaler is bicubic: it samples across block boundaries and leaves each
        block contaminated by its neighbours, which is exactly the mush that
        reads as low quality
      * the upscale uses `neighbor`, so a block is one flat colour with a hard
        edge rather than a gradient toward the next one
    """
    live = []
    for e in effects:
        if e.get("mute"):
            continue
        base = beats.get(e["beat"]) if e.get("beat") else float(e.get("at", 0) or 0)
        if base is None:
            continue
        t0 = base + float(e.get("offset", 0) or 0)
        dur = float(e.get("dur", 1) or 1)
        if dur <= 0:
            continue
        live.append((e, t0, dur))
    if not live:
        return None

    parts, label = [], "0:v"
    for i, (e, t0, dur) in enumerate(live):
        bx, by, bw, bh = (list(e.get("box") or [0.25, 0.25, 0.5, 0.5]) + [0, 0, 0, 0])[:4]
        X, Y = _even(bx * w), _even(by * h)
        W, H = _even(bw * w), _even(bh * h)
        W, H = min(W, _even(w - X)), min(H, _even(h - Y))
        if W < 8 or H < 8:
            continue
        kind = e.get("kind") or "pixelate"
        inten = max(0.0, min(1.0, float(e.get("intensity", 0.6) or 0)))
        # both fades have to fit inside the window, or they cross over and the
        # effect never reaches full strength
        f = max(0.0, min(float(e.get("fade", 0.2) or 0), dur / 2))
        a, b = f"fx{i}a", f"fx{i}b"

        if kind == "solid":
            body = (f"[{b}]crop={W}:{H}:{X}:{Y},"
                    f"drawbox=x=0:y=0:w={W}:h={H}:color=black@1:t=fill,format=rgba")
        elif kind == "blur":
            sigma = round(4 + inten * 40, 1)
            body = (f"[{b}]crop={W}:{H}:{X}:{Y},"
                    f"gblur=sigma={sigma}:steps=3,format=rgba")
        else:
            # intensity reads as coarseness: how many blocks span the box
            across = max(2, _r(36 - inten * 30))
            blk = max(2, _r(W / across))
            cols, rows = max(1, W // blk), max(1, H // blk)
            SW, SH = cols * blk, rows * blk       # snapped to whole blocks
            body = (f"[{b}]crop={SW}:{SH}:{X}:{Y},"
                    f"scale={cols}:{rows}:flags=area,"
                    f"scale={SW}:{SH}:flags=neighbor,format=rgba")
            W, H = SW, SH

        parts.append(f"[{label}]split=2[{a}][{b}]")
        chain = body
        if f > 0:
            chain += (f",fade=t=in:st={t0:.3f}:d={f:.3f}:alpha=1"
                      f",fade=t=out:st={t0 + dur - f:.3f}:d={f:.3f}:alpha=1")
        chain += f"[px{i}]"
        parts.append(chain)
        out = f"v{i}"
        parts.append(f"[{a}][px{i}]overlay={X}:{Y}:"
                     f"enable='between(t,{t0:.3f},{t0 + dur:.3f})'[{out}]")
        label = out
    if label == "0:v":
        return None
    return ";".join(parts), label


def fx(project: Path, m: dict | None = None, log=lambda s: None) -> dict:
    """Burn the effect regions into the picture.

    Runs on the assembled cut and writes a second picture file; mux prefers it
    when it exists. Kept as its own step because it is the only part of the
    video path that has to re-encode — mux itself copies the stream — so a
    project with no effects pays nothing.
    """
    m = m or load(project)
    outdir = project / m.get("out", "out")
    src, dst = outdir / "preview.mp4", outdir / "preview-fx.mp4"
    if not src.is_file():
        raise RuntimeError("assemble first")
    effects = m.get("effects") or []
    beats = {}
    planfile = outdir / "plan.json"
    if planfile.is_file():
        beats = {s["name"]: s["start_at"]
                 for s in json.loads(planfile.read_text())["segments"]}
    built = _fx_chain(m, effects, beats, int(m.get("width", 1080)), int(m.get("height", 1920)))
    if built is None:
        dst.unlink(missing_ok=True)
        log("  no effects — picture used as cut")
        return {"applied": 0, "file": None}
    graph, label = built
    fps = int(m.get("fps", 60))
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
          "-filter_complex", graph, "-map", f"[{label}]",
          "-c:v", "libx264", "-preset", "medium", "-crf", "17",
          "-pix_fmt", "yuv420p", *_SEEKABLE(fps), str(dst)])
    n = graph.count("overlay=")
    log(f"  {n} effect(s) burned in")
    return {"applied": n, "file": str(dst.relative_to(project))}


def mux(project: Path, m: dict | None = None) -> dict:
    m = m or load(project)
    outdir = project / m.get("out", "out")
    # the effected picture wins when it exists, the same way the full mix beats
    # the bare voice track below
    vid = outdir / "preview-fx.mp4"
    if not vid.is_file():
        vid = outdir / "preview.mp4"
    target = outdir / "reel.mp4"
    if not vid.is_file():
        raise RuntimeError("assemble first")
    # the full mix wins if it exists, so music and cues are not silently dropped
    wav = outdir / "mixdown.wav"
    if not wav.is_file():
        wav = outdir / "voiceover.wav"
    if not wav.is_file():
        raise RuntimeError("render the voiceover first")
    # no -shortest: if the video runs longer that tail is usually the end card
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(vid), "-i", str(wav),
          "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
          "-b:a", "192k", "-movflags", "+faststart", str(target)])
    return {"reel": str(target.relative_to(project)),
            "duration": round(duration(target), 3)}


# ---------- what actually needs re-running ----------
# Each step records a signature of everything it consumed. A step is stale when
# that signature no longer matches, or its output is missing. Signatures are
# transitive on purpose — mux's includes the cut's — so one edit upstream marks
# everything downstream too, and `stale()` can hand the editor a single ordered
# list instead of asking which of seven buttons to press in what order.
#
# Signatures rather than mtimes because the browser autosaves reel.json on every
# keystroke: `reel.json newer than preview.mp4` would call for a 50s re-cut after
# nudging a cue 100ms, which is exactly the busywork this is meant to remove.
def _md5(*parts) -> str:
    return hashlib.md5(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _cut_sig(project: Path, m: dict) -> str:
    return _md5([[s.get(k) for k in ("name", "src", "start", "dur", "xfade")]
                 for s in m["segments"]],
                m.get("xfade"), m.get("width"), m.get("height"),
                m.get("fps"), m.get("bg"))


def _vo_sig(project: Path, m: dict) -> str:
    v = m.get("vo") or {}
    return _md5(v.get("voice"), v.get("temperature"), v.get("seed"),
                [[l.get(k) for k in ("text", "exag", "cfg", "seed", "pause")]
                 for l in v.get("lines", [])])


def wants_mix(project: Path, m: dict) -> bool:
    a = m.get("audio") or {}
    v = m.get("vo") or {}
    if a.get("sfx") or (a.get("music") or {}).get("src"):
        return True
    if [t for t in v.get("takes", []) if not t.get("mute")] or v.get("mute_tts"):
        return True
    if any(l.get("mute") for l in v.get("lines", [])):
        return True
    # A mixdown left over from cues since deleted would otherwise keep getting
    # muxed in forever, because mux() prefers it over the bare voice. Rebuilding
    # it (voice only) is how that heals itself.
    return (project / m.get("out", "out") / "mixdown.wav").is_file()


def _mix_sig(project: Path, m: dict) -> str:
    # takes live under vo but are deliberately NOT in _vo_sig: recording one
    # must not re-synthesize every TTS line
    v = m.get("vo") or {}
    # only widen the signature once takes are actually in play: adding these
    # unconditionally would have marked every existing reel stale for a feature
    # it does not use
    extra = ([v.get("takes") or [], v.get("mute_tts"), v.get("clean", True), tone_of(m), CHAIN]
             if (v.get("takes") or v.get("mute_tts")) else [])
    # a line's mute only changes the mix: _vo_sig leaves it out so muting never
    # re-synthesizes or re-fits anything
    hushed = [i for i, l in enumerate(v.get("lines", [])) if l.get("mute")]
    if hushed:
        extra.append(hushed)
    return _md5(m.get("audio") or {}, _cut_sig(project, m), _vo_sig(project, m), *extra)


def _fx_sig(project: Path, m: dict) -> str:
    return _md5(_cut_sig(project, m), m.get("effects") or [])


def _mux_sig(project: Path, m: dict) -> str:
    return _md5(_cut_sig(project, m),
                _mix_sig(project, m) if wants_mix(project, m) else _vo_sig(project, m))


STEP_SIGS = {"assemble": _cut_sig, "vo": _vo_sig, "mix": _mix_sig, "mux": _mux_sig,
             "fx": _fx_sig}
STEP_OUTPUT = {"assemble": "preview.mp4", "vo": "voiceover.wav",
               "mix": "mixdown.wav", "mux": "reel.mp4",
               "fx": "preview-fx.mp4"}


def _sigfile(project: Path, m: dict, step: str) -> Path:
    d = project / m.get("out", "out") / ".steps"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{step}.sig"


def mark_step(project: Path, m: dict, step: str) -> None:
    """Record what a just-completed step consumed."""
    f = STEP_SIGS.get(step)
    if f:
        _sigfile(project, m, step).write_text(f(project, m))


def _current(project: Path, m: dict, step: str) -> bool:
    out = project / m.get("out", "out") / STEP_OUTPUT[step]
    if not out.is_file():
        return False
    p = _sigfile(project, m, step)
    return p.is_file() and p.read_text().strip() == STEP_SIGS[step](project, m)


def _vo_current(project: Path, m: dict) -> bool:
    lines = [l for l in (m.get("vo") or {}).get("lines", []) if l.get("text", "").strip()]
    if not lines:
        return True
    vod = project / m.get("out", "out") / "vo"
    if any(not (vod / (line_key(m, l) + ".wav")).is_file() for l in lines):
        return False
    return _current(project, m, "vo")


def _older_than(out: Path, *inputs: Path) -> bool:
    """True when `out` predates any of the inputs it was built from.

    Signatures cover the manifest; this covers everything else — a clip
    regenerated outside the app, a file edited by hand, or a change in how the
    pipeline itself renders. Output-vs-output mtimes are safe to compare (unlike
    output-vs-reel.json, which the browser rewrites on every keystroke).
    """
    if not out.is_file():
        return True
    t = out.stat().st_mtime
    return any(i.is_file() and i.stat().st_mtime > t + 0.001 for i in inputs)


def _voice_adrift(project: Path, m: dict, tol: float = 0.1) -> bool:
    """Is the built voice track actually out of step with the cut?

    Mtimes only say a file *might* be out of date. This asks the question
    directly — where each line sits inside voiceover.wav, against the beat it is
    pinned to — because the two can disagree without either file being touched
    since: a pause clamped at its ceiling, or a line that simply runs longer
    than the gap it was given. Both leave the track stale-in-fact and current-by
    -timestamp, which is how a finished reel sat three and a half seconds out
    without anything noticing.
    """
    if (m.get("vo") or {}).get("pinned") is False:
        return False          # hand-timed reel: the beat labels are notes, not targets
    out = project / m.get("out", "out")
    planfile, linesfile = out / "plan.json", out / "vo" / "lines.json"
    if not (planfile.is_file() and linesfile.is_file()):
        return False
    try:
        beats = {s["name"]: s["start_at"]
                 for s in json.loads(planfile.read_text())["segments"]}
        lines = json.loads(linesfile.read_text())
    except (OSError, ValueError, KeyError):
        return False
    for l in lines:
        if l.get("beat") in beats:
            if abs(l["start"] - (beats[l["beat"]] + float(l.get("offset", 0) or 0))) > tol:
                return True
    return False


def stale(project: Path, m: dict) -> list[str]:
    """The ordered steps that would change something, or [] if all current."""
    out = project / m.get("out", "out")
    preview, voice = out / "preview.mp4", out / "voiceover.wav"
    mixdown, reel = out / "mixdown.wav", out / "reel.mp4"
    fxfile = out / "preview-fx.mp4"
    vod = out / "vo"

    steps: list[str] = []
    if clips_pending(project, m):
        steps.append("clips")
    # A composited segment reads more than its own src: each layer has a source
    # and often a matte. Editing a mask changed nothing until those were counted
    # here, because the cut looked current by every file it knew about.
    sources = [project / s["src"] for s in m["segments"]]
    for s in m["segments"]:
        for lay in s.get("layers") or []:
            for key in ("src", "mask"):
                if lay.get(key):
                    sources.append(project / lay[key])
    if steps or not _current(project, m, "assemble") or _older_than(preview, *sources):
        steps.append("assemble")
    line_wavs = [vod / (line_key(m, l) + ".wav")
                 for l in (m.get("vo") or {}).get("lines", []) if l.get("text", "").strip()]
    # Re-solve the pauses whenever the cut moved.
    #
    # Voice lines pin to a beat plus an offset, and `fit` turns those targets
    # into the silences between lines. Change any segment's length and every
    # beat after it slides, so the old silences put the voice in the wrong
    # place. Rebuilding the track is not enough — `vo` concatenates whatever
    # pauses are already stored, so without `fit` first it faithfully rebuilds
    # the wrong answer. This was missing, and resizing one segment in the editor
    # left every later line seconds late with nothing reporting it.
    planfile = out / "plan.json"
    pinned = [l for l in (m.get("vo") or {}).get("lines", [])
              if l.get("text", "").strip() and l.get("beat")]
    # `line_wavs and ...` because a project with no script has no voiceover to
    # be out of date — _older_than would read the missing file as "needs building"
    vo_stale = not _vo_current(project, m) or bool(line_wavs and _older_than(voice, *line_wavs))
    # A re-worded or rerolled line changes its length too, and every pause after
    # it was solved for the old one — so a stale voice re-fits before rebuilding.
    if pinned and ("assemble" in steps or _older_than(voice, planfile)
                   or _voice_adrift(project, m) or vo_stale):
        steps.append("fit")
    if "fit" in steps or vo_stale:
        steps.append("vo")
    # anything above changes what the mix is made of: the cut moves the beats
    # cues pin to, the voice is an input and the duck's sidechain key
    a = m.get("audio") or {}
    beds = [project / c["src"] for c in [a.get("music") or {}, *(a.get("sfx") or [])] if c.get("src")]
    if wants_mix(project, m) and (steps or not _current(project, m, "mix")
                                  or _older_than(mixdown, voice, preview, *beds)):
        steps.append("mix")
    # Decided after the mix on purpose: an effect only touches the picture, so
    # leaving it in `steps` any earlier made every nudge of a box re-mix audio
    # that could not possibly have changed.
    if m.get("effects") or fxfile.is_file():
        if "assemble" in steps or not _current(project, m, "fx"):
            steps.append("fx")

    picture = fxfile if fxfile.is_file() else preview
    if steps or not _current(project, m, "mux") \
            or _older_than(reel, picture, mixdown if mixdown.is_file() else voice):
        steps.append("mux")
    if steps:
        steps.append("align")
    return steps


def do_step(project: Path, m: dict, step: str, log=lambda s: None) -> dict:
    """Run one step and record what it consumed. The server and the command line
    both go through here, so they cannot drift apart."""
    if step == "clips":
        r = {"rendered": render_clips(project, m, log=log)}
    elif step == "assemble":
        r = assemble(project, m, log=log)
        log(f"  cut is {r['duration']}s")
    elif step == "vo":
        r = vo(project, m, log=log)
        log(f"  {r['duration']}s of voiceover · {r['rendered']} line(s) rendered")
    elif step == "mix":
        r = mix(project, m, log=log)
    elif step == "fx":
        r = fx(project, m, log=log)
    elif step == "mux":
        r = mux(project, m)
        log(f"  {r['reel']} · {r['duration']}s")
    elif step == "fit":
        # fit needs every line's length, and an edited line has no audio until
        # `vo` — which runs after fit. Render the missing ones here instead.
        render_lines(project, m, log=log)
        r = fit_pauses(project, m)
        log(f"  adjusted {r['changed']} pause(s)")
        for t in r["late"]:
            log(f"  ! runs long, cannot pull earlier: {t}")
    elif step == "align":
        r = align(project, m)
        worst = max((abs(x["drift"]) for x in r.get("lines", [])
                     if x["drift"] is not None), default=0)
        log(f"  worst drift {worst:+.2f}s")
    else:
        raise RuntimeError(f"unknown step {step}")
    mark_step(project, m, step)
    return r


def demo() -> None:
    """Staleness is pure arithmetic over the manifest — check it that way."""
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        proj = Path(d)
        m = {"out": "out", "segments": [{"name": "a", "src": "a.mp4"}],
             "vo": {"voice": "v.wav", "lines": []}}
        (proj / "out").mkdir()
        assert stale(proj, m) == ["assemble", "mux", "align"], stale(proj, m)

        # pretend assemble + mux ran (source must predate them, as it would)
        (proj / "a.mp4").write_bytes(b"x")
        import os as _os
        import time as _time
        _os.utime(proj / "a.mp4", (1, 1))
        for f in ("preview.mp4", "reel.mp4"):
            (proj / "out" / f).write_bytes(b"x")
        for s in ("assemble", "mux"):
            mark_step(proj, m, s)
        assert stale(proj, m) == [], stale(proj, m)

        # a source regenerated behind the app's back invalidates the cut.
        # Stamp it clearly ahead rather than "now": preview.mp4 was written a
        # moment earlier, and when the two land on the same timestamp the
        # comparison is ambiguous and this assert fails at random.
        _os.utime(proj / "a.mp4", (_time.time() + 10, _time.time() + 10))
        assert stale(proj, m) == ["assemble", "mux", "align"], stale(proj, m)
        _os.utime(proj / "a.mp4", (1, 1))

        # a reel older than the picture it was muxed from is stale
        _os.utime(proj / "out" / "reel.mp4", (1, 1))
        assert stale(proj, m) == ["mux", "align"], stale(proj, m)
        _os.utime(proj / "out" / "reel.mp4", None)
        assert stale(proj, m) == [], stale(proj, m)

        # an audio-only edit must NOT call for a re-cut
        m["audio"] = {"sfx": [{"kind": "impact", "beat": "a"}]}
        assert stale(proj, m) == ["mix", "mux", "align"], stale(proj, m)

        # trimming a segment must call for one
        m["audio"] = {}
        m["segments"][0]["dur"] = 2.0
        assert stale(proj, m) == ["assemble", "mux", "align"], stale(proj, m)
    print("ok")


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["demo"]:
        demo()
        raise SystemExit
    proj = Path(sys.argv[1]).expanduser().resolve()
    step = sys.argv[2] if len(sys.argv) > 2 else "all"
    man = load(proj)
    if step == "stale":
        print(" → ".join(stale(proj, man)) or "everything is current")
        raise SystemExit
    steps = ["clips", "assemble", "vo", "mix", "mux", "align"] if step == "all" \
        else stale(proj, man) if step == "auto" else [step]
    for s in steps:
        print(f"▶ {s}")
        print(json.dumps(do_step(proj, load(proj), s, log=print), indent=1, default=str))
