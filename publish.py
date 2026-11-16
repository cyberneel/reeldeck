#!/usr/bin/env python3
"""Publishing: what each reel would send to Instagram, and the queue that sends it.

One record per reel, `publish.json` at the project root, beside the files it
points at (the cut, the cover jpg). The caption itself stays in share-copy.txt,
the file every reel already delivers, so there is never a second copy to drift.

The Instagram API has no drafts and no scheduling,
so reeldeck is the queue. A reel with a `slot` is approved to go public then:
the scheduler stages it STAGE_AHEAD early (container + resumable upload, nothing
public) and runs media_publish at the slot. Setting a slot is the approval, and
only a person does that: the Publish tab's two-tap Schedule, or an agent that
asked first.

Without a token everything is a dry run: the tab shows the exact request and
nothing leaves the machine. The token arrives once through the tab's paste box
(save_token, which swaps a user token for the Page token that never expires),
lives in ~/.config/reeldeck/instagram.json (600) and nowhere else,
and rides in an Authorization header: never in a log line, an error, or an API
response, and in a URL only for the one debug_token check, sent to Meta.

The cover jpg goes as cover_url, which Meta fetches from a public URL. _expose
makes one for the few minutes Instagram processes the upload: a loopback server
that answers a single random path, behind a cloudflared quick tunnel. If that
fails, thumb_offset (a frame of the cut) is the cover.

Trial reels go another way. The Facebook Login route above takes trial_params and
posts a normal reel anyway (three tries, Nov 2026), so a trial goes through
Instagram Login instead: graph.instagram.com, its own token (a second paste box,
~/.config/reeldeck/instagram-login.json, 600, renewed weekly by ig_refresh), and
the cut as video_url through the same tunnel, since that host has no resumable
upload. The container remembers which way it went ("via": "ig").

    python3 publish.py        # self-check against a fake Graph API, no network
"""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline
import projects

TOKEN_FILE = Path.home() / ".config" / "reeldeck" / "instagram.json"
IG_TOKEN_FILE = Path.home() / ".config" / "reeldeck" / "instagram-login.json"   # trials only
IGHOST = "https://graph.instagram.com"
IGRAPH = f"{IGHOST}/v25.0"
V = "v24.0"   # ponytail: pinned; Graph versions live ~2 years, bump when Meta deprecates it
GRAPH = f"https://graph.facebook.com/{V}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{V}"
RECORD, CAPTION = "publish.json", "share-copy.txt"
STAGE_AHEAD = 2 * 3600   # upload + processing take minutes; containers live 24 h
EXPIRY = 23 * 3600       # restage a container older than this rather than risk it
LATE = 3600              # an hour past the slot (machine was off): don't post, say so
TRIALS = ("", "MANUAL", "SS_PERFORMANCE")
CLOUDFLARED = shutil.which("cloudflared") or str(Path.home() / ".local" / "bin" / "cloudflared")
DEFAULTS = {"share_to_feed": True, "cover": None, "thumb_offset": None, "audio_name": "",
            "collaborators": [], "user_tags": [], "location_id": "", "trial": ""}
BUSY = ("staging", "publishing")   # a call is in flight: no edits, no second call
LOCK = threading.Lock()            # ponytail: one lock for every record; posts are a few a week


# ---------- records ----------
def _rec(d: Path) -> dict:
    f = d / RECORD
    return json.loads(f.read_text()) if f.is_file() else {}


def _save(d: Path, rec: dict) -> None:
    tmp = d / (RECORD + ".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    tmp.replace(d / RECORD)


def _log(rec: dict, msg: str) -> None:
    rec.setdefault("log", []).append([round(time.time()), msg])
    del rec["log"][:-40]


def _out(d: Path) -> Path:
    try:
        return d / pipeline.load(d).get("out", "out")
    except (OSError, ValueError):
        return d / "out"


def videos(d: Path) -> list[str]:
    """Postable cuts: the voiced reel.mp4 first (the default), then the finished
    files at the project root (music-only brag cuts, or a copy of reel.mp4).
    `-ig.mp4` upload copies are not cuts."""
    root = sorted(p.name for p in d.glob("*.mp4") if not p.name.endswith("-ig.mp4"))
    reel = _out(d) / "reel.mp4"
    return ([str(reel.relative_to(d))] if reel.is_file() else []) + root


@lru_cache(maxsize=64)
def _sound(path: str, mtime: float) -> str:
    """The cut's audio packets, hashed: a copy of reel.mp4 hashes the same."""
    try:
        return pipeline._run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0",
                              "-c", "copy", "-f", "md5", "-"]).strip()
    except RuntimeError:
        return ""   # no audio track


def unvoiced(d: Path, video: str) -> str:
    """Why `video` would go out without the reel's voice-over, or "". BLE 02
    went public music-only on 2026-10-14 because a root cut was the default."""
    # ponytail: no override for a deliberate music-only post; add a publish.json flag if one is ever wanted
    try:
        vo = pipeline.load(d).get("vo") or {}
    except (OSError, ValueError):
        return ""
    voiced = any(not t.get("mute") for t in vo.get("takes", [])) or not vo.get("mute_tts") and any(
        l.get("text", "").strip() and not l.get("mute") for l in vo.get("lines", []))
    reel, f = _out(d) / "reel.mp4", d / video
    if not voiced or not video or f == reel:
        return ""
    if not reel.is_file():
        return f"{video} has no voice-over and there's no voiced cut yet (render reel.mp4)"
    if _sound(str(f), f.stat().st_mtime) == _sound(str(reel), reel.stat().st_mtime):
        return ""
    r = reel.relative_to(d)
    return f"{video} has no voice-over (its sound isn't {r}'s): post {r}"


@lru_cache(maxsize=64)
def _probe(path: str, mtime: float) -> dict:
    out = pipeline._run(["ffprobe", "-v", "error", "-show_entries",
                         "stream=codec_type,codec_name,width,height,r_frame_rate,bit_rate:format=duration,size",
                         "-of", "json", path])
    j = json.loads(out)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), {})
    n, _, dd = v.get("r_frame_rate", "0/1").partition("/")
    return {"w": v.get("width"), "h": v.get("height"), "vcodec": v.get("codec_name"),
            "fps": round(int(n) / int(dd or 1), 2), "acodec": a.get("codec_name"),
            "akbps": round(int(a.get("bit_rate") or 0) / 1000),
            "dur": round(float(j["format"]["duration"]), 2), "mb": round(int(j["format"]["size"]) / 1e6, 1)}


def probe(f: Path) -> dict:
    return _probe(str(f), f.stat().st_mtime)


def _cover_ms(d: Path, fps: float) -> int:
    """The cover frame finish.sh grabs (`F=${1:-75}`), as an offset into the
    cut: the thumb_offset that stands in when the cover jpg can't be sent."""
    f = d / "work" / "finish.sh"
    m = re.search(r"^F=\$\{1:-(\d+)\}", f.read_text(), re.M) if f.is_file() else None
    return round(int(m.group(1)) / (fps or 30) * 1000) if m else 0


def tags(caption: str) -> dict:
    return {"hashtags": re.findall(r"(?<!\w)#\w+", caption),
            "mentions": re.findall(r"(?<![\w.])@[\w.]+", caption)}


def request(post: dict) -> dict:
    """The container call's parameters, exactly as they'd be sent (minus the token)."""
    s = post["settings"]
    r = {"media_type": "REELS", "upload_type": "resumable", "caption": post["caption"],
         "share_to_feed": str(s["share_to_feed"]).lower(), "thumb_offset": s["thumb_offset"]}
    if s["cover"]:   # Meta uses cover_url and ignores thumb_offset when both come
        r["cover_url"] = f"<a temporary public link to {s['cover']}, made at upload>"
    if s["audio_name"]:
        r["audio_name"] = s["audio_name"]
    if s["collaborators"]:
        r["collaborators"] = json.dumps(s["collaborators"])
    if s["user_tags"]:
        r["user_tags"] = json.dumps([{"username": u} for u in s["user_tags"]])
    if s["location_id"]:
        r["location_id"] = s["location_id"]
    if s["trial"]:   # Instagram Login: it fetches the cut from a link, and a trial has no feed choice
        r["trial_params"] = json.dumps({"graduation_strategy": s["trial"]})
        del r["upload_type"], r["share_to_feed"]
        r["video_url"] = "<a temporary public link to the upload copy, made at upload>"
    return r


def problems(post: dict) -> list[str]:
    """Anything Instagram would refuse, or that should stop a post going out."""
    c, t, s, p = post["caption"], post["tags"], post["settings"], post.get("probe") or {}
    out = [post["hold"]] if post.get("hold") else []   # a person's "check this first" note (publish.json "hold"); delete it to clear
    if not c.strip():
        out.append(f"no caption ({CAPTION} is missing or empty)")
    if len(c) > 2200:
        out.append(f"caption is {len(c)} characters (max 2200)")
    if len(t["hashtags"]) > 30:
        out.append(f"{len(t['hashtags'])} hashtags (max 30)")
    if len(t["mentions"]) > 20:
        out.append(f"{len(t['mentions'])} @-tags (max 20)")
    if len(s["collaborators"]) > 3:
        out.append("more than 3 collaborators")
    if not post["video"]:
        out.append("no cut to post")
    elif p:
        if p["w"] and p["h"] and abs(p["w"] / p["h"] - 9 / 16) > .01:
            out.append(f"cut is {p['w']}×{p['h']}, not 9:16")
        if not 3 <= p["dur"] <= 900:
            out.append(f"cut is {p['dur']}s (reels take 3 s to 15 min)")
        if s["thumb_offset"] > p["dur"] * 1000:
            out.append("cover offset is past the end of the cut")
    if post.get("cover_mb", 0) > 8:
        out.append(f"cover jpg is {post['cover_mb']} MB (max 8)")
    if s["trial"] and post.get("status") != "published" and not ig_token():
        out.append("a trial reel goes out through Instagram Login: paste that token first (Instagram token, below the account)")
    return out


def view(row: dict) -> dict:
    """Everything the tab and the MCP tools show for one reel."""
    d = Path(row["dir"])
    rec, vids = _rec(d), videos(d)
    cap = d / CAPTION
    caption = cap.read_text().strip() if cap.is_file() else ""
    video = rec.get("video") if rec.get("video") in vids else (vids[0] if vids else "")
    pr = probe(d / video) if video else None
    s = {**DEFAULTS, **rec.get("settings", {})}
    if s["thumb_offset"] is None:
        s["thumb_offset"] = _cover_ms(d, pr["fps"] if pr else 30)
    covers = sorted(p.name for p in d.glob("*.jpg"))
    if s["cover"] is None or s["cover"] and s["cover"] not in covers:
        s["cover"] = covers[0] if covers else ""   # finish.sh's poster, when there is one
    post = {"pid": row["id"], "name": row.get("name"), "folder": row.get("folder", ""),
            "video": video, "videos": vids, "probe": pr, "covers": covers,
            "cover_mb": round((d / s["cover"]).stat().st_size / 1e6, 1) if s["cover"] else 0,
            "caption": caption, "tags": tags(caption), "settings": s,
            "slot": rec.get("slot"), "status": rec.get("status", "draft"),
            "container": rec.get("container"), "permalink": rec.get("permalink", ""), "hold": rec.get("hold", ""),
            "log": rec.get("log", [])[-12:]}
    u = unvoiced(d, video) if post["status"] != "published" else ""
    post["problems"] = problems(post) + ([u] if u else [])
    post["request"] = request(post)
    return post


def _row(pid: str) -> dict:
    for r in projects.list_projects():
        if r["id"] == pid and not r["missing"]:
            return r
    raise KeyError(pid)


def _ready(d: Path) -> bool:
    """The timeline's status picker: only reels set to "ready" show here (and in the MCP's list_reels)."""
    try:
        return pipeline.load(d).get("status") == "ready"
    except (OSError, ValueError):
        return False


def listing() -> dict:
    """Every reel set to ready in the timeline that has a cut, the ones going out soonest first."""
    posts = []
    for r in projects.list_projects():
        if not r["missing"] and _ready(Path(r["dir"])) and videos(Path(r["dir"])):
            try:
                posts.append(view(r))
            except Exception as e:   # one broken project must not blank the tab
                posts.append({"pid": r["id"], "name": r.get("name"), "status": "error",
                              "problems": [str(e)], "log": []})
    rank = {"publishing": 0, "staged": 1, "processing": 1, "staging": 1, "queued": 1,
            "failed": 2, "missed": 2, "draft": 3, "error": 4, "published": 5}
    posts.sort(key=lambda p: (rank.get(p["status"], 3), p.get("slot") or 0, bool(p.get("problems"))))
    return {"posts": posts, "account": account()}


# ---------- edits from the tab / MCP ----------
def _clean_settings(s: dict) -> dict:
    """Input from a browser or an agent: coerce it, or refuse it."""
    out = {}
    if "share_to_feed" in s:
        out["share_to_feed"] = bool(s["share_to_feed"])
    if "thumb_offset" in s:
        out["thumb_offset"] = max(0, int(s["thumb_offset"]))
    if "audio_name" in s:
        out["audio_name"] = str(s["audio_name"]).strip()[:100]
    for k in ("collaborators", "user_tags"):
        if k in s:
            v = s[k] if isinstance(s[k], list) else str(s[k]).replace(",", " ").split()
            out[k] = [str(u).strip().lstrip("@") for u in v if str(u).strip().lstrip("@")]
    if "location_id" in s:
        if not re.fullmatch(r"\d*", str(s["location_id"]).strip()):
            raise ValueError("location_id is a numeric Facebook place id")
        out["location_id"] = str(s["location_id"]).strip()
    if "cover" in s:   # "" = the frame at thumb_offset; edit() checks the jpg exists
        out["cover"] = str(s["cover"] or "")
        if out["cover"] and not re.fullmatch(r"[^/\\]+\.jpe?g", out["cover"], re.I):
            raise ValueError("cover is a .jpg in the project folder, or empty for the frame")
    if "trial" in s:
        if s["trial"] not in TRIALS:
            raise ValueError(f"trial is one of {TRIALS}")
        out["trial"] = s["trial"]
    return out


def edit(pid: str, body: dict) -> dict:
    """Caption, cut, settings. A staged container carries the old copy, so an
    edit drops it; the next tick restages if the reel is still scheduled."""
    row = _row(pid)
    d = Path(row["dir"])
    with LOCK:
        rec = _rec(d)
        if rec.get("status") in BUSY or rec.get("status") == "published":
            raise ValueError(f"can't edit while {rec['status']}")
        if "video" in body:
            if body["video"] not in videos(d):
                raise ValueError(f"not a cut in this project: {body['video']}")
            rec["video"] = body["video"]
        if "settings" in body:
            clean = _clean_settings(body["settings"])
            if clean.get("cover") and not (d / clean["cover"]).is_file():
                raise ValueError(f"no {clean['cover']} in this project")
            rec["settings"] = {**rec.get("settings", {}), **clean}
        if "caption" in body:
            (d / CAPTION).write_text(str(body["caption"]).strip() + "\n")
        if rec.get("status") in ("processing", "staged"):
            rec.pop("container", None)
            rec["status"] = "queued" if rec.get("slot") else "draft"
            _log(rec, "edited after staging: the container was dropped")
        _save(d, rec)
    return view(row)


def schedule(pid: str, slot: float) -> dict:
    """Approve a public post at `slot` (epoch seconds; the past means now)."""
    row = _row(pid)
    d = Path(row["dir"])
    p = view(row)
    if p["problems"]:
        raise ValueError("fix first: " + "; ".join(p["problems"]))
    with LOCK:
        rec = _rec(d)
        if rec.get("status") in BUSY or rec.get("status") == "published":
            raise ValueError(f"can't schedule while {rec['status']}")
        rec["slot"] = max(float(slot), time.time())
        rec["video"] = p["video"]
        if rec.get("status") not in ("processing", "staged"):
            rec["status"] = "queued"
        _log(rec, "scheduled: public at " + time.strftime("%a %d %b %H:%M %Z", time.localtime(rec["slot"])))
        _save(d, rec)
    return view(row)


def unschedule(pid: str) -> dict:
    row = _row(pid)
    d = Path(row["dir"])
    with LOCK:
        rec = _rec(d)
        if rec.get("status") in BUSY or rec.get("status") == "published":
            raise ValueError(f"can't unschedule while {rec['status']}")
        rec["slot"] = None
        if rec.get("status") in ("queued", "failed", "missed"):
            rec["status"] = "draft"
        _log(rec, "unscheduled")
        _save(d, rec)
    return view(row)


def stage_now(pid: str) -> dict:
    """Steps 1–3 right away, in the background: nothing public. Without a
    token it builds the upload copy and logs the request it would send."""
    row = _row(pid)
    p = view(row)
    if p["problems"]:
        raise ValueError("fix first: " + "; ".join(p["problems"]))
    threading.Thread(target=_stage, args=(row,), daemon=True).start()
    return p


# ---------- the Graph API ----------
def token(f: Path | None = None) -> dict | None:
    f = f or TOKEN_FILE
    if not f.is_file():
        return None
    try:
        t = json.loads(f.read_text())
    except ValueError:
        return None
    gone = t.get("expires") and t["expires"] < time.time()
    return t if t.get("ig_user_id") and t.get("access_token") and not gone else None


def ig_token() -> dict | None:
    """The Instagram Login token, for trials. Same shape as token()'s."""
    return token(IG_TOKEN_FILE)


def _auth(via: str | None) -> dict | None:
    return ig_token() if via == "ig" else token()


def _write_secret(f: Path, obj: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh)
    tmp.replace(f)


def account() -> dict:
    """Token state for the tab, never the token itself."""
    if not TOKEN_FILE.is_file():
        return {"token": False, "note": "no token yet: dry run, nothing leaves this machine", **ig_account()}
    t = token()
    if not t:
        return {"token": False, "note": "the saved token is expired or incomplete: paste a fresh one", **ig_account()}
    note = "@" + (t.get("username") or t["ig_user_id"])
    if t.get("expires"):
        note += time.strftime(" · token good till %d %b", time.localtime(t["expires"]))
        if t["expires"] - time.time() < 7 * 86400:
            note += ": paste a fresh one soon"
    if TOKEN_FILE.stat().st_mode & 0o077:
        note += " · chmod 600 the token file"
    return {"token": True, "note": note, **ig_account()}


def ig_account() -> dict:
    t = ig_token()
    if not t:
        return {"ig": False, "ig_note": "trial reels: no Instagram Login token yet" if not IG_TOKEN_FILE.is_file()
                else "trial reels: the Instagram Login token expired: paste a fresh one"}
    return {"ig": True, "ig_note": f"trial reels: @{t['username'] or t['ig_user_id']} via Instagram Login"
            + time.strftime(" · token good till %d %b, renewed weekly", time.localtime(t["expires"]))}


def save_token(tok: str) -> dict:
    """The tab's paste box. Ask Meta what the token is, find the Instagram
    account it reaches, then write TOKEN_FILE (600). The token is never sent
    back, logged, or shown again."""
    tok = str(tok or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{40,1000}", tok):
        raise ValueError("that doesn't look like a Meta access token (they start EAA…)")
    q = urllib.parse.urlencode
    debug = lambda t: _graph("GET", f"{GRAPH}/debug_token?" + q({"input_token": t}), tok=t).get("data", {})
    info = debug(tok)
    if not info.get("is_valid"):
        raise ValueError("Meta says this token isn't valid: expired, or made for another app?")
    missing = {"instagram_basic", "instagram_content_publish"} - set(info.get("scopes", []))
    if missing:
        raise ValueError("the token is missing " + " and ".join(sorted(missing)) + ": tick it in the Explorer")
    f = q({"fields": "access_token,instagram_business_account{id,username}"})
    pages = ([_graph("GET", f"{GRAPH}/me?{f}", tok=tok)] if info.get("type") == "PAGE"
             else _graph("GET", f"{GRAPH}/me/accounts?{f}", tok=tok).get("data", []))
    page = next((p for p in pages if p.get("instagram_business_account")), None)
    if not page:
        raise ValueError("the token works but reaches no Instagram professional account linked to a Page")
    ig = page["instagram_business_account"]
    if info.get("type") != "PAGE":
        # Meta's publishing docs list a Page token, and one made from a long-lived
        # user token never expires, so nothing needs refreshing. What's left is
        # data_access_expires_at: 90 days, renewed only by the person logging in again.
        tok = page["access_token"]
        info = debug(tok)
    ends = [t for t in (info.get("expires_at"), info.get("data_access_expires_at")) if t]
    _write_secret(TOKEN_FILE, {"access_token": tok, "ig_user_id": ig["id"], "username": ig.get("username", ""),
                               "expires": min(ends) if ends else 0, "saved": round(time.time())})
    _seen.clear()   # a new token sees comments differently (names): forget the old view
    return account()


def save_ig_token(tok: str) -> dict:
    """The second paste box: an Instagram Login token (App Dashboard → Instagram →
    API setup with Instagram business login → Generate token, good for 60 days).
    Instagram says whose it is; it must be the account the reels post as."""
    tok = str(tok or "").strip()
    if not re.fullmatch(r"[\w-]{40,1000}", tok):
        raise ValueError("that doesn't look like an Instagram access token")
    me = _graph("GET", f"{IGRAPH}/me?fields=user_id,username", tok=tok)
    fb = token()
    if fb and fb.get("username") and me.get("username") != fb["username"]:
        raise ValueError(f"that token is for @{me.get('username')}, but reels post as @{fb['username']}")
    now = round(time.time())
    # ponytail: 60 days assumed (Meta's figure for dashboard tokens); the first weekly refresh corrects it
    _write_secret(IG_TOKEN_FILE, {"access_token": tok, "ig_user_id": str(me["user_id"]), "username": me.get("username", ""),
                                  "expires": now + 60 * 86400, "saved": now})
    return account()


def ig_refresh(now: float | None = None) -> None:
    """Instagram Login tokens last 60 days and can be renewed for another 60 once
    a day old: renew weekly, retry a failure daily."""
    t, now = ig_token(), now or time.time()
    if not t or now - t["saved"] < 7 * 86400 or now - t.get("tried", 0) < 86400:
        return
    _write_secret(IG_TOKEN_FILE, {**t, "tried": round(now)})
    # the one call that takes the token in the URL, as Meta documents it; _graph keeps it out of logs
    j = _graph("GET", f"{IGHOST}/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": t["access_token"]}), tok=t["access_token"])
    _write_secret(IG_TOKEN_FILE, {**t, "access_token": j["access_token"], "saved": round(now),
                                  "expires": round(now + int(j.get("expires_in") or 60 * 86400))})


def _graph(method: str, url: str, data: dict | None = None, headers: dict | None = None,
           body=None, tok: str | None = None) -> dict:
    ig = url.startswith(IGHOST)
    tok = tok or _auth("ig" if ig else None)["access_token"]
    h = {"Authorization": f"{'Bearer' if ig else 'OAuth'} {tok}", **(headers or {})}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")
        # Meta's whole answer (code, subcode, trace id) goes to the journal: the tab only has room for the message
        print(f"graph: {method} {url.split('?')[0].removeprefix(GRAPH)} -> {e.code} {msg}".replace(tok, "***"), flush=True)
        try:
            msg = json.loads(msg)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise RuntimeError(f"{e.code}: {msg}".replace(tok, "***")) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach Meta: {e.reason}") from None


def quota() -> dict:
    t = token()
    if not t:
        return {}
    j = _graph("GET", f"{GRAPH}/{t['ig_user_id']}/content_publishing_limit?fields=quota_usage,config")
    d = (j.get("data") or [{}])[0]
    return {"used": d.get("quota_usage"), "total": (d.get("config") or {}).get("quota_total")}


# ---------- comments: the Comments tab ----------
def comments(media: str):
    """Every top-level comment on one post, newest first, each with its replies."""
    q = {"fields": "id,text,username,timestamp,user,replies{username,user}", "limit": 50}
    while True:
        page = _graph("GET", f"{GRAPH}/{media}/comments?" + urllib.parse.urlencode(q))
        yield from page.get("data", [])
        # the cursor, not paging.next: that URL carries the token
        after = (page.get("paging") or {}).get("cursors", {}).get("after")
        if not after or not page["paging"].get("next"):
            return
        q["after"] = after


def _me() -> dict:
    me = token()
    if not me:
        raise ValueError("no Instagram token yet: paste one in Publish")
    return me


def _mine(c: dict, me: dict) -> bool:
    return (c.get("user") or {}).get("id") == me["ig_user_id"] or c.get("username") == me.get("username")


def waiting(cs: list[dict], me: dict) -> list[dict]:
    """The comments we haven't answered. Meta names an author only with
    instagram_manage_comments, but it always marks our own (`user`)."""
    return [c for c in cs if not _mine(c, me) and not any(_mine(r, me) for r in (c.get("replies") or {}).get("data", []))]


# the caption's first sentence names a post
_title = lambda m: re.split(r"(?<=[.!?])\s", (m.get("caption") or "").strip(), maxsplit=1)[0][:80]


# media id -> the reel as the tab last got it. Its comments stand while Meta's
# comments_count (replies included) hasn't moved, so a quiet reel costs no call.
_seen: dict[str, dict] = {}


def inbox(fresh: bool = False) -> dict:
    """The reels that have comments, newest first, in one Graph call. A reel whose
    count hasn't moved comes from _seen; the rest come back `stale`, as last seen,
    for the tab to ask reel() about one by one. `fresh` distrusts every count."""
    me = _me()
    # ponytail: the 25 newest posts; page /media if an older reel still draws comments
    f = urllib.parse.urlencode({"fields": "id,caption,permalink,timestamp,comments_count,media_product_type", "limit": 25})
    reels = []
    for m in _graph("GET", f"{GRAPH}/{me['ig_user_id']}/media?{f}").get("data", []):
        if m.get("media_product_type") != "REELS" or not m.get("comments_count"):
            continue
        old = _seen.get(m["id"], {})
        _seen[m["id"]] = r = {"id": m["id"], "n": m["comments_count"], "title": _title(m),
                              "permalink": m.get("permalink", ""), "posted": m.get("timestamp", ""),
                              "comments": old.get("comments", []), "answered": old.get("answered", []),
                              "stale": fresh or old.get("stale", True) or old["n"] != m["comments_count"]}
        reels.append(r)
    return {"reels": reels, "auto": auto_view()}


def remembered() -> dict:
    """The list as the tab last got it, no Graph call: something to show while inbox() asks."""
    return {"reels": sorted(_seen.values(), key=lambda r: r["posted"], reverse=True), "auto": auto_view()}


def reel(media: str) -> dict:
    """One listed reel's comments, asked of Instagram again. `comments` are the
    unanswered ones, longest wait first (the tab); `answered` is the public's other ones."""
    r, me = _seen[media], token()
    cs = list(comments(media))
    wait = {c["id"] for c in waiting(cs, me)}
    slim = lambda c: {k: c.get(k, "") for k in ("id", "text", "username", "timestamp")}
    r.update(stale=False, comments=[slim(c) for c in cs[::-1] if c["id"] in wait],
             answered=[slim(c) for c in cs if c["id"] not in wait and not _mine(c, me)])
    return r


def recent(limit: int = 20, unanswered: bool = False) -> dict:
    """The newest comments across the listed reels, answered or not: what an agent
    reads (publish_mcp.py). It rides the tab's memory and re-reads only the reels that moved."""
    rows = []
    # ponytail: moved reels are re-read one after another (about 50 ms a comment, so a cold start takes seconds)
    for r in inbox()["reels"]:
        if r["stale"]:
            r = reel(r["id"])
        for done in (False, True)[:1 if unanswered else 2]:
            rows += [{**c, "answered": done, "reel": r["title"], "link": f"{r['permalink'].rstrip('/')}/c/{c['id']}/"}
                     for c in r["answered" if done else "comments"]]
    rows.sort(key=lambda c: c["timestamp"], reverse=True)
    return {"comments": rows[:max(1, int(limit))], "of": len(rows)}


_dmed: dict[str, str] = {}   # comment -> the id Meta gave our DM to its author, since this server started
SEND = threading.Lock()   # one answer at a time: a tap in the tab and the auto pass never take the same comment together


class Refused(RuntimeError):
    """This one comment's DM or reply didn't go. Asking again won't change it."""


def reply(cid: str, text: str = "", dm: str = "", need_dm: bool = False) -> dict:
    """Answer one comment: a DM to its author first (Meta's "private reply"), then a
    public reply under it. Either line may be empty, and `need_dm` holds the reply
    back when the DM didn't go. A person's tap in the tab, or the auto pass a person
    switched on there (auto_tick)."""
    text, dm = str(text or "").strip(), str(dm or "").strip()
    if not re.fullmatch(r"\d{5,30}", str(cid)):
        raise ValueError("not a comment id")
    if not (text or dm):
        raise ValueError("nothing to send")
    if len(text) > 300 or len(dm.encode()) > 1000:
        raise ValueError("a reply is up to 300 characters, a DM up to 1000 bytes")
    me = _me()
    with SEND:
        # the tab shows what it remembers, so ask first: one answer of ours per comment
        new = bool(waiting([_graph("GET", f"{GRAPH}/{cid}?fields=replies{{username,user}}")], me))
        out = {"dm": False, "replied": False}
        # ponytail: _dmed dies with the server; after a restart Meta's own one-DM-per-comment rule is the guard
        if new and dm and cid not in _dmed:
            # the Page's door (Business Messaging, pages_messaging): "me" is the Page, the saved token being its own.
            # The Instagram account's /messages refuses this app with code 3 (2026-10-12)
            try:
                sent = _graph("POST", f"{GRAPH}/me/messages", headers={"Content-Type": "application/json"},
                              body=json.dumps({"recipient": {"comment_id": cid}, "message": {"text": dm}}).encode())
                # a 2xx alone is not the DM: only a message Meta names counts
                if not (isinstance(sent, dict) and sent.get("message_id")):
                    print(f"graph: POST /me/messages -> 2xx with no message_id {json.dumps(sent)[:300]}", flush=True)
                    raise RuntimeError("Meta took the DM call but named no message")
                _dmed[cid] = str(sent["message_id"])
            except RuntimeError as e:
                if need_dm or not text:
                    raise Refused(f"the DM didn't go, nothing was replied. Meta said {e}") from None
                out["dm_error"] = str(e)
        out["dm"] = bool(dm) and cid in _dmed
        if new and text:
            try:
                _graph("POST", f"{GRAPH}/{cid}/replies", data={"message": text})
            except RuntimeError as e:
                raise Refused(f"{'the DM went, but ' if out['dm'] else ''}the reply didn't. Meta said {e}") from None
            out["replied"] = True
        # a public answer, ours now or one found already there, takes it off the list we remember;
        # a reply of ours is one more on that reel's count
        if out["replied"] or not new:
            for s in _seen.values():
                hit = [c for c in s["comments"] if c["id"] == cid]
                if hit:
                    s.update(n=s["n"] + out["replied"], comments=[c for c in s["comments"] if c["id"] != cid],
                             answered=s["answered"] + hit)
        if not new:
            raise ValueError("already answered")
        print(f"comments: {cid} answered, dm {out['dm']}, reply {out['replied']}"
              + (f", message_id {_dmed[cid]}" if cid in _dmed else ""), flush=True)
        return out


# ---------- auto: a comment with a reel's keyword gets the reel's DM and a reply from the bank ----------
AUTO_PER_TICK = 4   # ponytail: a flat few a minute, far under Meta's 750 private replies an hour
AUTO_FAILS = 5      # this many refusals in a row switch a reel off: something is wrong, don't keep knocking
DM_DAYS = 7         # Meta takes a private reply for a week after the comment
# the bank until the tab saves its own. No reel's subject in them: one bank serves every reel.
# Each line is true whether or not the DM landed: Meta answers 200 + a message_id for a DM Instagram then blocks
# (about 1 in 8), so no line says "it's in your DMs". Whoever got nothing is told to message first.
REPLIES = ["Sent you the link. Check your DMs and Message requests. Nothing there? Message me and I'll send it",
           "Link sent. Don't see it? Look in Message requests. Still nothing, send me a message",
           "Sent! If it's not in your DMs, check Message requests, or message me and I'll resend",
           "DMed it to you. Instagram hides some of my DMs, so if it's not in Message requests, message me",
           "Just sent it. Not there? Check Message requests, or send me a message",
           "Sent it over. If Instagram blocked my DM, message me and I'll reply with the link",
           "Link's on its way. No DM? Check Message requests or message me",
           "Sent. Check Message requests if it's not in your DMs. Not there either? Message me"]
AUTO = lambda: TOKEN_FILE.with_name("autoreply.json")   # beside the token, so a test that moves one moves both


def _auto() -> dict:
    """The auto answers as saved: the bank of public replies, the one used last, and per
    reel (media id) {on, word, dm, sent, fails, skip: {comment id: why it didn't go}, note,
    never: {comment id: username}}. `never` is put in by hand: people the pass leaves alone for good.
    No file, or a broken one, is everything off."""
    try:
        a = json.loads(AUTO().read_text())
    except (OSError, ValueError):
        a = {}
    return {"replies": REPLIES, "reels": {}, **a}


def _auto_edit(change) -> dict:
    with LOCK:
        a = _auto()
        change(a)
        tmp = AUTO().with_suffix(".tmp")
        tmp.write_text(json.dumps(a, indent=1) + "\n")
        tmp.replace(AUTO())
    return a


def auto_set(b: dict) -> dict:
    """The tab's two saves: the bank of replies, and one reel's keyword, DM and switch.
    Switching a reel on approves every answer the pass then sends, the way a slot approves a post."""
    def change(a):
        if "replies" in b:
            rs = b["replies"] if isinstance(b["replies"], list) else []
            rs = list(dict.fromkeys(str(r).strip() for r in rs if str(r).strip()))
            if not 1 <= len(rs) <= 50 or any(len(r) > 300 for r in rs):
                raise ValueError("the bank holds 1 to 50 replies of up to 300 characters each")
            a["replies"] = rs
        if "reel" in b:
            mid = _seen[str(b["reel"])]["id"]   # KeyError: only a reel the tab lists
            old = a["reels"].get(mid, {})
            if b.get("on") is not True:
                a["reels"][mid] = {**old, "on": False}
                return
            word, dm = str(b.get("word") or "").strip(), str(b.get("dm") or "").strip()
            if not re.fullmatch(r"\w[\w ]{0,28}\w", word) or not 0 < len(dm.encode()) <= 1000:
                raise ValueError("auto needs a keyword (2 to 30 letters or digits) and a DM of up to 1000 bytes")
            # a fresh start: what was refused before is tried once more. `never` stays
            a["reels"][mid] = {"on": True, "word": word, "dm": dm, "sent": old.get("sent", 0),
                               **({"never": old["never"]} if old.get("never") else {})}
    _auto_edit(change)
    return auto_view()


def _due(c: dict, r: dict, cfg: dict, now: float) -> bool:
    """Will the pass answer this waiting comment on reel r? The keyword as a whole word, any
    case; not one Meta refused; not a comment or a person in `never`; inside Meta's week;
    and one answer a person."""
    if not cfg.get("on"):
        return False
    hit, nv = re.compile(rf"\b{re.escape(cfg['word'])}\b", re.I), cfg.get("never", {})
    return bool(c["id"] not in cfg.get("skip", {}) and c["id"] not in nv and c["username"] not in nv.values()
                and hit.search(c["text"])
                and now - datetime.fromisoformat(c["timestamp"]).timestamp() <= DM_DAYS * 86400 - 3600
                and not (c["username"] and any(d["username"] == c["username"] and hit.search(d["text"]) for d in r["answered"])))


def auto_view() -> dict:
    """_auto() as the tab gets it: each reel also says which waiting comments the pass
    will answer (`due`), so the tab offers no Send button for those."""
    a, now = _auto(), time.time()
    for mid, cfg in a["reels"].items():
        r = _seen.get(mid) or {"comments": []}
        cfg["due"] = [c["id"] for c in r["comments"] if _due(c, r, cfg, now)]
    return a


def auto_tick(now: float | None = None) -> None:
    """One pass, once a minute. On a reel switched on in the tab, a waiting comment with
    the reel's keyword gets the reel's DM, then a reply from the bank (never without the
    DM): the oldest first, a few a pass, one answer a person. What doesn't go is kept in
    `skip`, shown in the tab and left to a person."""
    now, left = now or time.time(), AUTO_PER_TICK
    if not token() or not any(r.get("on") for r in _auto()["reels"].values()):
        return
    # ponytail: only reels inbox() lists (the 25 newest posts) are answered
    for r in inbox()["reels"]:
        mid = r["id"]
        if not _auto()["reels"].get(mid, {}).get("on"):
            continue
        if r["stale"]:
            r = reel(mid)
        for c in list(r["comments"]):
            a = _auto()
            cfg = a["reels"][mid]
            if left <= 0 or not cfg["on"]:   # read before every send: off in the tab is off now
                break
            if not _due(c, r, cfg, now):
                continue
            text, why = random.choice([t for t in a["replies"] if t != a.get("last")] or a["replies"]), ""
            left -= 1
            try:
                reply(c["id"], text, cfg["dm"], True)
            except ValueError:    # answered in the app meanwhile
                continue
            except Refused as e:  # any other error means Meta can't be asked right now: the pass ends, the next one tries
                why = str(e)

            def note(a):
                k = a["reels"][mid]
                if not why:
                    a["last"] = text
                    k.update(sent=k.get("sent", 0) + 1, fails=0)
                    return
                # ponytail: skip grows until the reel is switched on again; a handful of ids
                k.setdefault("skip", {})[c["id"]] = why
                k["fails"] = k.get("fails", 0) + 1
                if k["fails"] >= AUTO_FAILS:
                    k.update(on=False, note=f"Switched off after {AUTO_FAILS} in a row didn't go. The last: {why}")
            _auto_edit(note)
            print(f"auto: {c['id']} on {mid} " + (f"left to you: {why}" if why else f"answered: {text}"), flush=True)


# ---------- numbers: what an agent asks about (publish_mcp.py) ----------
NO_INSIGHTS = ("Instagram insights (views, reach, saves) need instagram_manage_insights on the token: "
               "log in again with that scope, then paste the token in Publish")


def _insights(node: str, **q) -> dict:
    """{metric: value} off an insights edge: a post's lifetime `values`, or a period's `total_value`."""
    d = _graph("GET", f"{GRAPH}/{node}/insights?" + urllib.parse.urlencode(q)).get("data", [])
    return {m["name"]: m["total_value"]["value"] if "total_value" in m else m["values"][0]["value"] for m in d}


def metrics(ref: str = "") -> dict:
    """Likes and comments for each of the 25 newest posts, plus views, reach, saves and shares
    where the token may read insights. `ref` narrows it to one: a project id or a media id."""
    me = _me()
    f = "id,caption,permalink,timestamp,media_product_type,like_count,comments_count"
    if ref:
        media = ref if ref.isdigit() else _rec(Path(_row(ref)["dir"])).get("media_id")
        if not media:
            raise ValueError(f"{ref}: not published from here, give the media id")
        posts = [_graph("GET", f"{GRAPH}/{media}?fields={f}")]
    else:
        posts = _graph("GET", f"{GRAPH}/{me['ig_user_id']}/media?" + urllib.parse.urlencode({"fields": f, "limit": 25})).get("data", [])
    out, note = [], ""
    # ponytail: one insights call per post, in turn; a thread pool if 25 of them drag
    for m in posts:
        p = {"id": m["id"], "title": _title(m), "permalink": m.get("permalink", ""), "posted": m.get("timestamp", ""),
             "type": m.get("media_product_type", ""), "likes": m.get("like_count"), "comments": m.get("comments_count")}
        if not note:
            try:
                p.update(_insights(m["id"], metric="views,reach,saved,shares"))
            except RuntimeError as e:
                if "(#10)" in str(e):      # the scope is missing: true of every post, so stop asking
                    note = NO_INSIGHTS
                else:                      # this post's alone (a type that lacks a metric)
                    p["insights"] = str(e)
        out.append(p)
    return {"posts": out, **({"note": note} if note else {})}


def insights(days: int = 7) -> dict:
    """The account: followers and post count, plus the last `days`' reach, views,
    accounts engaged and interactions where the token may read insights."""
    me = _me()
    days = min(max(int(days), 1), 30)   # Meta takes 30 days a call
    a = _graph("GET", f"{GRAPH}/{me['ig_user_id']}?fields=username,followers_count,follows_count,media_count")
    out = {"username": a.get("username"), "followers": a.get("followers_count"), "following": a.get("follows_count"),
           "posts": a.get("media_count"), "days": days}
    now = int(time.time())
    try:
        out.update(_insights(me["ig_user_id"], metric="reach,views,accounts_engaged,total_interactions", period="day",
                             metric_type="total_value", since=now - days * 86400, until=now))
    except RuntimeError as e:
        out["note"] = NO_INSIGHTS if "(#10)" in str(e) else str(e)
    return out


def upload_copy(d: Path, video: str) -> Path:
    """Meta lists reel audio at 128 kbps and our masters are ~200: re-encode the
    audio only, picture untouched. Rebuilt whenever the cut is newer."""
    src, dst = d / video, _out(d) / "ig-upload.mp4"
    if not dst.is_file() or dst.stat().st_mtime < src.stat().st_mtime or _rec(d).get("upload_of") != video:
        pipeline._run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-c:v", "copy", "-c:a", "aac",
                       "-b:a", "128k", "-ar", "48000", "-movflags", "+faststart", str(dst)])
    return dst


COVERS: dict = {}   # project dir -> close() for its cover's public link


def _expose(d: Path, *files: Path) -> list[str]:
    """A public https URL for each file (the cover jpg, and a trial's mp4), up
    until the container is processed (or an hour, whichever is first). Nothing
    else on this machine is exposed: the tunnel reaches a loopback server that
    answers one random path per file."""
    serve = {"/" + secrets.token_hex(16) + f.suffix: f for f in files}
    names = list(serve)
    name, body = names[0], files[0].read_bytes()   # the one fetched to check the tunnel

    class One(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in serve:
                return self.send_error(404)
            f = serve[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4" if f.suffix == ".mp4" else "image/jpeg")
            self.send_header("Content-Length", str(f.stat().st_size))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(f.read_bytes())
        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), One)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log = _out(d) / "cover-tunnel.log"
    cf = subprocess.Popen([CLOUDFLARED, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{srv.server_port}"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log.open("w"))

    def close():
        cf.terminate()
        cf.wait(10)
        srv.shutdown()
        srv.server_close()
    _close(d)
    COVERS[str(d)] = close
    t = threading.Timer(3600, lambda: COVERS.get(str(d)) is close and _close(d))
    t.daemon = True
    t.start()
    # the quick tunnel prints its URL in ~3 s; from here it answered after ~65 s
    # (a lookup made too early is cached as missing), so check it the way Meta will
    end = time.time() + 180
    while time.time() < end and cf.poll() is None:
        m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", log.read_text())
        if m:
            try:
                req = urllib.request.Request(m.group(0) + name, headers={"User-Agent": "reeldeck"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    if r.read() == body:
                        return [m.group(0) + n for n in names]
            except OSError:
                pass
        time.sleep(3)
    _close(d)
    raise RuntimeError(f"the cloudflared quick tunnel didn't come up (see {log.name})")


def _close(d: Path) -> None:
    COVERS.pop(str(d), lambda: None)()


def _claim(d: Path, ok: tuple, to: str) -> dict | None:
    """Move a record into an in-flight state, or None if someone else has it."""
    with LOCK:
        rec = _rec(d)
        if rec.get("status", "draft") not in ok:
            return None
        rec["status"] = to
        _save(d, rec)
        return rec


def _finish(d: Path, **kw) -> None:
    with LOCK:
        rec = _rec(d)
        msg = kw.pop("msg", None)
        rec.update(kw)
        if msg:
            _log(rec, msg)
        _save(d, rec)


def _stage(row: dict) -> None:
    d = Path(row["dir"])
    was = _claim(d, ("draft", "queued", "failed", "missed", "staged"), "staging")
    if was is None:
        return
    back = "queued" if was.get("slot") else "draft"
    try:
        p = view(row)
        f = upload_copy(d, p["video"])
        req = dict(p["request"])
        via = "ig" if "video_url" in req else None   # a trial: Instagram Login, never the route that drops it
        t = _auth(via)
        if not t:
            return _finish(d, status=back, upload_of=p["video"],
                           msg=f"dry run: upload copy ready ({f.stat().st_size / 1e6:.1f} MB); no token, nothing sent")
        links = {k: v for k, v in (("cover_url", d / p["settings"]["cover"]), ("video_url", f)) if k in req}
        if links:
            try:
                req.update(zip(links, _expose(d, *links.values())))
            except Exception as e:
                if via:
                    raise RuntimeError(f"no public link for the upload copy ({e})") from None
                del req["cover_url"]
                _finish(d, msg=f"no public link for the cover ({e}); the frame at {req['thumb_offset']} ms is the cover")
        c = _graph("POST", f"{IGRAPH if via else GRAPH}/{t['ig_user_id']}/media", data=req)
        if not via:
            _graph("POST", f"{RUPLOAD}/{c['id']}", body=f.read_bytes(),
                   headers={"offset": "0", "file_size": str(f.stat().st_size)})
        _finish(d, status="processing", upload_of=p["video"], container={"id": c["id"], "at": time.time(), "via": via},
                msg=f"{'linked' if via else 'uploaded'} {f.stat().st_size / 1e6:.1f} MB, Instagram is processing it (nothing public)")
    except Exception as e:
        _close(d)
        # a scheduled reel is staged again by the next tick: say so, or the line reads as a dead end
        again = "" if back == "draft" else "; retrying in a minute" if time.time() >= was["slot"] - STAGE_AHEAD \
            else "; it will try again 2 h before the slot"
        _finish(d, status=back, msg=f"staging failed: {e}{again}")


def _poll(d: Path, rec: dict) -> None:
    j = _graph("GET", f"{_base(rec)}/{rec['container']['id']}?fields=status_code,status")
    code = j.get("status_code")
    if code in ("FINISHED", "ERROR", "EXPIRED"):
        _close(d)   # Meta has the cover by now, or never will
    if code == "FINISHED":
        _finish(d, status="staged", msg="staged: ready to publish")
    elif code in ("ERROR", "EXPIRED"):
        _finish(d, status="queued" if rec.get("slot") else "failed", container=None,
                msg=f"processing {code.lower()}: {j.get('status', '')}; restaging" if rec.get("slot") else f"processing {code.lower()}")


def _base(rec: dict) -> str:
    """The host a container was made on is the one that polls and publishes it."""
    return IGRAPH if (rec.get("container") or {}).get("via") == "ig" else GRAPH


def _publish(d: Path) -> None:
    rec = _claim(d, ("staged",), "publishing")
    if rec is None:
        return
    via = rec["container"].get("via")
    t = _auth(via)
    try:
        j = _graph("POST", f"{_base(rec)}/{t['ig_user_id']}/media_publish", data={"creation_id": rec["container"]["id"]})
    except Exception as e:
        # never retried by itself: a reply lost after Meta posted would post it twice
        return _finish(d, status="failed", msg=f"publish failed: {e}. Check Instagram before rescheduling")
    link = ""
    try:
        link = _graph("GET", f"{_base(rec)}/{j['id']}?fields=permalink").get("permalink", "")
    except Exception:
        pass
    _finish(d, status="published", media_id=j["id"], permalink=link, published_at=time.time(),
            msg="published " + link)


def tick(now: float | None = None) -> None:
    """One pass of the queue. Called every minute, which is also Meta's
    polling limit for a processing container."""
    now = now or time.time()
    for row in projects.list_projects():
        d = Path(row["dir"])
        if row["missing"] or not (d / RECORD).is_file():
            continue
        rec = _rec(d)
        slot, st = rec.get("slot"), rec.get("status")
        via = (rec.get("container") or {}).get("via") or ("ig" if (rec.get("settings") or {}).get("trial") else None)
        try:
            if slot and st in ("queued", "processing", "staged") and now > slot + LATE:
                _finish(d, status="missed", container=None, msg="missed the slot by over an hour; not posted")
            elif not _auth(via):
                if slot and st == "queued" and now >= slot - STAGE_AHEAD and \
                        not rec.get("log", [[0, ""]])[-1][1].startswith("waiting"):
                    _finish(d, msg="waiting for the Instagram " + ("Login token (a trial needs it)" if via else "token")
                            + "; nothing will post without it")
            elif st == "processing":
                _poll(d, rec)
            elif slot and st == "queued" and now >= slot - STAGE_AHEAD:
                _stage(row)
            elif slot and st == "staged" and now - rec["container"]["at"] > EXPIRY:
                _finish(d, status="queued", container=None, msg="container near its 24 h expiry; restaging")
            elif slot and st == "staged" and now >= slot:
                _publish(d)
        except Exception as e:
            _finish(d, msg=f"scheduler: {e}")


def start() -> None:
    """Recover from a restart, then tick forever. A half-done upload is
    useless and is redone; a half-done publish is never retried blind."""
    for row in projects.list_projects():
        d = Path(row["dir"])
        st = _rec(d).get("status") if not row["missing"] else None
        if st == "staging":
            _finish(d, status="queued" if _rec(d).get("slot") else "draft", msg="restart interrupted staging")
        elif st == "publishing":
            _finish(d, status="failed", msg="restart interrupted publishing. Check Instagram before rescheduling")

    def loop():
        while True:
            try:
                tick()
            except Exception as e:
                print("publish tick:", e, flush=True)
            try:
                auto_tick()
            except Exception as e:
                print("auto tick:", e, flush=True)
            try:
                ig_refresh()
            except Exception as e:
                print("instagram login refresh:", e, flush=True)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()


# ---------- self-check: the whole queue against a fake Graph API ----------
if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    d = tmp / "reel"
    d.mkdir()
    pipeline.save(d, {"name": "t", "out": "clips/out", "segments": []})
    (d / "clips" / "out").mkdir(parents=True)
    pipeline._run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=108x192:r=30:d=4",
                   "-f", "lavfi", "-i", "sine=d=4", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(d / "t.mp4")])
    (d / "work").mkdir()
    (d / "work" / "finish.sh").write_text("F=${1:-75}  # cover\n")
    (d / CAPTION).write_text("hello @friend #one #two\n")
    pipeline._run(["ffmpeg", "-v", "error", "-i", str(d / "t.mp4"), "-frames:v", "1", str(d / "t.jpg")])
    projects.list_projects = lambda: [{"id": "t", "name": "t", "dir": str(d), "missing": False}]
    TOKEN_FILE, IG_TOKEN_FILE = tmp / "instagram.json", tmp / "instagram-login.json"

    p = view({"id": "t", "name": "t", "dir": str(d)})
    assert p["settings"]["thumb_offset"] == 2500 and p["tags"] == {"hashtags": ["#one", "#two"], "mentions": ["@friend"]}
    assert p["problems"] == [] and p["request"]["caption"] == "hello @friend #one #two"
    # the tab lists only reels set to ready in the timeline
    assert listing()["posts"] == []
    pipeline.save(d, {**pipeline.load(d), "status": "ready"})
    assert [p["pid"] for p in listing()["posts"]] == ["t"]
    # the cover: finish.sh's jpg by default, "" for the frame, nothing outside the project
    assert p["settings"]["cover"] == "t.jpg" and "cover_url" in p["request"]
    for bad in ("../t.jpg", "nope.jpg", "t.mp4"):
        try:
            edit("t", {"settings": {"cover": bad}})
            raise AssertionError(f"took cover {bad}")
        except ValueError:
            pass
    edit("t", {"settings": {"cover": ""}})
    assert "cover_url" not in view(_row("t"))["request"]
    # a hold note is a problem, so Schedule refuses until it is deleted
    _save(d, {**_rec(d), "hold": "audio: check it"})
    assert view(_row("t"))["problems"] == ["audio: check it"]
    try:
        schedule("t", 0)
        raise AssertionError("scheduled through a hold")
    except ValueError:
        pass
    _save(d, {k: v for k, v in _rec(d).items() if k != "hold"})
    edit("t", {"settings": {"cover": "t.jpg"}})
    # a reel with a voice-over: the voiced cut is the default, a music-only cut can't go out, a copy of it can
    m0 = pipeline.load(d)
    pipeline.save(d, {**m0, "vo": {"takes": [{"file": "x.wav"}]}})
    assert "no voiced cut yet" in view(_row("t"))["problems"][0]
    pipeline._run(["ffmpeg", "-v", "error", "-i", str(d / "t.mp4"), "-f", "lavfi", "-i", "sine=f=880:d=4",
                   "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", str(d / "clips/out/reel.mp4")])
    p = view(_row("t"))
    assert p["video"] == "clips/out/reel.mp4" and p["problems"] == [], p["problems"]
    edit("t", {"video": "t.mp4"})
    assert "t.mp4 has no voice-over" in view(_row("t"))["problems"][0]
    try:
        schedule("t", 0)
        raise AssertionError("scheduled a music-only cut of a voiced reel")
    except ValueError:
        pass
    shutil.copy(d / "clips/out/reel.mp4", d / "copy.mp4")
    edit("t", {"video": "copy.mp4"})
    assert view(_row("t"))["problems"] == []
    (d / "copy.mp4").unlink()
    (d / "clips/out/reel.mp4").unlink()
    pipeline.save(d, m0)
    edit("t", {"video": "t.mp4"})

    # no token: scheduling queues it, staging is a dry run, nothing is sent
    schedule("t", time.time() + 3600)
    tick()
    assert _rec(d)["status"] == "queued" and "waiting" in _rec(d)["log"][-1][1]
    _stage(_row("t"))
    assert _rec(d)["status"] == "queued" and (d / "clips/out/ig-upload.mp4").is_file()

    # with a token: stage at slot-2h, poll, publish at the slot, exactly once
    # the paste box: Meta vouches for it, the file is 600, the token never comes back
    def meta(method, url, data=None, headers=None, body=None, tok=None):
        if "debug_token" in url:
            page = "input_token=PAGE" in url
            return {"data": {"is_valid": True, "type": "PAGE" if page else "USER",
                             "expires_at": 0 if page else time.time() + 60 * 86400,
                             "data_access_expires_at": time.time() + 90 * 86400,
                             "scopes": ["instagram_basic", "instagram_content_publish"]}}
        return {"data": [{"id": "P0", "access_token": "PAGE0"},
                         {"access_token": "PAGE1", "instagram_business_account": {"id": "IG", "username": "me"}}]}
    _graph = meta
    try:
        save_token("not a token")
        raise AssertionError("saved junk")
    except ValueError:
        pass
    a = save_token("EAA" + "x" * 60)
    assert a["token"] and a["note"].startswith("@me · token good till") and "EAA" not in json.dumps(a)
    assert TOKEN_FILE.stat().st_mode & 0o777 == 0o600 and token()["ig_user_id"] == "IG"
    # a user token is swapped for the linked Page's, which lasts until data access ends (90 d)
    assert token()["access_token"] == "PAGE1" and token()["expires"] - time.time() > 80 * 86400

    calls, opened = [], []

    def expose(d_, *files):
        opened.extend(f.name for f in files)
        COVERS[str(d_)] = lambda: opened.append("closed")
        return [f"https://x.trycloudflare.com/{f.name}" for f in files]
    _expose = expose

    def fake(method, url, data=None, headers=None, body=None, tok=None):
        calls.append((method, url.split("/", 3)[-1], data))
        if url.endswith("/media"):
            return {"id": "C1"}
        if "status_code" in url:
            return {"status_code": "FINISHED"}
        if url.endswith("/media_publish"):
            return {"id": "M1"}
        if "permalink" in url:
            return {"permalink": "https://example.invalid/reel/1"}
        return {"success": True}
    _graph = fake
    slot = _rec(d)["slot"]
    tick(slot - STAGE_AHEAD - 60)
    assert _rec(d)["status"] == "queued" and not calls          # too early to stage
    tick(slot - STAGE_AHEAD + 1)
    assert _rec(d)["status"] == "processing" and len(calls) == 2  # container + bytes
    assert calls[0][2]["thumb_offset"] == 2500 and calls[0][2]["media_type"] == "REELS"
    assert calls[0][2]["cover_url"] == "https://x.trycloudflare.com/t.jpg" and opened == ["t.jpg"]
    tick(slot - 60)
    assert _rec(d)["status"] == "staged" and opened[-1] == "closed" and not COVERS  # link down once processed
    tick(slot - 30)
    assert _rec(d)["status"] == "staged"                         # never early
    tick(slot + 1)
    tick(slot + 61)
    assert _rec(d)["status"] == "published" and sum(c[1].endswith("media_publish") for c in calls) == 1
    try:
        edit("t", {"caption": "x"})
        raise AssertionError("edited a published reel")
    except ValueError:
        pass

    # the Comments tab: ours and the answered drop out, with or without names
    cs = [{"id": "1", "replies": {"data": [{"user": {"id": "IG"}}]}}, {"id": "2", "replies": {"data": [{"username": "x"}]}},
          {"id": "3", "user": {"id": "IG"}}, {"id": "4", "username": "me"}, {"id": "5"}]
    assert [c["id"] for c in waiting(cs, token())] == ["2", "5"]
    # a reply goes under that one comment, and only a real id reaches the URL
    calls.clear()
    reply("17900000000000001", " Sent! ")
    assert calls[1:] == [("POST", f"{V}/17900000000000001/replies", {"message": "Sent!"})] and calls[0][0] == "GET"
    for cid, text in (("1/../me", "hi"), ("17900000000000001", " "), ("17900000000000001", "x" * 301)):
        try:
            reply(cid, text)
            raise AssertionError(f"replied {cid!r} {text[:5]!r}")
        except ValueError:
            pass
    assert len(calls) == 2
    # with a DM line: the DM goes first, then the reply; a refused DM holds the reply back only when asked to
    sent, refuse, blank = [], [], []

    def dms(method, url, data=None, headers=None, body=None, tok=None):
        if url.endswith("/messages"):
            if url.split("/")[-2] in refuse:
                raise RuntimeError("400: (#3) no capability")
            sent.append(("dm", url.split("/")[-2], json.loads(body)))
            return {} if blank else {"message_id": "m1"}
        elif method == "POST":
            sent.append(("reply", url.split("/")[-2], data["message"]))
        return {}
    _graph = dms
    assert reply("17900000000000007", "Sent!", "the link", True) == {"dm": True, "replied": True}
    assert sent == [("dm", "me", {"recipient": {"comment_id": "17900000000000007"}, "message": {"text": "the link"}}),
                    ("reply", "17900000000000007", "Sent!")]
    refuse[:] = ["me"]                            # Meta won't take the DM
    try:
        reply("17900000000000008", "Sent!", "the link", True)
        raise AssertionError("replied although the DM was refused")
    except RuntimeError as e:
        assert "(#3)" in str(e) and len(sent) == 2
    r = reply("17900000000000008", "Sent!", "the link")
    assert r == {"dm": False, "replied": True, "dm_error": "400: (#3) no capability"} and sent[-1][0] == "reply"
    refuse.clear()
    blank.append(1)                               # a 2xx that names no message is not a DM either
    try:
        reply("17900000000000010", "Sent!", "the link", True)
        raise AssertionError("replied on a DM with no message_id")
    except RuntimeError as e:
        assert "named no message" in str(e) and sent.pop()[0] == "dm"   # asked, and nothing replied after it
    blank.clear()
    assert reply("17900000000000009", "", "the link") == {"dm": True, "replied": False}       # a DM alone
    assert sent[-1][:2] == ("dm", "me")
    assert reply("17900000000000009", "Sent!", "the link")["replied"] and [k for k, *_ in sent].count("dm") == 2   # never twice
    _graph = fake
    # what the tab remembers: a quiet reel costs no call, a moved count is asked about again
    posts = [{"id": "R1", "media_product_type": "REELS", "comments_count": 2, "caption": "Hi. More"}]

    def ig(method, url, data=None, **k):
        calls.append(method)
        if "/media?" in url or "/comments?" in url:
            return {"data": posts if "/media?" in url else [{"id": "17900000000000002"}, {"id": "17900000000000001"}]}
        return {"replies": {"data": [{"user": {"id": "IG"}}]}} if "0002?" in url else {}   # 2 got answered in the app
    _graph = ig
    calls.clear()
    assert inbox()["reels"][0]["stale"] and len(calls) == 1          # listed, not read yet
    assert [c["id"] for c in reel("R1")["comments"]] == ["17900000000000001", "17900000000000002"]
    assert not inbox()["reels"][0]["stale"] and len(calls) == 3      # the count stands: from memory
    reply("17900000000000001", "Sent!")
    posts[0]["comments_count"] = 3                                   # our reply, as Meta counts it
    r = inbox()["reels"][0]
    assert not r["stale"] and [c["id"] for c in r["comments"]] == ["17900000000000002"]
    try:
        reply("17900000000000002", "Sent!")
        raise AssertionError("answered a comment twice")
    except ValueError:
        pass
    assert calls.count("POST") == 1 and not _seen["R1"]["comments"]
    posts[0]["comments_count"] = 4                                   # someone new
    assert inbox()["reels"][0]["stale"] and inbox()["reels"][0]["stale"]   # until it's read again
    assert not reel("R1")["stale"] and inbox(fresh=True)["reels"][0]["stale"]
    n = len(calls)
    assert remembered()["reels"][0]["id"] == "R1" and len(calls) == n   # no call to Meta
    # an agent's list: every reel's comments newest first, the answered ones marked
    _graph = lambda method, url, **k: {"data": posts if "/media?" in url else [
        {"id": "17900000000000002", "timestamp": "2", "replies": {"data": [{"user": {"id": "IG"}}]}},
        {"id": "17900000000000001", "timestamp": "1"}]}
    got = recent()["comments"]   # the reel is stale (fresh, above), so this reads it
    assert [(c["id"][-1], c["answered"]) for c in got] == [("2", True), ("1", False)] and got[1]["link"].endswith("/c/17900000000000001/")
    assert [c["id"][-1] for c in recent(unanswered=True)["comments"]] == ["1"] and recent(1)["of"] == 2

    # auto: nothing until the tab switches a reel on. Then the keyword comments get the DM and a
    # reply from the bank: the oldest first, a few a pass, one a person, never past Meta's week,
    # and what Meta refuses is not tried twice
    stamp = lambda days: time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(time.time() - days * 86400))
    board = [{"id": f"1790000000000001{i}", "text": t, "username": u, "timestamp": stamp(w)} for i, (t, u, w) in enumerate((
        ("BUILD", "a", 0), ("nice reel", "b", 0), ("Build please!", "c", 0), ("rebuild it", "d", 0), ("build", "no_dms", 0),
        ("BUILD", "late", 8), ("build again", "a", 0), ("BUILD", "e", 0), ("BUILD", "f", 0)))]
    ours, sent, block = set(), [], []

    def insta(method, url, data=None, headers=None, body=None, tok=None):
        node = url.split("?")[0].split("/")
        mine = lambda cid: {"replies": {"data": [{"user": {"id": "IG"}}]}} if cid in ours else {}
        if method == "GET":
            if "/media?" in url:
                return {"data": [{"id": "R2", "media_product_type": "REELS", "comments_count": len(board) + len(ours), "caption": "x"}]}
            return {"data": [{**c, **mine(c["id"])} for c in board[::-1]]} if "/comments?" in url else mine(node[-1])
        if node[-1] == "messages":
            cid = json.loads(body)["recipient"]["comment_id"]
            sent.append(("dm", cid, json.loads(body)["message"]["text"]))
            if block or cid == board[4]["id"]:
                sent[-1] = ("refused", cid, "")
                raise RuntimeError("400: this person doesn't take message requests")
            return {"message_id": f"m{len(sent)}"}
        else:
            ours.add(node[-2])
            sent.append(("reply", node[-2], data["message"]))
        return {}
    _graph = insta
    _seen.clear()
    auto_tick()
    assert not sent and not AUTO().exists() and str(AUTO()).startswith(str(tmp))   # nothing is on, and no real file in play
    assert inbox()["auto"]["replies"] == REPLIES                                     # the tab lists the reel and the bank
    for bad in ({"reel": "R2", "on": True, "word": "b", "dm": "link"}, {"reel": "R2", "on": True, "word": "BUILD", "dm": " "},
                {"reel": "R2", "on": True, "word": "a.*", "dm": "link"}, {"replies": []}, {"replies": "Sent!"}, {"replies": ["x" * 301]}):
        try:
            auto_set(bad)
            raise AssertionError(f"took {bad}")
        except ValueError:
            pass
    try:
        auto_set({"reel": "R9", "on": True, "word": "BUILD", "dm": "link"})
        raise AssertionError("switched on a reel the tab never listed")
    except KeyError:
        pass
    auto_tick()
    assert not sent                                                                  # a refused save switched nothing on
    assert auto_set({"replies": ["one", " two ", "two", " "]})["replies"] == ["one", "two"]
    auto_set({"reel": "R2", "on": True, "word": "BUILD", "dm": "the link"})
    auto_tick()
    tail = lambda kind: [s[1][-1] for s in sent if s[0] == kind]
    assert tail("dm") == ["0", "2", "7"] and tail("reply") == ["0", "2", "7"] and tail("refused") == ["4"], sent
    assert all(s[2] == "the link" for s in sent if s[0] == "dm") and [s[0] for s in sent[:2]] == ["dm", "reply"]   # DM first
    said = [s[2] for s in sent if s[0] == "reply"]
    assert set(said) <= {"one", "two"} and all(x != y for x, y in zip(said, said[1:]))   # never the same twice running
    k = _auto()["reels"]["R2"]
    assert k["sent"] == 3 and k["fails"] == 0 and "didn't go" in k["skip"][board[4]["id"]]
    line = lambda: [i[-1] for i in remembered()["auto"]["reels"]["R2"]["due"]]
    assert line() == ["8"] and "due" not in k                                       # the tab knows who is still in line
    auto_tick()
    assert line() == []
    assert tail("reply") == ["0", "2", "7", "8"]                                    # the one the pace left over
    n = len(sent)
    auto_tick()
    assert len(sent) == n        # the refused one, the week-old one, a's second and the non-keyword ones stay for a person
    board += [{"id": f"1790000000000002{i}", "text": "BUILD", "username": f"n{i}", "timestamp": stamp(0)} for i in range(6)]
    auto_set({"reel": "R2", "on": False})
    auto_tick()
    assert len(sent) == n and _auto()["reels"]["R2"]["skip"]                        # off is off, and what didn't go stays shown
    _auto_edit(lambda a: a["reels"]["R2"].update(never={board[-6]["id"]: "n0", "x": "n1"}))   # by hand: by comment, and by person
    block.append(1)                                                                 # Meta starts refusing everything
    auto_set({"reel": "R2", "on": True, "word": "BUILD", "dm": "the link"})
    auto_tick()
    auto_tick()
    auto_tick()
    k = _auto()["reels"]["R2"]
    assert len(sent) == n + AUTO_FAILS and not k["on"] and k["note"].startswith("Switched off") and k["sent"] == 4, (len(sent) - n, k)
    assert "skip" in k and len(k["never"]) == 2 and not {s[1][-2:] for s in sent[n:]} & {"20", "21"}   # switching on kept `never`, and both were left alone
    assert len(ours) == 4                                                           # and not one reply without its DM

    # numbers: likes always, insights when the token may read them, a note when it may not
    def nums(method, url, data=None, **k):
        if "/insights?" not in url:
            return {"data": [{"id": "R1", "like_count": 5, "comments_count": 2, "caption": "Hi. More"}], "followers_count": 9}
        if not scope:
            raise RuntimeError("400: (#10) Application does not have permission for this action")
        return {"data": [{"name": "reach", "total_value": {"value": 7}}] if "total_value" in url
                else [{"name": "views", "values": [{"value": 40}]}, {"name": "saved", "values": [{"value": 3}]}]}
    _graph, scope = nums, True
    assert metrics() == {"posts": [{"id": "R1", "title": "Hi.", "permalink": "", "posted": "", "type": "",
                                    "likes": 5, "comments": 2, "views": 40, "saved": 3}]}
    assert insights(99) == {"username": None, "followers": 9, "following": None, "posts": None, "days": 30, "reach": 7}
    scope = False
    m = metrics()
    assert m["note"] == NO_INSIGHTS and m["posts"][0]["likes"] == 5 and "views" not in m["posts"][0]
    assert insights()["note"] == NO_INSIGHTS and insights()["followers"] == 9
    _graph = fake

    # no public link for the cover: the frame stands in, the reel still stages
    _save(d, {"status": "queued", "slot": slot + 7200})
    _expose = lambda *a: (_ for _ in ()).throw(RuntimeError("no tunnel"))
    calls.clear()
    _stage(_row("t"))
    assert "cover_url" not in calls[0][2] and _rec(d)["status"] == "processing"
    assert any("frame at 2500 ms" in m for _, m in _rec(d)["log"])

    # a failed upload says when it is tried again: next tick near the slot, not at all without one
    _graph = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("500: boom"))
    for rec, st, end in (({"status": "queued", "slot": time.time() + 60}, "queued", "boom; retrying in a minute"),
                         ({"status": "queued", "slot": time.time() + 9 * 3600}, "queued", "boom; it will try again 2 h before the slot"),
                         ({"status": "draft"}, "draft", "boom")):
        _save(d, rec)
        _stage(_row("t"))
        assert _rec(d)["status"] == st and _rec(d)["log"][-1][1].endswith(end), _rec(d)["log"]

    # a failed publish is not retried, and an edit after staging drops the container
    _save(d, {"status": "staged", "slot": slot, "container": {"id": "C2", "at": slot}})
    tick(slot + 1)
    assert _rec(d)["status"] == "failed"
    _save(d, {"status": "staged", "slot": slot, "container": {"id": "C3", "at": slot}})
    edit("t", {"settings": {"share_to_feed": False}})
    assert _rec(d)["status"] == "queued" and "container" not in _rec(d)
    tick(slot + LATE + 5)
    assert _rec(d)["status"] == "missed"

    # a trial goes through Instagram Login, never the Facebook route that posts it as a normal reel
    _save(d, {"settings": {"trial": "MANUAL"}})
    p = view(_row("t"))
    r = p["request"]
    assert json.loads(r["trial_params"]) == {"graduation_strategy": "MANUAL"} and "video_url" in r
    assert "share_to_feed" not in r and "upload_type" not in r and "cover_url" in r
    assert "Instagram Login" in p["problems"][-1]                       # no IG Login token: Schedule refuses
    try:
        schedule("t", 0)
        raise AssertionError("scheduled a trial without the Instagram Login token")
    except ValueError:
        pass
    _save(d, {"settings": {"trial": "MANUAL"}, "status": "queued", "slot": slot})
    n = len(calls)
    tick(slot - 60)
    assert len(calls) == n and "Login token" in _rec(d)["log"][-1][1]  # it waits, nothing goes the old way
    # the second paste box: Instagram says whose token it is, and it must be the account reels post as
    me = {"user_id": 1784, "username": "other"}
    _graph = lambda method, url, tok=None, **k: (calls.append(url), me)[1]
    try:
        save_ig_token("IGAA" + "y" * 60)
        raise AssertionError("saved another account's token")
    except ValueError as e:
        assert "@other" in str(e) and not IG_TOKEN_FILE.exists()
    me["username"] = "me"
    a = save_ig_token("IGAA" + "y" * 60)
    assert a["ig"] and "@me via Instagram Login" in a["ig_note"] and "IGAA" not in json.dumps(a)
    assert IG_TOKEN_FILE.stat().st_mode & 0o777 == 0o600 and ig_token()["ig_user_id"] == "1784"
    assert calls[-1] == f"{IGRAPH}/me?fields=user_id,username"
    assert token()["access_token"] == "PAGE1"                          # the Facebook Login token is untouched
    # stage, poll, publish: all on graph.instagram.com, the cut as a link, no resumable upload
    seen, opened[:] = [], []

    def igapi(method, url, data=None, headers=None, body=None, tok=None):
        seen.append((method, url, data, tok or _auth("ig" if url.startswith(IGHOST) else None)["access_token"]))
        return fake(method, url, data)
    _graph, _expose = igapi, expose
    _save(d, {"settings": {"trial": "MANUAL"}, "status": "queued", "slot": slot})
    tick(slot - 60)
    assert _rec(d)["status"] == "processing" and _rec(d)["container"]["via"] == "ig" and len(seen) == 1
    m, url, data, tok = seen[0]
    assert url == f"{IGRAPH}/1784/media" and tok.startswith("IGAA") and "rupload" not in json.dumps(seen)
    assert data["video_url"] == "https://x.trycloudflare.com/ig-upload.mp4" and data["cover_url"].endswith("t.jpg")
    assert json.loads(data["trial_params"]) == {"graduation_strategy": "MANUAL"} and "share_to_feed" not in data
    assert opened == ["t.jpg", "ig-upload.mp4"]
    tick(slot - 30)
    tick(slot + 1)
    tick(slot + 61)
    assert _rec(d)["status"] == "published" and all(u.startswith(IGRAPH) and t.startswith("IGAA") for _, u, _, t in seen)
    assert [u for _, u, _, _ in seen if u.endswith("media_publish")] == [f"{IGRAPH}/1784/media_publish"]
    # no tunnel, no trial: it fails rather than dropping the video link
    _save(d, {"settings": {"trial": "MANUAL"}, "status": "queued", "slot": time.time() + 60})
    _expose = lambda *a: (_ for _ in ()).throw(RuntimeError("no tunnel"))
    _stage(_row("t"))
    assert _rec(d)["status"] == "queued" and "no public link for the upload copy" in _rec(d)["log"][-1][1]
    # the token renews weekly, retries a failure daily, and only the new one is kept
    t0 = ig_token()
    seen.clear()
    _graph = lambda method, url, tok=None, **k: (seen.append(url), {"access_token": "IGAA" + "z" * 60, "expires_in": 5183944})[1]
    ig_refresh(t0["saved"] + 6 * 86400)
    assert not seen
    ig_refresh(t0["saved"] + 8 * 86400)
    assert seen[0].startswith(f"{IGHOST}/refresh_access_token?grant_type=ig_refresh_token&access_token=IGAA")
    t1 = ig_token()
    assert t1["access_token"] == "IGAA" + "z" * 60 and t1["expires"] == t0["saved"] + 8 * 86400 + 5183944
    _graph = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("400: expired"))
    for dt in (15, 15.5):
        try:
            ig_refresh(t1["saved"] + dt * 86400)
            assert dt == 15.5, "a failed refresh was retried within the day"
        except RuntimeError:
            assert dt == 15
    assert ig_token()["access_token"] == t1["access_token"]
    shutil.rmtree(tmp)
    print("publish: ok")
