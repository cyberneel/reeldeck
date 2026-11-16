# Vendored from https://publikhq.com/sdk/publik.py.md (Sign in with publik + usage counting).
# Only APP_SLUG, APP_VERSION and ORIGIN differ from upstream; ai.py drives it, so a
# newer upstream copy can replace this file as is.
"""publik.py: Sign in with publik, usage counting, and publik API as one provider option.
At app start, before any screen of your own: gate = ensure_signed_in(ui). It
  1. loads or provisions this computer's publik key (POST /api/v1/installs);
  2. shows Sign in with publik until GET /api/v1/wallet says "claimed" (no other way past it);
  3. when USES_AI: asks once how the app pays for AI and stores the answer (set_choice() changes it);
  4. starts usage counting: app_opened now, active_session every 30 minutes (POST /api/v1/telemetry)."""
import atexit, json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

APP_SLUG = "reeldeck"
APP_VERSION = (Path(__file__).with_name("VERSION").read_text().strip()
               if Path(__file__).with_name("VERSION").is_file() else "0.0.0")
USES_AI = True  # False: the app calls no model. It still signs in and counts users; it skips the plan screen.
ORIGIN = os.environ.get("PUBLIK_ORIGIN") or "https://publikhq.com"  # PUBLIK_ORIGIN: tests only
DEFAULT_BASE_URL = f"{ORIGIN}/api/v1"
MODELS = {"fast": "publik-fast", "balanced": "publik-balanced", "smart": "publik-smart"}  # a tier, never a vendor model name
POLL_SECONDS, POLL_LIMIT_SECONDS = 3, 600
OS = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else None)


def _arch():
    """"arm64" or "x64", as publik names them; None for any other chip."""
    return {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())


def _app_token() -> str:
    env = os.environ.get("PUBLIK_APP_TOKEN", "").strip()
    if env:
        return env
    for d in Path(__file__).resolve().parents:
        if (d / "publik-app-token.txt").exists():
            return (d / "publik-app-token.txt").read_text().strip()
    return ""


def _publik_dir() -> Path:
    """~/Library/Application Support/publik, %LOCALAPPDATA%\\publik, ~/.config/publik: shared by every publik app."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "publik"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "publik"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "publik"


def credential_path() -> Path:
    return _publik_dir() / "apps" / f"{APP_SLUG}.json"


def read_credential():
    try:
        c = json.loads(credential_path().read_text())
        return c if str(c.get("key", "")).startswith("pk_") else None
    except (OSError, ValueError, AttributeError):
        return None


def _write_credential(c) -> None:
    p = credential_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(c or {}, f, indent=2)
    os.chmod(p, 0o600)


def set_choice(choice) -> None:
    """The person's answer to the plan screen: "publik", "own_key", or None to ask again. Settings call it."""
    c = read_credential()
    if c:
        _write_credential({**c, "choice": choice})


def resolve_provider(user_key=None):
    """("publik", base_url, key), ("own", base_url, key) or ("needs_own_key", None, None): the stored choice decides."""
    if os.environ.get("PUBLIK_API_KEY"):
        return ("publik", os.environ.get("PUBLIK_API_BASE_URL", DEFAULT_BASE_URL), os.environ["PUBLIK_API_KEY"])
    c = read_credential() or {}
    if c.get("choice") == "publik":
        return ("publik", c.get("base_url") or DEFAULT_BASE_URL, c["key"])
    if user_key and user_key.get("api_key"):
        return ("own", user_key["base_url"], user_key["api_key"])
    return ("needs_own_key", None, None)


def _http(method: str, url: str, body=None, key=None, timeout=20):
    """(status, json). Raises OSError when publik is not reachable."""
    headers = {"content-type": "application/json", **({"authorization": f"Bearer {key}"} if key else {})}
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def provision() -> dict:
    """POST /api/v1/installs: this computer's publik key. Raises RuntimeError with a readable message."""
    token = _app_token()
    if not token.startswith("pat_"):
        raise RuntimeError("This build has no publik app token. Ask the developer for a new build.")
    prior = read_credential() or {}
    install_id = prior.get("install_id") or str(uuid.uuid4())
    for _ in range(2):
        status, body = _http("POST", f"{ORIGIN}/api/v1/installs", {
            "app_token": token, "app_slug": APP_SLUG, "app_version": APP_VERSION, "os": OS or "linux",
            "arch": _arch(), "device_name": socket.gethostname()[:120],
            "install_id": install_id, "disclosure_version": 1, "dialects": ["chat_completions"],
        })
        key = body.get("key") or (prior.get("key") if status == 200 else None)
        if status in (200, 201) and key:
            c = {"key": key, "base_url": body.get("base_url", DEFAULT_BASE_URL), "install_id": body.get("install_id", install_id),
                 "claim_url": body.get("claim_url"), "choice": prior.get("choice")}
            _write_credential(c)  # the key reaches disk before anything else sees it
            return c
        if status == 200 or (body.get("error") or {}).get("type") == "install_revoked":
            install_id, prior = str(uuid.uuid4()), {}
            continue
        raise RuntimeError((body.get("error") or {}).get("message") or f"publik could not set up this computer ({status}).")
    raise RuntimeError("publik could not set up this computer.")


def read_wallet(key: str):
    """GET /api/v1/wallet: (status, wallet or None, message). Raises OSError when publik is not reachable."""
    status, b = _http("GET", f"{ORIGIN}/api/v1/wallet", key=key, timeout=15)
    if status != 200 or not isinstance(b.get("claim_state"), str):
        return status, None, (b.get("error") or {}).get("message")
    return status, {**b, "account": b.get("account"), "plan": b.get("plan") or {"kind": "none", "label": None}, "balance_micros": b.get("balance_micros") or 0}, None


def gate_screen(screen_id: str, w=None, note=None, message=None) -> dict:
    """The words of the gate. The same in every publik module."""
    if screen_id == "unreachable":
        return {"id": screen_id, "title": "Sign in with publik", "body": message or "publik is not reachable. Check your connection.", "note": note, "buttons": [("retry", "Retry")]}
    if screen_id == "sign_in":
        return {"id": screen_id, "title": "Sign in with publik", "note": note, "buttons": [("sign_in", "Sign in with publik")],
                "body": "This app uses your publik account. Click Sign in with publik, then sign in in your browser. This window continues when you are signed in."}
    handle = ((w or {}).get("account") or {}).get("handle")
    who = f"You are signed in as @{handle}." if handle else "You are signed in to publik."
    plan = (w or {}).get("plan") or {"kind": "none"}
    if plan["kind"] == "none" and not (w or {}).get("balance_micros"):
        return {"id": screen_id, "title": "Choose how to pay for AI", "note": note, "body": f"{who} This app uses AI. Get a publik plan, or use your own key.",
                "buttons": [("get_plan", "Get a plan"), ("own_key", "Use my own key")]}
    what = f"Your plan: {plan.get('label') or 'publik'}." if plan["kind"] == "plan" else "You have publik use ready."
    return {"id": screen_id, "title": "Connect your plan", "note": note, "body": f"{who} {what} Connect it to use AI in this app, or use your own key.",
            "buttons": [("connect", "Connect your plan"), ("own_key", "Use my own key")]}


def _ask(ui, screen, key=None, until=None):
    """Show a screen; with `until`, poll GET /wallet every 3 s for at most 10 minutes. "done": the poll saw it; "timeout": it did not."""
    stop, polled = threading.Event(), []

    def poll():
        end = time.monotonic() + POLL_LIMIT_SECONDS
        while time.monotonic() < end and not stop.wait(POLL_SECONDS):
            try:
                w = read_wallet(key)[1]
            except OSError:
                w = None
            if w and until(w):
                polled.append("done")
                break
        else:
            polled.append("timeout")
        stop.set()

    if until:
        threading.Thread(target=poll, daemon=True).start()
    action = ui.show(screen, stop)  # a button id, or None once `stop` is set
    stop.set()
    return polled[0] if action is None and polled else action


def ensure_signed_in(ui=None) -> dict:
    """The gate (see the top of the file). Returns {"key", "wallet", "choice"} once the person is signed in; choice is None when USES_AI is False."""
    if os.environ.get("PUBLIK_SELF_TEST"):
        print(self_test())
        sys.exit(0)
    ui = ui or TerminalUI()
    sign_note, sign_poll, plan_note, plan_poll = None, True, None, False
    while True:
        try:
            env_key = os.environ.get("PUBLIK_API_KEY")
            c = {"key": env_key, "claim_url": None} if env_key else read_credential() or provision()
            status, w, message = read_wallet(c["key"])
        except (OSError, RuntimeError) as e:
            _ask(ui, gate_screen("unreachable", message=str(e) if isinstance(e, RuntimeError) else None))
            continue
        if status == 401 and not os.environ.get("PUBLIK_API_KEY"):
            _write_credential(None)  # a revoked key: provision again
            continue
        if not w:
            _ask(ui, gate_screen("unreachable", message=message))
            continue
        if w["claim_state"] != "claimed":
            a = _ask(ui, gate_screen("sign_in", w, sign_note), c["key"], (lambda x: x["claim_state"] == "claimed") if sign_poll else None)
            if a == "sign_in":
                ui.open_url(w.get("claim_url") or c.get("claim_url") or f"{ORIGIN}/wallet")
                sign_note, sign_poll = "Waiting for you to sign in in your browser.", True
            if a == "timeout":
                sign_note, sign_poll = "No sign-in after 10 minutes. Click Sign in with publik to try again.", False
            continue
        choice = (read_credential() or {}).get("choice") if USES_AI else None
        if USES_AI and not choice:
            if w["plan"]["kind"] != "none" or w["balance_micros"] > 0:
                plan_note, plan_poll = None, False  # the plan is there: Connect your plan
            a = _ask(ui, gate_screen("choose", w, plan_note), c["key"], (lambda x: x["plan"]["kind"] != "none" or x["balance_micros"] > 0) if plan_poll else None)
            plan_note, plan_poll = None, False
            if a == "get_plan":
                ui.open_url(w["top_up_url"])
                plan_note, plan_poll = "Waiting for your plan. Finish in your browser.", True
            if a == "timeout":
                plan_note = "No plan after 10 minutes. Click Get a plan to try again."
            if a not in ("connect", "own_key"):
                continue  # "done": the plan is there; the next screen is Connect your plan
            choice = "publik" if a == "connect" else "own_key"
            set_choice(choice)
        start_telemetry()
        return {"key": c["key"], "wallet": w, "choice": choice}


def self_test() -> str:
    """Builder check, run by the BUILT app: PUBLIK_SELF_TEST=1 makes ensure_signed_in() print this line and exit."""
    try:
        status, w, message = read_wallet(provision()["key"])
        if not w:
            raise RuntimeError(message or f"GET /wallet answered {status}")
        return f"publik self-test: ok {APP_SLUG} {APP_VERSION}"
    except OSError:
        return "publik self-test: failed: publik is not reachable. Check your connection."
    except Exception as e:  # a readable RuntimeError
        return f"publik self-test: failed: {e}"


class TerminalUI:
    """A terminal UI for a command-line app. A desktop app passes its own object with show() and open_url() (see the page)."""

    def show(self, screen, stop):
        print(f"\n{screen['title']}\n{screen['body']}" + (f"\n{screen['note']}" if screen["note"] else ""))
        for i, (_, label) in enumerate(screen["buttons"], 1):
            print(f"  {i}. {label}")
        answered = []
        threading.Thread(target=lambda: stop.wait() and not answered and print("\nDone. Press Enter to continue."), daemon=True).start()
        while True:
            try:
                line = input("Type a number and press Enter: " if len(screen["buttons"]) > 1 else "Press Enter: ").strip() or "1"
            except EOFError:
                stop.wait()
                return None
            if stop.is_set():
                return None
            if line.isdigit() and 1 <= int(line) <= len(screen["buttons"]):
                answered.append(line)
                return screen["buttons"][int(line) - 1][0]

    def open_url(self, url):
        print(f"Open this link if your browser does not open: {url}")
        webbrowser.open(url)


# ---------- usage counting: /skills/telemetry.md sections 3-5 ----------
_telemetry = {"started": False, "install_id": None}


def _usage_off() -> bool:
    dnt = os.environ.get("DO_NOT_TRACK", "").strip().lower()
    if dnt not in ("", "0", "false") or os.environ.get("PUBLIK_TELEMETRY", "").strip().lower() in ("0", "false", "off"):
        return True
    try:
        return json.loads((_publik_dir() / "consent.json").read_text()).get("usage") is False
    except (OSError, ValueError, AttributeError):
        return False


def _telemetry_install_id() -> str:
    """One random UUID per computer account, shared by every publik app, in <publik dir>/telemetry.json (mode 0600)."""
    f = _publik_dir() / "telemetry.json"

    def saved():
        try:
            i = str(json.loads(f.read_text()).get("install_id", "")).lower()
            return i if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", i) else None
        except (OSError, ValueError, AttributeError):
            return None

    if not _telemetry["install_id"]:
        _telemetry["install_id"] = saved()
    if not _telemetry["install_id"]:
        fresh = str(uuid.uuid4())  # if the file cannot be saved, this ID lasts for this run only
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
                out.write(json.dumps({"install_id": fresh, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")
            _telemetry["install_id"] = fresh
        except OSError:
            _telemetry["install_id"] = saved() or fresh  # another publik app saved its ID first
    return _telemetry["install_id"]


def _send_event(event: str) -> None:
    def say(result):
        if os.environ.get("PUBLIK_TELEMETRY_DEBUG") == "1":
            print(f"publik-telemetry: {event} -> {result}", file=sys.stderr)
    try:
        if not OS:
            return
        if _usage_off():
            return say("off (opt-out)")
        key = os.environ.get("PUBLIK_API_KEY") or (read_credential() or {}).get("key")
        body = {"v": 1, "app": APP_SLUG, "install_id": _telemetry_install_id(), "event": event, "app_version": APP_VERSION.lstrip("vV"), "os": OS}
        if _arch():
            body["arch"] = _arch()
        url = os.environ.get("PUBLIK_TELEMETRY_URL") or f"{ORIGIN}/api/v1/telemetry"
        say(_http("POST", url, body, key=key if str(key).startswith("pk_") else None, timeout=5)[0])
    except Exception as e:  # never throws
        say(type(e).__name__)


def start_telemetry() -> None:
    """Called by ensure_signed_in(). app_opened now, active_session every 30 minutes; never fails the app, never blocks."""
    if _telemetry["started"]:
        return
    _telemetry["started"] = True
    try:
        seconds = float(os.environ.get("PUBLIK_TELEMETRY_INTERVAL_SECONDS", ""))  # tests only
    except ValueError:
        seconds = 0
    interval = seconds if 1 <= seconds <= 86400 else 1800

    first = threading.Thread(target=_send_event, args=("app_opened",), daemon=True)
    first.start()
    atexit.register(first.join, 2)  # a CLI that exits at once still reports app_opened; waits at most 2 s, only if the send is still running

    def loop():
        while True:
            time.sleep(interval)
            _send_event("active_session")

    threading.Thread(target=loop, daemon=True).start()


# ---------- publik API calls ----------
def client(provider):
    """OpenAI-compatible client: pip install openai."""
    from openai import OpenAI
    return OpenAI(base_url=provider[1], api_key=provider[2])


def top_up_url_from_402(body) -> tuple:
    """A 402 renders its message plus exactly ONE link: this one."""
    e = (body or {}).get("error") or {}
    return e.get("message", "Not enough publik API balance."), e.get("top_up_url")


def search(provider, q: str, count: int = 10):
    """POST /search: web results for q (quote a phrase to find it exactly), one published price per search; (results, message, top_up_url)."""
    status, body = _http("POST", f"{provider[1]}/search", {"q": q, "count": count}, key=provider[2], timeout=30)
    if status == 402:
        return ([], *top_up_url_from_402(body))
    return (body.get("results", []), None, None) if status == 200 else ([], f"publik API answered {status}", None)
