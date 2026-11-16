// Voice takes: read the reel aloud while it plays, and keep what you said at
// the time you said it.
//
// The server takes the audio and hands back a row rather than writing the
// manifest itself — the editor autosaves reel.json on every keystroke, so a
// take appended server-side would be erased by the next autosave carrying the
// browser's older copy. The browser owns the manifest; the server owns the file.
import { api } from "./api.js";
import { S, toast } from "./store.js";

let rec = null, stream = null, chunks = [], startT = 0, perLine = false;

export function recording() { return !!rec; }

export function takes() {
  const vo = (S.manifest.vo = S.manifest.vo || {});
  return (vo.takes = vo.takes || []);
}

// getUserMedia simply does not exist on an insecure origin, so reaching the
// editor over the bare tailnet IP gives you no microphone at all. Name the two
// origins that do work, rather than letting it read as a broken button.
const SECURE_ORIGINS = "an https address (tailscale serve gives your phone one) or " +
  "http://localhost:8810 on this machine";

// The picked mic is this device's, not the project's, so it lives in the browser.
// Its name is kept too: browsers hand out new ids between visits (Safari does).
const MIC_KEY = "reeldeck.mic";
export function micPick() { try { return JSON.parse(localStorage.getItem(MIC_KEY)) || {}; } catch { return {}; } }
export function setMic(id, label) { try { localStorage.setItem(MIC_KEY, JSON.stringify({ id, label })); } catch {} }
// names stay blank until the mic has been allowed once
export async function mics() {
  const ds = (await navigator.mediaDevices?.enumerateDevices?.()) || [];
  return ds.filter((d) => d.kind === "audioinput" && d.deviceId);
}

async function mic() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error(
      window.isSecureContext
        ? "this browser exposes no microphone API"
        : `no microphone on ${location.origin} — a mic needs a secure origin. ` +
          `Use ${SECURE_ORIGINS}.`);
  }
  // every processor off: this is a take you will post, not a phone call, and
  // the browser's noise gate chews the ends off quiet words
  const audio = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
  const open = (id) => navigator.mediaDevices.getUserMedia({ audio: id ? { ...audio, deviceId: { exact: id } } : audio });
  const { id, label } = micPick();
  if (!id) return open();
  try {
    return await open(id);
  } catch (e) {
    if (e.name !== "OverconstrainedError" && e.name !== "NotFoundError") throw e;
  }
  // a new id: find it by name, which only shows once the default mic is open
  const s = await open();
  const d = label && (await mics()).find((d) => d.label === label);
  if (!d) { setMic("", ""); toast("that microphone is gone — using the default one"); return s; }
  setMic(d.deviceId, label);
  s.getTracks().forEach((t) => t.stop());
  return open(d.deviceId);
}

const MIMES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", ""];

/**
 * Arm the recorder, then start the reel from wherever the playhead sits.
 * `at` is stamped once the recorder is actually running rather than when the
 * button was clicked: MediaRecorder takes a moment to spin up, and a take that
 * lands late is one you have to nudge back by hand.
 *
 * With no player it is a per-line read (the Script tab): no picture runs, the
 * take goes at `at`, and the server trims it to the voice so it starts there.
 *
 * `ready` is a count-in: it runs with the mic already open, so a permission
 * prompt can't eat into it (and iOS wants the mic asked for inside the tap).
 * Recording starts once it resolves; if it rejects, the mic closes.
 */
export async function start(player, onChange, at = 0, ready) {
  stream = await mic();
  if (ready) {
    try { await ready(); }
    catch (e) { stream.getTracks().forEach((t) => t.stop()); stream = null; throw e; }
  }
  chunks = [];
  perLine = !player;
  const type = MIMES.find((t) => !t || MediaRecorder.isTypeSupported(t));
  rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = () => finish(onChange);
  rec.onstart = () => {
    startT = player ? player.t : at;
    if (player) {
      // the guide track would bleed into the take through speakers, and hearing
      // the synthesized read of the same line while you say it is its own kind of
      // unusable. ponytail: no monitor toggle — add one if you record on headphones.
      player.audio.muted = true;
      player.play();
    }
    onChange?.();
  };
  rec.start();
}

export function stop(player) {
  if (rec && rec.state !== "inactive") rec.stop();
  player?.pause();
}

async function finish(onChange) {
  const blob = new Blob(chunks, { type: chunks[0]?.type || "audio/webm" });
  stream?.getTracks().forEach((t) => t.stop());
  rec = null; stream = null;
  onChange?.();
  if (blob.size < 512) return toast("nothing recorded");
  const row = await api.take(S.pid, blob, startT, perLine);
  if (row.error) return toast(row.error);
  takes().push(row);
  toast(`take ${takes().length} · ${row.dur.toFixed(1)}s`);
  onChange?.(row);
}
