// The Comments tab: every comment on a recent reel that we haven't answered,
// reel by reel, with two lines under each: a public reply and a DM to the
// commenter (publish.py inbox / reply). Like Publish it is the whole account,
// not one project. Both reach the public. They are sent by the Send button
// here, or with no tap once a reel's Auto row is switched on here: a comment
// with the reel's keyword then gets the reel's DM and a reply from the bank
// (publish.py auto_set / auto_tick), and until its turn it shows "auto, in
// line" in place of its two lines and Send.
import { api } from "./api.js";
import { toast } from "./store.js";

let root, named = false;
let auto = { replies: [], reels: {} };       // the bank of replies and each reel's auto answer, as the server has them
let turn = Math.floor(Math.random() * 50);   // the Reply lines walk the bank, so neighbours differ
const pick = () => auto.replies[turn++ % auto.replies.length] || "";
let armed = null;     // the Turn on button waiting for its confirming tap
let dms = {};         // the DM line starts with the last DM sent on that reel,
let need = true;      // and a DM that didn't go holds the reply back
try {
  dms = JSON.parse(localStorage.getItem("cmtDms") || "{}");
  need = localStorage.getItem("cmtNeed") !== "0";
} catch {}
const keep = (k, v) => { try { localStorage.setItem(k, v); } catch {} };

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
// Meta writes +0000, which Safari won't parse
const date = (t) => new Date(t.replace("+0000", "Z"));
const ago = (t) => {
  const s = (Date.now() - date(t)) / 1000;
  return s < 3600 ? `${Math.max(1, Math.round(s / 60))} min` : s < 86400 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
};

export function mount(el) {
  root = el;
  root.innerHTML = `<section><h2>Comments <span class="sub" id="cmtN"></span><span class="grow"></span>
      <button data-a="refresh">Refresh</button></h2>
      <label class="pubCheck"><input type="checkbox" id="cmtNeed"${need ? " checked" : ""}> Reply only if the DM went through</label>
      <details id="cmtBank"><summary>Reply bank <span class="sub"></span></summary>
        <textarea rows="9" placeholder="one reply per line"></textarea>
        <div class="row"><span class="hint grow">One per line. Each Reply line below starts with one of these, and an auto answer picks one at random, never the same twice running.</span>
          <button data-a="bank">Save</button></div></details>
      <div class="hint" id="cmtHint"></div></section>
    <div id="cmtList"></div>`;
  root.addEventListener("click", onClick);
  root.addEventListener("change", (e) => {
    if (e.target.id === "cmtNeed") keep("cmtNeed", (need = e.target.checked) ? "1" : "0");
  });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.matches(".cmt input")) e.target.closest(".cmt").querySelector("button").click();
  });
}

// Instagram takes seconds per busy reel, so nothing here waits for all of it:
// an empty page first paints what the server remembers, the list is one call,
// and a reel whose count moved keeps what it had while it is re-read
// ("checking…"). Remembered rows may be answered by now, which is fine: the
// server asks before it replies.
export async function shown(fresh) {
  const list = root.querySelector("#cmtList");
  if (!list.querySelector(".cmtReel")) {
    list.innerHTML = `<section class="stat">asking Instagram…</section>`;
    show(await api.comments("seen"), true);
  }
  show(await api.comments(fresh ? "fresh" : ""));
}

function show(d, old) {
  const list = root.querySelector("#cmtList");
  if (d.error) { if (!old) list.innerHTML = `<section class="bad">${esc(d.error)}</section>`; return; }
  auto = d.auto || auto;
  bank();
  // remembered reels are all unconfirmed until the real list says which moved; a reel on auto stays to be switched off
  const reels = d.reels.map((r) => old ? { ...r, stale: true } : r).filter((r) => r.stale || r.comments.length || auto.reels[r.id]?.on);
  if (old && !reels.length) return;
  [...list.children].forEach((el) => reels.some((r) => r.id === el.dataset.id) || el.remove());
  reels.forEach((r, i) => {
    put(r, i);
    if (r.stale && !old) api.reelComments(r.id).then((f) => f.error ? toast(f.error, "error") : put(f));
  });
  count();
}

// one reel onto the page, patched in place: rows already there stay (with
// whatever is typed in them), answered ones go, new ones join at the end
function put(r, i) {
  const list = root.querySelector("#cmtList");
  let el = list.querySelector(`.cmtReel[data-id="${r.id}"]`);
  auto = r.auto || auto;   // a re-read reel comes with who is in line now
  if (!el) {
    if (i == null) return;   // re-read after its reel left the list
    list.children[i] ? list.children[i].insertAdjacentHTML("beforebegin", reel(r)) : list.insertAdjacentHTML("beforeend", reel(r));
    el = list.children[i];
  }
  const have = new Map([...el.querySelectorAll(".cmt")].map((c) => [c.dataset.id, c]));
  r.comments.forEach((c) => have.delete(c.id) || el.insertAdjacentHTML("beforeend", row(c, r)));
  have.forEach((c) => c.remove());
  el.classList.toggle("stale", !!r.stale);
  named ||= r.comments.some((c) => c.username);
  autoRow(el);
  if (i == null) count();
}

// the bank as saved, unless it's being typed in
function bank() {
  const t = root.querySelector("#cmtBank textarea");
  if (document.activeElement !== t) t.value = auto.replies.join("\n");
  root.querySelector("#cmtBank .sub").textContent = auto.replies.length;
}

// a reel's Auto row as the server has it: on locks the keyword and the DM, the
// comments the pass will answer (a.due) give up their lines and Send, and a
// comment that didn't go says why under it
function autoRow(el) {
  const a = auto.reels[el.dataset.id] || {}, [word, dm] = el.querySelectorAll(".cmtAuto input"), b = el.querySelector("[data-a=auto]");
  const due = new Set(a.due);
  el.querySelectorAll(".cmt").forEach((c) => c.classList.toggle("queued", due.has(c.dataset.id)));
  if (a.on || !word.value) word.value = a.word || "BUILD";
  if (a.on || !dm.value) dm.value = a.dm || dms[el.dataset.id] || "";
  word.disabled = dm.disabled = !!a.on;
  if (armed !== b) b.textContent = a.on ? "Turn off" : "Turn on";
  el.querySelector(".cmtAutoNote").textContent = a.on
    ? `On: a comment with “${a.word}” gets the DM “${a.dm}”, then a reply from the bank. Oldest first, a few a minute. ${a.sent || 0} answered so far, ${due.size} in line.`
    : a.note || (a.sent ? `Off. ${a.sent} answered so far.` : "");
  Object.entries(a.skip || {}).forEach(([id, why]) => {
    const c = el.querySelector(`.cmt[data-id="${id}"]`);
    if (c && !c.querySelector(".cmtErr")) c.insertAdjacentHTML("beforeend", `<div class="hint cmtErr">auto: ${esc(why)}</div>`);
  });
}

const reel = (r) => `<details class="cmtReel" data-id="${esc(r.id)}" open>
  <summary>${esc(r.title || "no caption")} <span class="sub"></span></summary>
  <div class="row"><span class="stat">posted ${date(r.posted).toLocaleDateString([], { day: "numeric", month: "short" })}</span>
    <a href="${esc(r.permalink)}" target="_blank">open the reel ↗</a></div>
  <div class="row cmtAuto"><span class="stat cmtTag">Auto</span>
    <input name="word" maxlength="30" placeholder="keyword">
    <input class="grow" name="adm" maxlength="1000" placeholder="the DM everyone who comments it gets">
    <button data-a="auto">Turn on</button></div>
  <div class="hint cmtAutoNote"></div></details>`;

// <reel>/c/<comment id>/ opens the reel with that comment on top
const row = (c, { id, permalink }) => `<section class="cmt" data-id="${esc(c.id)}">
  <div class="row">${c.username ? `<a href="https://www.instagram.com/${esc(c.username)}/" target="_blank">@${esc(c.username)}</a>` : ""}
    <span class="stat">${ago(c.timestamp)} ago</span><span class="stat cmtQ">auto, in line</span><span class="grow"></span>
    ${permalink ? `<a href="${esc(permalink.replace(/\/?$/, "/"))}c/${esc(c.id)}/" target="_blank">open comment ↗</a>` : ""}</div>
  <div class="cmtText">${esc(c.text)}</div>
  <label class="row cmtLine"><span class="stat cmtTag">Reply</span>
    <input class="grow" name="reply" value="${esc(pick())}" maxlength="300" placeholder="public, under the comment"></label>
  <div class="row cmtLine"><label class="row grow"><span class="stat cmtTag">DM</span>
      <input class="grow" name="dm" value="${esc(dms[id] || "")}" maxlength="1000" placeholder="private, to the commenter"></label>
    <button data-a="reply" class="go">Send</button></div>
</section>`;

// the numbers come from what's on screen, so a sent reply just removes its row
function count() {
  root.querySelectorAll(".cmtReel").forEach((r) => {
    const n = r.querySelectorAll(".cmt").length;
    r.querySelector("summary .sub").textContent = n || "";
    if (!n && !r.matches(".stale") && !auto.reels[r.dataset.id]?.on) r.remove();
  });
  const n = root.querySelectorAll(".cmt").length, busy = root.querySelector(".cmtReel.stale");
  const q = root.querySelectorAll(".cmt.queued").length;
  root.querySelector("#cmtN").textContent = `${n - q} waiting${q ? `, ${q} in line for auto` : ""}${busy ? "…" : ""}`;
  root.querySelector("#cmtHint").textContent = "Unanswered comments on reels among your 25 newest posts, longest wait first. The DM goes first, privately to the commenter; the reply is public. An empty line is skipped."
    + (n && !named ? " Names and replies need instagram_manage_comments on the token: Publish → Instagram token." : "");
  if (!root.querySelector(".cmtReel")) root.querySelector("#cmtList").innerHTML = `<section class="stat">Every comment is answered.</section>`;
}

async function onClick(e) {
  const b = e.target.closest("[data-a]");
  if (!b) return;
  if (b.dataset.a === "refresh") return shown(true);   // distrust what the server remembers
  if (b.dataset.a === "bank" || b.dataset.a === "auto") {
    const el = b.closest(".cmtReel"), on = el && !auto.reels[el.dataset.id]?.on;
    const [word, dm] = el ? [...el.querySelectorAll(".cmtAuto input")].map((i) => i.value.trim()) : [];
    // switching on answers people with no further tap: two taps, the first says what the second does
    if (on && armed !== b) {
      if (!word || !dm) return toast("a keyword and a DM first", "error");
      const hit = new RegExp(`\\b${word.replace(/[^\w ]/g, "")}\\b`, "i");
      armed = b;
      b.textContent = `Confirm: ${[...el.querySelectorAll(".cmtText")].filter((t) => hit.test(t.textContent)).length} waiting get it`;
      setTimeout(() => { if (armed === b) { armed = null; autoRow(el); } }, 6000);
      return;
    }
    armed = null;
    const r = await api.commentAuto(el ? { reel: el.dataset.id, on, word, dm } : { replies: root.querySelector("#cmtBank textarea").value.split("\n") });
    if (r.error) return toast(r.error, "error");
    auto = r;
    bank();
    root.querySelectorAll(".cmtReel").forEach(autoRow);
    if (!el) toast("Replies saved");
    return count();
  }
  const c = b.closest(".cmt"), reel = c.closest(".cmtReel");
  const [text, dm] = ["reply", "dm"].map((n) => c.querySelector(`[name=${n}]`).value.trim());
  if (!text && !dm) return;
  b.disabled = true;
  const r = await api.commentReply(c.dataset.id, text, dm, need);
  if (r.error) {
    toast(r.error, "error");
    if (r.error === "already answered") { c.remove(); count(); return; }
    // a toast is gone before a long Meta error is read: it stays under the comment
    c.querySelector(".cmtErr")?.remove();
    c.insertAdjacentHTML("beforeend", `<div class="hint cmtErr">${esc(r.error)}</div>`);
    b.disabled = false;
    return;
  }
  if (r.dm_error) toast(`Replied, but the DM didn't go. Meta said ${r.dm_error}`, "error");
  if (r.dm) {
    keep("cmtDms", JSON.stringify(Object.assign(dms, { [reel.dataset.id]: dm })));
    reel.querySelectorAll("[name=dm]").forEach((i) => { i.value ||= dm; });   // the reel's other rows start with it too
  }
  if (r.replied) c.remove();
  else {   // a DM alone: the comment still waits for its public reply
    toast("DM sent");
    c.querySelector("[name=dm]").value = "";
    b.disabled = false;
  }
  count();
}
