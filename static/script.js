// The Script tab: write the reel before voicing it. Beats of lines, a timing
// estimate on every line, named versions, and an assistant for drafts, hooks and
// rewrites. The draft lives in script.json (scripts.py); "Send to Voice" copies it
// into reel.json's vo.lines, where the Voice tab and the render take over.
import { api } from "./api.js";
import { S, subscribe, autosave, emit, toast } from "./store.js";

const WPS = 2.6, LONG = 4.5;           // words a second read aloud; a line past LONG is a breath too many
const TARGETS = [15, 30, 45, 60, 90];
const HOWS = ["punchier", "shorter", "clearer", "casual"];
let root, pid = null, doc = null, versions = [], timer = null, loadErr = "", seeded = false;
let focus = null;                      // {b, l}: the line the Rewrite tab works on
let tab = "draft", how = "punchier", res = {}, lastProv;

const $ = (s) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const words = (t) => (t.trim() ? t.trim().split(/\s+/).length : 0);
const secs = (t) => words(t) / WPS;
const fmt = (s) => (s < 10 ? s.toFixed(1) : Math.round(s)) + "s";
const blank = () => ({ name: "", lines: [{ text: "", note: "" }] });

export function mount(el) {
  root = el;
  root.addEventListener("input", onInput);
  root.addEventListener("click", onClick);
  root.addEventListener("keydown", onKey);
  root.addEventListener("focusin", (e) => {
    const at = lineOf(e.target);
    if (!at || e.target.tagName !== "TEXTAREA") return;
    focus = at;
    root.querySelectorAll(".line.picked").forEach((x) => x.classList.remove("picked"));
    if (tab === "rewrite") { e.target.closest(".line").classList.add("picked"); drawAI(); }
  });
  subscribe(onStore);
  onStore();
}

export function shown() { root?.querySelectorAll(".line textarea").forEach(grow); }
addEventListener("resize", shown);   // a narrower window wraps lines onto more rows

function onStore() {
  if (S.pid !== pid) return load(S.pid);
  // an existing reel with no script yet: start from the lines it already has
  if (doc && !doc.updated && !seeded && S.loaded && S.manifest) seed();
  const prov = S.app?.ai?.provider ?? null;
  if (doc && prov !== lastProv) { lastProv = prov; drawAI(); }
}

async function load(p) {
  if (timer) await saveNow();           // the last keystrokes belong to the reel they were typed in
  pid = p; doc = null; loadErr = ""; seeded = false; focus = null; res = {};
  if (!p) { root.innerHTML = ""; return; }
  root.innerHTML = `<div class="aiEmpty" style="border:0"><span class="spinner"></span></div>`;
  const d = await api.getScript(p);
  if (p !== pid) return;
  if (d.error) {
    root.innerHTML = `<div class="aiEmpty"><div class="bad">${esc(d.error)}</div>
      <button style="margin-top:10px" data-act="reload">Retry</button></div>`;
    return;
  }
  doc = d.script; versions = d.versions || [];
  onStore();
  draw();
}

function seed() {
  seeded = true;
  if (doc.beats.some((b) => b.lines.some((l) => l.text.trim()))) return;
  doc.title ||= S.manifest.name || "";
  const ls = S.manifest.vo?.lines || [];
  const beats = [];
  ls.forEach((l, i) => {
    // a new beat where the anchor changes, or after a paragraph's long pause
    if (!beats.length || (l.beat || "") !== beats.at(-1).name || (!l.beat && (ls[i - 1]?.pause ?? 0) >= 700))
      beats.push({ name: l.beat || "", lines: [] });
    beats.at(-1).lines.push({ text: l.text || "", note: "" });
  });
  if (beats.length) doc.beats = beats;
  if (root.querySelector(".sw")) draw();
  if (beats.length) setSave("From the Voice tab's lines");
}

// ---------- saving ----------
function dirty() {
  setSave("Saving…");
  clearTimeout(timer);
  timer = setTimeout(saveNow, 600);
}

async function saveNow() {
  clearTimeout(timer); timer = null;
  const p = pid, r = await api.putScript(p, doc);
  if (p !== pid) return true;
  if (r.error) { setSave("Couldn't save", true); toast(r.error, "error"); return false; }
  doc.updated = r.script.updated;
  setSave("Saved");
  return true;
}

function setSave(text, err) {
  const el = root.querySelector("#swSave");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("err", !!err);
}

// ---------- the vo.lines this script becomes ----------
// Pauses as the old splitter set them: 250ms between lines of a beat, 700 at a
// beat's end, none after the last.
function toLines() {
  const out = [];
  doc.beats.forEach((b) => {
    const ls = b.lines.map((l) => l.text.trim()).filter(Boolean);
    ls.forEach((text, i) => out.push({ text, beat: b.name.trim(), pause: i < ls.length - 1 ? 250 : 700 }));
  });
  if (out.length) out.at(-1).pause = 0;
  return out;
}

function sendToVoice() {
  if (!S.manifest) return toast("the reel is still opening", "warn");
  const segs = S.manifest.segments || [], old = S.manifest.vo?.lines || [];
  const next = toLines().map(({ text, beat, pause }) => {
    const prev = old.find((o) => o.text === text);     // same words: keep its voice settings and take
    const seg = segs.find((s) => (s.name || "").toLowerCase() === beat.toLowerCase());
    return { exag: 0.5, cfg: 0.5, ...prev, text, pause, beat: seg ? seg.name : prev?.beat ?? "" };
  });
  if (!next.length) return toast("write a line first", "warn");
  const apply = (lines) => {
    S.manifest.vo ??= {};
    S.manifest.vo.lines = lines;
    autosave(); emit();
    document.querySelector("#modalRoot").classList.remove("open");
    toast(`${next.length} line${next.length > 1 ? "s" : ""} sent to Voice`, "ok");
    window.reeldeckSwitchTab?.("voice");
  };
  if (!old.length) return apply(next);
  const m = document.querySelector("#modalBody");
  m.innerHTML = `<h3>Send to Voice</h3>
    <div class="hint">The Voice tab already has ${old.length} line${old.length > 1 ? "s" : ""}. Lines whose
      words haven't changed keep their settings and recorded takes.</div>
    <div class="row"><button data-x="cancel">Cancel</button><span class="grow"></span>
      <button data-x="add">Add after them</button><button class="go" data-x="replace">Replace with ${next.length}</button></div>`;
  document.querySelector("#modalRoot").classList.add("open");
  m.querySelector('[data-x="cancel"]').onclick = () => document.querySelector("#modalRoot").classList.remove("open");
  m.querySelector('[data-x="add"]').onclick = () => {
    if (old.length) old.at(-1).pause = Math.max(old.at(-1).pause || 0, 700);
    apply([...old, ...next]);
  };
  m.querySelector('[data-x="replace"]').onclick = () => apply(next);
}

// ---------- drawing ----------
function draw() {
  root.innerHTML = `<div class="sw">
    <aside>
      <section>
        <h2>Length</h2>
        <div class="swTotal"><b id="swT"></b><span id="swOf"></span></div>
        <div class="meter" id="swMeter"><i></i></div>
        <div class="hint" id="swGap" style="margin-top:6px"></div>
        <div class="chips" style="margin-top:10px">${TARGETS.map((t) =>
          `<button class="chip" data-target="${t}">${t}s</button>`).join("")}</div>
        <ol class="swOutline" id="swOutline"></ol>
      </section>
      <section>
        <h2>Versions</h2>
        <div class="row"><input id="swLabel" placeholder="Name this version" maxlength="80"><button data-act="snap">Save</button></div>
        <ul class="vers" id="swVers" style="margin-top:8px"></ul>
      </section>
    </aside>
    <div>
      <div class="swHead">
        <input class="swTitle grow" id="swTitle" placeholder="Untitled reel" value="${esc(doc.title)}">
        <span class="stat" id="swWords"></span>
        <span class="swSave" id="swSave">${doc.updated ? "Saved" : ""}</span>
        <button class="go" data-act="send" title="Copy these lines into the Voice tab">Send to Voice</button>
      </div>
      <div id="swBeats"></div>
      <button class="addBeat" data-act="addbeat">+ Add beat</button>
    </div>
    <aside class="ai">
      <section>
        <h2>Assistant</h2>
        <div class="seg">${["draft", "hooks", "rewrite"].map((t) =>
          `<button data-tab="${t}">${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div>
        <div id="aiBody"></div>
      </section>
    </aside>
  </div>`;
  drawBeats(); drawVersions(); drawAI();
}

function drawBeats() {
  const n = doc.beats.length;
  $("#swBeats").innerHTML = doc.beats.map((b, bi) => `
    <div class="beat" id="beat-${bi}" data-b="${bi}">
      <div class="beatHead">
        <input data-k="name" value="${esc(b.name)}" placeholder="Beat ${bi + 1}" aria-label="Beat name">
        <span class="stat" data-bt></span>
        <button class="mini" data-act="up" title="Move up"${bi ? "" : " disabled"}>↑</button>
        <button class="mini" data-act="down" title="Move down"${bi < n - 1 ? "" : " disabled"}>↓</button>
        <button class="mini" data-act="delbeat" title="Delete this beat">✕</button>
      </div>
      ${b.lines.map((l, li) => `
        <div class="line${!bi && !li ? " hook" : ""}" data-l="${li}">
          <textarea rows="1" placeholder="${!bi && !li ? "The first line. Why would anyone stop scrolling?" : "Next line…"}">${esc(l.text)}</textarea>
          <span class="secs"></span>
          <span><button class="mini" data-act="rewrite" title="Rewrite with AI">✦</button><button class="mini" data-act="delline" title="Delete line">✕</button></span>
        </div>`).join("")}
      <div class="beatFoot">
        <button class="ghost" data-act="addline">+ Line</button>
        <button class="ghost" data-act="next" title="Ask the assistant how this beat goes on">✦ Suggest next lines</button>
      </div>
    </div>`).join("");
  root.querySelectorAll(".line textarea").forEach(grow);
  if (focus && tab === "rewrite") lineEl(focus.b, focus.l)?.classList.add("picked");
  drawTimes();
}

function drawTimes() {
  const ls = toLines();
  const total = ls.reduce((a, l) => a + secs(l.text) + l.pause / 1000, 0);
  const w = ls.reduce((a, l) => a + words(l.text), 0);
  root.querySelectorAll(".beat").forEach((el) => {
    const b = doc.beats[el.dataset.b];
    el.querySelectorAll(".line").forEach((le) => {
      const s = secs(b.lines[le.dataset.l].text), pill = le.querySelector(".secs");
      pill.textContent = s ? fmt(s) : "–";
      pill.classList.toggle("long", s > LONG);
      pill.title = s > LONG ? "Long for one breath: split it?" : "";
    });
    el.querySelector("[data-bt]").textContent = fmt(b.lines.reduce((a, l) => a + secs(l.text), 0));
  });
  const t = doc.target, off = total - t;
  $("#swT").textContent = fmt(total);
  $("#swOf").textContent = `of ${t}s`;
  $("#swMeter").className = "meter" + (total > t * 1.1 ? " over" : total < t * .9 ? " warn" : "");
  $("#swMeter i").style.width = Math.min(100, (total / t) * 100) + "%";
  $("#swGap").textContent = Math.abs(off) <= t * .1 ? "On target." : off < 0 ? `About ${Math.round(-off)}s to go.` : `About ${Math.round(off)}s over.`;
  $("#swWords").textContent = `${w} words`;
  root.querySelectorAll("[data-target]").forEach((c) => c.classList.toggle("on", +c.dataset.target === t));
  $("#swOutline").innerHTML = doc.beats.map((b, bi) =>
    `<li data-go="${bi}"><span>${esc(b.name || `Beat ${bi + 1}`)}</span><span>${fmt(b.lines.reduce((a, l) => a + secs(l.text), 0))}</span></li>`).join("");
}

function drawVersions() {
  const when = (at) => new Date(at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  $("#swVers").innerHTML = versions.length ? versions.map((v) => `
    <li><span><b>${esc(v.label || "Saved")}</b> · ${when(v.at)} · ${v.words}w</span>
      <button class="mini" data-restore="${v.id}" title="Restore this version (the current one is kept as a version first)">Restore</button></li>`).join("")
    : `<li>None yet. Save one before a big change.</li>`;
}

// ---------- the assistant ----------
function drawAI() {
  const body = root.querySelector("#aiBody");
  if (!body) return;
  root.querySelectorAll(".seg button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  if (!S.app?.ai?.provider) {
    body.innerHTML = `<div class="aiEmpty">Drafts, hooks and rewrites need an AI provider.
      <div style="margin-top:10px"><button class="go" data-act="setup">Set up AI</button></div></div>`;
    return;
  }
  const r = res[tab] || {};
  const go = (mode, label, cls = "go") => `<button class="${cls}${r.busy && r.mode === mode ? " busy" : ""}" data-ask="${mode}"${r.busy ? " disabled" : ""}>${label}</button>`;
  const err = r.err ? `<div class="aiErr">${esc(r.err)}${r.link ? ` <a href="${esc(r.link)}" target="_blank" rel="noopener">Top up</a>` : ""}</div>` : "";
  let html = "";
  if (tab === "draft") {
    html = `<div class="fld"><label>What's the reel about?</label>
      <textarea id="aiBrief" placeholder="Who it's for, the one thing they should take away, the tone.">${esc(doc.brief)}</textarea></div>
      <div class="row">${go("ideate", "✦ Draft a script")}${go("outline", "Check the structure", "")}</div>${err}`;
    const o = r.out;
    if (o && r.mode === "ideate" && Array.isArray(o.beats)) {
      html += `<div class="aiOut"><div class="aiDraft">${o.title ? `<div style="font-weight:600;margin-bottom:4px">${esc(o.title)}</div>` : ""}
        ${o.beats.map((b) => `<b>${esc(b.name)}</b>${(b.lines || []).map(esc).join("<br>")}`).join("")}</div>
        <div class="row"><button class="go" data-act="usedraft">Use this draft</button><span class="hint" style="margin:0">Your current script is saved as a version first.</span></div></div>`;
    } else if (o && r.mode === "outline" && Array.isArray(o.beats)) {
      html += `<div class="aiOut">${o.beats.map((b, i) =>
        `<button class="aiOpt" data-addbeat="${i}">+ ${esc(b.name)}<small>${esc(b.why)}</small></button>`).join("")}</div>`;
    }
  } else if (tab === "hooks") {
    html = `<p class="hint" style="margin:0 0 10px">Six ways to open. Pick one to make it the first line.</p>
      <div class="row">${go("hooks", r.out ? "✦ More hooks" : "✦ Suggest hooks")}</div>${err}`;
    if (Array.isArray(r.out?.options)) html += `<div class="aiOut">${r.out.options.map((h, i) =>
      `<button class="aiOpt" data-hook="${i}">${esc(h)}<small>${fmt(secs(String(h)))}</small></button>`).join("")}</div>`;
  } else {
    const l = focus && doc.beats[focus.b]?.lines[focus.l];
    if (!l) {
      body.innerHTML = `<div class="aiEmpty">Click into a line, or its ✦, to rewrite it.</div>`;
      return;
    }
    html = `<div class="aiTarget">${esc(l.text) || "<i>empty line</i>"}</div>
      <div class="chips">${HOWS.map((h) => `<button class="chip${h === how ? " on" : ""}" data-how="${h}">${h}</button>`).join("")}</div>
      <input id="aiInstr" placeholder="Or say how: e.g. end on a question" style="margin-top:8px">
      <div class="row" style="margin-top:8px">${go("rewrite", "✦ Rewrite")}</div>${err}`;
    if (Array.isArray(r.out?.options)) html += `<div class="aiOut">${r.out.options.map((o, i) =>
      `<button class="aiOpt" data-pick="${i}">${[].concat(o).map(esc).join("<br>")}</button>`).join("")}</div>`;
  }
  body.innerHTML = html;
}

async function ask(mode, extra = {}) {
  const r = (res[tab] = { busy: true, mode, ...extra.keep });
  drawAI();
  const d = await api.assist({ mode, script: doc, target: doc.target, ...extra.body });
  Object.assign(r, { busy: false }, d.error ? { err: d.error, link: d.link } : { out: d });
  if (res[tab] === r) drawAI();
  return d;
}

// ---------- editing ----------
function lineOf(el) {
  const b = el.closest?.(".beat"), l = el.closest?.(".line");
  return b && { b: +b.dataset.b, l: l ? +l.dataset.l : -1 };
}
const lineEl = (b, l) => root.querySelector(`#beat-${b} .line[data-l="${l}"]`);
function grow(ta) { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; }
function focusLine(b, l, caret = Infinity) {
  const ta = lineEl(b, l)?.querySelector("textarea");
  if (!ta) return;
  ta.focus();
  const at = Math.min(caret, ta.value.length);
  ta.setSelectionRange(at, at);
}
function change(fn) { fn(); drawBeats(); dirty(); }

function onInput(e) {
  const t = e.target;
  if (t.id === "swTitle") doc.title = t.value;
  else if (t.id === "aiBrief") doc.brief = t.value;
  else if (t.dataset.k === "name") { doc.beats[lineOf(t).b].name = t.value; drawTimes(); }
  else if (t.tagName === "TEXTAREA" && t.closest(".line")) {
    const { b, l } = lineOf(t);
    doc.beats[b].lines[l].text = t.value.replace(/\n/g, " ");
    grow(t); drawTimes();
  } else return;
  dirty();
}

function onKey(e) {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); saveNow(); return; }
  const t = e.target;
  if (t.tagName !== "TEXTAREA" || !t.closest(".line") || e.isComposing) return;
  const { b, l } = lineOf(t), ls = doc.beats[b].lines;
  // Enter splits the line at the caret: one breath per line
  if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    const at = t.selectionStart;
    change(() => { ls[l].text = t.value.slice(0, at).trimEnd(); ls.splice(l + 1, 0, { text: t.value.slice(at).trimStart(), note: "" }); });
    focusLine(b, l + 1, 0);
  } else if (e.key === "Backspace" && t.selectionStart === 0 && t.selectionEnd === 0 && l > 0) {
    e.preventDefault();   // join onto the line above
    const prev = ls[l - 1].text, sep = prev && ls[l].text ? " " : "";
    change(() => { ls[l - 1].text = prev + sep + ls[l].text; ls.splice(l, 1); });
    focusLine(b, l - 1, prev.length + sep.length);
  } else if (e.key === "ArrowUp" && t.selectionStart === 0 && l > 0) { e.preventDefault(); focusLine(b, l - 1); }
  else if (e.key === "ArrowDown" && t.selectionStart === t.value.length && l < ls.length - 1) { e.preventDefault(); focusLine(b, l + 1, 0); }
}

async function onClick(e) {
  const el = e.target.closest("button, li[data-go]");
  if (!el) return;
  const d = el.dataset, at = lineOf(el);
  if (d.tab) { tab = d.tab; drawBeats(); drawAI(); return; }
  if (d.target) { doc.target = +d.target; drawTimes(); dirty(); return; }
  if (d.go != null) {
    root.querySelector(`#beat-${d.go}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    return focusLine(+d.go, 0);
  }
  if (d.how) { how = d.how; drawAI(); return; }
  if (d.restore) return restore(d.restore, el);
  if (d.hook != null) {
    const h = String(res.hooks.out.options[+d.hook]);
    change(() => {
      if (!doc.beats.length) doc.beats.push(blank());
      const first = doc.beats[0].lines;
      first.length ? (first[0].text = h) : first.push({ text: h, note: "" });
    });
    return toast("Hook set as the first line", "ok");
  }
  if (d.pick != null) {
    const lines = [].concat(res.rewrite.out.options[+d.pick]).map((t) => ({ text: String(t), note: "" }));
    const { b, l } = res.rewrite.at;
    if (!doc.beats[b]?.lines[l]) return;
    change(() => doc.beats[b].lines.splice(l, 1, ...lines));
    focus = { b, l };
    res.rewrite = {};
    drawAI();
    return;
  }
  if (d.addbeat != null) {
    const nb = res.draft.out.beats[+d.addbeat];
    change(() => doc.beats.push({ name: String(nb.name), lines: [{ text: "", note: "" }] }));
    return focusLine(doc.beats.length - 1, 0);
  }
  if (d.ask === "rewrite") {
    const l = doc.beats[focus.b].lines[focus.l];
    const instr = root.querySelector("#aiInstr")?.value.trim();
    return ask("rewrite", { keep: { at: { ...focus } }, body: { text: l.text, how, instruction: instr } });
  }
  if (d.ask === "ideate" && !doc.brief.trim() && !doc.beats.some((b) => b.lines.some((x) => x.text.trim())))
    return toast("Say what the reel is about first", "warn"), root.querySelector("#aiBrief")?.focus();
  if (d.ask) return ask(d.ask, { body: { brief: doc.brief } });

  switch (d.act) {
    case "reload": return load(S.pid);
    case "setup": return window.reeldeckAISettings?.();
    case "send": return sendToVoice();
    case "snap": return snap(el);
    case "addbeat":
      change(() => doc.beats.push(blank()));
      return root.querySelector(`#beat-${doc.beats.length - 1} input`)?.focus();
    case "up": case "down": {
      const j = at.b + (d.act === "up" ? -1 : 1);
      return change(() => { [doc.beats[at.b], doc.beats[j]] = [doc.beats[j], doc.beats[at.b]]; });
    }
    case "delbeat":
      return change(() => doc.beats.splice(at.b, 1));
    case "addline":
      change(() => doc.beats[at.b].lines.push({ text: "", note: "" }));
      return focusLine(at.b, doc.beats[at.b].lines.length - 1);
    case "delline":
      return change(() => doc.beats[at.b].lines.splice(at.l, 1));
    case "rewrite":
      focus = { b: at.b, l: at.l }; tab = "rewrite";
      drawBeats(); drawAI();
      return root.querySelector(".ai")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    case "usedraft": return useDraft(el);
    case "next": {
      const b = doc.beats[at.b];
      el.classList.add("busy");
      const r = await api.assist({ mode: "next", script: doc, target: doc.target, beat: b.name || `Beat ${at.b + 1}` });
      el.classList.remove("busy");
      if (r.error) return toast(r.error, "error");
      const add = (r.lines || []).map((t) => ({ text: String(t), note: "" }));
      if (!add.length) return toast("Nothing came back. Try again?", "warn");
      change(() => { b.lines = b.lines.filter((x) => x.text.trim()).concat(add); });
      add.forEach((_, k) => lineEl(at.b, b.lines.length - add.length + k)?.classList.add("picked"));
      return;
    }
  }
}

async function snap(el, label) {
  if (timer && !(await saveNow())) return false;
  if (!doc.updated) await saveNow();   // a never-saved script has nothing on disk to copy
  el?.classList.add("busy");
  const r = await api.snapshotScript(pid, label ?? root.querySelector("#swLabel").value.trim());
  el?.classList.remove("busy");
  if (r.error) { toast(r.error, "error"); return false; }
  if (!label) root.querySelector("#swLabel").value = "";
  versions = (await api.getScript(pid)).versions || versions;
  drawVersions();
  return true;
}

async function restore(id, el) {
  if (!(await snap(null, "Before restore"))) return;
  el.classList.add("busy");
  const r = await api.scriptVersion(pid, id);
  if (r.error) { el.classList.remove("busy"); return toast(r.error, "error"); }
  doc = r.script;
  draw();
  await saveNow();
  toast("Restored. The script you had is kept as a version.", "ok");
}

async function useDraft(el) {
  const o = res.draft.out;
  el.classList.add("busy");
  const ok = await snap(null, "Before AI draft");
  el.classList.remove("busy");
  if (!ok) return;
  doc.title ||= String(o.title || "");
  doc.beats = o.beats.map((b) => ({ name: String(b.name || ""), lines: (b.lines || []).map((t) => ({ text: String(t), note: "" })) }));
  res.draft = {};
  draw();
  dirty();
}
