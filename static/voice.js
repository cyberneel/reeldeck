// The Script tab: write the voiceover, one dedicated surface instead of a
// sidebar table. Paste a script and split it into lines, tune each line, render
// through swaram, and see the same drift numbers the Timeline shows — so you
// can fix pacing here without switching tabs.
import { api } from "./api.js";
import { S, subscribe, emit, autosave, renderLine, run, toast } from "./store.js";
import { isTyping, preserveFocus, setValue } from "./dom.js";
import * as L from "./layout.js";
import * as REC from "./rec.js";

let els = {};
// the line being read into the mic (● on its row), and when the mic opened
let recI = null, recT0 = 0, raf = 0, count = 0, countOff = null;
const COUNT = 3;   // seconds to get ready after ● before the mic records
// trim_to_voice keeps 80ms before the first word and 200ms after the last
export const PAD = 0.28;
// mirrors pipeline.TONE: what a slider reads when reel.json leaves it unset
const TONE = { denoise: 60, room: 50, comp: 50, crisp: 50, deess: 0 };
const TONE_TIP = {
  denoise: "hiss and hum under the voice",
  room: "the boom of a bare room, and the ring after each word",
  comp: "studio compression: loud and soft words brought together",
  crisp: "presence and air, so it cuts through on a phone speaker",
  deess: "softens a harsh read: cuts 3-6kHz and tames sharp \"s\" sounds",
};
// one player for previews: iOS lets it play from code once a tap has played it
const hearEl = new Audio();
let toneFile = null, ready = null, inFlight = false, again = false;

export function mount(root) {
  root.innerHTML = `
    <div id="tlLayout">
      <div>
        <section>
          <h2>Paste a script <span class="sub">one sentence per line, blank line = longer pause</span></h2>
          <textarea id="paste" rows="5" placeholder="One terminal, one agent. Close the tab and it dies with it.
Claude can be better.

So I built a deck for them."></textarea>
          <div class="row" style="margin-top:9px">
            <button id="split">Split into lines</button>
            <span class="hint" style="margin:0">Appends to the table below — it does not replace existing lines.</span>
          </div>
        </section>

        <section>
          <h2>Lines <span class="sub" id="scriptSum"></span></h2>
          <table id="lines">
            <thead><tr><th style="width:20px">#</th><th>line</th>
              <th style="width:104px">beat</th><th style="width:52px">off</th>
              <th style="width:50px">exag</th><th style="width:50px">cfg</th>
              <th style="width:58px">pause</th><th style="width:56px">seed</th>
              <th style="width:110px">start → target</th><th style="width:86px"></th></tr></thead>
            <tbody id="linesBody"></tbody>
          </table>
          <div class="row" style="margin-top:9px">
            <button id="addLine">+ line</button>
            <button id="bFit">Fit pauses</button>
            <button id="bVoAll" class="go">Render all + mux</button>
            <span class="hint" style="margin:0">● counts 3, then reads a line into the mic. <b>aim</b> is the
              TTS length, <b>max</b> runs to where the next line starts.</span>
          </div>
        </section>
      </div>

      <div>
        <section>
          <h2>Voice</h2>
          <div class="fld"><label>sample</label><select id="voice"></select></div>
          <div class="row">
            <div class="fld" style="flex:1"><label>temperature</label><input class="num" id="temp"></div>
            <div class="fld" style="flex:1"><label>default seed</label><input class="num" id="seed"></div>
          </div>
          <div class="fld"><label>swaram server</label><input id="voServer"></div>
          <div class="fld"><label>microphone <span class="sub">this device</span></label><select id="mic"></select></div>
          <div class="row"><button class="mini" id="clean"></button>
            <span class="hint" style="margin:0">Your takes: hiss taken down, the room
              gated out between words, level matched to the TTS. The raw recording is kept.</span></div>
          <div id="tone">
            ${Object.keys(TONE).map((k) => `<label class="bus" title="${TONE_TIP[k]}"><span>${k}</span>
              <input type="range" min="0" max="100" step="5" data-tone="${k}"><output></output></label>`).join("")}
            <div class="row"><select id="toneTake"></select>
              <button class="mini" id="hearRaw">▶ raw</button>
              <button class="mini" id="hearClean">▶ cleaned</button>
              <button class="mini" id="toneReset">reset</button></div>
            <div class="hint">0 turns a stage off. Let go of a slider to hear the take
              with it; Update puts it in the reel.</div>
          </div>
          <div class="hint">Lines are cached per (text, exag, cfg, seed) — editing one
            line re-renders only that line. Pauses cost nothing to change; they are
            applied here, not by the TTS.</div>
        </section>
        <section>
          <h2>Log <span class="sub" id="running"></span></h2>
          <pre class="log" id="log">idle</pre>
        </section>
      </div>
    </div>`;

  els = {
    paste: root.querySelector("#paste"),
    body: root.querySelector("#linesBody"),
    scriptSum: root.querySelector("#scriptSum"),
    voice: root.querySelector("#voice"), temp: root.querySelector("#temp"),
    seed: root.querySelector("#seed"), voServer: root.querySelector("#voServer"),
    log: root.querySelector("#log"), running: root.querySelector("#running"),
    clean: root.querySelector("#clean"),
    tone: root.querySelector("#tone"), toneTake: root.querySelector("#toneTake"),
    mic: root.querySelector("#mic"),
  };
  els.mic.onchange = () => REC.setMic(els.mic.value, els.mic.selectedOptions[0].dataset.label || "");
  navigator.mediaDevices?.addEventListener?.("devicechange", drawMics);
  drawMics();

  root.querySelector("#split").onclick = () => splitScript(els.paste.value);
  root.querySelector("#addLine").onclick = () => {
    S.manifest.vo.lines.push({ text: "", exag: 0.45, cfg: 0.45, pause: 250, beat: "" });
    autosave(); emit();
  };
  root.querySelector("#bFit").onclick = () => run(["fit", "vo", "mix", "mux", "align"]);
  root.querySelector("#bVoAll").onclick = () => run(["vo", "mix", "mux", "align"]);

  els.voice.addEventListener("change", (e) => { S.manifest.vo.voice = e.target.value; autosave(); });
  els.temp.addEventListener("input", (e) => { S.manifest.vo.temperature = +e.target.value || 0.8; autosave(); });
  els.seed.addEventListener("input", (e) => { S.manifest.vo.seed = e.target.value === "" ? 0 : +e.target.value; autosave(); });
  els.voServer.addEventListener("input", (e) => { S.manifest.vo.server = e.target.value; autosave(); });
  els.clean.onclick = () => { S.manifest.vo.clean = S.manifest.vo.clean === false; autosave(); emit(); };
  els.tone.addEventListener("input", (e) => {
    const k = e.target.dataset.tone;
    if (k) e.target.nextElementSibling.value = e.target.value;
  });
  els.tone.addEventListener("change", (e) => {
    const k = e.target.dataset.tone;
    if (k) { (S.manifest.vo.tone = S.manifest.vo.tone || {})[k] = +e.target.value; autosave(); }
    else toneFile = els.toneTake.value;
    preview();
  });
  root.querySelector("#hearRaw").onclick = () => toneFile && play(api.mediaUrl(S.pid, toneFile));
  root.querySelector("#hearClean").onclick = () =>
    ready?.key === toneKey() ? play(ready.url) : preview();
  root.querySelector("#toneReset").onclick = () => { delete S.manifest.vo.tone; autosave(); emit(); preview(); };

  root.addEventListener("input", onInput);
  root.addEventListener("change", onChange);
  root.addEventListener("focusout", onChange);
  root.addEventListener("click", onClick);

  subscribe(render);
  render();
}

// Mirrors swaram's own split() (sentence boundaries, blank line = paragraph
// break -> longer pause) so scripts split identically whether pasted here or
// in swaram directly. Kept local rather than calling swaram's /api/split over
// the network — that's a cross-origin request to a different port with no
// guarantee of CORS, for ten lines of regex that are simple to duplicate.
function splitScript(text) {
  if (!text.trim()) return;
  const paras = text.trim().split(/\n\s*\n/);
  const rows = [];
  for (const para of paras) {
    const sentences = para.replace(/\n/g, " ").split(/(?<=[.!?])\s+/).filter((s) => s.trim());
    for (const s of sentences) rows.push({ text: s.trim(), exag: 0.5, cfg: 0.5, pause: 250, beat: "" });
    if (rows.length) rows[rows.length - 1].pause = 700; // paragraph break
  }
  if (rows.length) rows[rows.length - 1].pause = 0;
  S.manifest.vo.lines.push(...rows);
  els.paste.value = "";
  autosave(); emit();
  toast(`added ${rows.length} line(s)`);
}

export function computeDrift() {
  const m = S.manifest;
  const plan = S.plan;
  const videoL = L.videoLayout(m, plan);
  const voL = L.voLayout(m, S.voLines);
  const targets = L.beatTargets(m, videoL);
  const last = videoL[videoL.length - 1];
  return { voL, targets, end: last ? last.start + last.len : null };
}

function render() {
  if (!S.manifest) return;
  const m = S.manifest;
  els.voice.innerHTML = (S.voices.length ? S.voices : [m.vo.voice || ""])
    .map((v) => `<option${v === m.vo.voice ? " selected" : ""}>${v}</option>`).join("");
  setValue(els.temp, m.vo.temperature ?? 0.8);
  setValue(els.seed, m.vo.seed ?? 0);
  setValue(els.voServer, m.vo.server || "");
  els.clean.textContent = m.vo.clean === false ? "clean takes: off" : "clean takes: on";
  drawTone(m);

  // Leave the table alone while it is being typed into. Rebuilding it would
  // replace the field mid-word — losing the caret, the undo history, and any
  // half-typed number — and every keystroke emits, as does the autosave 500ms
  // later. The `change` handler redraws once the edit is committed.
  if (isTyping(els.body)) return drawSummary(L.voLayout(m, S.voLines), m);

  const { voL, targets, end } = computeDrift();
  const opts = m.segments.map((s) => s.name);
  preserveFocus(els.body, () => { els.body.innerHTML = m.vo.lines.map((l, i) => {
    const v = voL[i];
    const d = targets[i] == null ? null : v.start - targets[i];
    const cell = `${v.start.toFixed(2)}${targets[i] == null ? "" : " → " + targets[i].toFixed(2)}` +
      (d == null ? "" : ` <span class="${L.driftClass(d)}">${d > 0 ? "+" : ""}${d.toFixed(2)}</span>`);
    const meta = S.voLines[i];
    // the room this line has: up to the next line, or for the last one, the end of the cut
    const slot = Math.max(v.dur, (i + 1 < voL.length ? voL[i + 1].start : end ?? v.start + v.dur) - v.start);
    return `<tr>
      <td class="stat">${i + 1}</td>
      <td><textarea rows="2" data-k="text" data-i="${i}">${l.text}</textarea></td>
      <td data-l="beat"><select data-k="beat" data-i="${i}"><option value=""></option>
        ${opts.map((o) => `<option${o === l.beat ? " selected" : ""}>${o}</option>`).join("")}</select></td>
      <td data-l="offset"><input class="num" value="${l.offset ?? ""}" data-k="offset" data-i="${i}"></td>
      <td data-l="exag"><input class="num" value="${l.exag ?? 0.5}" data-k="exag" data-i="${i}"></td>
      <td data-l="cfg"><input class="num" value="${l.cfg ?? 0.5}" data-k="cfg" data-i="${i}"></td>
      <td data-l="pause"><input class="num" value="${l.pause ?? 250}" data-k="pause" data-i="${i}"></td>
      <td data-l="seed"><input class="num" value="${l.seed ?? ""}" data-k="seed" data-i="${i}"></td>
      <td class="stat">${l.mute ? '<span class="badge o" title="muted in the mix: a take carries this line">TTS muted</span> ' : ""}${S.voReady[i] === false
        ? '<span class="badge o" title="no audio for this text/seed yet — Update or ↻ will render it">needs voice</span>'
        : cell}${aimCell(i, slot, meta)}</td>
      <td>${S.job.running && S.job.step === `line ${i + 1}`
        ? '<span class="rendering">rendering</span>'
        : `<button class="mini rec${recI === i ? " on" : ""}" data-rec="${i}" title="read this line into the mic">${recI === i ? "■" : "●"}</button>
        ${meta ? `<button class="mini" data-play="${i}">▶</button>` : ""}
        <button class="mini" data-roll="${i}" title="new take of the same words (bumps the seed)">↻</button>
        <button class="mini" data-mv="${i}" data-d="-1">↑</button>
        <button class="mini" data-mv="${i}" data-d="1">↓</button>
        <button class="mini" data-del="${i}">✕</button>`}</td>
    </tr>`;
  }).join(""); });

  drawSummary(voL, m);
}

// What to hit: the TTS read's length (aim) and the room before the next line
// (max). While this line records, a live bar; after, how long you actually ran.
function aimCell(i, slot, meta) {
  const aim = meta ? meta.dur : slot;
  if (recI === i && count) return `<div class="recbar count"><span>get ready… ${count}</span></div>`;
  if (recI === i) return bar(aim, slot);
  const k = REC.takes().findLastIndex((t) => t.line === i && !t.mute);
  const said = k < 0 ? 0 : Math.max(0, +REC.takes()[k].dur - PAD);
  const you = k < 0 ? "" : ` · <button class="mini ${said > slot ? "bad" : said > aim * 1.1 ? "warn" : "ok"}"
    data-hear="${k}" title="hear your take (raw — Update mixes in the cleaned one)">you ${said.toFixed(1)}s</button>`;
  return `<div class="aim">${meta ? `aim ${aim.toFixed(1)}s · ` : ""}max ${slot.toFixed(1)}s${you}</div>`;
}

// The pacing bar (here and on the lines page): the room up to the next line,
// the TTS read's length marked on it, and how far you have run — `s` seconds.
export function bar(aim, slot, s = 0) {
  return `<div class="recbar${s > slot ? " over" : s > aim ? " long" : ""}" data-aim="${aim}" data-slot="${slot}">
    <i style="width:${Math.min(100, (s / slot) * 100)}%"></i>
    <b style="left:${Math.min(100, (aim / slot) * 100)}%"></b><span>${s.toFixed(1)}s</span></div>`;
}
export function tickBar(el, s) {
  el.querySelector("i").style.width = `${Math.min(100, (s / +el.dataset.slot) * 100)}%`;
  el.classList.toggle("long", s > +el.dataset.aim);
  el.classList.toggle("over", s > +el.dataset.slot);
  el.querySelector("span").textContent = `${s.toFixed(1)}s`;
}

// line i's room: where it starts, up to the next line (or the end of the cut),
// and the TTS read's length to aim for
export function room(i) {
  const { voL, end } = computeDrift(), v = voL[i];
  const slot = Math.max(v.dur, (i + 1 < voL.length ? voL[i + 1].start : end ?? v.start + v.dur) - v.start);
  return { start: v.start, slot, aim: S.voLines[i]?.dur ?? slot };
}

// ponytail: the timer starts when recording does, so it counts your lead-in too;
// the verdict after the take is on the trimmed read, which is the real number
function tick() {
  const el = els.body.querySelector(".recbar");
  if (el) tickBar(el, (performance.now() - recT0) / 1000);
  raf = requestAnimationFrame(tick);
}

async function recLine(i) {
  if (countOff) return countOff();               // ■ during the count-in calls it off
  if (REC.recording()) return REC.stop(null);   // ■, on whichever row
  try {
    await REC.start(null, (row) => onRec(i, row), room(i).start, () => countIn(i));
  } catch (e) {
    if (e) toast(e.message || String(e));
  }
}

// COUNT seconds to get ready; the mic is open, it records when this resolves
function countIn(i) {
  recI = i;
  return new Promise((res, rej) => {
    const step = (n) => {
      count = n; emit();
      if (!n) { countOff = null; return res(); }
      const t = setTimeout(() => step(n - 1), 1000);
      countOff = () => { clearTimeout(t); count = 0; recI = countOff = null; emit(); rej(null); };
    };
    step(COUNT);
  });
}

// rec.js calls back with no row as the mic opens and closes, then with the
// saved take. ponytail: the take is pinned to where the line starts NOW; if
// the lines re-time later, re-record or drag it on the Timeline.
function onRec(i, row) {
  if (!row) {
    cancelAnimationFrame(raf);
    recI = REC.recording() ? i : null;
    if (recI != null) { recT0 = performance.now(); raf = requestAnimationFrame(tick); drawMics(); }
    return emit();
  }
  const heard = keepTake(i, row);
  emit();
  if (!heard) return toast(`line ${i + 1}: no voice heard — check the mic and read it again`);
  api.delivery(S.pid, i, row.file);   // into the dataset; the lines page shows it
  const meta = S.voLines[i], said = Math.max(0, row.dur - PAD), { slot } = room(i);
  toast(`line ${i + 1}: you ${said.toFixed(1)}s` + (meta ? ` · TTS ${meta.dur.toFixed(1)}s` : "") +
        (said > slot ? ` — ${(said - slot).toFixed(1)}s into the next line` : "") + " · Update to hear it in the reel");
}

// A take read for line i joins the reel. The newest read of a line wins; older
// ones stay on the Timeline, muted. False for a false start, which changes
// nothing: whatever played on this line still does.
export function keepTake(i, row) {
  const heard = row.voice !== false;
  delete row.voice;
  row.line = i;
  if (heard) {
    REC.takes().forEach((t) => { if (t !== row && t.line === i) t.mute = true; });
    S.manifest.vo.lines[i].mute = true;
  } else row.mute = true;
  autosave();
  return heard;
}

function tone() {
  const t = S.manifest.vo.tone || {};
  return Object.fromEntries(Object.keys(TONE).map((k) => [k, t[k] ?? TONE[k]]));
}
const toneKey = () => toneFile + JSON.stringify(tone());

function drawTone(m) {
  const t = tone();
  els.tone.querySelectorAll("[data-tone]").forEach((r) => {
    setValue(r, t[r.dataset.tone]);
    r.nextElementSibling.value = r.value;
  });
  const takes = REC.takes();
  if (!takes.some((x) => x.file === toneFile)) toneFile = takes.findLast((x) => !x.mute)?.file ?? takes.at(-1)?.file ?? null;
  els.toneTake.innerHTML = takes.length ? takes.map((x) => `<option value="${x.file}"${x.file === toneFile ? " selected" : ""}>
    ${x.line != null ? `line ${x.line + 1} · ` : ""}${x.file.split("/").pop()}${x.mute ? " (muted)" : ""}</option>`).join("")
    : "<option>no takes yet — ● a line</option>";
}

async function drawMics() {
  const ms = await REC.mics(), { id, label } = REC.micPick();
  els.mic.replaceChildren(new Option("default", ""), ...ms.map((d, i) => {
    const o = new Option(d.label || `mic ${i + 1} (record once to see its name)`, d.deviceId, false,
                         d.deviceId === id || (!!label && d.label === label));
    o.dataset.label = d.label;
    return o;
  }));
}

function play(url) {
  hearEl.src = url;
  hearEl.play().catch(() => toast("tap ▶ cleaned to hear it"));
}

// Render the picked take with the sliders as they stand and play it. One at a
// time; a slider let go mid-render is picked up by the next pass.
async function preview() {
  if (!toneFile) return;
  if (inFlight) { again = true; return; }
  inFlight = true; again = false;
  const key = toneKey();
  const r = await api.cleanPreview(S.pid, toneFile, tone());
  inFlight = false;
  if (again) return preview();
  if (r.error) return toast(r.error, "warn");
  ready = { key, url: api.mediaUrl(S.pid, r.file, r.v) };
  play(ready.url);
}

function drawSummary(voL, m) {
  const total = voL.length ? voL[voL.length - 1].start + voL[voL.length - 1].dur : 0;
  els.scriptSum.textContent = `${m.vo.lines.length} lines · ${total.toFixed(1)}s`;
  els.log.textContent = (S.job.log || []).slice().reverse().join("\n") || "idle";
  els.running.textContent = S.job.running ? "· " + S.job.step : "";
  document.querySelectorAll("#pane-script button").forEach((b) => (b.disabled = S.job.running));
}

function onInput(e) {
  const t = e.target, i = t.dataset.i, k = t.dataset.k;
  if (i == null || !k) return;
  const l = S.manifest.vo.lines[i];
  if (!l) return;
  const isText = k === "text" || k === "beat";
  l[k] = isText ? t.value : (t.value === "" ? null : Number(t.value));
  if (l[k] == null) delete l[k];
  autosave();
  // deliberately no emit(): the field already shows what was typed, and a
  // redraw per keystroke is what made this table impossible to type in
}

// The edit is settled (dropdown picked, or focus left the field), so redraw with
// the drift it produced. Deferred a tick: `change` fires while the field still
// holds focus, and render() would correctly treat that as "still typing" and
// skip the rebuild.
function onChange() { setTimeout(emit, 0); }

function onClick(e) {
  const b = e.target;
  const lines = S.manifest.vo.lines;
  if (b.dataset.mv != null) {
    const i = +b.dataset.mv, j = i + +b.dataset.d;
    if (j >= 0 && j < lines.length) { [lines[i], lines[j]] = [lines[j], lines[i]]; autosave(); emit(); }
  }
  if (b.dataset.del != null) { lines.splice(+b.dataset.del, 1); autosave(); emit(); }
  if (b.dataset.roll != null) renderLine(+b.dataset.roll, true);
  if (b.dataset.rec != null) recLine(+b.dataset.rec);
  if (b.dataset.hear != null) {
    const t = REC.takes()[+b.dataset.hear];
    if (t) new Audio(api.mediaUrl(S.pid, t.file)).play();
  }
  if (b.dataset.play != null) {
    const meta = S.voLines[+b.dataset.play];
    if (meta) new Audio(api.outUrl(S.pid, S.manifest, "vo/" + meta.file)).play();
  }
}
