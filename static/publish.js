// The Publish tab: every reel with a cut, exactly what Instagram would get for
// it, and the queue that posts it (publish.py). Unlike the other tabs it isn't
// per-project: it's the whole catalogue, so it fetches its own list instead of
// reading the store. publish_mcp.py drives the same endpoints, so what an agent
// schedules shows up here and the other way round.
import { api } from "./api.js";
import { toast } from "./store.js";
import { isTyping } from "./dom.js";

let root;
let data = { posts: [], account: {} };
let quota = null;
const dirty = new Set();   // cards with unsaved edits survive a refresh untouched
let armed = null;          // the Schedule button waiting for its confirming tap
let folded = new Set();    // reels folded down to their title row, remembered in this browser
try { folded = new Set(JSON.parse(localStorage.getItem("pubFolded") || "[]")); } catch {}

const LIVE = ["queued", "staging", "processing", "staged", "publishing"];
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
const when = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
// datetime-local wants local wall time with no zone
const local = (t) => new Date(t * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
const tagsOf = (c) => ({ h: c.match(/(?<!\w)#\w+/g) || [], m: c.match(/(?<![\w.])@[\w.]+/g) || [] });

export function mount(el) {
  root = el;
  // the token box is drawn once, so a refresh never wipes a half-pasted token
  root.innerHTML = `<section id="pubHead"></section>
    <details id="pubToken"><summary>Instagram token</summary>
      <ol class="hint">
        <li>The Instagram account must be a professional (Business or Creator) account, linked to a Facebook Page.</li>
        <li><a href="https://developers.facebook.com/tools/explorer/" target="_blank">Graph API Explorer</a> → your Meta app → User Token, tick
          instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement, and for the Comments tab
          instagram_manage_comments → Generate, approve.</li>
        <li>ⓘ next to the token → Open in Access Token Tool → <b>Extend Access Token</b> (lasts 60 days).</li>
        <li>Paste it here. reeldeck checks it with Meta, finds the Instagram account, and saves it to
          ~/.config/reeldeck/instagram.json (600). It never shows it again.</li>
      </ol>
      <div class="row"><input type="password" id="pubTok" autocomplete="off" placeholder="EAA…" class="grow">
        <button data-a="token" class="go">Save token</button></div>
      <p class="hint"><b>Trial reels</b> post through Instagram Login, which needs a second token (the one above posts them as normal reels):</p>
      <ol class="hint">
        <li><a href="https://developers.facebook.com/apps/" target="_blank">Meta app</a> → Add product → <b>Instagram</b> →
          API setup with Instagram business login.</li>
        <li>Add the Instagram account (App roles → Roles → Instagram Testers if it asks, then accept the invite in Instagram).</li>
        <li><b>Generate token</b> next to the account, allow instagram_business_basic and instagram_business_content_publish (lasts 60 days; reeldeck renews it weekly).</li>
        <li>Paste it here. It must be the same account; it's saved to ~/.config/reeldeck/instagram-login.json (600).</li>
      </ol>
      <div class="row"><input type="password" id="pubIgTok" autocomplete="off" placeholder="IG…" class="grow">
        <button data-a="igtoken" class="go">Save trial token</button></div>
    </details>
    <div id="pubList"></div>`;
  root.addEventListener("input", onInput);
  root.addEventListener("change", onInput);
  root.addEventListener("click", onClick);
  setInterval(() => {
    if (root.classList.contains("active") && !isTyping(root)) load();
  }, 20000);
}

export async function shown() {
  await load();
  if (data.account.token && !quota) {
    const q = await api.publishQuota();
    if (!q.error) { quota = q; head(); }
  }
}

async function load() {
  const d = await api.publishList();
  if (d.error) { toast(d.error, "error"); return; }
  data = d;
  render();
}

function head() {
  const a = data.account;
  const n = (s) => data.posts.filter(s).length;
  root.querySelector("#pubHead").innerHTML = `
    <h2>Publish <span class="sub">${n((p) => LIVE.includes(p.status))} going out ·
      ${n((p) => p.status === "draft" && !p.problems.length)} ready · ${n((p) => p.status === "published")} posted</span></h2>
    <div class="row">
      ${a.token ? `<span class="badge g">connected</span>` : `<span class="badge o">dry run</span>`}
      <span class="stat">${esc(a.note || (quota ? `${quota.used ?? "?"}/${quota.total ?? 100} posts in the last 24 h` : ""))}</span>
      <span class="stat">${esc(a.ig_note || "")}</span>
    </div>
    <div class="hint">Only reels set to ready (the picker by the timeline's zoom) show here. Scheduling is the approval: a reel with a time posts publicly then, staged two hours
      ahead (nothing is public until the slot). Editing a staged reel drops its upload. The API takes no
      licensed music or filters.</div>`;
  const tb = root.querySelector("#pubToken");
  if (!a.token && !tb.dataset.seen) { tb.open = true; tb.dataset.seen = 1; }
}

function render() {
  head();
  const g = { live: [], ready: [], trouble: [], unready: [], done: [] };
  for (const p of data.posts) {
    const k = LIVE.includes(p.status) ? "live" : p.status === "published" ? "done"
      : ["failed", "missed", "error"].includes(p.status) ? "trouble" : p.problems.length ? "unready" : "ready";
    g[k].push(p);
  }
  const list = root.querySelector("#pubList");
  const keep = new Map([...dirty].map((pid) => [pid, list.querySelector(`.pub[data-pid="${pid}"]`)]));
  const was = new Map([...list.querySelectorAll(".pubgroup")].map((d) => [d.dataset.g, d.open]));   // a refresh leaves each group as it was
  const block = (title, ps, open = true) => !ps.length ? "" : `
    <details class="pubgroup" data-g="${title}"${was.get(title) ?? open ? " open" : ""}><summary>${title} <span class="sub">${ps.length}</span></summary>
      ${ps.map(card).join("")}</details>`;
  list.innerHTML = block("Going out", g.live) + block("Needs a look", g.trouble) + block("Ready", g.ready) +
    block("Not ready: no caption, a hold, or Instagram would refuse it", g.unready, false) + block("Posted", g.done, false);
  for (const [pid, old] of keep) {
    const n = list.querySelector(`.pub[data-pid="${pid}"]`);
    if (n && old) n.replaceWith(old);
  }
  list.querySelectorAll(".pub").forEach(counts);
}

function status(p) {
  const last = p.log?.at(-1)?.[1] || "";
  return {
    draft: p.problems.length ? ["not ready", ""] : ["ready", "g"],
    queued: [`queued · public ${p.slot ? when(p.slot) : ""}`, "o"],
    staging: ["uploading…", "o"],
    processing: [`Instagram is processing · public ${p.slot ? when(p.slot) : "not scheduled"}`, "o"],
    staged: [p.slot ? `staged · public ${when(p.slot)}` : `staged, not scheduled · expires ${when((p.container?.at || 0) + 86400)}`, "o"],
    publishing: ["publishing…", "o"],
    published: ["posted", "g"],
    failed: [last || "failed", "bad"],
    missed: ["missed its slot: not posted", "bad"],
    error: [p.problems?.[0] || "error", "bad"],
  }[p.status] || [p.status, ""];
}

function card(p) {
  if (p.status === "error") {
    return `<section class="pub" data-pid="${p.pid}"><b>${esc(p.name)}</b><div class="bad">${esc(p.problems[0])}</div></section>`;
  }
  const s = p.settings, pr = p.probe || {};
  const [st, cls] = status(p);
  const locked = ["staging", "publishing", "published"].includes(p.status);
  const frame = poster(p.pid, s.cover, p.video, s.thumb_offset);
  const f = folded.has(p.pid);
  return `<section class="pub${f ? " folded" : ""}" data-pid="${p.pid}">
    <div class="pubPhone">
      <video src="${p.video ? api.mediaUrl(p.pid, p.video) : ""}" poster="${frame}" preload="none" loop playsinline></video>
      <div class="pubOverlay">${overlay(p.caption, s.audio_name)}</div>
    </div>
    <div class="pubForm">
      <div class="row pubTitle"><button data-a="fold" class="pubFold" aria-expanded="${!f}" title="collapse / expand this reel"><b>${esc(p.name)}</b></button>${p.folder ? `<span class="stat">${esc(p.folder)}</span>` : ""}
        <span class="grow"></span><span class="badge ${cls}">${esc(st)}</span></div>
      ${p.problems.length ? `<ul class="pubProblems">${p.problems.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <div class="fld"><label>caption <span class="pubCount"></span></label>
        <textarea data-k="caption" rows="9"${locked ? " disabled" : ""}>${esc(p.caption)}</textarea>
        <div class="pubTags"></div></div>
      <div class="pubGrid">
        <div class="fld"><label>cut</label><select data-k="video"${locked ? " disabled" : ""}>
          ${p.videos.map((v) => `<option${v === p.video ? " selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
        <div class="fld"><label>cover</label><select data-k="cover"${locked ? " disabled" : ""}>
          ${[...p.covers, ""].map((c) => `<option value="${esc(c)}"${c === s.cover ? " selected" : ""}>${c ? esc(c) : "a frame of the cut"}</option>`).join("")}</select></div>
        <div class="fld"><label>${s.cover ? "fallback frame" : "cover frame"}: ms into the cut</label>
          <input class="num" type="number" min="0" step="100" data-k="thumb_offset" value="${s.thumb_offset}"${locked ? " disabled" : ""}></div>
        <div class="fld"><label>audio name</label>
          <input data-k="audio_name" value="${esc(s.audio_name)}" placeholder="Original audio"${locked ? " disabled" : ""}></div>
        <div class="fld"><label>trial reel</label><select data-k="trial"${locked ? " disabled" : ""}>
          ${[["", "off: show to followers"], ["MANUAL", "trial, I graduate it"], ["SS_PERFORMANCE", "trial, graduates on performance"]]
            .map(([v, l]) => `<option value="${v}"${v === s.trial ? " selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="fld"><label>collaborators (max 3)</label>
          <input data-k="collaborators" value="${esc(s.collaborators.join(" "))}" placeholder="@someone"${locked ? " disabled" : ""}></div>
        <div class="fld"><label>tag people</label>
          <input data-k="user_tags" value="${esc(s.user_tags.join(" "))}" placeholder="@someone"${locked ? " disabled" : ""}></div>
        <div class="fld"><label>location id</label>
          <input data-k="location_id" value="${esc(s.location_id)}" placeholder="Facebook place id"${locked ? " disabled" : ""}></div>
        <div class="fld"><label class="pubCheck"><input type="checkbox" data-k="share_to_feed"${s.share_to_feed ? " checked" : ""}${locked ? " disabled" : ""}>
          also in the main feed</label></div>
      </div>
      <div class="stat">${pr.w ? `${pr.w}×${pr.h} · ${pr.fps} fps · ${pr.dur} s · ${pr.mb} MB · ${pr.vcodec} / ${pr.acodec} ${pr.akbps}k → 128k on upload` : ""}</div>
      <div class="hint">${s.cover ? `Cover: ${esc(s.cover)} (${p.cover_mb} MB). Meta fetches it from a temporary public link
        while it processes the upload; if the link can't be made, the frame at ${s.thumb_offset} ms is the cover.`
        : `Cover: the frame at ${s.thumb_offset} ms.`}</div>
      <div class="row pubActs">
        ${p.status === "published" ? (p.permalink ? `<a href="${esc(p.permalink)}" target="_blank">open the post ↗</a>` : `<span class="stat">posted outside reeldeck</span>`) : `
        <button data-a="save" class="pubSave" hidden>Save</button>
        <button data-a="stage"${locked ? " disabled" : ""} title="${data.account.token ? "upload now as an unpublished container: nothing public, expires in 24 h" : "no token: build the upload copy and show the request, send nothing"}">${data.account.token ? "Stage now" : "Dry run"}</button>
        <span class="grow"></span>
        <input type="datetime-local" data-k="slot" value="${p.slot ? local(p.slot) : ""}"${locked ? " disabled" : ""}>
        <button data-a="now" class="mini" title="fill in now"${locked ? " disabled" : ""}>now</button>
        ${p.slot && !locked ? `<button data-a="unschedule">Unschedule</button>` : ""}
        <button data-a="schedule" class="go"${locked || p.problems.length ? " disabled" : ""}>${p.slot ? "Reschedule" : "Schedule"}</button>`}
      </div>
      <details><summary>what Instagram gets</summary><pre class="pubReq">${esc(JSON.stringify(p.request, null, 1))}</pre></details>
      ${p.log.length ? `<details><summary>log</summary><div class="pubLog">${p.log.slice().reverse()
        .map(([t, m]) => `<div><span class="stat">${when(t)}</span> ${esc(m)}</div>`).join("")}</div></details>` : ""}
    </div>
  </section>`;
}

// Roughly how the caption sits on a reel: two lines, then "… more"
function overlay(caption, audio) {
  const all = caption.replace(/\s+/g, " ").trim();
  const short = all.length > 90 ? all.slice(0, 90).replace(/\s\S*$/, "") : all;
  const body = esc(short).replace(/(^|\s)([#@][\w.]+)/g, '$1<span class="pubHash">$2</span>');
  return `<div class="pubCap">${body}${short !== all ? "… <i>more</i>" : ""}</div>
    <div class="pubAudio">♫ ${esc(audio || "Original audio")}</div>`;
}

function cardOf(el) { return el.closest(".pub"); }

// what the phone preview shows before play: the cover jpg, or the frame Instagram would take
function poster(pid, cover, video, ms) {
  return cover ? api.mediaUrl(pid, cover) : video ? api.thumbUrl(pid, video, null, ms / 1000) : "";
}

function counts(c) {
  const box = c.querySelector('[data-k="caption"]');
  if (!box) return;
  const t = box.value, { h, m } = tagsOf(t);
  const over = (n, max) => (n > max ? ` class="bad"` : "");
  c.querySelector(".pubCount").innerHTML =
    `<span${over(t.length, 2200)}>${t.length}/2200</span> · <span${over(h.length, 30)}>${h.length}/30 #</span> · <span${over(m.length, 20)}>${m.length}/20 @</span>`;
  c.querySelector(".pubTags").innerHTML = h.map((x) => `<span class="pflag">${esc(x)}</span>`).join("");
}

function onInput(e) {
  const c = cardOf(e.target), k = e.target.dataset.k;
  if (!c || !k || k === "slot") return;
  dirty.add(c.dataset.pid);
  c.querySelector(".pubSave").hidden = false;
  if (k === "caption" || k === "audio_name") {
    c.querySelector(".pubOverlay").innerHTML = overlay(c.querySelector('[data-k="caption"]').value,
      c.querySelector('[data-k="audio_name"]').value);
    counts(c);
  }
  if (k === "thumb_offset" || k === "cover" || k === "video") {
    const v = (x) => c.querySelector(`[data-k="${x}"]`).value;
    c.querySelector("video").poster = poster(c.dataset.pid, v("cover"), v("video"), +v("thumb_offset") || 0);
  }
}

function formOf(c) {
  const v = (k) => c.querySelector(`[data-k="${k}"]`);
  return {
    caption: v("caption").value, video: v("video").value,
    settings: {
      cover: v("cover").value, thumb_offset: +v("thumb_offset").value || 0, audio_name: v("audio_name").value, trial: v("trial").value,
      collaborators: v("collaborators").value, user_tags: v("user_tags").value,
      location_id: v("location_id").value, share_to_feed: v("share_to_feed").checked,
    },
  };
}

async function save(c) {
  const r = await api.publishEdit(c.dataset.pid, formOf(c));
  if (r.error) { toast(r.error, "error"); return false; }
  dirty.delete(c.dataset.pid);
  return true;
}

async function onClick(e) {
  // tap the phone to play the cut as posted; the caption steps aside while it plays
  const ph = e.target.closest(".pubPhone");
  if (ph) {
    const v = ph.querySelector("video");
    if (v.paused) v.play(); else v.pause();
    ph.classList.toggle("playing", !v.paused);
    return;
  }
  const b = e.target.closest("[data-a]");
  if (b?.dataset.a === "token" || b?.dataset.a === "igtoken") {
    const ig = b.dataset.a === "igtoken";
    const box = root.querySelector(ig ? "#pubIgTok" : "#pubTok");
    b.disabled = true;
    const r = await (ig ? api.publishIgToken : api.publishToken)(box.value);
    b.disabled = false;
    if (r.error) { toast(r.error, "error"); return; }
    box.value = "";
    root.querySelector("#pubToken").open = false;
    toast(`token saved · ${ig ? r.ig_note : r.note}`);
    quota = null;
    return shown();
  }
  const c = b && cardOf(b);
  if (!c) return;
  const pid = c.dataset.pid, a = b.dataset.a;
  if (a === "fold") {
    const on = c.classList.toggle("folded");
    b.setAttribute("aria-expanded", !on);
    if (on) { c.querySelector("video").pause(); c.querySelector(".pubPhone").classList.remove("playing"); }
    folded[on ? "add" : "delete"](pid);
    try { localStorage.setItem("pubFolded", JSON.stringify([...folded])); } catch {}
    return;
  }
  if (a === "now") { c.querySelector('[data-k="slot"]').value = local(Date.now() / 1000); return; }
  if (a === "schedule") {
    const v = c.querySelector('[data-k="slot"]').value;
    if (!v) { toast("pick a time first", "error"); return; }
    const at = new Date(v).getTime() / 1000;
    // two taps: the first says exactly what the second one does
    if (armed !== b) {
      armed = b;
      const label = b.textContent;
      b.textContent = `Confirm: public ${at <= Date.now() / 1000 + 60 ? "now" : when(at)}`;
      setTimeout(() => { if (armed === b) { armed = null; b.textContent = label; } }, 6000);
      return;
    }
    armed = null;
    // schedule what's on screen, not what was last saved
    if (dirty.has(pid) && !(await save(c))) return;
    const r = await api.publishAct(pid, "schedule", { at });
    if (r.error) toast(r.error, "error"); else toast(`${r.name}: public ${when(r.slot)}`);
  } else if (a === "save") {
    if (await save(c)) toast("saved");
  } else {
    if (a === "stage" && dirty.has(pid) && !(await save(c))) return;
    const r = await api.publishAct(pid, a);
    if (r.error) toast(r.error, "error");
    else toast(a === "stage" ? (data.account.token ? "staging: uploading, nothing public" : "dry run started, see the log") : "unscheduled");
  }
  await load();
}
