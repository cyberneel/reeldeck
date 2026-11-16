// Shared project state: one mutable object, one place that saves it, one place
// that polls jobs. Tabs subscribe and redraw on change — no framework, just an
// explicit "something changed" signal, which is all this app actually needs.
import { api } from "./api.js";

export const S = {
  pid: null,
  manifest: null,
  plan: [],
  voLines: [],
  voReady: [],        // per line: is there audio for its current text/settings?
  discovery: null,
  requests: [],
  voices: [],
  rev: null,
  stale: [],          // ordered steps an Update would run; [] = everything current
  srcv: {},           // media path -> mtime, so regenerated files get a fresh URL
  hasPreview: false, hasVo: false, hasReel: false, hasMix: false,
  job: { running: false, step: "", log: [], result: {}, error: "" },
  sel: null,          // {type:'seg'|'line'|'cue', i}
  loaded: false,
  loadError: "",      // why the open project couldn't be read
};

const listeners = new Set();
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function emit() { for (const fn of listeners) fn(); }

const toastListeners = new Set();
export function onToast(fn) { toastListeners.add(fn); return () => toastListeners.delete(fn); }
export function toast(msg, kind = "info") {
  for (const fn of toastListeners) fn(msg, kind);
}

let lastWrite = 0;   // when the most recent successful save landed

export async function openProject(pid) {
  S.pid = pid;
  S.loaded = false;
  S.loadError = "";
  emit();
  const at = Date.now();
  const d = await api.getProject(pid);
  if (d.error) { S.loadError = d.error; emit(); return; }   // the shell shows it, with Retry
  applyProjectPayload(d, lastWrite < at);
  S.loaded = true;
  emit();
}

// `trustStale` is false when a local save landed while this GET was in flight:
// the server computed `stale` before that write, so applying it would step on
// newer state and leave the Update button claiming there is nothing to do.
function applyProjectPayload(d, trustStale = true) {
  S.manifest = d.manifest;
  S.projectDir = d.project;
  S.plan = d.plan || [];
  S.voLines = d.vo_lines || [];
  S.voReady = d.vo_ready || [];
  S.discovery = d.discovery || null;
  S.requests = d.requests || [];
  S.voices = d.voices || [];
  S.rev = d.rev;
  if (trustStale) S.stale = d.stale || [];
  S.srcv = d.srcv || {};
  S.hasPreview = !!d.has_preview;
  S.hasVo = !!d.has_vo;
  S.hasReel = !!d.has_reel;
  S.hasMix = !!d.has_mix;
}

export async function refreshProject() {
  if (!S.pid) return;
  const at = Date.now();
  const d = await api.getProject(S.pid);
  if (d.error) return;
  applyProjectPayload(d, lastWrite < at);
  emit();
}

// Save is the ONE writer of reel.json. The server never mutates it (see
// server.py's run_line note) — every edit, including a reroll's seed bump,
// happens here first. A 409 means someone else (another tab, another session)
// wrote it since we last read; reload rather than silently clobbering them.
export async function save() {
  const r = await api.putProject(S.pid, S.manifest, S.rev);
  if (r.error === "stale") {
    toast("reel.json changed elsewhere — reloaded; redo your last edit", "warn");
    await refreshProject();
    return false;
  }
  if (r.error) { toast(r.error, "error"); return false; }
  S.rev = r.rev;
  lastWrite = Date.now();
  // Every writer lands here, so this is the one place that can keep the Update
  // button in sync. Emitting is not optional: without it the button keeps the
  // disabled state from the last render and silently swallows the click.
  if (r.stale) { S.stale = r.stale; emit(); }
  return true;
}

// One button: the server works out which steps are actually stale when the job
// starts, so it can never run the wrong ones because the label was out of date.
export async function update() { return run(["auto"]); }

let debounceTimer = null;
// Edits that don't cost a render (text, numbers, drags) autosave in the
// background; anything that DOES cost a render calls save() explicitly first,
// synchronously, so the job never starts against a stale file.
export function autosave() {
  clearTimeout(debounceTimer);
  // save() emits with the new staleness
  debounceTimer = setTimeout(() => { debounceTimer = null; save(); }, 500);
}

// Work done outside this tab (the command line, another session) rewrites media
// this tab still holds under old URLs, and a <video> whose file changed under it
// just stalls — play does nothing. Coming back to the tab picks up the new
// versions. Skipped while an edit is waiting to save, so it cannot clobber it.
// iOS Safari often skips `focus` when you switch back from another app, so
// listen for the tab becoming visible too.
function comeBack() {
  if (document.visibilityState !== "visible") return;
  if (S.pid && S.loaded && !S.job.running && !debounceTimer) refreshProject();
}
window.addEventListener("focus", comeBack);
document.addEventListener("visibilitychange", comeBack);

export async function run(steps) {
  if (S.job.running) return false;
  if (!(await save())) return false;
  const r = await api.run(S.pid, steps);
  if (r.error) { toast(r.error, "error"); return false; }
  pollJob();
  return true;
}

// Reroll (force) bumps the line's seed to get a different take of the same
// words — the one thing Update will never do for you, since it only renders
// audio that is missing for the current (text, exag, cfg, seed). It is also the
// narrow path: one line, then rebuild and re-mux, instead of the whole pipeline.
export async function renderLine(i, force) {
  // silently doing nothing is what made this button feel broken
  if (S.job.running) { toast("something is already rendering — wait for it", "warn"); return false; }
  if (force) {
    const l = S.manifest.vo.lines[i];
    l.seed = (l.seed == null ? (S.manifest.vo.seed ?? 0) : +l.seed) + 1;
  }
  if (!(await save())) return false;
  emit();
  const r = await api.renderLine(S.pid, i, force);
  if (r.error) { toast(r.error, "error"); return false; }
  await pollJob();
  if (!S.job.error) {
    const seed = S.manifest.vo.lines[i]?.seed;
    toast(force ? `line ${i + 1} rerolled — new take at seed ${seed}`
                : `line ${i + 1} rendered`, "ok");
  }
  return true;
}

export async function generateClip(spec) {
  if (S.job.running) return false;
  const r = await api.generate(S.pid, spec);
  if (r.error) { toast(r.error, "error"); return false; }
  pollJob();
  return true;
}

let polling = false;
async function pollJob() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      const s = await api.status(S.pid);
      S.job = s;
      emit();
      if (!s.running) break;
      await new Promise((r) => setTimeout(r, 1000));
    }
    await refreshProject();
    if (S.job.error) toast(S.job.error, "error");
  } finally {
    polling = false;
  }
}

export function busy() { return S.job.running; }
