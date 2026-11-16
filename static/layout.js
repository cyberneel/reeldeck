// Pure layout math — no DOM, no fetch. Both the Timeline tab (what you see) and
// the Player (what plays) call these, so they can never disagree about where
// anything sits. This mirrors pipeline.plan() closely enough to redraw instantly
// on a drag; Re-cut is what makes it authoritative.

// The manifest says what you asked for; plan.json records what ffmpeg actually
// produced. They diverge in two real cases: a source shorter than the `dur`
// asked of it (ffmpeg truncates, silently), and a generator that ignores `secs`
// (manim and capture clips are however long the thing they recorded was). Both
// made the timeline disagree with the render — by 0.6s on the reel this was
// built for — and because sfx cues resolve against the render's beats, they
// landed at times the timeline had never shown.
//
// So the measured length wins whenever nothing that determines it has changed
// since the plan was made. Drag a clip and the manifest wins instead: nothing
// has re-rendered yet, so the prediction is the best available answer, and the
// next Update replaces it with a measurement.
function planRow(plan, s) {
  return plan.find((r) => r && r.name === s.name);
}

function planIsCurrent(s, p) {
  if (!p || p.src !== s.src || p.length == null) return false;
  if ((p.start ?? null) !== (s.start ?? null)) return false;
  if ((p.dur ?? null) !== (s.dur ?? null)) return false;
  return ((s.render && s.render.secs) ?? null) === ((p.render && p.render.secs) ?? null);
}

export function segLength(m, plan, i) {
  const s = m.segments[i];
  const p = planRow(plan, s);
  if (planIsCurrent(s, p)) return +p.length;
  if (s.render && s.render.secs != null && s.dur == null) return +s.render.secs;
  if (s.dur != null) return +s.dur;
  return p && p.length != null ? p.length : 3;
}

/** Seconds of held last frame at the end of this clip, or 0. */
export function frozenTail(m, plan, i) {
  const s = m.segments[i];
  const p = planRow(plan, s);
  return planIsCurrent(s, p) ? +(p.frozen || 0) : 0;
}

/** True when this clip's rendered length isn't what the manifest asked for. */
export function lengthSurprise(m, plan, i) {
  const s = m.segments[i];
  const p = planRow(plan, s);
  if (!planIsCurrent(s, p)) return null;
  const asked = s.dur != null ? +s.dur
    : (s.render && s.render.secs != null) ? +s.render.secs : null;
  if (asked == null || Math.abs(asked - p.length) < 0.03) return null;
  return { asked, got: +p.length };
}

export const isGen = (s) => !!(s && s.render);

// [{seg, start, len}] aligned with m.segments
export function videoLayout(m, plan) {
  const def = +(m.xfade ?? 0.12);
  const out = [];
  let clock = 0;
  m.segments.forEach((s, i) => {
    const fade = i === 0 ? 0 : (s.xfade == null ? def : +s.xfade);
    if (i && fade !== 0) clock -= fade;   // new group pulls back by its own fade
    const len = segLength(m, plan, i);
    out.push({ seg: s, i, start: clock, len, xfadeIn: fade });
    clock += len;
  });
  return out;
}

// [{line, start, dur}] aligned with m.vo.lines
export function voLayout(m, voLines) {
  const out = [];
  // Start at the track's lead-in, not at zero.
  //
  // voiceover.wav opens with `vo.lead` seconds of silence so the first line can
  // sit partway into its beat — a cold open puts seconds of it there. Packing
  // the blocks from 0 drew the whole script that far left of where it actually
  // plays, which reads as the audio being broken when it is the ruler that is
  // wrong. The cue lane never had this bug because cues are positioned from
  // their beat, which is why sfx looked fine while the voice did not.
  let clock = +(m.vo.lead || 0);
  m.vo.lines.forEach((l, i) => {
    const dur = voLines[i] ? voLines[i].dur : 1.5;
    out.push({ line: l, i, start: clock, dur });
    clock += dur + (+(l.pause ?? 250) || 0) / 1000;
  });
  return out;
}

// index -> {kind, i} for cue positions, resolved against the video layout
export function cueLayout(m, videoL) {
  const at = {};
  videoL.forEach((v) => { at[v.seg.name] = v.start; });
  const cues = (m.audio && m.audio.sfx) || [];
  return cues.map((c, i) => {
    const base = c.beat ? at[c.beat] : +(c.at || 0);
    return base == null ? null : { cue: c, i, start: base + (+(c.offset || 0)) };
  });
}

// one target time per VO line, or null if its beat is unknown/unset
export function beatTargets(m, videoL) {
  const at = {};
  videoL.forEach((v) => { at[v.seg.name] = v.start; });
  return m.vo.lines.map((l) =>
    l.beat && at[l.beat] != null ? at[l.beat] + (+(l.offset || 0)) : null);
}

export function totalDuration(videoL, voL) {
  const vEnd = videoL.length ? videoL[videoL.length - 1].start + videoL[videoL.length - 1].len : 0;
  const aEnd = voL.length ? voL[voL.length - 1].start + voL[voL.length - 1].dur : 0;
  return Math.max(vEnd, aEnd);
}

export function driftClass(d) {
  if (d == null) return "";
  const a = Math.abs(d);
  return a < 0.5 ? "ok" : a < 1.2 ? "warn" : "bad";
}

// which video-layout entries are "live" at time t — normally one, briefly two
// during a crossfade window (the overlap is xfadeIn seconds wide, ending at the
// later segment's own start)
export function activeAt(videoL, t) {
  return videoL.filter((v) => t >= v.start && t < v.start + v.len);
}
