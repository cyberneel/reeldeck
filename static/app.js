// The shell: project switcher, tab router, toasts, job indicator, theme, the
// sign-in gate and AI settings, and the whole-page states (welcome, can't open).
// Each tab module owns its own DOM subtree and re-renders on store change; this
// file just wires them together and boots.
import { api } from "./api.js";
import { S, subscribe, emit, onToast, openProject, toast, update } from "./store.js";
import * as timeline from "./timeline.js";
import * as script from "./script.js";
import * as voice from "./voice.js";
import * as clips from "./clips.js";
import * as agent from "./agent.js";
import * as publish from "./publish.js";
import * as comments from "./comments.js";

const TABS = [
  { id: "timeline", label: "Timeline", mod: timeline },
  { id: "script", label: "Script", mod: script },
  { id: "voice", label: "Voice", mod: voice },
  { id: "clips", label: "Clips", mod: clips },
  { id: "agent", label: "Agent", mod: agent },
  { id: "publish", label: "Publish", mod: publish },
  { id: "comments", label: "Comments", mod: comments },
];
let active = "timeline";
let mounted = new Set();

function $(sel) { return document.querySelector(sel); }
const MARK = `<img src="/static/icon.svg" alt="">`;

function buildShell() {
  document.body.innerHTML = `
    <div id="topbar">
      <h1>${MARK}<span>Reel<b>Deck</b></span></h1>
      <select id="projSwitch" aria-label="Reel"></select>
      <button id="browseBtn" class="icon" title="Browse all reels">▦</button>
      <button id="newProjBtn" class="icon" title="New or existing reel">+</button>
      <span class="grow"></span>
      <div id="jobIndicator"><span class="dot"></span><span id="jobText"></span></div>
      <button id="aiBtn" class="ghost" title="Where the Script and Agent tabs' AI comes from"><i></i><span>AI</span></button>
      <button id="themeBtn" class="ghost icon"></button>
      <span class="topsep"></span>
      <button id="dropBtn" class="icon" title="Send to a device (Taildrop)" hidden>⇪</button>
      <button id="linesBtn" class="icon" title="Re-record line by line (phone page)">☰</button>
      <button id="topUpdate" class="go" title="Render whatever your edits made stale (⌘/Ctrl+↵)">Update</button>
    </div>
    <div id="tabs">
      ${TABS.map((t) => `<button data-tab="${t.id}">${t.label}</button>`).join("")}
    </div>
    <main>
      ${TABS.map((t) => `<div class="tabpane" id="pane-${t.id}"></div>`).join("")}
    </main>
    <div id="toasts"></div>
    <div id="modalRoot"><div class="modal" id="modalBody"></div></div>`;

  $("#newProjBtn").onclick = openProjectModal;
  $("#browseBtn").onclick = () => openBrowser();
  $("#dropBtn").onclick = openDropModal;
  $("#aiBtn").onclick = openAISettings;
  $("#themeBtn").onclick = () => { window.reeldeckTheme.cycle(); drawTheme(); };
  $("#linesBtn").onclick = () => (location.href = `/master?p=${S.pid || ""}`);
  $("#topUpdate").onclick = () => update();
  $("#projSwitch").addEventListener("change", (e) => {
    if (e.target.value) switchProject(e.target.value);
  });
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });
  $("#modalRoot").addEventListener("click", (e) => {
    if (e.target.id === "modalRoot") closeModal();
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
    // works from any tab, including with a script field focused — the whole
    // point is not having to go find the right button after an edit
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && S.pid) { e.preventDefault(); update(); }
  });
  drawTheme();
}

function drawTheme() {
  const t = window.reeldeckTheme.get();
  const b = $("#themeBtn");
  b.textContent = { auto: "◐", light: "☀", dark: "☾" }[t];
  b.title = `Theme: ${t === "auto" ? "follows your system" : t} (click to change)`;
}

function switchTab(id) {
  active = id;
  history.replaceState(null, "", `${location.search}#${id}`);   // each tab is a bookmark: /#comments
  document.querySelectorAll("#tabs button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === id));
  document.querySelectorAll(".tabpane").forEach((p) =>
    p.classList.toggle("active", p.id === `pane-${id}`));
  const tab = TABS.find((t) => t.id === id);
  if (tab && !mounted.has(id)) {
    tab.mod.mount($(`#pane-${id}`));
    mounted.add(id);
  }
  tab?.mod.shown?.();   // a tab that isn't per-project (Publish) refreshes on view
}
window.reeldeckSwitchTab = switchTab; // clips.js and the Script tab's "Send to Voice" use this

async function switchProject(pid) {
  history.replaceState(null, "", `?p=${pid}${location.hash}`);
  if ($("#welcome")) {   // the first reel: the tabs come in now
    $("#welcome").remove();
    document.body.classList.remove("noproj");
    switchTab(TABS.find((t) => "#" + t.id === location.hash)?.id || "script");
  }
  $("#projSwitch").value = pid;   // boot lists projects before one is open
  await openProject(pid);
}

async function refreshProjectList(selectPid) {
  const d = await api.listProjects();
  const sel = $("#projSwitch");
  sel.innerHTML = byFolder(d.projects || []).map(([f, rows]) =>
    `<optgroup label="${esc(f || "unfiled")}">${rows.map((p) =>
      `<option value="${p.id}"${p.missing ? " disabled" : ""}${p.id === (selectPid || S.pid) ? " selected" : ""}>
        ${esc(p.name)}${p.missing ? " (missing)" : ""}</option>`).join("")}</optgroup>`).join("");
  return d.projects || [];
}

function jobIndicator() {
  const ind = $("#jobIndicator");
  ind.classList.toggle("running", !!S.job.running);
  $("#jobText").textContent = S.job.running ? S.job.step || "working…" : "";

  const n = (S.stale || []).length;
  const b = $("#topUpdate");
  b.disabled = S.job.running || !n;
  b.textContent = S.job.running ? "Working…" : n ? `Update (${n})` : "Up to date";
  b.classList.toggle("pending", !!n && !S.job.running);
}

// ---------- whole-page states ----------
function pageState(id, html) {
  $(`#${id}`)?.remove();
  $("main").insertAdjacentHTML("beforeend", `<div class="state" id="${id}"><div class="card">${html}</div></div>`);
  return $(`#${id}`);
}

// opening a reel shows a bar; a reel that can't be read gets a page, with Retry
function drawLoad() {
  $("main").classList.toggle("loading", !!S.pid && !S.loaded && !S.loadError);
  if (!S.loadError) { $("#loadErr")?.remove(); return; }
  if ($("#loadErr")) return;
  const el = pageState("loadErr", `
    <h3>This reel didn't open</h3>
    <p>${esc(S.loadError)}</p>
    <div class="acts"><button class="go" data-a="retry">Retry</button><button data-a="browse">Browse reels</button></div>`);
  el.querySelector('[data-a="retry"]').onclick = () => openProject(S.pid);
  el.querySelector('[data-a="browse"]').onclick = () => openBrowser();
}

function showWelcome() {
  document.body.classList.add("noproj");
  const el = pageState("welcome", `
    <img class="mark" src="/static/icon.svg" alt="">
    <h3>Make your first reel</h3>
    <p>ReelDeck takes a short vertical video from first draft to posted.</p>
    <ol class="steps">
      <li><div><b>Write</b>Beats and lines in the Script tab, with AI for drafts, hooks and rewrites.</div></li>
      <li><div><b>Voice</b>Send the script to Voice and render it with TTS, or record each line yourself.</div></li>
      <li><div><b>Cut</b>Put clips on the Timeline. Picture, voice and sound stay in sync as you trim.</div></li>
      <li><div><b>Publish</b>Post to Instagram as a reel or a trial reel, then answer comments.</div></li>
    </ol>
    <div class="acts"><button class="go" data-a="new">New reel</button><button data-a="open">Open a folder</button></div>`);
  el.querySelector('[data-a="new"]').onclick = () => openProjectModal();
  el.querySelector('[data-a="open"]').onclick = () => { openProjectModal(); $("#mDir").focus(); };
}

// ---------- AI: Sign in with publik, and where the words come from ----------
// The gate runs in the server (publik.py's ensure_signed_in); this page draws
// whatever screen it is on and posts the button pressed back.
let wantOwnKey = false, lastUrl = "", urlFor = "";   // the link a press opened, and the screen it was for

async function refreshApp(wallet) {
  const d = await api.app(wallet);
  if (d.error) return d;
  S.app = d;
  drawAI();
  emit();   // the Script tab shows whether AI is set up
  return d;
}

function drawAI() {
  const ai = S.app?.ai || {};
  const b = $("#aiBtn");
  const name = { publik: "publik", own: "own key", claude: "Claude (dev)" }[ai.provider];
  b.classList.toggle("on", !!name);
  b.classList.toggle("off", !name);
  b.querySelector("span").textContent = name ? `AI · ${name}` : "Set up AI";
  $("#dropBtn").hidden = !S.app?.taildrop;
}

const OPENS = new Set(["sign_in", "get_plan"]);   // the buttons that open publik in the browser

function drawGate() {
  const ai = S.app?.ai;
  let g = $("#gate");
  if (!ai?.publik || ai.ready) { g?.remove(); return false; }
  if (!g) {
    g = document.createElement("div");
    g.id = "gate"; g.className = "state";
    document.body.appendChild(g);
  }
  const sc = ai.screen;
  if (sc && sc.title !== urlFor) lastUrl = "";   // a new screen: the old link is spent
  const key = JSON.stringify([sc, lastUrl]);
  if (g.dataset.key === key) return true;   // same screen: leave hover and focus alone
  g.dataset.key = key;
  g.innerHTML = `<div class="card">
    <img class="mark" src="/static/icon.svg" alt="">
    ${sc ? `<h3>${esc(sc.title)}</h3><p>${esc(sc.body)}</p>
      ${sc.note ? `<div class="note">${esc(sc.note)}</div>` : ""}
      <div class="acts">${sc.buttons.map(([id, label], i) =>
        `<button data-a="${id}"${i ? "" : ` class="go"`}>${esc(label)}</button>`).join("")}</div>
      ${lastUrl ? `<p class="hint">Nothing opened? <a href="${esc(lastUrl)}" target="_blank" rel="noopener">Open publik in a new tab</a>.</p>` : ""}`
    : `<h3>Sign in with publik</h3><p><span class="spinner"></span>&nbsp; Connecting to publik…</p>`}
    <div class="byline">ReelDeck uses publik for sign-in and AI. Your reels and footage stay on this computer.</div>
  </div>`;
  g.querySelectorAll("[data-a]").forEach((b) => { b.onclick = () => pressGate(b); });
  return true;
}

async function pressGate(b) {
  const a = b.dataset.a;
  // a window opened inside the click gets past popup blockers; its address comes after
  const win = OPENS.has(a) ? window.open("", "_blank") : null;
  if (win) win.opener = null;
  b.classList.add("busy");
  const r = await api.aiPress(a);
  b.classList.remove("busy");
  if (r.url) {
    lastUrl = r.url; urlFor = S.app?.ai?.screen?.title || "";
    if (win) win.location.href = r.url;
  } else win?.close();
  if (a === "own_key") wantOwnKey = true;
  await refreshApp();
  drawGate();
}

// resolves once the gate is through: signed in, and a way to pay for AI chosen
function waitGate() {
  return new Promise((done) => {
    const tick = async () => {
      await refreshApp();
      if (drawGate()) setTimeout(tick, 1500);
      else {
        lastUrl = "";
        done();
        if (wantOwnKey && !S.app.ai.provider) { wantOwnKey = false; openAISettings(); }
      }
    };
    tick();
  });
}
window.reeldeckAISettings = () => openAISettings();   // the Script tab's "Set up AI"

async function openAISettings() {
  $("#modalBody").innerHTML = `<h3>AI</h3><div class="hint"><span class="spinner"></span></div>`;
  $("#modalRoot").classList.add("open");
  const d = await refreshApp(true);
  if (d.error) { $("#modalBody .hint").textContent = d.error; return; }
  const ai = S.app.ai, own = ai.own, acct = ai.account;
  const cur = ai.provider || ai.chosen || (ai.publik ? "publik" : "own");
  const opt = (v, title, sub) => `<label class="opt"><input type="radio" name="aiP" value="${v}"${v === cur ? " checked" : ""}>
    <b>${title}</b><span>${sub}</span></label>`;
  const left = acct?.remaining_percent ?? acct?.prepaid_remaining_percent;
  $("#modalBody").innerHTML = `
    <h3>AI <span class="sub">for the Script tab</span></h3>
    <div class="hint">Drafts, hooks and rewrites come from here. Nothing else in ReelDeck sends anything to an AI.</div>
    <div class="opts">
      ${ai.publik ? opt("publik", "publik", "Your publik plan. No keys to manage.") : ""}
      ${opt("own", "Your own key", "Any OpenAI-compatible API: OpenAI, OpenRouter, a local Ollama…")}
      ${ai.claude ? opt("claude", "Claude Code", "The claude CLI on this computer. For development.") : ""}
    </div>
    <div id="ownFields"${cur === "own" ? "" : " hidden"}>
      <div class="fld"><label>Base URL</label><input id="oBase" value="${esc(own.base_url)}" placeholder="https://api.openai.com/v1"></div>
      <div class="fld"><label>Model</label><input id="oModel" value="${esc(own.model)}" placeholder="gpt-4o-mini"></div>
      <div class="fld"><label>API key</label><input id="oKey" type="password" autocomplete="off"
        placeholder="${own.has_key ? "saved · leave blank to keep it" : "sk-…"}"></div>
      <div class="hint" style="margin-top:0">Kept on this computer only (~/.config/reeldeck/ai.json). The page never sees it again.</div>
    </div>
    ${acct ? `<div class="acct">
      <div class="row"><span>Signed in as <b>@${esc(acct.handle || "you")}</b></span><span class="grow"></span>
        <span class="stat">${esc(acct.plan || "")}</span></div>
      ${left != null ? `<div class="meter${left < 10 ? " over" : left < 25 ? " warn" : ""}" style="margin-top:10px"><i style="width:${Math.max(0, Math.min(100, left))}%"></i></div>
        <div class="row" style="margin-top:6px"><span class="stat">${Math.round(left)}% left</span><span class="grow"></span>
        ${acct.top_up_url ? `<a href="${esc(acct.top_up_url)}" target="_blank" rel="noopener">Top up</a>` : ""}</div>` : ""}
      <div class="row" style="margin-top:8px"><button class="mini" id="aiAgain">Choose how to pay again</button></div>
    </div>` : ""}
    <div class="row"><span class="grow"></span><button id="aiCancel">Cancel</button><button class="go" id="aiSave">Save</button></div>`;
  const body = $("#modalBody");
  body.querySelectorAll('input[name="aiP"]').forEach((r) => {
    r.onchange = () => { $("#ownFields").hidden = r.value !== "own"; };
  });
  $("#aiCancel").onclick = closeModal;
  if ($("#aiAgain")) $("#aiAgain").onclick = async () => {
    const r = await api.aiChooseAgain();
    if (r.error) return toast(r.error, "error");
    closeModal();
    await waitGate();
  };
  $("#aiSave").onclick = async (e) => {
    const provider = body.querySelector('input[name="aiP"]:checked')?.value;
    e.target.classList.add("busy");
    const r = await api.aiSettings({ provider, base_url: $("#oBase").value.trim(),
      model: $("#oModel").value.trim(), api_key: $("#oKey").value.trim() });
    e.target.classList.remove("busy");
    if (r.error) return toast(r.error, "error");
    S.app.ai = r;
    drawAI(); emit();
    closeModal();
    toast(r.provider ? "AI is ready" : "Saved", "ok");
  };
}

// ---------- toasts ----------
onToast((msg, kind) => {
  const el = document.createElement("div");
  el.className = "toast" + (kind && kind !== "info" ? " " + kind : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
});

// ---------- modal: new / open project ----------
function openProjectModal() {
  $("#modalBody").innerHTML = `
    <h3>New reel</h3>
    <div class="hint">Start a fresh project, or point ReelDeck at a directory you
      already have. A directory without a reel.json gets one.</div>
    <div class="fld"><label>Name</label><input id="mName" placeholder="My next reel"></div>
    <div class="fld"><label>Existing directory <span style="text-transform:none;letter-spacing:0">(optional)</span></label>
      <input id="mDir" placeholder="/path/to/some-project"></div>
    <div class="fld"><label>Folder <span style="text-transform:none;letter-spacing:0">(optional, a series say)</span></label>
      <input id="mFolder" list="mFolders" placeholder="Series name"><datalist id="mFolders"></datalist></div>
    <div class="row">
      <button id="mCancel">Cancel</button><span class="grow"></span>
      <button id="mGo" class="go">Create</button>
    </div>`;
  $("#modalRoot").classList.add("open");
  $("#mName").focus();
  api.listProjects().then((d) => { $("#mFolders").innerHTML = folderOptions(d.projects || []); });
  $("#mDir").oninput = () => { $("#mGo").textContent = $("#mDir").value.trim() ? "Open" : "Create"; };
  $("#mCancel").onclick = closeModal;
  $("#modalBody").onkeydown = (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") $("#mGo").click(); };
  $("#mGo").onclick = async () => {
    const name = $("#mName").value.trim();
    const dir = $("#mDir").value.trim();
    const folder = $("#mFolder").value.trim();
    if (!name) { $("#mName").focus(); return toast("Give the reel a name", "warn"); }
    $("#mGo").classList.add("busy");
    const r = dir ? await api.registerProject(dir, name, folder) : await api.createProject(name, folder);
    $("#mGo").classList.remove("busy");
    if (r.error) { toast(r.error, "error"); return; }
    closeModal();
    await refreshProjectList(r.id);
    switchProject(r.id);
  };
}
// ---------- modal: taildrop the finished files to a phone ----------
async function openDropModal() {
  if (!S.pid) return;
  $("#modalBody").innerHTML = `<h3>Send to a device</h3><div class="hint">asking tailscale…</div>`;
  $("#modalRoot").classList.add("open");
  const r = await api.taildrop(S.pid);
  if (r.error) { $("#modalBody .hint").textContent = r.error; return; }
  $("#modalBody").innerHTML = `
    <h3>Send to a device</h3>
    <div class="hint">Taildrop — on an iPhone it lands in the Tailscale share sheet,
      then Save Video puts it in Photos.</div>
    ${r.files.length ? "" : `<div class="hint">nothing to send yet — Update first.</div>`}
    <div class="fld"><label>file</label><select id="dFile">
      ${r.files.map((f) => `<option>${f}</option>`).join("")}</select></div>
    <div class="row" style="flex-wrap:wrap;gap:6px;margin-top:10px">
      ${r.targets.map((t) => `<button data-t="${t.name}" ${t.online && r.files.length ? "" : "disabled"}
        title="${t.online ? "" : "offline"}">${t.name}</button>`).join("") || `<span class="hint">no taildrop targets</span>`}
    </div>`;
  $("#modalBody").querySelectorAll("button[data-t]").forEach((b) => {
    b.onclick = async () => {
      const label = b.textContent;
      b.disabled = true; b.textContent = "sending…";
      const res = await api.sendTaildrop(S.pid, $("#dFile").value, b.dataset.t);
      b.disabled = false; b.textContent = label;
      if (res.error) toast(res.error, "error");
      else toast(`sent ${res.name} → ${b.dataset.t}`);
    };
  });
}
function closeModal() { $("#modalRoot").classList.remove("open"); }

// ---------- boot ----------
async function boot() {
  buildShell();
  subscribe(jobIndicator);
  subscribe(drawLoad);
  refreshApp().then((d) => { if (d.ai?.publik && !d.ai.ready) waitGate(); });
  const list = await refreshProjectList();
  const qp = new URLSearchParams(location.search).get("p");
  const startPid = list.find((p) => p.id === qp && !p.missing)?.id
    || list.find((p) => !p.missing)?.id;
  if (!startPid) return showWelcome();
  switchTab(TABS.find((t) => "#" + t.id === location.hash)?.id || "timeline");   // /#comments is bookmarkable
  await switchProject(startPid);
}
boot();
// a typed or linked /#voice switches tabs too
addEventListener("hashchange", () => { const t = TABS.find((t) => "#" + t.id === location.hash); if (t && S.pid) switchTab(t.id); });

/* ---- project browser -----------------------------------------------------
   A switcher that is a <select> stops being usable somewhere around the fifth
   reel: the names are similar, and nothing in a dropdown tells you which ones
   are finished, which are waiting on footage, or which have unrendered edits.
   The gallery answers those at a glance and opens on click. */
function projectCard(p) {
  if (p.missing) {
    return `<div class="pcard missing"><div class="pname">${esc(p.name)}</div>
      <div class="pmeta">directory is gone: ${esc(p.dir)}</div></div>`;
  }
  const thumb = p.has_reel
    ? api.thumbUrl(p.id, `${"clips/out"}/reel.mp4`, p.reel_mtime)
    : null;
  const n = (p.stale || []).length;
  const flags = [];
  if (!p.has_reel) flags.push(`<span class="pflag warn">not rendered</span>`);
  else if (n) flags.push(`<span class="pflag warn">${n} step${n > 1 ? "s" : ""} pending</span>`);
  else flags.push(`<span class="pflag ok">up to date</span>`);
  if ((p.needs_footage || []).length) {
    flags.push(`<span class="pflag hot">needs footage: ${esc(p.needs_footage.join(", "))}</span>`);
  }
  if (p.effects) flags.push(`<span class="pflag">${p.effects} effect${p.effects > 1 ? "s" : ""}</span>`);
  return `<div class="pcard${p.id === S.pid ? " current" : ""}" data-open="${p.id}">
    <div class="pthumb">${thumb ? `<img src="${thumb}" alt="" loading="lazy">` : "<span>no reel yet</span>"}</div>
    <div class="pbody">
      <div class="pname">${esc(p.title || p.name)}</div>
      <div class="pmeta">${p.length != null ? p.length.toFixed(1) + "s" : "—"} ·
        ${p.segments} beats · ${p.lines} lines</div>
      <div class="pflags">${flags.join("")}</div>
      <input class="pfolder" data-pid="${p.id}" list="pFolders" value="${esc(p.folder || "")}"
        placeholder="+ folder" title="folder — type a new name to make one">
    </div></div>`;
}

/* Folders are one level, and only a label (projects.py): enough to turn a long
   list into a few series. Newest folder first, so the series you're working on
   is on top; inside a folder, registry order (a series reads 01, 02, 03). */
function byFolder(rows) {
  const g = new Map();
  for (const p of rows) {
    const f = p.folder || "";
    if (!g.has(f)) g.set(f, []);
    g.get(f).push(p);
  }
  const newest = (ps) => Math.max(...ps.map((p) => p.created || 0));
  return [...g].sort(([a, pa], [b, pb]) => (!a) - (!b) || newest(pb) - newest(pa));
}
function folderOptions(rows) {
  return [...new Set(rows.map((p) => p.folder).filter(Boolean))].map((f) => `<option value="${esc(f)}">`).join("");
}
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }

export async function openBrowser(keepOpen = new Set()) {
  const body = $("#modalBody");
  body.innerHTML = `<h3>Reels</h3><div class="hint"><span class="spinner"></span></div>`;
  $("#modalRoot").classList.add("open");
  const d = await api.browseProjects();
  const rows = d.projects || [];
  const groups = byFolder(rows);
  // open the folder you're in (or everything, when there's only one); the rest
  // collapse to a name and a count
  body.innerHTML = `<h3>Reels <span class="sub">${rows.length}</span></h3>
    <datalist id="pFolders">${folderOptions(rows)}</datalist>
    <div class="pfolders">${groups.map(([f, ps]) => `
      <details class="pfold" data-f="${esc(f)}"${groups.length === 1 || keepOpen.has(f) || ps.some((p) => p.id === S.pid) ? " open" : ""}>
        <summary>${esc(f || "unfiled")} <span class="sub">${ps.length}</span></summary>
        <div class="pgrid">${ps.map(projectCard).join("")}</div>
      </details>`).join("")}</div>
    <div class="row" style="margin-top:14px">
      <button id="pNew">+ New reel</button>
      <span class="grow"></span>
      <button id="pClose">Close</button>
    </div>`;
  body.querySelectorAll("[data-open]").forEach((el) => {
    el.onclick = () => { closeModal(); switchProject(el.dataset.open); };
  });
  body.querySelectorAll(".pfolder").forEach((el) => {
    el.onclick = (e) => e.stopPropagation();   // typing a folder isn't opening the reel
    el.onkeydown = (e) => { if (e.key === "Enter") el.blur(); };
    el.onchange = async () => {
      const r = await api.setFolder(el.dataset.pid, el.value.trim());
      if (r.error) { toast(r.error, "error"); return; }
      await refreshProjectList();
      // follow the card: whatever was open stays open, plus where it went
      const open = new Set([...body.querySelectorAll(".pfold[open]")].map((d) => d.dataset.f));
      openBrowser(open.add(el.value.trim()));
    };
  });
  body.querySelector("#pClose").onclick = closeModal;
  body.querySelector("#pNew").onclick = openProjectModal;
}
