// The shell: project switcher, tab router, toasts, job indicator. Each tab
// module owns its own DOM subtree and re-renders on store change; this file
// just wires them together and boots.
import { api } from "./api.js";
import { S, subscribe, onToast, openProject, toast, update } from "./store.js";
import * as timeline from "./timeline.js";
import * as script from "./script.js";
import * as clips from "./clips.js";
import * as publish from "./publish.js";
import * as comments from "./comments.js";

const TABS = [
  { id: "timeline", label: "Timeline", mod: timeline },
  { id: "script", label: "Script", mod: script },
  { id: "clips", label: "Clips", mod: clips },
  { id: "publish", label: "Publish", mod: publish },
  { id: "comments", label: "Comments", mod: comments },
];
let active = "timeline";
let mounted = new Set();

function $(sel) { return document.querySelector(sel); }

function buildShell() {
  document.body.innerHTML = `
    <div id="topbar">
      <h1>reel<b>deck</b></h1>
      <select id="projSwitch"></select>
      <button id="browseBtn" class="icon" title="browse all reels">▦</button>
      <button id="newProjBtn" class="icon" title="new / open project">+</button>
      <span class="grow"></span>
      <div id="jobIndicator"><span class="dot"></span><span id="jobText"></span></div>
      <button id="dropBtn" class="icon" title="send to a device (taildrop)">⇪</button>
      <button id="linesBtn" class="icon" title="re-record line by line (phone page)">☰</button>
      <button id="topUpdate" class="go" title="render whatever your edits made stale (⌘/Ctrl+↵)">Update</button>
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
  $("#linesBtn").onclick = () => (location.href = `/master?p=${S.pid || ""}`);
  $("#projSwitch").addEventListener("change", (e) => {
    if (e.target.value) switchProject(e.target.value);
  });
  document.querySelectorAll("#tabs button").forEach((b) => {
    b.addEventListener("click", () => switchTab(b.dataset.tab));
  });
  $("#modalRoot").addEventListener("click", (e) => {
    if (e.target.id === "modalRoot") closeModal();
  });
  $("#topUpdate").onclick = update;
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
    // works from any tab, including with a script field focused — the whole
    // point is not having to go find the right button after an edit
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); update(); }
  });
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
window.reeldeckSwitchTab = switchTab; // clips.js uses this after "insert into timeline"

async function switchProject(pid) {
  history.replaceState(null, "", `?p=${pid}${location.hash}`);
  $("#projSwitch").value = pid;   // boot lists projects before one is open
  await openProject(pid);
}

async function refreshProjectList(selectPid) {
  const d = await api.listProjects();
  const sel = $("#projSwitch");
  sel.innerHTML = byFolder(d.projects).map(([f, rows]) =>
    `<optgroup label="${esc(f || "unfiled")}">${rows.map((p) =>
      `<option value="${p.id}"${p.missing ? " disabled" : ""}${p.id === (selectPid || S.pid) ? " selected" : ""}>
        ${p.name}${p.missing ? " (missing)" : ""}</option>`).join("")}</optgroup>`).join("");
  return d.projects;
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
    <h3>New or existing reel</h3>
    <div class="hint">Create a fresh project, or point reeldeck at an existing
      directory that already has a reel.json (or will get one scaffolded).</div>
    <div class="fld"><label>name</label><input id="mName" placeholder="my next reel"></div>
    <div class="fld"><label>existing directory (optional — leave blank to create new)</label>
      <input id="mDir" placeholder="/path/to/some-project"></div>
    <div class="fld"><label>folder (optional — a series, say)</label>
      <input id="mFolder" list="mFolders" placeholder="BLE"><datalist id="mFolders"></datalist></div>
    <div class="row" style="margin-top:10px">
      <button id="mCancel">Cancel</button><span class="grow"></span>
      <button id="mGo" class="go">Create / Open</button>
    </div>`;
  $("#modalRoot").classList.add("open");
  api.listProjects().then((d) => { $("#mFolders").innerHTML = folderOptions(d.projects || []); });
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    const name = $("#mName").value.trim();
    const dir = $("#mDir").value.trim();
    const folder = $("#mFolder").value.trim();
    if (!name) return;
    const r = dir ? await api.registerProject(dir, name, folder) : await api.createProject(name, folder);
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
  switchTab(TABS.find((t) => "#" + t.id === location.hash)?.id || "timeline");   // /#comments is bookmarkable
  const list = await refreshProjectList();
  subscribe(jobIndicator);

  const qp = new URLSearchParams(location.search).get("p");
  const startPid = list.find((p) => p.id === qp && !p.missing)?.id
    || list.find((p) => !p.missing)?.id;
  if (startPid) {
    await switchProject(startPid);
  } else {
    openProjectModal();
  }
}
boot();

/* ---- project browser -----------------------------------------------------
   A switcher that is a <select> stops being usable somewhere around the fifth
   reel: the names are similar, and nothing in a dropdown tells you which ones
   are finished, which are waiting on footage, or which have unrendered edits.
   The gallery answers those at a glance and opens on click. */
function projectCard(p) {
  if (p.missing) {
    return `<div class="pcard missing"><div class="pname">${p.name}</div>
      <div class="pmeta">directory is gone — ${p.dir}</div></div>`;
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
    flags.push(`<span class="pflag hot">needs footage: ${p.needs_footage.join(", ")}</span>`);
  }
  if (p.effects) flags.push(`<span class="pflag">${p.effects} effect${p.effects > 1 ? "s" : ""}</span>`);
  return `<div class="pcard${p.id === S.pid ? " current" : ""}" data-open="${p.id}">
    <div class="pthumb">${thumb ? `<img src="${thumb}" alt="" loading="lazy">` : "<span>no reel yet</span>"}</div>
    <div class="pbody">
      <div class="pname">${p.title || p.name}</div>
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
  body.innerHTML = `<h3>Reels</h3><div class="hint">Loading…</div>`;
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
      <button class="mini" id="pNew">+ new reel</button>
      <span class="grow"></span>
      <button class="mini" id="pClose">close</button>
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
