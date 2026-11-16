/* Effect regions: draw a box on the picture, give it a window, burn it in.
 *
 * The box is stored as fractions of the frame, not pixels, so it keeps meaning
 * if the reel's resolution changes and so the editor never has to know the
 * encode size. Timing pins to a beat the way sound cues do — a retimed cut
 * carries its effects along instead of stranding them on a timestamp.
 *
 * This module owns only the picture-surface interaction (draw, move, resize).
 * The timeline lane and the inspector live with the rest of the timeline UI,
 * because they are the same widgets everything else there already uses.
 */
import { S, emit } from "./store.js";

const MIN = 0.02;                     // a box smaller than this is a stray click
const clamp01 = (v) => Math.max(0, Math.min(1, v));

export const fxState = { drawing: false, sel: null };

export function effects() {
  if (!S.manifest) return [];
  if (!S.manifest.effects) S.manifest.effects = [];
  return S.manifest.effects;
}

/** Where an effect sits on the reel's clock, given the video layout. */
export function fxTime(e, videoL) {
  const at = {};
  videoL.forEach((v) => { at[v.seg.name] = v.start; });
  const base = e.beat ? at[e.beat] : +(e.at || 0);
  if (base == null) return null;
  return { start: base + (+(e.offset || 0)), dur: +(e.dur || 1) };
}

/** The beat under a given reel time, so a new box pins itself sensibly. */
export function beatAt(t, videoL) {
  for (const v of videoL) if (t >= v.start && t < v.start + v.len) return v;
  return videoL[videoL.length - 1] || null;
}

/** Move an effect to an absolute time, re-pinning it to whatever beat it now
 *  sits over.
 *
 *  Dragging used to change `offset` alone, which meant an effect could never
 *  leave the beat it was created on — push it past the boundary and it either
 *  stopped or kept a nonsense offset into a beat it was no longer above. Timing
 *  still pins to a beat (so a retimed cut carries effects along), but which
 *  beat is now decided by where the block actually is. */
export function rebase(e, t, videoL) {
  const at = Math.max(0, t);
  const v = beatAt(at, videoL);
  if (v) {
    e.beat = v.seg.name;
    e.offset = +(at - v.start).toFixed(2);
    delete e.at;
  } else {
    e.beat = "";
    e.at = +at.toFixed(2);
    e.offset = 0;
  }
  return e;
}

export function addAt(t, box, videoL) {
  const v = beatAt(t, videoL);
  const e = {
    kind: "pixelate",
    beat: v ? v.seg.name : "",
    offset: v ? +Math.max(0, t - v.start).toFixed(2) : +t.toFixed(2),
    dur: 1.5, fade: 0.25, intensity: 0.6,
    box: box.map((n) => +n.toFixed(4)),
  };
  effects().push(e);
  fxState.sel = effects().length - 1;
  return e;
}

/* ---- the surface ---------------------------------------------------------
   One absolutely-positioned layer over the picture. It only takes pointer
   events while drawing or while an effect is selected, so it never gets in the
   way of scrubbing. */
export function mountSurface(wrap, { onChange, timeOf }) {
  const el = document.createElement("div");
  el.id = "fxSurface";
  wrap.appendChild(el);

  /* Track the PICTURE, not the box it sits in.
   *
   * #previewWrap carries an aspect-ratio and a max-height, so whenever the
   * height clamp bites the wrap is wider than the video and object-fit:contain
   * pillarboxes it. A surface pinned to the wrap therefore measures a drawn box
   * against the black bars too — the fractions come out narrower than what was
   * drawn, and the effect lands as a thin strip up the middle. Size the surface
   * to the letterboxed video rect instead, so a fraction here is a fraction of
   * the frame. */
  const fit = () => {
    const r = wrap.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const m = S.manifest || {};
    const aspect = (+m.width || 1080) / (+m.height || 1920);
    let w = r.width, h = r.width / aspect;
    if (h > r.height) { h = r.height; w = h * aspect; }
    el.style.left = `${(r.width - w) / 2}px`;
    el.style.top = `${(r.height - h) / 2}px`;
    el.style.width = `${w}px`;
    el.style.height = `${h}px`;
    const c = el.querySelector(".fxcanvas");
    if (c) {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
    }
  };
  // the canvas paints the effect itself; the boxes layer holds the outlines.
  // Separate children because the outlines are re-written on every render and
  // would otherwise wipe the canvas with them.
  el.innerHTML = '<canvas class="fxcanvas"></canvas><div class="fxboxes"></div>';
  fit();
  new ResizeObserver(fit).observe(wrap);
  el._fit = fit;

  let drag = null;
  const rectOf = () => el.getBoundingClientRect();
  const toFrac = (ev) => {
    const r = rectOf();
    return [clamp01((ev.clientX - r.left) / r.width),
            clamp01((ev.clientY - r.top) / r.height)];
  };

  el.addEventListener("pointerdown", (ev) => {
    const list = effects();
    const handle = ev.target.dataset && ev.target.dataset.handle;
    const [x, y] = toFrac(ev);
    if (handle && fxState.sel != null) {
      drag = { mode: handle, start: [x, y], box: [...list[fxState.sel].box] };
    } else if (fxState.drawing) {
      drag = { mode: "new", start: [x, y] };
    } else {
      return;
    }
    el.setPointerCapture(ev.pointerId);
    ev.preventDefault();
    ev.stopPropagation();
  });

  el.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const [x, y] = toFrac(ev);
    const list = effects();
    if (drag.mode === "new") {
      drag.box = [Math.min(drag.start[0], x), Math.min(drag.start[1], y),
                  Math.abs(x - drag.start[0]), Math.abs(y - drag.start[1])];
      paint(el, drag.box, true);
      if (el._live) el._live();
    } else if (fxState.sel != null) {
      const b = [...drag.box];
      const dx = x - drag.start[0], dy = y - drag.start[1];
      if (drag.mode === "move") { b[0] = clamp01(b[0] + dx); b[1] = clamp01(b[1] + dy); }
      else { b[2] = Math.max(MIN, b[2] + dx); b[3] = Math.max(MIN, b[3] + dy); }
      b[2] = Math.min(b[2], 1 - b[0]);
      b[3] = Math.min(b[3], 1 - b[1]);
      list[fxState.sel].box = b.map((n) => +n.toFixed(4));
      draw(el, timeOf());
      if (el._live) el._live();
    }
    ev.preventDefault();
  });

  const finish = (ev) => {
    if (!drag) return;
    const d = drag; drag = null;
    try { el.releasePointerCapture(ev.pointerId); } catch { /* already gone */ }
    if (d.mode === "new") {
      if (d.box && d.box[2] > MIN && d.box[3] > MIN) {
        addAt(timeOf(), d.box, S.videoL || []);
        fxState.drawing = false;
      }
    }
    onChange();
  };
  el.addEventListener("pointerup", finish);
  el.addEventListener("pointercancel", finish);
  return el;
}

function paint(el, box, live) {
  const host = el.querySelector(".fxboxes") || el;
  host.innerHTML =
    `<div class="fxbox${live ? " live" : ""}" style="left:${box[0] * 100}%;` +
    `top:${box[1] * 100}%;width:${box[2] * 100}%;height:${box[3] * 100}%">` +
    (live ? "" : `<span class="fxgrip" data-handle="move"></span>` +
                 `<span class="fxres" data-handle="resize"></span>`) + `</div>`;
}

/** Redraw the surface for the current playhead: the selected box always shows,
    others only while they are actually on screen. */
export function draw(el, t) {
  if (!el) return;
  const host = el.querySelector(".fxboxes") || el;
  const list = effects();
  const sel = fxState.sel;
  el.classList.toggle("armed", fxState.drawing || sel != null);
  if (fxState.drawing && !list.length) { host.innerHTML = ""; return; }
  if (sel != null && list[sel]) { paint(el, list[sel].box, false); return; }
  const vis = list.filter((e) => {
    const w = fxTime(e, S.videoL || []);
    return w && !e.mute && t >= w.start && t <= w.start + w.dur;
  });
  host.innerHTML = vis.map((e) =>
    `<div class="fxbox ghost" style="left:${e.box[0] * 100}%;top:${e.box[1] * 100}%;` +
    `width:${e.box[2] * 100}%;height:${e.box[3] * 100}%"></div>`).join("");
}

/* ---- live preview --------------------------------------------------------
   Paint the effects onto a canvas over the picture, so moving a box shows the
   real thing instead of an outline and a guess.

   The block maths deliberately mirrors _fx_chain() in pipeline.py: same
   coarseness curve, same snapping to whole blocks, same downscale-then-
   nearest-upscale. A preview that looks different from the render is worse than
   no preview, because you tune against it and then the encode disagrees.

   Only runs while the picture is the live composite. When the finished reel is
   on screen the effects are already burned into it, and painting again would
   show them twice. */
const tiny = document.createElement("canvas");

// Mirrors _r() and _even() in pipeline.py. JS rounds .5 up and Python rounds it
// to even, which is enough to put the preview one block out from the render at
// certain intensities — so neither side uses its language's default.
const r = (x) => Math.floor(x + 0.5);
const even = (n) => Math.max(2, Math.floor(n / 2) * 2);

export function paintLive(el, t, video, on) {
  const c = el && el.querySelector(".fxcanvas");
  if (!c) return;
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (!on || !video || !video.videoWidth) return;

  const list = effects();
  const videoL = S.videoL || [];
  const sx = c.width / video.videoWidth;      // canvas px per frame px
  const sy = c.height / video.videoHeight;

  for (const e of list) {
    if (e.mute) continue;
    const w = fxTime(e, videoL);
    if (!w) continue;
    const into = t - w.start;
    if (into < 0 || into > w.dur) continue;
    // same fade shape the filter graph uses, clamped the same way
    const f = Math.max(0, Math.min(+(e.fade ?? 0.25), w.dur / 2));
    let alpha = 1;
    if (f > 0) {
      if (into < f) alpha = into / f;
      else if (into > w.dur - f) alpha = (w.dur - into) / f;
    }
    if (alpha <= 0.01) continue;

    const [bx, by, bw, bh] = e.box || [0, 0, 0, 0];
    const FW = video.videoWidth, FH = video.videoHeight;
    let X = even(bx * FW), Y = even(by * FH);
    let W = even(bw * FW), H = even(bh * FH);
    W = Math.min(W, even(FW - X)); H = Math.min(H, even(FH - Y));
    if (W < 8 || H < 8) continue;

    const inten = Math.max(0, Math.min(1, +(e.intensity ?? 0.6)));
    ctx.save();
    ctx.globalAlpha = alpha;
    if (e.kind === "solid") {
      ctx.fillStyle = "#000";
      ctx.fillRect(X * sx, Y * sy, W * sx, H * sy);
    } else if (e.kind === "blur") {
      const sigma = 4 + inten * 40;
      ctx.beginPath();
      ctx.rect(X * sx, Y * sy, W * sx, H * sy);
      ctx.clip();
      ctx.filter = `blur(${Math.max(1, sigma * sx)}px)`;
      ctx.drawImage(video, 0, 0, c.width, c.height);
    } else {
      const across = Math.max(2, r(36 - inten * 30));
      const blk = Math.max(2, r(W / across));
      const cols = Math.max(1, Math.floor(W / blk));
      const rows = Math.max(1, Math.floor(H / blk));
      const SW = cols * blk, SH = rows * blk;
      tiny.width = cols; tiny.height = rows;
      const tctx = tiny.getContext("2d");
      tctx.clearRect(0, 0, cols, rows);
      tctx.drawImage(video, X, Y, SW, SH, 0, 0, cols, rows);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(tiny, 0, 0, cols, rows, X * sx, Y * sy, SW * sx, SH * sy);
    }
    ctx.restore();
  }
}
