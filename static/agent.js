// The Agent tab: say what the reel needs, get a list of concrete changes
// (voice-over lines, cuts, new HTML clips), tick the ones you want, apply.
// Applying edits reel.json and writes clip files; Update renders them, as after
// any edit. agent.py has the change shapes and the checks.
import { api } from "./api.js";
import { S, subscribe, save, refreshProject, update, toast } from "./store.js";

const ASKS = ["Tighten the cut to the voice-over", "Make the clips the pending requests ask for",
  "Turn SHOTS.md into a first cut", "Write voice-over lines for this cut"];
let root, out = null, err = null, busy = false, done = null, pid = null;

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const q = (s) => `“${esc(s)}”`;
const secs = (s) => [s.start != null && `from ${s.start}s`, s.dur != null && `for ${s.dur}s`].filter(Boolean).join(" ");
const where = (a) => (a === "first" ? "at the start" : a ? `after ${q(a)}` : "at the end");

function describe(c) {
  const segs = S.manifest?.segments || [], lines = S.manifest?.vo?.lines || [];
  if (c.op === "set") {
    const old = segs.find((s) => s.name === c.seg) || {};
    const parts = Object.entries(c.fields || {}).map(([k, v]) => {
      const was = k === "secs" || k === "params" ? old.render?.[k] : old[k];
      const f = (x) => (x == null ? "—" : typeof x === "object" ? JSON.stringify(x) : x);
      return `${k} ${esc(f(was))} → ${esc(f(v))}`;
    });
    return `Change ${q(c.seg)}: ${parts.join(", ")}`;
  }
  if (c.op === "add") {
    const s = c.seg || {};
    const from = s.render ? `a render of ${esc(s.render.html)}${s.render.params ? " " + esc(JSON.stringify(s.render.params)) : ""}`
      : `${esc(s.src)} ${secs(s)}`;
    return `Add ${q(s.name)} ${where(c.after)}: ${from}`;
  }
  if (c.op === "remove") return `Remove ${q(c.seg)}`;
  if (c.op === "move") return `Move ${q(c.seg)} ${where(c.after)}`;
  if (c.op === "line") {
    if ("i" in c) return c.text ? `Line ${c.i + 1}: ${q(lines[c.i]?.text)} → ${q(c.text)}` : `Remove line ${c.i + 1}: ${q(lines[c.i]?.text)}`;
    return `New line ${c.after === "first" ? "at the top" : c.after == null ? "at the end" : `after line ${c.after + 1}`}: ${q(c.text)}`;
  }
  if (c.op === "clip") return `New clip ${esc(c.file)}${c.request ? " for its request" : ""}
    <details><summary>source (${(c.html || "").split("\n").length} lines)</summary><pre class="log">${esc(c.html)}</pre></details>`;
  return esc(JSON.stringify(c));
}

function draw() {
  const box = root.querySelector("#agOut");
  if (!S.app?.ai?.provider) {
    box.innerHTML = `<div class="aiEmpty">The agent needs an AI provider.
      <div style="margin-top:10px"><button class="go" data-act="setup">Set up AI</button></div></div>`;
    return;
  }
  root.querySelector("#agGo").disabled = busy || !S.pid;
  root.querySelector("#agGo").classList.toggle("busy", busy);
  let html = err ? `<div class="aiErr">${esc(err.error)}${err.link ? ` <a href="${esc(err.link)}" target="_blank" rel="noopener">Top up</a>` : ""}</div>` : "";
  if (done) {
    html += `<div class="aiTarget">Applied ${done.n} change(s)${done.wrote.length ? ` · wrote ${done.wrote.map(esc).join(", ")}` : ""}.
      Update renders them.</div><div class="row"><button class="go" data-act="update"${S.job.running ? " disabled" : ""}>Update</button></div>`;
  }
  if (out) {
    html += `<p class="hint">${esc(out.summary) || "No summary."}</p>` + (out.changes.length ? `
      <div class="aiOut">${out.changes.map((c, i) => `
        <label class="agChange${c.error ? " bad" : ""}">
          <input type="checkbox" data-i="${i}" ${c.error ? "disabled" : c.off ? "" : "checked"}>
          <span>${describe(c)}${c.why ? `<small>${esc(c.why)}</small>` : ""}${c.error ? `<small class="aiErr">${esc(c.error)}</small>` : ""}</span>
        </label>`).join("")}</div>
      <div class="row" style="margin-top:10px"><button class="go" data-act="apply"${S.job.running ? " disabled" : ""}>Apply ticked</button>
        <button class="ghost" data-act="discard">Discard</button></div>` : "");
  }
  box.innerHTML = html;
}

async function propose() {
  const ask = root.querySelector("#agAsk").value;
  if (!ask.trim() || busy) return;
  if (!(await save())) return;       // the agent reads reel.json from disk: flush pending edits first
  busy = true; err = null; done = null; out = null; pid = S.pid; draw();
  const d = await api.agent(pid, ask);
  busy = false;
  if (pid !== S.pid) return draw();  // switched reels while it thought
  if (d.error) err = d; else out = d;
  draw();
}

async function apply() {
  const ticked = out.changes.filter((c) => !c.error && !c.off);
  if (!ticked.length) return toast("tick at least one change", "warn");
  if (!(await save())) return;
  const r = await api.agentApply(S.pid, ticked, S.rev);
  if (r.error === "stale") { toast("reel.json changed elsewhere — ask again", "warn"); await refreshProject(); return; }
  if (r.error) return toast(r.error, "error");
  done = { n: ticked.length, wrote: r.wrote || [] };
  out = null;
  await refreshProject();
  draw();
}

export function mount(el) {
  root = el;
  el.innerHTML = `<section class="ai" style="max-width:860px">
      <h2>Agent <span class="sub">proposes edits, cuts and new clips from this reel's footage and script; you pick what lands</span></h2>
      <div class="fld"><textarea id="agAsk" placeholder="What should change? e.g. cut the intro to 3s and add a title card for the hook"></textarea></div>
      <div class="chips">${ASKS.map((a) => `<button class="chip" data-ask="${esc(a)}">${esc(a)}</button>`).join("")}</div>
      <div class="row" style="margin-top:10px"><button class="go" id="agGo">✦ Propose changes</button>
        <span class="hint" style="margin:0">Nothing changes until you apply, and nothing renders or posts until you press Update.</span></div>
      <div id="agOut" style="margin-top:12px"></div>
    </section>`;
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.ask) { el.querySelector("#agAsk").value = b.dataset.ask; return; }
    if (b.id === "agGo") return propose();
    ({ setup: () => window.reeldeckAISettings?.(), apply, update, discard: () => { out = null; draw(); } })[b.dataset.act]?.();
  });
  el.addEventListener("change", (e) => { if (e.target.dataset.i != null) out.changes[+e.target.dataset.i].off = !e.target.checked; });
  // Redraw only when something this tab shows changed: job polling emits often,
  // and a redraw would close an open clip source mid-read.
  let was = null, key = null;
  subscribe(() => {
    if (S.pid !== was) { was = S.pid; out = err = done = null; }
    const k = `${S.pid}|${S.job.running}|${!!S.app?.ai?.provider}`;
    if (k !== key) { key = k; draw(); }
  });
  draw();
}
