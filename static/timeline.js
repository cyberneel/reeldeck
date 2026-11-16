// The Timeline tab: drag/trim/reorder/blade over two tracks (video, voice, sfx),
// a shared ruler, and a live composited preview above it. This is the "home"
// surface — everything else (Script, Clips) feeds into segments and lines that
// ultimately show up here.
import { api } from "./api.js";
import * as FX from "./fx.js";
import { S, subscribe, emit, autosave, run, update, toast, renderLine } from "./store.js";
import { Player } from "./player.js";
import * as REC from "./rec.js";
import { isTyping, preserveFocus } from "./dom.js";
import * as L from "./layout.js";

const COLORS = ["#d97757", "#7ab27a", "#6f9dc7", "#c9a227", "#a97bc4", "#59b3a9"];
let PPS = 26;      // pixels per second
let sel = null;    // {type:'seg'|'line'|'cue'|'fx', i}
let fxSurface = null;
let player = null;
let els = {};
let rootEl = null;
let dragCtx = null;

export function mount(root) {
  rootEl = root;
  root.innerHTML = `
    <div id="tlLayout">
      <div>
        <section>
          <h2>Preview <span class="sub" id="tlPreviewSum"></span>
            <span class="grow"></span>
            <span class="badge" id="previewMode"></span>
            <button class="mini" id="fxDraw" title="drag a box on the picture">+ effect</button>
            <button class="mini" id="safeBtn" title="shade where Instagram's UI covers a posted reel">safe zone</button>
            <button class="mini" id="previewToggle">use live</button></h2>
          <div id="previewWrap">
            <div id="previewStage"></div>
            <div id="previewOverlay"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2">
              <circle cx="12" cy="12" r="10" stroke-opacity=".3"/>
              <path d="M12 2a10 10 0 0 1 10 10"/>
            </svg></div>
          </div>
          <div id="transport">
            <button id="goStart" class="icon" title="jump to start (Home)">⏮</button>
            <button id="stepBack" class="icon" title="one frame back (←)">◀|</button>
            <button id="playBtn" class="icon">▶</button>
            <button id="stepFwd" class="icon" title="one frame forward (→)">|▶</button>
            <button id="goEnd" class="icon" title="jump to end (End)">⏭</button>
            <button id="loopBtn" class="icon" title="loop playback, to see the end run into the start (L)">⟲</button>
            <button id="recBtn" class="icon rec" title="record a voice take over the picture from here (R)">●</button>
            <button id="ttsBtn" class="icon" title="mute the synthesized read and keep only your takes">TTS</button>
            <span class="tcode" id="tCode">0:00.00</span>
            <span class="fcount" id="fCount"></span>
          </div>
          <div class="tlkey" id="transportKeys"><kbd>space</kbd> play ·
            <kbd>←</kbd><kbd>→</kbd> frame · <kbd>shift</kbd> +1s ·
            <kbd>Home</kbd>/<kbd>End</kbd> · <kbd>S</kbd> split · <kbd>Del</kbd> remove ·
            <kbd>R</kbd> record · <kbd>L</kbd> loop</div>

          <div class="row" style="margin-top:14px">
            <h2 style="margin:0">Timeline <span class="sub" id="tlSum"></span></h2>
            <span class="grow"></span>
            <select id="tlStatus" title="ready = it shows in the Publish tab">
              <option value="draft">draft</option><option value="ready">ready</option></select>
            <span class="stat">zoom</span>
            <input type="range" id="zoom" min="8" max="90" value="26" style="width:110px">
          </div>
          <div id="tlwrap"><div id="tl">
            <div class="ruler" id="ruler"></div>
            <div class="track" id="tvid"><span class="tlabel">video</span></div>
            <div class="track audio" id="taud"><span class="tlabel">voice</span></div>
            <div class="track take" id="ttake"><span class="tlabel">takes</span></div>
            <div class="track sfx" id="tsfx"><span class="tlabel">sfx</span></div>
            <div class="track fx" id="tfx"><span class="tlabel">fx</span></div>
            <div id="playhead"></div>
          </div></div>
          <div class="hint">Drag a clip <b>body</b> to reorder, its <b>edge</b> to trim.
            The small diamond between clips is the crossfade — drag it, or double-click for a hard cut.
            <b>◆</b> segments are generated; their length retimes the animation (Clips tab renders it).
            Drag a voice clip to change the pause before it.</div>
        </section>
      </div>

      <div>
        <section id="insp">
          <h2>Selected <span class="sub" id="selKind"></span></h2>
          <div id="fields" class="hint">Click a clip, line, or cue.</div>
        </section>
        <section>
          <h2>Actions</h2>
          <button id="bUpdate" class="go wide">Update</button>
          <div class="hint" id="updWhat"></div>
          <details id="advSteps"><summary>run one step</summary>
            <div class="row" style="margin-top:8px">
              <button id="bFit" title="solve pauses so every line lands on its beat">Fit pauses</button>
              <button id="bClips">Clips</button></div>
            <div class="row" style="margin-top:7px"><button id="bAsm">Re-cut</button>
              <button id="bVo">Voice</button><button id="bMix">Mix</button><button id="bMux">Mux</button></div>
            <div class="row" style="margin-top:7px"><button id="bAll">Force everything</button></div>
            <div class="hint">Update already picks these for you. Reach in here to
              force a step that isn't stale — re-recording a live capture, say.</div>
          </details>
        </section>
        <section>
          <h2>Audio bus <span class="sub" id="audSum"></span></h2>
          <div class="bus"><span>voice</span><input type="range" id="busVoice" min="-24" max="12" step="0.5"><output id="busVoiceV"></output></div>
          <div class="bus"><span>sfx</span><input type="range" id="busSfx" min="-24" max="12" step="0.5"><output id="busSfxV"></output></div>
          <div class="bus"><span>music</span><input type="range" id="busMusic" min="-40" max="6" step="0.5"><output id="busMusicV"></output></div>
          <div class="fld" style="margin-top:9px"><label>music file (relative to project)</label>
            <input id="mSrc" placeholder="audio/bed.mp3"></div>
          <div class="row"><div class="fld" style="flex:1"><label>from</label><input class="num" id="mStart"></div>
            <div class="fld" style="flex:1"><label>duck dB</label><input class="num" id="mDuck"></div></div>
          <div class="row"><button id="addCue">+ cue</button></div>
          <div class="hint">Cues pin to beats like voice lines do. Sliders offset a
            whole bus at once, leaving each cue's own trim intact. <b>duck 0</b> disables ducking.</div>
        </section>
        <section>
          <h2>Log <span class="sub" id="running"></span></h2>
          <pre class="log" id="log">idle</pre>
        </section>
      </div>
    </div>`;

  els = {
    stage: root.querySelector("#previewStage"),
    overlay: root.querySelector("#previewOverlay"),
    playBtn: root.querySelector("#playBtn"),
    tCode: root.querySelector("#tCode"), fCount: root.querySelector("#fCount"),
    tlwrap: root.querySelector("#tlwrap"), tl: root.querySelector("#tl"),
    ruler: root.querySelector("#ruler"),
    tvid: root.querySelector("#tvid"), taud: root.querySelector("#taud"), tsfx: root.querySelector("#tsfx"), tfx: root.querySelector("#tfx"),
    ttake: root.querySelector("#ttake"), recBtn: root.querySelector("#recBtn"), ttsBtn: root.querySelector("#ttsBtn"),
    playhead: root.querySelector("#playhead"),
    zoom: root.querySelector("#zoom"), status: root.querySelector("#tlStatus"),
    tlSum: root.querySelector("#tlSum"), tlPreviewSum: root.querySelector("#tlPreviewSum"),
    fields: root.querySelector("#fields"), selKind: root.querySelector("#selKind"),
    log: root.querySelector("#log"), running: root.querySelector("#running"),
  };

  const audioEl = new Audio();
  audioEl.preload = "auto";
  player = new Player(els.stage, audioEl);
  fxSurface = FX.mountSurface(root.querySelector("#previewWrap"),
    { onChange: () => { autosave(); emit(); }, timeOf: () => player.t });
  // repaint the moment a box moves, so dragging feels like the effect itself
  // rather than an outline you have to imagine
  fxSurface._live = () =>
    FX.paintLive(fxSurface, player.t, player.visibleVideo(), !player.reelUrl);
  root.querySelector("#fxDraw").onclick = () => {
    FX.fxState.drawing = !FX.fxState.drawing;
    sel = null; FX.fxState.sel = null;
    toast(FX.fxState.drawing ? "drag a box on the picture" : "cancelled");
    emit();
  };
  // Instagram's UI over a posted reel, shaded red on the picture (safezone.py's
  // numbers). On the fx surface because that one tracks the letterboxed frame.
  const safe = document.createElement("div");
  safe.id = "safeZone";
  safe.innerHTML = '<i class="t"></i><i class="b"></i><i class="r"></i><i class="sl"></i><i class="sr"></i>';
  fxSurface.appendChild(safe);
  const safeBtn = root.querySelector("#safeBtn");
  const showSafe = (on) => {
    safe.classList.toggle("on", on); safeBtn.classList.toggle("on", on);
    try { localStorage.setItem("safeZone", on ? "1" : "0"); } catch {}
  };
  let safeOn = false;
  try { safeOn = localStorage.getItem("safeZone") === "1"; } catch {}
  showSafe(safeOn);
  safeBtn.onclick = () => showSafe(!safe.classList.contains("on"));

  player.onTime = (t) => {
    drawTimecode(t); movePlayhead(t);
    FX.draw(fxSurface, t);
    FX.paintLive(fxSurface, t, player.visibleVideo(), !player.reelUrl);
  };
  player.onEnd = () => setPlayIcon(false);

  root.querySelector("#previewToggle").onclick = () => {
    previewPref = previewPref === "live" ? "auto" : "live";
    syncPreviewMode();
  };
  root.querySelector("#goStart").onclick = () => seekTo(0);
  root.querySelector("#goEnd").onclick = () => seekTo(player.duration);
  root.querySelector("#stepBack").onclick = () => step(-1);
  root.querySelector("#stepFwd").onclick = () => step(1);
  root.querySelector("#loopBtn").onclick = toggleLoop;

  els.zoom.addEventListener("input", (e) => { PPS = +e.target.value; drawTimeline(); });
  els.ruler.addEventListener("mousedown", (e) => {
    const r = els.ruler.getBoundingClientRect();
    player.seek((e.clientX - r.left) / PPS);
  });
  els.playBtn.addEventListener("click", togglePlay);
  els.recBtn.addEventListener("click", toggleRecord);
  els.ttsBtn.addEventListener("click", () => {
    const vo = (S.manifest.vo = S.manifest.vo || {});
    vo.mute_tts = !vo.mute_tts;
    autosave(); emit();
  });
  // only "ready" reels show in the Publish tab; draft is the key left out
  els.status.addEventListener("change", () => {
    if (els.status.value === "ready") S.manifest.status = "ready"; else delete S.manifest.status;
    autosave();
  });

  root.querySelector("#bUpdate").onclick = update;
  root.querySelector("#bFit").onclick = () => run(["fit", "vo", "mix", "mux", "align"]);
  root.querySelector("#bAsm").onclick = () => run(["assemble", "align"]);
  root.querySelector("#bVo").onclick = () => run(["vo", "mix", "mux", "align"]);
  root.querySelector("#bMix").onclick = () => run(["mix", "mux"]);
  root.querySelector("#bMux").onclick = () => run(["mux"]);
  root.querySelector("#bClips").onclick = () => run(["clips", "assemble", "align"]);
  root.querySelector("#bAll").onclick = () => run(["clips", "assemble", "vo", "mix", "mux", "align"]);
  wireAudioBus(root);

  els.tl.addEventListener("mousedown", onTrackMouseDown);
  window.addEventListener("mousemove", onDragMove);
  window.addEventListener("mouseup", onDragUp);
  // touch: a tap selects (the browser's compat mousedown above), and only the
  // selected clip drags — it alone gets touch-action:none in style.css — so a
  // swipe across the packed video/voice lanes scrolls instead of reordering.
  // preventDefault in onTrackMouseDown stops the compat mouse events doubling up.
  const touch = (fn) => (e) => { if (e.pointerType !== "mouse") fn(e); };
  els.tl.addEventListener("pointerdown", touch((e) => { if (e.target.closest(".sel")) onTrackMouseDown(e); }));
  window.addEventListener("pointermove", touch(onDragMove));
  window.addEventListener("pointerup", touch(onDragUp));
  window.addEventListener("pointercancel", touch(onDragUp));
  // scoped to root, not document: the Voice tab reuses the same data-t="line"
  // convention for its own inputs, and a document-level delegate would fire
  // twice for the same edit whenever this pane happens to also be mounted
  root.addEventListener("input", onFieldInput);
  // redraw the inspector once an edit is committed — it is skipped while the
  // field is being typed in (see drawInspector)
  root.addEventListener("focusout", () => setTimeout(emit, 0));
  root.addEventListener("click", onFieldClick);
  window.addEventListener("keydown", (e) => { if (root.offsetParent) onKeyDown(e); });

  subscribe(render);
  render();
}

function aud() { S.manifest.audio = S.manifest.audio || {}; return S.manifest.audio; }
function music() { const a = aud(); a.music = a.music || {}; return a.music; }

function wireAudioBus(root) {
  const bind = (id, get, set) => {
    const el = root.querySelector(id), out = root.querySelector(id + "V");
    el.addEventListener("input", () => { set(+el.value); autosave(); syncAudioBus(root); });
    return () => { const v = get() ?? 0; el.value = v; if (out) out.textContent = (v > 0 ? "+" : "") + Number(v).toFixed(1) + " dB"; };
  };
  els.syncVoice = bind("#busVoice", () => aud().voice_db, (v) => (aud().voice_db = v));
  els.syncSfx = bind("#busSfx", () => aud().sfx_db, (v) => (aud().sfx_db = v));
  els.syncMusic = bind("#busMusic", () => music().gain_db ?? -20, (v) => (music().gain_db = v));
  root.querySelector("#mSrc").addEventListener("input", (e) => { music().src = e.target.value; autosave(); });
  root.querySelector("#mStart").addEventListener("input", (e) => { music().start = +e.target.value || 0; autosave(); });
  root.querySelector("#mDuck").addEventListener("input", (e) => { music().duck_db = e.target.value === "" ? null : +e.target.value; autosave(); });
  root.querySelector("#addCue").onclick = () => {
    const a = aud(); a.sfx = a.sfx || [];
    a.sfx.push({ kind: "impact", beat: S.manifest.segments[0]?.name || "", offset: 0, gain_db: -9 });
    sel = { type: "cue", i: a.sfx.length - 1 };
    autosave(); emit();
  };
}

function syncAudioBus(root) {
  if (!els.syncVoice) return;
  els.syncVoice(); els.syncSfx(); els.syncMusic();
  const mu = music();
  root.querySelector("#mSrc").value = mu.src || "";
  root.querySelector("#mStart").value = mu.start ?? 0;
  root.querySelector("#mDuck").value = mu.duck_db ?? -6;
  root.querySelector("#audSum").textContent =
    `${(aud().sfx || []).length} cues${mu.src ? " + music" : " · no music"}`;
}

async function toggleRecord() {
  if (REC.recording()) return REC.stop(player);
  try {
    await REC.start(player, (row) => {
      if (row) syncLines(row, true);
      setPlayIcon(player.playing);
      // the guide track is muted while recording; put it back the way the
      // rendered/live preview choice wants it
      if (!REC.recording()) player.audio.muted = !!player.reelUrl;
      autosave(); emit();
    });
  } catch (err) {
    toast(String(err.message || err));
  }
}

function togglePlay() { player.toggle(); setPlayIcon(player.playing); }
function toggleLoop() { player.setLoop(!player.loop); document.querySelector("#loopBtn")?.classList.toggle("on", player.loop); }
function setPlayIcon(playing) { els.playBtn.textContent = playing ? "⏸" : "▶"; }
function movePlayhead(t) { els.playhead.style.left = (t * PPS) + "px"; }

const fps = () => +(S.manifest?.fps || 60);

// "rendered" plays out/reel.mp4: one file, natively buffered and seekable, with
// its own audio — the finished thing, and far smoother to scrub than twelve
// stacked <video>s. It is only honest while nothing is stale, so pending edits
// force the live composite back on.
let previewPref = "auto";   // auto | live
function renderedUrl() {
  if (previewPref === "live" || !S.hasReel || (S.stale || []).length) return null;
  const rel = api.outRel(S.manifest, "reel.mp4");
  // proxy: the server's 720p copy (~2.5 Mbps, 1 s keyframes), not the ~45 Mbps master
  const u = api.outUrl(S.pid, S.manifest, "reel.mp4", S.srcv[rel]);
  return u + (u.includes("?") ? "&" : "?") + "proxy=1";
}

function syncPreviewMode() {
  const url = renderedUrl();
  player.setRendered(url);
  // the reel carries its own audio; the separate track would double it
  player.audio.muted = !!url;
  const el = rootEl.querySelector("#previewMode");
  if (!el) return;
  el.textContent = url ? "rendered" : "live composite";
  el.className = "badge " + (url ? "g" : "o");
  el.title = url
    ? "playing the finished reel.mp4 — scrubs natively"
    : (S.stale || []).length
      ? "you have unrendered edits, so this is composited live from the clips"
      : "forced to the live composite";
  rootEl.querySelector("#previewToggle").textContent =
    previewPref === "live" ? "use rendered" : "use live";
}

// Frames are the unit you actually edit in, so the playhead snaps to one: a
// seek to an arbitrary float would leave the counter showing a frame the video
// is not really parked on.
function seekTo(t) {
  const f = Math.round(Math.max(0, Math.min(player.duration, t)) * fps());
  if (player.playing) { player.pause(); setPlayIcon(false); }
  player.seek(f / fps());
}

function step(frames) { seekTo(player.t + frames / fps()); }

function drawTimecode(t) {
  const f = fps();
  const frame = Math.round(t * f);
  const total = Math.round(player.duration * f);
  const mm = Math.floor(t / 60), ss = t - mm * 60;
  els.tCode.textContent = `${mm}:${ss < 10 ? "0" : ""}${ss.toFixed(2)}`;
  els.fCount.textContent = `frame ${frame} / ${total}`;
}

function onKeyDown(e) {
  if (!S.manifest || document.activeElement?.matches("input,textarea,select")) return;
  // leave browser shortcuts alone — Ctrl+Shift+R was starting a recording
  // instead of reloading. Shift stays allowed: Shift+arrows scrub by a second.
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); e.shiftKey ? seekTo(player.t - 1) : step(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); e.shiftKey ? seekTo(player.t + 1) : step(1); }
  else if (e.key === "Home") { e.preventDefault(); seekTo(0); }
  else if (e.key === "End") { e.preventDefault(); seekTo(player.duration); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelected(); }
  else if (e.key.toLowerCase() === "s" && sel?.type === "seg") { e.preventDefault(); splitAtPlayhead(); }
  else if (e.key.toLowerCase() === "r") { e.preventDefault(); toggleRecord(); }
  else if (e.key.toLowerCase() === "l") { e.preventDefault(); toggleLoop(); }
}

// ---------- derived layout (recomputed on every render — cheap, pure) ----------
function computeAll() {
  const m = S.manifest;
  const videoL = L.videoLayout(m, S.plan);
  const voL = L.voLayout(m, S.voLines);
  const cues = L.cueLayout(m, videoL);
  const targets = L.beatTargets(m, videoL);
  const total = L.totalDuration(videoL, voL);
  return { videoL, voL, cues, targets, total };
}

function render() {
  if (!S.manifest) return;
  const { videoL, voL, cues, targets, total } = computeAll();
  S.videoL = videoL;                       // fx.js needs it to pin a new box
  if (!sel || sel.type !== "fx") FX.fxState.sel = null;
  drawTimeline(videoL, voL, cues, targets, total);
  preserveFocus(els.fields, () => drawInspector(videoL));
  drawLog();
  syncAudioBus(rootEl);

  // versioned by the audio file's own mtime, not by reel.json's — keying it on
  // the manifest revision reloaded the whole track on every keystroke
  const audioName = S.hasMix ? "mixdown.wav" : "voiceover.wav";
  const wantSrc = api.outUrl(S.pid, S.manifest, audioName,
                             S.srcv[api.outRel(S.manifest, audioName)]);
  if (!player.audio.src.endsWith(wantSrc) && (S.hasVo || S.hasMix)) {
    player.audio.src = wantSrc;
  }
  player.setSources(S.pid, S.srcv);
  FX.draw(fxSurface, player.t);
  FX.paintLive(fxSurface, player.t, player.visibleVideo(), !player.reelUrl);
  player.setTimeline(videoL, total);
  syncPreviewMode();
  drawTimecode(player.t);
  els.tlPreviewSum.textContent = `${total.toFixed(1)}s · ${Math.round(total * fps())}f`;
}

// what each stale step means in the language of the edit that caused it
const STEP_WORDS = {
  clips: "re-render clips", assemble: "re-cut", vo: "render voice",
  mix: "mix audio", mux: "lay audio over picture", align: "measure drift",
};

function drawLog() {
  els.log.textContent = (S.job.log || []).slice().reverse().join("\n") || "idle";
  els.running.textContent = S.job.running ? "· " + S.job.step : "";
  const busy = S.job.running;
  document.querySelectorAll("#insp ~ section button").forEach((b) => (b.disabled = busy));

  const steps = S.stale || [];
  const btn = rootEl.querySelector("#bUpdate");
  const what = rootEl.querySelector("#updWhat");
  const clean = steps.length === 0;
  btn.disabled = busy || clean;
  btn.textContent = busy ? "Working…" : clean ? "Up to date" : `Update (${steps.length})`;
  what.textContent = busy ? ""
    : clean ? "Everything matches the current edit."
    : steps.map((s) => STEP_WORDS[s] || s).join(" → ") + "  ·  ⌘/Ctrl+↵";
}

// ---------- drawing ----------
function drawTimeline(videoL, voL, cues, targets, total) {
  const W = Math.max((total + 1) * PPS, 200);
  els.tl.style.width = W + "px";

  const step = PPS < 14 ? 5 : PPS < 34 ? 2 : 1;
  let r = "";
  for (let t = 0; t <= total + 1; t += step) r += `<div class="tick" style="left:${t * PPS}px">${t}s</div>`;
  els.ruler.innerHTML = r;

  els.tvid.innerHTML = '<span class="tlabel">video</span>' + videoL.map((v) => {
    const isSel = sel && sel.type === "seg" && sel.i === v.i;
    const wedge = v.i > 0 && v.xfadeIn > 0
      ? `<div class="xfadewedge" data-xfade="${v.i}" title="${v.xfadeIn.toFixed(2)}s crossfade"></div>` : "";
    // a held last frame, or a length the render could not honour — both are
    // invisible in the output until you go looking for them
    const frozen = L.frozenTail(S.manifest, S.plan, v.i);
    const surprise = L.lengthSurprise(S.manifest, S.plan, v.i);
    const warn = frozen
      ? ` <span class="warnmark freeze" title="source ran out — last frame held for ${frozen.toFixed(2)}s">❄</span>`
      : surprise
      ? ` <span class="warnmark" title="asked for ${surprise.asked}s, the render is ${surprise.got.toFixed(2)}s">!</span>` : "";
    return `<div class="clip${isSel ? " sel" : ""}" data-seg="${v.i}"
        style="left:${v.start * PPS}px;width:${Math.max(v.len * PPS - 2, 10)}px;background:${COLORS[v.i % COLORS.length]}">
        <div class="grip l" data-edge="l" data-seg="${v.i}"></div>
        <div class="grip r" data-edge="r" data-seg="${v.i}"></div>
        ${v.seg.name}${L.isGen(v.seg) ? " ◆" : ""}${warn}<br><span class="len">${v.len.toFixed(2)}s</span></div>${wedge}`;
  }).join("");

  els.taud.innerHTML = '<span class="tlabel">voice</span>' + voL.map((v) => {
    const t = targets[v.i];
    const d = t == null ? null : v.start - t;
    const isSel = sel && sel.type === "line" && sel.i === v.i;
    const driftTxt = d == null ? "" : ` <b>${d > 0 ? "+" : ""}${d.toFixed(2)}</b>`;
    return `<div class="aclip ${L.driftClass(d)}${v.line.mute ? " muted" : ""}${isSel ? " sel" : ""}" data-line="${v.i}"
        title="${(v.line.text || "").replace(/"/g, "&quot;")}"
        style="left:${v.start * PPS}px;width:${Math.max(v.dur * PPS - 2, 8)}px">
        ${v.i + 1}. ${(v.line.text || "").slice(0, 40)}${driftTxt}</div>`;
  }).join("");

  els.tfx.innerHTML = '<span class="tlabel">fx</span>' + FX.effects().map((e, i) => {
    const w = FX.fxTime(e, videoL);
    if (!w) return "";
    const isSel = FX.fxState.sel === i;
    return `<div class="fxclip${e.mute ? " muted" : ""}${isSel ? " sel" : ""}" data-fx="${i}"
        title="${e.kind} · ${w.dur.toFixed(2)}s — drag to move, edges to trim"
        style="left:${w.start * PPS}px;width:${Math.max(w.dur * PPS - 2, 14)}px">
        <div class="grip l" data-fxedge="l" data-fx="${i}"></div>
        <div class="grip r" data-fxedge="r" data-fx="${i}"></div>
        ${e.kind}</div>`;
  }).join("");

  els.ttake.innerHTML = '<span class="tlabel">takes</span>' + REC.takes().map((t, i) => {
    const isSel = sel && sel.type === "take" && sel.i === i;
    return `<div class="takeclip${t.mute ? " muted" : ""}${isSel ? " sel" : ""}" data-take="${i}"
        title="take ${i + 1} · ${(+t.dur || 0).toFixed(2)}s at ${(+t.at || 0).toFixed(2)}s — drag to nudge it into sync"
        style="left:${(+t.at || 0) * PPS}px;width:${Math.max((+t.dur || 0) * PPS - 2, 14)}px">🎙 ${i + 1}</div>`;
  }).join("");
  els.recBtn.classList.toggle("on", REC.recording());
  els.ttsBtn.classList.toggle("off", !!(S.manifest.vo || {}).mute_tts);
  els.status.value = S.manifest.status === "ready" ? "ready" : "draft";
  els.taud.classList.toggle("dimmed", !!(S.manifest.vo || {}).mute_tts);

  els.tsfx.innerHTML = '<span class="tlabel">sfx</span>' + cues.filter(Boolean).map((c) => {
    const isSel = sel && sel.type === "cue" && sel.i === c.i;
    return `<div class="cue${c.cue.mute ? " mute" : ""}${isSel ? " sel" : ""}" data-cue="${c.i}"
        title="${c.cue.kind || c.cue.src} @ ${c.start.toFixed(2)}s"
        style="left:${c.start * PPS}px;width:${Math.max(0.25 * PPS, 6)}px">${c.cue.kind || "sfx"}</div>`;
  }).join("");

  els.tlSum.textContent = total.toFixed(1) + "s";
  movePlayhead(player ? player.t : 0);
}

function drawInspector(videoL) {
  const f = els.fields;
  if (!sel) { els.selKind.textContent = ""; f.className = "hint"; f.textContent = "Click a clip, line, or cue."; return; }
  // same rule as the Voice tab: never rebuild a field mid-keystroke
  if (isTyping(f)) return;
  f.className = "";
  const m = S.manifest;

  if (sel.type === "seg") {
    const s = m.segments[sel.i], i = sel.i;
    if (!s) { sel = null; return drawInspector(videoL); }
    els.selKind.textContent = "segment · " + s.name;
    f.innerHTML = `
      <div class="fld"><label>name</label><input value="${s.name}" data-t="seg" data-k="name" data-i="${i}"></div>
      <div class="fld"><label>source</label><input value="${s.src}" data-t="seg" data-k="src" data-i="${i}"></div>
      <div class="row">
        <div class="fld" style="flex:1"><label>in</label><input class="num" value="${s.start ?? ""}" data-t="seg" data-k="start" data-i="${i}"></div>
        <div class="fld" style="flex:1"><label>${L.isGen(s) && s.dur == null ? "secs (retimes)" : "length"}</label>
          ${L.isGen(s) && s.dur == null
            ? `<input class="num" value="${s.render.secs ?? ""}" data-t="gen" data-k="secs" data-i="${i}">`
            : `<input class="num" value="${s.dur ?? ""}" data-t="seg" data-k="dur" data-i="${i}">`}</div>
        <div class="fld" style="flex:1"><label>fade in</label><input class="num" value="${s.xfade ?? ""}" data-t="seg" data-k="xfade" data-i="${i}"></div>
      </div>
      <div class="row"><button class="mini" data-mv="${i}" data-d="-1">← earlier</button>
        <button class="mini" data-mv="${i}" data-d="1">later →</button>
        <button class="mini" data-split="${i}">✂ split at playhead</button>
        <button class="mini danger" data-del="${i}">✕ remove</button></div>`;
  } else if (sel.type === "line") {
    const l = m.vo.lines[sel.i], meta = S.voLines[sel.i], i = sel.i;
    if (!l) { sel = null; return drawInspector(videoL); }
    els.selKind.textContent = "line " + (i + 1);
    const opts = m.segments.map((s) => s.name);
    f.innerHTML = `
      <div class="fld"><label>text</label><textarea rows="3" data-t="line" data-k="text" data-i="${i}">${l.text}</textarea></div>
      <div class="row">
        <div class="fld" style="flex:1"><label>beat</label><select data-t="line" data-k="beat" data-i="${i}"><option value=""></option>
          ${opts.map((o) => `<option${o === l.beat ? " selected" : ""}>${o}</option>`).join("")}</select></div>
        <div class="fld" style="flex:1"><label>offset</label><input class="num" value="${l.offset ?? ""}" data-t="line" data-k="offset" data-i="${i}"></div>
      </div>
      <div class="row">
        <div class="fld" style="flex:1"><label>exag</label><input class="num" value="${l.exag ?? 0.5}" data-t="line" data-k="exag" data-i="${i}"></div>
        <div class="fld" style="flex:1"><label>cfg</label><input class="num" value="${l.cfg ?? 0.5}" data-t="line" data-k="cfg" data-i="${i}"></div>
        <div class="fld" style="flex:1"><label>pause ms</label><input class="num" value="${l.pause ?? 250}" data-t="line" data-k="pause" data-i="${i}"></div>
      </div>
      <div class="row">
        ${meta ? `<button class="mini" data-play="${meta.file}">▶ line</button>` : ""}
        <button class="mini" data-roll="${i}">↻ reroll</button>
        <button class="mini" data-lmute="${i}">${l.mute ? "unmute TTS" : "mute TTS"}</button>
        <button class="mini danger" data-ldel="${i}">✕ remove</button></div>`;
  } else if (sel.type === "cue") {
    const c = (m.audio.sfx || [])[sel.i], i = sel.i;
    if (!c) { sel = null; return drawInspector(videoL); }
    els.selKind.textContent = "cue · " + (c.kind || c.src);
    const opts = m.segments.map((s) => s.name);
    f.innerHTML = `
      <div class="row">
        <div class="fld" style="flex:1"><label>kind</label><select data-t="cue" data-k="kind" data-i="${i}">
          ${["impact", "sub", "tick", "whoosh", "riser"].map((k) => `<option${k === c.kind ? " selected" : ""}>${k}</option>`).join("")}
        </select></div>
        <div class="fld" style="flex:1"><label>gain dB</label><input class="num" value="${c.gain_db ?? -8}" data-t="cue" data-k="gain_db" data-i="${i}"></div>
      </div>
      <div class="row">
        <div class="fld" style="flex:1"><label>beat</label><select data-t="cue" data-k="beat" data-i="${i}"><option value=""></option>
          ${opts.map((o) => `<option${o === c.beat ? " selected" : ""}>${o}</option>`).join("")}</select></div>
        <div class="fld" style="flex:1"><label>offset</label><input class="num" value="${c.offset ?? 0}" data-t="cue" data-k="offset" data-i="${i}"></div>
      </div>
      <div class="row">
        <button class="mini" data-cplay="${i}">▶ hear it</button>
        <button class="mini" data-cmute="${i}">${c.mute ? "unmute" : "mute"}</button>
        <button class="mini danger" data-cdel="${i}">✕ remove</button></div>`;
  } else if (sel.type === "take") {
    const t = REC.takes()[sel.i], i = sel.i;
    if (!t) { sel = null; return drawInspector(videoL); }
    els.selKind.textContent = `take ${i + 1}`;
    f.innerHTML = `
      <div class="hint">Recorded live against the picture. If it sits slightly
        late, drag the card left — the mic stack adds a few tens of milliseconds
        the recorder cannot see. Mute the synthesized read with <b>TTS</b> in the
        transport once your takes cover the whole reel.</div>
      <div class="row">
        <div class="fld" style="flex:1"><label>starts at</label><input class="num" value="${+t.at || 0}" data-t="take" data-k="at" data-i="${i}"></div>
        <div class="fld" style="flex:1"><label>gain dB</label><input class="num" value="${t.gain_db ?? 0}" data-t="take" data-k="gain_db" data-i="${i}"></div>
      </div>
      <div class="fld"><label>length</label><div class="sub">${(+t.dur || 0).toFixed(2)}s · ${t.file}</div></div>
      <div class="row">
        <button class="mini" data-tplay="${i}">▶ play</button>
        <button class="mini" data-tmute="${i}">${t.mute ? "unmute" : "mute"}</button>
        <button class="mini danger" data-tdel="${i}">✕ remove</button></div>`;
  } else if (sel.type === "fx") {
    const e = FX.effects()[sel.i], i = sel.i;
    if (!e) { sel = null; return drawInspector(videoL); }
    els.selKind.textContent = "effect · " + e.kind;
    const opts = m.segments.map((x) => x.name);
    const b = e.box || [0, 0, 0, 0];
    const KINDS = [["pixelate", "pixelate — mosaic blocks"],
                   ["solid", "solid — filled black box"],
                   ["blur", "blur — gaussian softening"]];
    f.innerHTML = `
      <div class="hint">Drag the box on the picture to move it, its corner to
        resize. On the timeline, drag the card to move it (it re-pins to
        whatever beat it lands on) or its edges to trim.
        ${e.kind === "solid"
          ? "A solid box is the only one of these that leaks nothing."
          : "Intensity is coarseness — a mosaic still leaks structure, so use solid for anything genuinely sensitive."}</div>
      <div class="fld"><label>kind</label>
        <select data-t="fx" data-k="kind" data-i="${i}">
          ${KINDS.map(([k, lbl]) => `<option value="${k}"${k === e.kind ? " selected" : ""}>${lbl}</option>`).join("")}
        </select></div>
      <div class="row">
        <div class="fld" style="flex:1"><label>beat</label><select data-t="fx" data-k="beat" data-i="${i}"><option value=""></option>
          ${opts.map((o) => `<option${o === e.beat ? " selected" : ""}>${o}</option>`).join("")}</select></div>
        <div class="fld" style="flex:1"><label>offset</label><input class="num" value="${e.offset ?? 0}" data-t="fx" data-k="offset" data-i="${i}"></div>
      </div>
      <div class="row">
        <div class="fld" style="flex:1"><label>duration</label><input class="num" value="${e.dur ?? 1.5}" data-t="fx" data-k="dur" data-i="${i}"></div>
        <div class="fld" style="flex:1"><label>fade in/out</label><input class="num" value="${e.fade ?? 0.25}" data-t="fx" data-k="fade" data-i="${i}"></div>
      </div>
      ${e.kind === "solid" ? "" : `
      <div class="fld"><label>${e.kind === "blur" ? "blur amount" : "coarseness"} ${(+(e.intensity ?? 0.6)).toFixed(2)}</label>
        <input type="range" min="0" max="1" step="0.05" value="${e.intensity ?? 0.6}" data-t="fx" data-k="intensity" data-i="${i}"></div>`}
      <div class="row">
        ${["x", "y", "w", "h"].map((n, k) =>
          `<div class="fld" style="flex:1"><label>${n}</label><input class="num" value="${(+b[k]).toFixed(3)}" data-t="fxbox" data-k="${k}" data-i="${i}"></div>`).join("")}
      </div>
      <div class="row">
        <button class="mini" data-xmute="${i}">${e.mute ? "unmute" : "mute"}</button>
        <button class="mini danger" data-xdel="${i}">✕ remove</button></div>`;
  }
}

// ---------- editing ----------
function onFieldInput(e) {
  const t = e.target, i = t.dataset.i, k = t.dataset.k;
  if (i == null || !k || !S.manifest) return;
  const m = S.manifest;
  if (t.dataset.t === "fx") {
    const fxe = FX.effects()[+i];
    if (fxe) fxe[k] = (k === "beat" || k === "kind") ? t.value : (+t.value || 0);
    autosave(); emit(); return;
  }
  if (t.dataset.t === "take") {
    const tk = REC.takes()[+i];
    if (tk) tk[k] = +t.value || 0;
    autosave(); emit(); return;
  }
  if (t.dataset.t === "fxbox") {
    const fxe = FX.effects()[+i];
    if (fxe) { fxe.box = [...fxe.box]; fxe.box[+k] = Math.max(0, Math.min(1, +t.value || 0)); }
    autosave(); emit(); return;
  }
  const obj = t.dataset.t === "seg" ? m.segments[i]
            : t.dataset.t === "gen" ? m.segments[i].render
            : t.dataset.t === "cue" ? m.audio.sfx[i]
            : m.vo.lines[i];
  if (!obj) return;
  const isText = ["name", "src", "text", "beat", "kind"].includes(k);
  obj[k] = isText ? t.value : (t.value === "" ? null : Number(t.value));
  if (obj[k] == null) delete obj[k];
  autosave();
  emit();
}

function onFieldClick(e) {
  if (!S.manifest) return;
  const b = e.target;
  const m = S.manifest;
  const swap = (arr, i, d) => { const j = i + d; if (j < 0 || j >= arr.length) return; [arr[i], arr[j]] = [arr[j], arr[i]]; autosave(); emit(); };
  if (b.dataset.mv != null) swap(m.segments, +b.dataset.mv, +b.dataset.d);
  if (b.dataset.del != null) { m.segments.splice(+b.dataset.del, 1); sel = null; autosave(); emit(); }
  if (b.dataset.split != null) splitAtPlayhead();
  if (b.dataset.roll != null) rerollLine(+b.dataset.roll);
  if (b.dataset.lmute != null) { const l = m.vo.lines[+b.dataset.lmute]; l.mute = !l.mute; autosave(); emit(); }
  if (b.dataset.ldel != null) { m.vo.lines.splice(+b.dataset.ldel, 1); sel = null; autosave(); emit(); }
  if (b.dataset.play != null) new Audio(api.outUrl(S.pid, m, "vo/" + b.dataset.play)).play();
  if (b.dataset.cmute != null) { const c = m.audio.sfx[+b.dataset.cmute]; c.mute = !c.mute; autosave(); emit(); }
  if (b.dataset.cdel != null) { m.audio.sfx.splice(+b.dataset.cdel, 1); sel = null; autosave(); emit(); }
  if (b.dataset.tmute != null) { const t = REC.takes()[+b.dataset.tmute]; t.mute = !t.mute; syncLines(t, !t.mute); autosave(); emit(); }
  if (b.dataset.tdel != null) { syncLines(REC.takes()[+b.dataset.tdel], false); REC.takes().splice(+b.dataset.tdel, 1); sel = null; autosave(); emit(); }
  if (b.dataset.tplay != null) {
    const t = REC.takes()[+b.dataset.tplay];
    if (t) new Audio(api.mediaUrl(S.pid, t.file)).play();
  }
  if (b.dataset.xdel != null) {
    FX.effects().splice(+b.dataset.xdel, 1);
    sel = null; FX.fxState.sel = null; autosave(); emit();
  }
  if (b.dataset.xmute != null) {
    const fxe = FX.effects()[+b.dataset.xmute];
    if (fxe) fxe.mute = !fxe.mute;
    autosave(); emit();
  }
  if (b.dataset.cplay != null) {
    const c = m.audio.sfx[+b.dataset.cplay];
    new Audio(c.src ? api.mediaUrl(S.pid, c.src) : api.outUrl(S.pid, m, "sfx/" + c.kind + ".wav")).play();
  }
}

function rerollLine(i) {
  renderLine(i, true);
}

// A take covers a TTS line when it spans most of it: recording over a line is
// how you say "my voice here, not the synthesized one". Taking a take away (mute
// or remove) hands its lines back, unless another live take still covers them.
// ponytail: half-the-line rule — a take you start mid-line leaves that line on;
// mute it by hand from the line's card if you meant it.
function syncLines(take, on) {
  if (!take) return;
  const span = (t, v) => Math.min(+t.at + +t.dur, v.start + v.dur) - Math.max(+t.at, v.start) > v.dur / 2;
  const others = REC.takes().filter((t) => t !== take && !t.mute);
  const hit = computeAll().voL.filter((v) => (v.line.text || "").trim() && span(take, v)
    && !!v.line.mute !== on && (on || !others.some((t) => span(t, v))));
  hit.forEach((v) => { v.line.mute = on; });
  if (hit.length) toast(`TTS ${on ? "muted" : "back"} on line ${hit.map((v) => v.i + 1).join(", ")}`);
}

function deleteSelected() {
  if (!sel || !S.manifest) return;
  const m = S.manifest;
  if (sel.type === "seg") m.segments.splice(sel.i, 1);
  else if (sel.type === "line") m.vo.lines.splice(sel.i, 1);
  else if (sel.type === "cue") m.audio.sfx.splice(sel.i, 1);
  else if (sel.type === "take") { syncLines(REC.takes()[sel.i], false); REC.takes().splice(sel.i, 1); }
  else if (sel.type === "fx") FX.effects().splice(sel.i, 1);
  sel = null;
  autosave(); emit();
}

function splitAtPlayhead() {
  if (!sel || sel.type !== "seg" || !S.manifest) return;
  const { videoL } = computeAll();
  const v = videoL[sel.i];
  const t = player.t;
  if (t <= v.start + 0.05 || t >= v.start + v.len - 0.05) {
    toast("move the playhead inside the clip first", "warn");
    return;
  }
  const localOffset = t - v.start;
  const m = S.manifest;
  const s = m.segments[sel.i];
  const srcIn = (s.start || 0) + localOffset;
  const totalLen = v.len;

  const first = { ...s, dur: +localOffset.toFixed(3) };
  const second = { ...s, name: uniqueName(s.name + "-b"), start: +srcIn.toFixed(3),
                   dur: +(totalLen - localOffset).toFixed(3), xfade: 0 };
  delete second.note;
  m.segments.splice(sel.i, 1, first, second);
  sel = { type: "seg", i: sel.i + 1 };
  autosave(); emit();
}

function uniqueName(base) {
  const names = new Set(S.manifest.segments.map((s) => s.name));
  if (!names.has(base)) return base;
  let n = 2;
  while (names.has(`${base}${n}`)) n++;
  return `${base}${n}`;
}

// ---------- dragging ----------
function onTrackMouseDown(e) {
  const grip = e.target.closest(".grip");
  const wedge = e.target.closest(".xfadewedge");
  const seg = e.target.closest(".clip");
  const aud = e.target.closest(".aclip");
  const cue = e.target.closest(".cue");
  const fxc = e.target.closest(".fxclip");
  const fxg = e.target.closest("[data-fxedge]");
  const tk = e.target.closest(".takeclip");
  const m = S.manifest;

  if (wedge) {
    const i = +wedge.dataset.xfade;
    if (e.detail === 2) { m.segments[i].xfade = 0; autosave(); emit(); return; }
    dragCtx = { kind: "xfade", i, x: e.clientX, x0: +(m.segments[i].xfade ?? m.xfade ?? 0.12) };
    sel = { type: "seg", i };
  } else if (grip) {
    const i = +grip.dataset.seg;
    const { videoL } = computeAll();
    dragCtx = { kind: "trim", edge: grip.dataset.edge, i, x: e.clientX,
               dur0: videoL[i].len, start0: +(m.segments[i].start || 0) };
    sel = { type: "seg", i };
  } else if (seg) {
    dragCtx = { kind: "moveseg", i: +seg.dataset.seg, x: e.clientX };
    sel = { type: "seg", i: +seg.dataset.seg };
  } else if (aud) {
    const i = +aud.dataset.line;
    dragCtx = { kind: "pause", i, x: e.clientX, p0: i > 0 ? +(m.vo.lines[i - 1].pause ?? 250) : 0 };
    sel = { type: "line", i };
  } else if (cue) {
    const i = +cue.dataset.cue;
    dragCtx = { kind: "cue", i, x: e.clientX, off0: +(m.audio.sfx[i].offset || 0) };
    sel = { type: "cue", i };
  } else if (fxg) {
    const i = +fxg.dataset.fx;
    const { videoL } = computeAll();
    const w = FX.fxTime(FX.effects()[i], videoL);
    dragCtx = { kind: "fxtrim", edge: fxg.dataset.fxedge, i, x: e.clientX,
                abs0: w.start, dur0: w.dur };
    sel = { type: "fx", i };
    FX.fxState.sel = i;
  } else if (tk) {
    const i = +tk.dataset.take;
    dragCtx = { kind: "take", i, x: e.clientX, at0: +(REC.takes()[i].at || 0) };
    sel = { type: "take", i };
  } else if (fxc) {
    const i = +fxc.dataset.fx;
    const { videoL } = computeAll();
    // remember where it starts on the reel's clock, not its offset into a beat:
    // the drag may well finish over a different beat entirely
    dragCtx = { kind: "fx", i, x: e.clientX,
                abs0: FX.fxTime(FX.effects()[i], videoL).start };
    sel = { type: "fx", i };
    FX.fxState.sel = i;
  } else {
    return;
  }
  e.preventDefault();
  emit();
}

function onDragMove(e) {
  if (!dragCtx || !S.manifest) return;
  const m = S.manifest;
  const d = (e.clientX - dragCtx.x) / PPS;

  if (dragCtx.kind === "fx") {
    const fxe = FX.effects()[dragCtx.i];
    if (fxe) FX.rebase(fxe, dragCtx.abs0 + d, computeAll().videoL);
    autosave(); emit(); return;
  }

  if (dragCtx.kind === "fxtrim") {
    const fxe = FX.effects()[dragCtx.i];
    if (fxe) {
      if (dragCtx.edge === "r") {
        fxe.dur = +Math.max(0.1, dragCtx.dur0 + d).toFixed(2);
      } else {
        // dragging the left edge moves the start and shortens by the same
        // amount, so the tail stays put
        const grow = Math.min(d, dragCtx.dur0 - 0.1);
        FX.rebase(fxe, dragCtx.abs0 + grow, computeAll().videoL);
        fxe.dur = +Math.max(0.1, dragCtx.dur0 - grow).toFixed(2);
      }
      // a fade longer than half the window can never reach full strength
      fxe.fade = Math.min(+(fxe.fade ?? 0.25), fxe.dur / 2);
    }
    autosave(); emit(); return;
  }

  if (dragCtx.kind === "trim") {
    const s = m.segments[dragCtx.i];
    if (dragCtx.edge === "r") {
      const v = Math.max(0.2, +(dragCtx.dur0 + d).toFixed(2));
      if (L.isGen(s) && s.dur == null) s.render.secs = v; else s.dur = v;
    } else {
      const shift = Math.min(d, dragCtx.dur0 - 0.2);
      s.start = +Math.max(0, dragCtx.start0 + shift).toFixed(2);
      s.dur = +(dragCtx.dur0 - shift).toFixed(2);
    }
  } else if (dragCtx.kind === "xfade") {
    const s = m.segments[dragCtx.i];
    s.xfade = Math.max(0, +(dragCtx.x0 + d).toFixed(2));
  } else if (dragCtx.kind === "pause" && dragCtx.i > 0) {
    m.vo.lines[dragCtx.i - 1].pause = Math.max(0, Math.round((dragCtx.p0 + d * 1000) / 10) * 10);
  } else if (dragCtx.kind === "cue") {
    m.audio.sfx[dragCtx.i].offset = +(dragCtx.off0 + d).toFixed(2);
  } else if (dragCtx.kind === "take") {
    // the mic stack adds its own latency, so the recorder's stamp is close but
    // never exact — this drag is the calibration knob for it
    REC.takes()[dragCtx.i].at = +Math.max(0, dragCtx.at0 + d).toFixed(3);
  } else if (dragCtx.kind === "moveseg") {
    const { videoL } = computeAll();
    const i = dragCtx.i, j = d > 0 ? i + 1 : i - 1;
    if (j >= 0 && j < m.segments.length && Math.abs(d) > videoL[j].len / 2) {
      [m.segments[i], m.segments[j]] = [m.segments[j], m.segments[i]];
      if (S.plan[i] && S.plan[j]) [S.plan[i], S.plan[j]] = [S.plan[j], S.plan[i]];
      dragCtx.i = j; dragCtx.x = e.clientX; sel = { type: "seg", i: j };
    }
  }
  emit();
}

function onDragUp() {
  if (!dragCtx) return;
  dragCtx = null;
  autosave();
}

// ---------- called by clips.js when you "insert" a clip from the library ----------
export function insertClip(file, name) {
  const m = S.manifest;
  const seg = { name: uniqueName(name || file.split("/").pop().replace(/\.\w+$/, "")), src: file };
  m.segments.push(seg);
  sel = { type: "seg", i: m.segments.length - 1 };
  autosave(); emit();
}
