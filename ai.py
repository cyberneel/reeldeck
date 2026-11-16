#!/usr/bin/env python3
"""AI for the Script tab, behind one call: chat(messages, tier) -> text.

Where the words come from is the person's choice (Settings → AI):

  publik  Sign in with publik (publik.py's gate, drawn in the browser) and the
          install's pk_ key at publik's OpenAI-compatible /chat/completions,
          with tier models (publik-fast / -balanced / -smart). The default.
  own     their own OpenAI-compatible endpoint, key and model (OpenAI,
          OpenRouter, a local Ollama...). Kept in ~/.config/reeldeck/ai.json, 0600.
  claude  dev mode: the local `claude` CLI, so hacking on ReelDeck needs no key.

REELDECK_PUBLIK=off skips the publik gate entirely (development, or a self-host
that will never use publik). Keys stay in this process: the browser only ever
sees which provider is active, never a key.

    python3 ai.py      # self-check against a fake OpenAI-compatible server, no network
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import publik

CONFIG = Path.home() / ".config" / "reeldeck" / "ai.json"
PUBLIK_ON = os.environ.get("REELDECK_PUBLIK", "on").lower() not in ("off", "0", "false")
CLAUDE_MODELS = {"fast": "haiku", "balanced": "sonnet", "smart": "opus"}   # CLI aliases
TIERS = tuple(publik.MODELS)


class AIError(Exception):
    """A message for the person, and at most one link (publik's top-up page on a 402)."""
    def __init__(self, message: str, link: str | None = None):
        super().__init__(message)
        self.link = link


# ---------- settings ----------
def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def _write_cfg(c: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(c, f, indent=1)


def configure(body: dict) -> dict:
    """Settings → AI. provider: publik | own | claude. An empty api_key keeps the saved one."""
    c = _cfg()
    p = body.get("provider")
    if p not in ("publik", "own", "claude"):
        raise ValueError("provider is publik, own or claude")
    if p == "publik" and not PUBLIK_ON:
        raise ValueError("publik is switched off on this server (REELDECK_PUBLIK=off)")
    if p == "claude" and not shutil.which("claude"):
        raise ValueError("the claude CLI isn't installed on this server")
    if p == "own":
        base = str(body.get("base_url") or c.get("base_url") or "").strip().rstrip("/")
        if not re.match(r"^https?://", base):
            raise ValueError("base URL starts with http:// or https://")
        c.update(base_url=base, model=str(body.get("model") or c.get("model") or "").strip())
        if body.get("api_key"):
            c["api_key"] = str(body["api_key"]).strip()
        if not c["model"]:
            raise ValueError("name the model this endpoint should use")
    c["provider"] = p
    _write_cfg(c)
    if PUBLIK_ON and publik.read_credential():
        publik.set_choice("publik" if p == "publik" else "own_key")
    return state()


def _target():
    """(kind, base_url, key, model) for the next call."""
    c = _cfg()
    if c.get("provider") == "claude" and shutil.which("claude"):
        return "claude", None, None, None
    if PUBLIK_ON or os.environ.get("PUBLIK_API_KEY"):
        own = {"base_url": c.get("base_url"), "api_key": c.get("api_key")} if c.get("provider") == "own" else None
        kind, base, key = publik.resolve_provider(own)
        if kind == "publik":
            return kind, base, key, None
        if kind == "own":
            return kind, base, key, c.get("model")
    elif c.get("provider") == "own" and c.get("api_key"):
        return "own", c["base_url"], c["api_key"], c.get("model")
    if not PUBLIK_ON and shutil.which("claude") and c.get("provider") is None:
        return "claude", None, None, None   # dev mode default
    return None, None, None, None


# ---------- Sign in with publik, drawn by the browser ----------
class WebUI:
    """publik.ensure_signed_in() shows its screens through this object. The page
    polls GET /api/ai for `screen` and posts the button it pressed to press()."""

    def __init__(self):
        self.screen, self.url, self.pressed = None, None, queue.Queue()

    def show(self, screen, stop):
        self.screen = screen
        ids = {b for b, _ in screen["buttons"]}
        try:
            while not stop.is_set():
                try:
                    a = self.pressed.get(timeout=0.4)
                except queue.Empty:
                    continue
                if a in ids:
                    return a
            return None
        finally:
            self.screen = None

    def open_url(self, url):
        self.url = url   # the page opens it: the server may be in a container, or another machine

    def press(self, action: str) -> str | None:
        """A button from the page; answers the link to open, if pressing it opened one."""
        before, self.url = self.screen, None
        self.pressed.put(action)
        for _ in range(30):   # until the gate opens a link or draws its next screen, at most 3 s
            if self.url or self.screen not in (before, None):
                break
            threading.Event().wait(0.1)
        return self.url


UI = WebUI()
GATE = {"done": not PUBLIK_ON, "running": False, "wallet": None}


def start_gate() -> None:
    """Run the publik gate in the background; the page shows its screens until it returns."""
    if not PUBLIK_ON or GATE["running"]:
        return
    GATE.update(done=False, running=True)

    def run():
        try:
            r = publik.ensure_signed_in(UI)
            GATE.update(done=True, wallet=r.get("wallet"))
        finally:
            GATE["running"] = False
    threading.Thread(target=run, daemon=True, name="publik-gate").start()


def ask_again() -> dict:
    """Settings → "Choose again": the gate's plan screen comes back."""
    if not PUBLIK_ON:
        raise ValueError("publik is switched off on this server")
    publik.set_choice(None)
    start_gate()
    return state()


def state(wallet: bool = False) -> dict:
    c = _cfg()
    kind = _target()[0]
    out = {
        "publik": PUBLIK_ON, "ready": GATE["done"], "screen": UI.screen,
        "provider": kind, "chosen": c.get("provider"),
        "claude": bool(shutil.which("claude")),
        "own": {"base_url": c.get("base_url", ""), "model": c.get("model", ""), "has_key": bool(c.get("api_key"))},
    }
    w = GATE["wallet"]
    if wallet and PUBLIK_ON and GATE["done"]:
        try:
            cred = publik.read_credential() or {}
            w = publik.read_wallet(os.environ.get("PUBLIK_API_KEY") or cred.get("key"))[1] or w
            GATE["wallet"] = w
        except (OSError, TypeError):
            pass
    if w:
        plan = w.get("plan") or {}
        out["account"] = {
            "handle": (w.get("account") or {}).get("handle"), "plan": plan.get("label"),
            # a share of what's left, never an amount of money
            "remaining_percent": w.get("remaining_percent"),
            "prepaid_remaining_percent": w.get("prepaid_remaining_percent"),
            "top_up_url": w.get("top_up_url"),
        }
    return out


# ---------- the call ----------
def chat(messages: list[dict], tier: str = "balanced", max_tokens: int = 1400) -> str:
    kind, base, key, model = _target()
    if kind is None:
        raise AIError("Choose where AI comes from first: Settings → AI.")
    if kind == "claude":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages if m["role"] != "system")
        try:
            r = subprocess.run(["claude", "-p", "--model", CLAUDE_MODELS.get(tier, "sonnet"), "--output-format", "text",
                                "--system-prompt", system, "--tools", "", "--no-session-persistence",
                                "--strict-mcp-config"],
                               input=convo, capture_output=True, text=True, timeout=240, cwd=tempfile.gettempdir())
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AIError(f"the claude CLI didn't answer: {e}") from None
        if r.returncode:
            raise AIError((r.stderr or r.stdout).strip()[-300:] or "the claude CLI failed")
        return r.stdout.strip()
    body = {"model": publik.MODELS.get(tier, "publik-balanced") if kind == "publik" else model,
            "messages": messages, "max_tokens": max_tokens}
    try:
        status, b = publik._http("POST", f"{base.rstrip('/')}/chat/completions", body, key=key, timeout=120)
    except OSError as e:
        raise AIError("publik is not reachable. Check your connection." if kind == "publik"
                      else f"couldn't reach {base}: {e}") from None
    if status == 402:
        raise AIError(*publik.top_up_url_from_402(b))
    if status != 200:
        raise AIError((b.get("error") or {}).get("message") or f"the AI provider answered {status}")
    try:
        return b["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise AIError("the AI provider sent an answer with no text") from None


# ---------- the Script tab's assistant ----------
SYSTEM = """You help write voice-over scripts for short vertical videos (Reels, TikTok, Shorts).
Write for the ear: plain spoken words, one idea per line, each line short enough to say in one breath.
The first line is the hook: it has to earn the next three seconds. No emojis, no hashtags,
no stage directions, no camera notes. Answer with JSON only, exactly the shape asked for."""

WPS = 2.6   # spoken words per second at a brisk reel pace; the tab uses the same number

ASKS = {
    "ideate": ('Draft a script for this brief. Aim for about {target}s spoken (~{words} words). '
               'Split it into 3-6 beats (hook first, a payoff or call to action last).\n'
               'Answer {{"title": str, "beats": [{{"name": str, "lines": [str]}}]}}.'),
    "hooks": ('Write 6 different opening hooks for this script: curiosity, bold claim, problem, '
              'number, contrarian, story. Each under 14 words.\nAnswer {{"options": [str]}}.'),
    "rewrite": ('Rewrite the TARGET text below: {how}. Keep its meaning and its place in the script. '
                'Give 3 options, each a list of lines.\nAnswer {{"options": [[str]]}}.'),
    "next": ('Suggest the next 2-4 lines for the beat "{beat}", continuing from where it stops.\n'
             'Answer {{"lines": [str]}}.'),
    "outline": ('Suggest beats this script is missing, or a better order for the ones it has.\n'
                'Answer {{"beats": [{{"name": str, "why": str}}]}}.'),
}
HOW = {"punchier": "punchier and more concrete", "shorter": "shorter, cut every word that isn't pulling weight",
       "clearer": "clearer for someone hearing it once", "casual": "more casual, like talking to a friend"}


def _render(script: dict) -> str:
    out = [f"TITLE: {script.get('title') or '(untitled)'}"]
    if script.get("brief"):
        out.append(f"BRIEF: {script['brief']}")
    for b in script.get("beats") or []:
        out.append(f"\n## {b.get('name') or 'beat'}")
        out += [f"- {l.get('text', '')}" for l in b.get("lines") or [] if l.get("text", "").strip()]
    return "\n".join(out)


def parse_json(text: str):
    """The first JSON object in a reply: models wrap it in prose or ``` fences now and then."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    start = text.find("{")
    while start != -1:
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            start = text.find("{", start + 1)
    raise AIError("the AI answered in a shape the Script tab can't read; try again")


def assist(body: dict) -> dict:
    mode = body.get("mode")
    if mode not in ASKS:
        raise ValueError(f"mode is one of {', '.join(ASKS)}")
    script = body.get("script") or {}
    target = int(body.get("target") or script.get("target") or 30)
    ask = ASKS[mode].format(target=target, words=round(target * WPS), beat=body.get("beat", ""),
                            how=HOW.get(body.get("how"), body.get("how") or HOW["punchier"]))
    if body.get("instruction"):
        ask += f"\nAlso: {body['instruction']}"
    parts = [_render(script)]
    if body.get("text"):
        parts.append(f"TARGET:\n{body['text']}")
    if mode == "ideate" and body.get("brief"):
        parts.append(f"BRIEF: {body['brief']}")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n\n".join(parts) + "\n\n" + ask}]
    tier = body.get("tier") if body.get("tier") in TIERS else ("fast" if mode == "hooks" else "balanced")
    return parse_json(chat(msgs, tier))


# ---------- self-check ----------
def _check() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((self.headers.get("authorization"), b["model"]))
            if "broke" in b["messages"][-1]["content"]:
                code, out = 402, {"error": {"message": "Out of publik use.", "top_up_url": "https://x/top"}}
            else:
                code, out = 200, {"choices": [{"message": {"content": 'Sure!\n```json\n{"options": ["a", "b"]}\n```'}}]}
            raw = json.dumps(out).encode()
            self.send_response(code); self.send_header("content-length", str(len(raw))); self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    global CONFIG, PUBLIK_ON
    with tempfile.TemporaryDirectory() as d:
        CONFIG, PUBLIK_ON = Path(d) / "ai.json", False
        try:
            configure({"provider": "own", "base_url": "ftp://x", "model": "m"})
            raise AssertionError("a non-http base URL was accepted")
        except ValueError:
            pass
        configure({"provider": "own", "base_url": f"http://127.0.0.1:{srv.server_port}/v1/", "model": "m1", "api_key": "sk-test"})
        assert oct(CONFIG.stat().st_mode & 0o777) == "0o600"
        assert "sk-test" not in json.dumps(state()), "a key reached the page"
        assert assist({"mode": "hooks", "script": {"title": "t", "beats": [{"name": "hook", "lines": [{"text": "hi"}]}]}}) == {"options": ["a", "b"]}
        assert seen[-1] == ("Bearer sk-test", "m1"), seen
        configure({"provider": "own", "model": "m2"})   # no key: the saved one stays
        try:
            chat([{"role": "user", "content": "broke"}])
            raise AssertionError("a 402 passed")
        except AIError as e:
            assert (str(e), e.link) == ("Out of publik use.", "https://x/top")
        assert seen[-1] == ("Bearer sk-test", "m2")
    assert parse_json('x {"a": [1, {"b": 2}]} y') == {"a": [1, {"b": 2}]}
    srv.shutdown()
    print("ai: ok")


if __name__ == "__main__":
    _check()
