#!/usr/bin/env python3
"""reeldeck as MCP tools, over stdio: the Publish queue, the Comments tab's list,
and Instagram's numbers for the account.

Every call goes through the running reeldeck server (/api/…), so an agent sees
and changes exactly what the tabs show, and the server is the only thing that
ever talks to Instagram. Nothing here holds the token, and nothing here needs one.

    claude mcp add reeldeck -- python3 /path/to/reeldeck/publish_mcp.py
    python3 publish_mcp.py --check     # the tool names against a verb-based approval gate

The names are the safety. Agents that gate tools by the verbs in their names
(read verbs run on their own, write verbs wait for the user's yes, check() below)
hold back every tool here that changes anything.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote, urlencode

BASE = os.environ.get("REELDECK_URL", "http://127.0.0.1:8810")
PID = {"type": "string", "description": "the reel's id, from list_reels"}

TOOLS = [
    {"name": "list_reels",
     "description": "Every reel with a cut: status (draft/queued/staged/published…), slot, problems "
                    "Instagram would refuse, and whether a token is set (no token = dry run).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_reel",
     "description": "One reel's full post: caption, hashtags, cut and its specs, cover offset, settings, "
                    "the exact container request Instagram would get, and the log.",
     "inputSchema": {"type": "object", "properties": {"pid": PID}, "required": ["pid"]}},
    {"name": "list_comments",
     "description": "The newest comments on the account's recent reels, newest first: who, what, when, which "
                    "reel, a link to the comment, and whether we answered it. `of` is how many there are in all.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "how many, default 20"},
         "unanswered": {"type": "boolean", "description": "only the ones still waiting for a reply"}}}},
    {"name": "get_metrics",
     "description": "Instagram's numbers per post, newest first: likes and comments, plus views, reach, saved and shares "
                    "when the token may read insights (a `note` says so when it may not). Without `reel`, the 25 newest posts.",
     "inputSchema": {"type": "object", "properties": {
         "reel": {"type": "string", "description": "one post: a reel's id from list_reels, or an Instagram media id"}}}},
    {"name": "get_account_insights",
     "description": "The Instagram account: followers, following, post count, plus the last `days`' reach, views, "
                    "accounts engaged and interactions when the token may read insights (a `note` says so when it may not).",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "1 to 30, default 7"}}}},
    {"name": "update_reel",
     "description": "Edit a reel's caption (written to its share-copy.txt), cut, or settings: share_to_feed, "
                    "cover (a .jpg in the project, sent as cover_url; '' = use the frame at thumb_offset), "
                    "thumb_offset (ms), audio_name, collaborators (≤3), user_tags, location_id, "
                    "trial ('' | MANUAL | SS_PERFORMANCE; a trial needs the Instagram Login token from the tab). Editing a staged reel drops its upload.",
     "inputSchema": {"type": "object", "properties": {
         "pid": PID, "caption": {"type": "string"}, "video": {"type": "string"},
         "settings": {"type": "object"}}, "required": ["pid"]}},
    {"name": "post_public_reply",
     "description": "POSTS A PUBLIC REPLY under one Instagram comment, as the account. It cannot be unsent. Only "
                    "call after the user said yes to this comment and this text in this conversation. One reply "
                    "of ours per comment: a comment already answered is refused.",
     "inputSchema": {"type": "object", "properties": {
         "comment_id": {"type": "string", "description": "the comment's id, from list_comments"},
         "text": {"type": "string", "description": "1 to 300 characters"}}, "required": ["comment_id", "text"]}},
    {"name": "upload_reel",
     "description": "Upload now as an unpublished container: NOTHING public, it expires in 24 h. "
                    "Without a token it only builds the upload copy and logs the request.",
     "inputSchema": {"type": "object", "properties": {"pid": PID}, "required": ["pid"]}},
    {"name": "schedule_reel",
     "description": "APPROVES A PUBLIC POST: the reel goes live on Instagram at `at` (ISO 8601 with a "
                    "zone; a past time means now). Only call after the user said yes to this reel, this "
                    "caption and this time in this conversation.",
     "inputSchema": {"type": "object", "properties": {
         "pid": PID, "at": {"type": "string", "description": "e.g. 2026-10-14T18:00:00-05:00"}},
         "required": ["pid", "at"]}},
    {"name": "cancel_schedule",
     "description": "Take a reel out of the queue; it won't post.",
     "inputSchema": {"type": "object", "properties": {"pid": PID}, "required": ["pid"]}},
]


READS = {"list_reels", "get_reel", "list_comments", "get_metrics", "get_account_insights"}
for t in TOOLS:
    t["annotations"] = {"readOnlyHint": t["name"] in READS}


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}") or {"error": f"http {e.code}"}
    except urllib.error.URLError as e:
        return {"error": f"reeldeck isn't answering at {BASE} ({e.reason}): systemctl --user start reeldeck.service"}


def run(name, a):
    if name == "list_reels":
        d = call("GET", "/api/publish")
        return {"account": d.get("account"), "reels": [
            {k: p.get(k) for k in ("pid", "name", "status", "slot", "problems")} |
            {"slot_local": datetime.fromtimestamp(p["slot"]).astimezone().isoformat() if p.get("slot") else None}
            for p in d.get("posts", [])]}
    if name == "get_reel":
        d = call("GET", "/api/publish")
        return next((p for p in d.get("posts", []) if p.get("pid") == a["pid"]), {"error": "no such reel"})
    if name == "update_reel":
        return call("PUT", f"/api/publish/{a['pid']}", {k: a[k] for k in ("caption", "video", "settings") if k in a})
    if name == "schedule_reel":
        at = datetime.fromisoformat(a["at"])
        if at.tzinfo is None:
            return {"error": "give `at` a zone (e.g. -05:00), so the slot can't be read in the wrong one"}
        return call("POST", f"/api/publish/{a['pid']}/schedule", {"at": at.timestamp()})
    if name in ("upload_reel", "cancel_schedule"):
        return call("POST", f"/api/publish/{a['pid']}/{'stage' if name == 'upload_reel' else 'unschedule'}")
    if name == "list_comments":
        return call("GET", "/api/comments/recent?" + urlencode(
            {"limit": int(a.get("limit") or 20)} | ({"unanswered": 1} if a.get("unanswered") else {})))
    if name == "get_metrics":
        return call("GET", "/api/metrics?" + urlencode({"reel": a.get("reel") or ""}))
    if name == "get_account_insights":
        return call("GET", f"/api/insights?days={int(a.get('days') or 7)}")
    if name == "post_public_reply":
        return call("POST", f"/api/comments/{quote(str(a['comment_id']), safe='')}/reply", {"text": a["text"]})
    return {"error": f"unknown tool {name}"}


def reply(rid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(rid, {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "reeldeck", "version": "0.2.0"}})
        elif method == "tools/list":
            reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            p = msg.get("params", {})
            try:
                out = run(p.get("name", ""), p.get("arguments") or {})
            except Exception as e:   # a bad argument is a tool error, not a dead server
                out = {"error": str(e)}
            reply(rid, {"content": [{"type": "text", "text": json.dumps(out, indent=1)}],
                        "isError": "error" in out})
        elif rid is not None:
            reply(rid, {})


def check():
    """A verb-based approval gate, as agent hosts commonly read a tool name: split on _ and -, a read verb means it runs on its own, a
    write verb means it waits for a yes, and A NAME WITH NEITHER RUNS ON ITS OWN. A
    write whose name holds one of `soft` lands in a class that can run without a yes
    (task comments do). So: reads carry a read verb, writes carry a write verb and
    nothing else, and nothing trips the denylist."""
    read = {"search", "query", "retrieve", "list", "get", "fetch", "read", "history", "find"}
    write = {"update", "upload", "schedule", "cancel", "post", "reply"}   # the ones used here
    soft = ("comment", "status", "task", "project", "note", "calendar", "notion", "draft", "email", "label")
    denied = {"delete", "purge", "drop", "wipe", "reset", "truncate", "destroy", "merge"}
    for t in TOOLS:
        name = "reeldeck_" + t["name"]   # as an agent sees it, behind the mcp server prefix
        seg = set(name.split("_"))
        assert not seg & denied, name
        if t["name"] in READS:
            assert seg & read, f"{name}: a gate would not know it for a read"
        else:
            assert seg & write and not seg & read, f"{name}: a gate would run it without asking"
            assert not any(w in name for w in soft), f"{name}: lands in a class that can run without a yes"
    assert {t["name"] for t in TOOLS} >= READS
    print("publish_mcp: ok")


if __name__ == "__main__":
    check() if "--check" in sys.argv else main()
