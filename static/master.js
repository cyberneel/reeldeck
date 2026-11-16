// The lines page (/master): re-record a reel's voiceover from a phone, one
// line at a time. Hear the TTS read, read it yourself against the Voice tab's
// pacing bar, hear yourself back, and see how your delivery differs from the
// TTS's. Every take is also kept in the delivery dataset (delivery.py), so the
// gap can be studied, and a model tuned on it, later.
import { api } from "./api.js";
import { S, subscribe, openProject, renderLine, onToast, toast } from "./store.js";
import * as REC from "./rec.js";
import { keepTake, room, bar, tickBar, PAD } from "./voice.js";

const $ = (s) => document.querySelector(s);
const hear = new Audio();     // one element: iOS lets it play from code once a tap has
let i = 0, count = 0, countOff = null, t0 = 0, raf = 0;
let rows = {};                // take file -> its delivery row
const asked = new Set();      // takes sent off to be measured
const KEY = "reeldeck.lines"; // this device: the line you were on, per reel
const spot = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };
const keep = (o) => { try { localStorage.setItem(KEY, JSON.stringify({ ...spot(), ...o })); } catch {} };
const busy = () => REC.recording() || !!countOff || count > 0;

document.body.className = "lines";
document.body.innerHTML = `
  <div id="mTop"><select id="mProj"></select><span id="mN" class="stat"></span><a id="mEd" href="/">editor</a></div>
  <div id="mDots"></div>
  <div id="mLine">
    <div id="mText"></div>
    <div id="mBar"></div>
    <div class="row" id="mPlay"><button id="mTts"></button><button id="mYou"></button></div>
    <svg id="mCurve" viewBox="0 0 100 40" preserveAspectRatio="none"></svg>
    <table id="mCmp"></table>
  </div>
  <div id="mNav"><button id="mPrev">◀</button><button id="mRec">●</button><button id="mNext">▶</button></div>
  <div id="toasts"></div>`;

onToast((msg, kind) => {
  const el = document.createElement("div");
  el.className = "toast" + (kind && kind !== "info" ? " " + kind : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4000);
});

const lines = () => S.manifest?.vo?.lines || [];
const take = (j) => REC.takes().findLast((t) => t.line === j && !t.mute);
// the TTS read on disk for the line's current words, if there is one
function tts(j) {
  const meta = S.voLines[j];
  return meta && S.voReady[j] !== false && meta.text === lines()[j]?.text ? meta : null;
}

function go(j) {
  if (busy() || !lines().length) return;
  i = Math.max(0, Math.min(lines().length - 1, j));
  keep({ [S.pid]: i });
  hear.pause();
  draw();
}

function play(url) {
  hear.src = url;
  hear.play().catch(() => toast("tap ▶ again"));
}

async function load(pid) {
  hear.pause();
  history.replaceState(null, "", `?p=${pid}`);
  keep({ pid });
  $("#mEd").href = `/?p=${pid}`;
  // before the project: its first draw asks for any take with no row yet
  rows = (await api.deliveries(pid)).rows || {};
  await openProject(pid);
  if (S.loadError) toast(S.loadError, "error");
  i = Math.min(spot()[pid] ?? 0, Math.max(0, lines().length - 1));
  draw();
}

// ---- draw ----
const fmt = (v, dp = 1, unit = "") => (v == null ? "–" : (+v).toFixed(dp) + unit);
const arrow = (v) => (v == null ? "" : v > 1 ? " ↗" : v < -1 ? " ↘" : " →");
const CMP = [
  ["length", "span", 1, "s"], ["pace", "rate", 1, " syl/s"], ["pauses", "pauses", 0],
  ["pitch range", "st_range", 1, " st"], ["ends", "end", 1, " st"], ["emphasis", "emph", 0],
];

// pitch over the line, in semitones off each read's own median — the shape of
// the two reads, not the voice — with the loud peaks ticked along the bottom
function curve(d, cls) {
  if (!d?.st?.length) return "";
  let p = "", pen = false;
  d.st.forEach((v, k) => {
    if (v == null) return (pen = false);
    p += `${pen ? "L" : "M"}${((k / Math.max(1, d.st.length - 1)) * 100).toFixed(1)} ${(20 - 2 * v).toFixed(1)}`;
    pen = true;
  });
  return `<path class="${cls}" d="${p}"/><path class="${cls}" d="${(d.emph_at || []).map((a) => `M${a * 100} 40V33`).join("")}"/>`;
}

function draw() {
  if (!S.loaded) return;
  const ls = lines(), l = ls[i], busyNow = busy();
  $("#mN").textContent = ls.length ? `${i + 1} / ${ls.length}` : "";
  $("#mDots").innerHTML = ls.map((_, j) =>
    `<button data-j="${j}" class="${take(j) ? "done" : ""}${j === i ? " cur" : ""}"></button>`).join("");
  ["#mPrev", "#mNext"].forEach((s) => ($(s).disabled = busyNow));
  $("#mPrev").disabled ||= i === 0;
  $("#mNext").disabled ||= i >= ls.length - 1;
  $("#mRec").textContent = count ? count : REC.recording() ? "■" : "●";
  $("#mRec").classList.toggle("on", busyNow);
  $("#mRec").disabled = !l;
  if (!l) {
    $("#mText").textContent = "this reel has no script yet — write it in the editor";
    ["#mBar", "#mPlay", "#mCurve", "#mCmp"].forEach((s) => ($(s).innerHTML = ""));
    return;
  }
  $("#mText").textContent = l.text || "(empty line)";

  const { aim, slot } = room(i), t = take(i), meta = tts(i);
  const said = t ? Math.max(0, t.dur - PAD) : 0;
  $("#mBar").innerHTML = count ? `<div class="recbar count"><span>get ready… ${count}</span></div>`
    : bar(aim, slot, REC.recording() ? 0 : said);

  const rendering = S.job.running && S.job.step === `line ${i + 1}`;
  $("#mPlay").innerHTML = (meta
    ? `<button id="mTts">▶ TTS ${meta.dur.toFixed(1)}s</button>`
    : `<button id="mTts" data-render ${S.job.running ? "disabled" : ""}>${rendering ? "rendering…" : "render TTS"}</button>`) +
    `<button id="mYou" ${t ? "" : "disabled"}>${t ? `▶ you ${said.toFixed(1)}s` : "no take yet"}</button>`;

  const r = t && rows[t.file];
  if (t && !r && !asked.has(S.pid + t.file)) measure(i, t.file);   // takes from before this page
  $("#mCurve").innerHTML = r ? curve(r.tts, "tts") + curve(r.you, "you") : "";
  $("#mCmp").innerHTML = !t ? "" : !r ? `<tr><th>measuring your read…</th></tr>`
    : `<tr><th></th><th>TTS</th><th class="you">you</th></tr>` + CMP.map(([name, k, dp, unit]) =>
      `<tr><th>${name}</th><td>${fmt(r.tts[k], dp, unit)}${k === "end" ? arrow(r.tts[k]) : ""}</td>
        <td class="you">${fmt(r.you[k], dp, unit)}${k === "end" ? arrow(r.you[k]) : ""}</td></tr>`).join("");
}

async function measure(j, file) {
  asked.add(S.pid + file);
  const r = await api.delivery(S.pid, j, file);
  if (r.error) return toast(r.error, "warn");
  rows[file] = r;
  draw();
}

// ---- record: the Voice tab's ●, one line at a time ----
function countIn() {
  return new Promise((res, rej) => {
    const step = (n) => {
      count = n; draw();
      if (!n) { countOff = null; return res(); }
      const tm = setTimeout(() => step(n - 1), 1000);
      countOff = () => { clearTimeout(tm); count = 0; countOff = null; draw(); rej(null); };
    };
    step(3);
  });
}

function tick() {
  const el = $("#mBar .recbar");
  if (el) tickBar(el, (performance.now() - t0) / 1000);
  raf = requestAnimationFrame(tick);
}

async function rec() {
  if (countOff) return countOff();
  if (REC.recording()) return REC.stop(null);
  hear.pause();
  const j = i;   // the line this take is for
  try {
    await REC.start(null, (row) => onRec(j, row), room(j).start, countIn);
  } catch (e) {
    if (e) toast(e.message || String(e), "error");
  }
}

function onRec(j, row) {
  if (!row) {   // the mic opened or closed
    cancelAnimationFrame(raf);
    if (REC.recording()) { t0 = performance.now(); raf = requestAnimationFrame(tick); }
    return draw();
  }
  if (!keepTake(j, row)) { draw(); return toast("no voice heard — check the mic and read it again", "warn"); }
  draw();
  measure(j, row.file);
}

// ---- wiring ----
$("#mProj").onchange = (e) => !busy() && load(e.target.value);
$("#mPrev").onclick = () => go(i - 1);
$("#mNext").onclick = () => go(i + 1);
$("#mRec").onclick = rec;
$("#mDots").onclick = (e) => e.target.dataset.j != null && go(+e.target.dataset.j);
$("#mPlay").onclick = (e) => {
  if (e.target.id === "mYou") { const t = take(i); if (t) play(api.mediaUrl(S.pid, t.file)); }
  else if (e.target.dataset.render != null) renderLine(i, false);
  else if (e.target.id === "mTts") { const m = tts(i); if (m) play(api.outUrl(S.pid, S.manifest, "vo/" + m.file)); }
};
// swipe the line sideways to step
let x0 = null;
$("#mLine").addEventListener("touchstart", (e) => (x0 = e.touches[0].clientX), { passive: true });
$("#mLine").addEventListener("touchend", (e) => {
  const dx = e.changedTouches[0].clientX - (x0 ?? e.changedTouches[0].clientX);
  if (Math.abs(dx) > 60) go(i + (dx < 0 ? 1 : -1));
  x0 = null;
});
subscribe(draw);

(async () => {
  const ps = ((await api.listProjects()).projects || []).filter((p) => !p.missing);
  const want = new URLSearchParams(location.search).get("p") || spot().pid;
  const pid = ps.find((p) => p.id === want)?.id || ps[0]?.id;
  $("#mProj").innerHTML = ps.map((p) => `<option value="${p.id}"${p.id === pid ? " selected" : ""}>${p.name.replace(/</g, "&lt;")}</option>`).join("");
  if (pid) load(pid);
})();
