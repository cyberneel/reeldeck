// The live preview: composites the ALREADY-RENDERED per-segment source files
// directly in the browser, so scrubbing and edits are felt in milliseconds
// instead of waiting on a 50s ffmpeg re-cut. It is a preview, not the encode —
// Re-cut still produces the authoritative reel.mp4.
//
// Approach: one <video> per unique source, stacked and absolutely positioned,
// each seeked to its own local offset for the current playhead time. During a
// crossfade window two are visible at once, opacity-blended by progress through
// the overlap — good enough to edit against; it does not have to be
// frame-identical to ffmpeg's own xfade filter.
import { api } from "./api.js";
import { activeAt } from "./layout.js";

const DRIFT_TOLERANCE = 0.09; // seconds of local-time error before we bother reseeking

export class Player {
  constructor(container, audioEl) {
    this.container = container;
    this.audio = audioEl;
    this.videos = new Map();     // src -> <video>
    this.layout = [];
    this.duration = 0;
    this.playing = false;
    this.loop = false;
    this.t = 0;
    this._raf = null;
    this._activeSrcs = new Set();
    this.onTime = null;          // (t) => void, called every rendered frame
    this.onEnd = null;
  }

  /** The element actually on screen right now, so an overlay can read its
   *  pixels. During a crossfade two are visible; the more opaque one is the one
   *  worth sampling. */
  visibleVideo() {
    if (this.reel) return this.reel.readyState >= 2 ? this.reel : null;
    let best = null, bestA = -1;
    for (const v of this.videos.values()) {
      const a = v.style.opacity === "" ? 1 : +v.style.opacity;
      if (a > bestA && v.readyState >= 2 && v.videoWidth) { bestA = a; best = v; }
    }
    return bestA > 0.05 ? best : null;
  }

  // `versions` maps media path -> mtime; a changed mtime means the file was
  // re-rendered under the same name, which has to retire the element holding
  // the old frames (see setTimeline).
  setSources(pid, versions) { this.pid = pid; this.versions = versions || {}; }

  /**
   * Play the finished reel.mp4 instead of compositing, when one exists that
   * matches the current edit. It is a single file: the browser buffers it,
   * seeks it natively, and it carries its own audio — so scrubbing is a normal
   * video seek rather than twelve elements each hunting for a keyframe. Pass
   * null to go back to the live composite, which is what you want the moment
   * there are unrendered edits to see.
   */
  setRendered(url) {
    if (url === this.reelUrl) return;
    this.reelUrl = url;
    const wasPlaying = this.playing;
    if (wasPlaying) this.pause();
    if (!url) {
      if (this.reel) { this.reel.remove(); this.reel = null; }
      this._syncElements();          // build the composite back up
    } else {
      // Retire the composite elements: a dozen videos all preloading compete
      // for bandwidth and decoders with the one file actually on screen, which
      // is what makes playback stutter and scrubbing feel unbuffered.
      for (const v of this.videos.values()) v.remove();
      this.videos.clear();
      this._activeSrcs.clear();
      if (!this.reel) {
        const v = document.createElement("video");
        v.playsInline = true;
        v.preload = "auto";
        v.loop = this.loop;
        v.style.cssText =
          "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;background:#000;";
        v.addEventListener("ended", () => { this.pause(); this.onEnd?.(); });
        this.container.appendChild(v);
        this.reel = v;
      }
      this.reel.src = url;
      this.reel.currentTime = Math.min(this.t, this.duration || 0);
    }
    this._paint(true);
    if (wasPlaying) this.play();
  }

  // layout: the array videoLayout() produces — [{seg,i,start,len,xfadeIn}, ...]
  setTimeline(layout, duration) {
    this.layout = layout;
    this.duration = duration;
    if (this.reel) return this._paint();   // nothing to composite; don't fetch clips
    this._syncElements();
    this._paint();
  }

  // one <video> per unique source, created on demand and retired when its file
  // changes or it leaves the cut
  _syncElements() {
    const layout = this.layout;
    const wanted = new Map(
      layout.map((l) => [l.seg.src, api.mediaUrl(this.pid, l.seg.src, this.versions[l.seg.src])]));

    // drop anything no longer in the cut, and anything whose file changed on
    // disk — a <video> keeps serving the frames it already decoded, so a
    // regenerated clip only appears once the element itself is replaced
    for (const [src, v] of this.videos) {
      if (wanted.get(src) !== v.dataset.url) {
        v.remove(); this.videos.delete(src); this._activeSrcs.delete(src);
      }
    }
    for (const [src, url] of wanted) {
      if (this.videos.has(src)) continue;
      const v = document.createElement("video");
      v.src = url;
      v.dataset.url = url;
      v.muted = true;
      v.preload = "auto";
      v.playsInline = true;
      v.style.cssText =
        "position:absolute;inset:0;width:100%;height:100%;object-fit:contain;" +
        "background:#000;opacity:0;pointer-events:none;";
      // the currentTime set in _paint() while the video is still at
      // readyState 0 doesn't actually move the picture — it silently no-ops
      // until real frame data exists, so the first paint needs a follow-up
      // once that happens rather than assuming one call was enough
      v.addEventListener("loadeddata", () => this._paint(true), { once: true });
      this.container.appendChild(v);
      this.videos.set(src, v);
    }
  }

  seek(t) {
    this.t = Math.max(0, Math.min(this.duration, t));
    if (this.reel) this.reel.currentTime = this.t;
    else if (this.audio) this.audio.currentTime = this.t;
    this._paint(true);
    this.onTime?.(this.t);
  }

  // Clamped to what the file actually contains: a segment asked to run longer
  // than its source gets the last frame held, matching what assemble renders
  // (tpad=stop_mode=clone). Without this the element is seeked past its end,
  // where it has nothing to show and simply stalls.
  _localTime(hit, v) {
    const want = (hit.seg.start || 0) + (this.t - hit.start);
    const end = v && v.duration ? v.duration - 1 / 60 : Infinity;
    return Math.max(0, Math.min(want, end));
  }

  _paint(forceSeek = false) {
    if (this.reel) {
      // rendered mode: one element, nothing to composite
      for (const v of this.videos.values()) { v.style.opacity = 0; v.pause(); }
      this._activeSrcs = new Set();
      if (forceSeek && Math.abs(this.reel.currentTime - this.t) > DRIFT_TOLERANCE) {
        this.reel.currentTime = Math.max(0, Math.min(this.t, this.duration));
      }
      return;
    }
    const hits = activeAt(this.layout, this.t);
    const nowActive = new Set(hits.map((h) => h.seg.src));

    for (const [src, v] of this.videos) {
      const hit = hits.find((h) => h.seg.src === src);
      if (!hit) {
        v.style.opacity = 0;
        if (this._activeSrcs.has(src) && this.playing) v.pause();
        continue;
      }
      const local = this._localTime(hit, v);
      if (forceSeek || Math.abs(v.currentTime - local) > DRIFT_TOLERANCE) {
        v.currentTime = local;
      }
      let alpha = 1;
      if (hits.length === 2) {
        const [a, b] = hits;                    // a starts first
        const overlapStart = b.start, overlapEnd = a.start + a.len;
        const span = Math.max(0.001, overlapEnd - overlapStart);
        const prog = Math.max(0, Math.min(1, (this.t - overlapStart) / span));
        alpha = hit === a ? 1 - prog : prog;
      }
      v.style.opacity = alpha;
      if (this.playing && !this._activeSrcs.has(src)) v.play().catch(() => {});
    }
    this._activeSrcs = nowActive;
  }

  play() {
    if (this.duration <= 0) return;
    if (this.t >= this.duration) this.t = 0;
    this.playing = true;
    this._paint(true);

    // whatever is actually producing sound owns the clock, so picture and audio
    // cannot drift apart over a 40-second reel
    // Each returns null when its element isn't actually running, so a play()
    // the browser refused falls back to the wall clock instead of freezing the
    // playhead on a element that never started.
    let clock = null;
    if (this.reel) {
      this.reel.currentTime = this.t;
      this.reel.play().catch(() => {});
      clock = () => (this.reel.paused ? null : this.reel.currentTime);
    } else if (this.audio) {
      this.audio.currentTime = this.t;
      this.audio.play().catch(() => {});
      clock = () => (this.audio.paused ? null : this.audio.currentTime);
    }

    let last = performance.now();
    const step = (now) => {
      if (!this.playing) return;
      const c = clock ? clock() : null;
      this.t = c == null ? this.t + (now - last) / 1000 : c;
      last = now;
      this._paint();
      this.onTime?.(this.t);
      // the rendered reel loops natively (gapless, so a seam is really a seam);
      // the stacked preview has no single element to loop, so it restarts
      if (this.t >= this.duration) { this.pause(); this.loop ? this.play() : this.onEnd?.(); return; }
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  }

  pause() {
    this.playing = false;
    if (this._raf) cancelAnimationFrame(this._raf);
    for (const v of this.videos.values()) v.pause();
    if (this.reel) this.reel.pause();
    if (this.audio) this.audio.pause();
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  setLoop(on) { this.loop = on; if (this.reel) this.reel.loop = on; }

  destroy() {
    this.pause();
    for (const v of this.videos.values()) v.remove();
    this.videos.clear();
    if (this.reel) { this.reel.remove(); this.reel = null; }
  }
}
