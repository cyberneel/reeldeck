// The Clips tab: what this project can generate (discovered from its own
// files — no per-project config to maintain), a library of already-rendered
// footage you can drop onto the timeline, and a prompt queue for clips that
// don't exist as a template yet.
import { api } from "./api.js";
import { S, subscribe, emit, generateClip, toast, refreshProject } from "./store.js";

let els = {};
let genKind = "html";
let selected = null;         // library file path of the clip being inspected
let overwriteTarget = null;  // set by remake(): generate replaces this file

// How a clip was made, so it can be remade rather than only duplicated. A
// segment's own `render` block is the exact answer when one points at this
// file; otherwise fall back to a generator whose name matches the filename,
// which is how manim scenes and most templates land.
function originOf(file) {
  const stem = file.split("/").pop().replace(/\.\w+$/, "");
  const seg = (S.manifest?.segments || []).find((s) => s.src === file && s.render);
  if (seg) return { ...seg.render, kind: seg.render.kind || (seg.render.html ? "html" : null), from: `segment "${seg.name}"` };
  const disc = d();
  const scene = (disc.manim_scenes || []).find((s) => s.scene === stem);
  if (scene) return { kind: "manim", file: scene.file, scene: scene.scene, from: "manim scene" };
  const tpl = (disc.html_templates || []).find((t) => t.name === stem);
  if (tpl) return { kind: "html", html: tpl.file, from: "html template" };
  return null;
}

export function mount(root) {
  root.innerHTML = `
    <div id="tlLayout">
      <div>
        <section>
          <h2>Generate <span class="sub" id="genPipe"></span></h2>
          <div class="fld"><label>kind</label>
            <select id="genKind"><option value="html">HTML template — deterministic, frame-seeked</option>
              <option value="manim">Manim scene</option>
              <option value="capture">Chrome capture beat</option></select></div>
          <div id="genForm"></div>
          <div class="row" style="margin-top:10px"><button id="genGo" class="go">Generate</button></div>
          <div class="hint" id="genHint"></div>
        </section>

        <section>
          <h2>Library <span class="sub" id="libSum"></span>
            <span class="grow"></span>
            <button class="mini" id="upBtn">upload footage</button></h2>
          <input type="file" id="upInput" accept="video/*" multiple hidden>
          <div id="libGrid" class="clipgrid"></div>
          <div class="hint" id="upHint">Drop video files here (or use the button)
            to add your own footage — b-roll, screen recordings, anything shot on
            a phone. It is re-encoded to this reel's frame rate so it scrubs and
            cuts like a generated clip, then appears above ready to drop on the
            timeline.</div>
        </section>
      </div>

      <div>
        <section id="clipDetail"></section>
        <section>
          <h2>Request a new clip <span class="sub">queued, not automatic</span></h2>
          <div class="hint">This is a prompt queue, not live AI codegen — running
            arbitrary generated code in a real browser unattended is not something
            this app does on its own. A request just parks the ask; a build
            session (you, or Claude Code working in this project) writes the
            actual clip and marks it fulfilled.</div>
          <div class="fld" style="margin-top:9px"><label>prompt</label>
            <textarea id="reqPrompt" rows="3" placeholder="a clip of the logo spinning into place, ends on a hard cut"></textarea></div>
          <div class="row">
            <div class="fld" style="flex:1"><label>duration hint (s)</label><input class="num" id="reqDur"></div>
          </div>
          <div class="fld"><label>notes</label><input id="reqNotes" placeholder="palette, pacing, anything specific"></div>
          <div class="row"><button id="reqGo" class="go">Add to queue</button></div>
        </section>

        <section>
          <h2>Queue <span class="sub" id="reqSum"></span>
            <span class="grow"></span>
            <button class="mini" id="reqCopy">copy for Claude Code</button></h2>
          <div class="hint">Nothing here runs on its own — there is no start
            button. Hand the queue to a build session: open Claude Code in this
            project and say <b>“work the reeldeck clip queue”</b>, or copy the
            prompt above. It edits the template or scene, regenerates the clip,
            and marks the request fulfilled; <b>Update</b> then folds it into the
            reel.</div>
          <div id="reqList"></div>
        </section>

        <section>
          <h2>Log <span class="sub" id="running"></span></h2>
          <pre class="log" id="log">idle</pre>
        </section>
      </div>
    </div>`;

  els = {
    genKind: root.querySelector("#genKind"), genForm: root.querySelector("#genForm"),
    genHint: root.querySelector("#genHint"), genPipe: root.querySelector("#genPipe"),
    libGrid: root.querySelector("#libGrid"), libSum: root.querySelector("#libSum"),
    upBtn: root.querySelector("#upBtn"), upInput: root.querySelector("#upInput"),
    upHint: root.querySelector("#upHint"),
    reqPrompt: root.querySelector("#reqPrompt"), reqDur: root.querySelector("#reqDur"),
    reqNotes: root.querySelector("#reqNotes"), reqList: root.querySelector("#reqList"),
    reqSum: root.querySelector("#reqSum"), detail: root.querySelector("#clipDetail"),
    log: root.querySelector("#log"), running: root.querySelector("#running"),
  };

  els.genKind.addEventListener("change", (e) => { genKind = e.target.value; drawGenForm(); });
  root.querySelector("#genGo").onclick = doGenerate;
  root.querySelector("#reqGo").onclick = addRequest;
  root.querySelector("#reqCopy").onclick = copyQueue;

  els.upBtn.onclick = () => els.upInput.click();
  els.upInput.onchange = (e) => uploadFiles([...e.target.files]);
  wireDrop(root.querySelector("#libGrid"));
  wireDrop(els.upHint);

  root.addEventListener("click", onClick);

  subscribe(render);
  render();
}

/* Uploading footage.
 *
 * The library is just the project's out/*.mp4, so an uploaded clip needs no new
 * concept anywhere — once the server has landed it there it is listed, thumbed
 * and droppable exactly like a generated one. Files go up one at a time rather
 * than in parallel: the server serialises jobs anyway, and a queue of four
 * phone clips all transcoding at once would just fight over the same cores.
 */
function wireDrop(el) {
  if (!el) return;
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  el.addEventListener("dragover", (e) => { stop(e); el.classList.add("dropping"); });
  el.addEventListener("dragleave", (e) => { stop(e); el.classList.remove("dropping"); });
  el.addEventListener("drop", (e) => {
    stop(e);
    el.classList.remove("dropping");
    uploadFiles([...(e.dataTransfer?.files || [])]);
  });
}

let uploading = false;

async function uploadFiles(files) {
  const vids = files.filter((f) => f.type.startsWith("video/") ||
                                   /\.(mp4|mov|m4v|webm|mkv|avi)$/i.test(f.name));
  if (!vids.length || !S.pid) return;
  if (uploading) { els.upHint.textContent = "still importing the last one…"; return; }
  uploading = true;
  els.upBtn.disabled = true;
  try {
    for (let i = 0; i < vids.length; i++) {
      const f = vids[i];
      const mb = (f.size / 1048576).toFixed(1);
      els.upHint.textContent =
        `importing ${f.name} (${mb} MB)${vids.length > 1 ? ` — ${i + 1} of ${vids.length}` : ""}…`;
      const row = await api.upload(S.pid, f);
      if (row.error) { els.upHint.textContent = `could not import ${f.name}: ${row.error}`; break; }
      els.upHint.textContent = `imported ${row.name}`;
      await refreshProject();
    }
  } finally {
    uploading = false;
    els.upBtn.disabled = false;
    els.upInput.value = "";
  }
}


function d() { return S.discovery || { html_templates: [], manim_scenes: [], capture_beats: [], static: [], clips_dir: null }; }

function drawGenForm() {
  const disc = d();
  if (genKind === "html") {
    const opts = disc.html_templates;
    els.genForm.innerHTML = !opts.length ? `<div class="hint">no *.html templates found next to render.mjs</div>` : `
      <div class="fld"><label>template</label><select id="gHtml">
        ${opts.map((t) => `<option value="${t.file}">${t.name}${t.retimes ? " (retimes)" : ""}</option>`).join("")}
      </select></div>
      <div id="gParams"></div>
      <div class="row">
        <div class="fld" style="flex:1"><label>secs</label><input class="num" id="gSecs" value="2.0"></div>
        <div class="fld" style="flex:2"><label>filename</label><input id="gFile" placeholder="auto"></div>
      </div>`;
    const sel = els.genForm.querySelector("#gHtml");
    const drawParams = () => {
      const t = opts.find((o) => o.file === sel.value);
      els.genForm.querySelector("#gParams").innerHTML = (t?.params || [])
        .filter((p) => p !== "secs")
        .map((p) => `<div class="fld"><label>${p}</label><input data-param="${p}"></div>`).join("");
      els.genHint.textContent = t?.retimes
        ? "this template reflows its own animation to fit secs"
        : "secs trims or freeze-holds the last frame — it does not reflow the animation";
    };
    sel?.addEventListener("change", drawParams);
    drawParams();
  } else if (genKind === "manim") {
    const opts = disc.manim_scenes;
    els.genForm.innerHTML = !opts.length ? `<div class="hint">no manim Scene classes found</div>` : `
      <div class="fld"><label>scene</label><select id="gScene">
        ${opts.map((s) => `<option value="${s.file}::${s.scene}">${s.scene} <span class="stat">(${s.file})</span></option>`).join("")}
      </select></div>`;
    els.genHint.textContent = "renders at the project's fps; lands at out/<Scene>.mp4";
  } else {
    const opts = disc.capture_beats;
    els.genForm.innerHTML = !opts.length ? `<div class="hint">no capture-*.mjs with a BEATS object found</div>` : `
      <div class="fld"><label>beat</label><select id="gBeat">
        ${opts.map((b) => `<option value="${b.script}::${b.beat}">${b.beat} <span class="stat">(${b.script})</span></option>`).join("")}
      </select></div>`;
    els.genHint.textContent = "drives a real headed browser against your app — takes a while, and needs the app already running";
  }
}

async function doGenerate() {
  let spec;
  if (genKind === "html") {
    const html = els.genForm.querySelector("#gHtml")?.value;
    if (!html) return toast("no template available", "warn");
    const params = {};
    els.genForm.querySelectorAll("[data-param]").forEach((i) => { if (i.value) params[i.dataset.param] = i.value; });
    spec = { kind: "html", html, secs: +els.genForm.querySelector("#gSecs").value || undefined,
             params, filename: els.genForm.querySelector("#gFile").value.trim() || undefined };
  } else if (genKind === "manim") {
    const v = els.genForm.querySelector("#gScene")?.value;
    if (!v) return toast("no scene available", "warn");
    const [file, scene] = v.split("::");
    spec = { kind: "manim", file, scene };
  } else {
    const v = els.genForm.querySelector("#gBeat")?.value;
    if (!v) return toast("no beat available", "warn");
    const [script, beat] = v.split("::");
    spec = { kind: "capture", script, beat };
  }
  // remake() aimed us at an existing file; without this the server would
  // sidestep it and write a "-2" copy the timeline is not pointing at
  if (overwriteTarget) {
    spec.filename = overwriteTarget.split("/").pop().replace(/\.\w+$/, "");
    spec.overwrite = true;
  }
  const target = overwriteTarget;
  overwriteTarget = null;
  await generateClip(spec);
  if (target) toast(`${target.split("/").pop()} rebuilt`, "ok");
}

// Load an existing clip's own generator and settings into the Generate form,
// aimed back at the same filename — so "generate" edits this clip in place
// instead of leaving a -2 copy beside it.
function remake(file) {
  const origin = originOf(file);
  if (!origin) return toast("no generator matches this clip", "warn");
  genKind = origin.kind;
  els.genKind.value = genKind;
  drawGenForm();
  const stem = file.split("/").pop().replace(/\.\w+$/, "");
  if (genKind === "html") {
    const sel = els.genForm.querySelector("#gHtml");
    if (sel && origin.html) { sel.value = origin.html; sel.dispatchEvent(new Event("change")); }
    if (origin.secs != null) els.genForm.querySelector("#gSecs").value = origin.secs;
    for (const [k, v] of Object.entries(origin.params || {})) {
      const i = els.genForm.querySelector(`[data-param="${k}"]`);
      if (i) i.value = v;
    }
    els.genForm.querySelector("#gFile").value = stem;
  } else if (genKind === "manim") {
    const sel = els.genForm.querySelector("#gScene");
    if (sel) sel.value = `${origin.file}::${origin.scene}`;
  }
  overwriteTarget = file;
  els.genHint.textContent = `remaking ${stem} — generating will overwrite it in place`;
  els.genForm.scrollIntoView({ block: "center", behavior: "smooth" });
  toast(`loaded ${stem} into the form`);
}

async function queueChange(file) {
  const box = els.detail.querySelector("#chgPrompt");
  const prompt = (box?.value || "").trim();
  if (!prompt) return toast("describe the change first", "warn");
  const r = await api.addRequest(S.pid, prompt, null, "", file);
  if (r.error) return toast(r.error, "error");
  box.value = "";
  S.requests.push(r);
  emit();
  toast(`change queued for ${file.split("/").pop()}`, "ok");
}

// The queue is fulfilled by a build session, so the most useful button is the
// one that hands the whole thing over in a form that session can act on.
async function copyQueue() {
  const pending = S.requests.filter((r) => r.status === "pending");
  if (!pending.length) return toast("nothing pending", "warn");
  const body = [
    `Work the reeldeck clip queue in ${S.projectDir || "this project"}.`,
    `For each request: edit the template/scene it names, regenerate that clip`,
    `(reeldeck's Clips tab or pipeline.py), and mark it fulfilled.`,
    "",
    ...pending.map((r, n) => [
      `${n + 1}. ${r.target ? `CHANGE ${r.target}` : "NEW CLIP"} — id ${r.id}`,
      `   ${r.prompt}`,
      r.duration_hint ? `   target duration: ${r.duration_hint}s` : "",
      r.notes ? `   notes: ${r.notes}` : "",
    ].filter(Boolean).join("\n")),
  ].join("\n");
  // navigator.clipboard needs a secure context, which plain http over the
  // tailnet is not — execCommand still works there. No dialogs: a modal
  // prompt() blocks every later interaction with the page.
  try {
    await navigator.clipboard.writeText(body);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = body;
    ta.style.cssText = "position:fixed;top:-1000px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(`copied ${pending.length} request(s) — paste into Claude Code`, "ok");
}

async function addRequest() {
  const prompt = els.reqPrompt.value.trim();
  if (!prompt) return;
  const r = await api.addRequest(S.pid, prompt, els.reqDur.value ? +els.reqDur.value : null, els.reqNotes.value);
  if (r.error) return toast(r.error, "error");
  els.reqPrompt.value = ""; els.reqDur.value = ""; els.reqNotes.value = "";
  S.requests.push(r);
  emit();
}

function onClick(e) {
  const b = e.target;

  const card = b.closest?.("[data-pick]");
  if (card && b.dataset.insert == null) {
    selected = card.dataset.pick === selected ? null : card.dataset.pick;
    emit();
    return;
  }
  if (b.dataset.deselect != null) { selected = null; emit(); return; }
  if (b.dataset.remake != null) { remake(b.dataset.remake); return; }
  if (b.dataset.changereq != null) { queueChange(b.dataset.changereq); return; }

  if (b.dataset.insert != null) {
    const file = b.dataset.insert, name = b.dataset.name || "";
    import("./timeline.js").then((tl) => {
      tl.insertClip(file, name);
      window.reeldeckSwitchTab?.("timeline");
      toast(`added "${name}" to the timeline`);
    });
  }
  if (b.dataset.reqdel != null) {
    api.deleteRequest(S.pid, b.dataset.reqdel).then(() => {
      S.requests = S.requests.filter((r) => r.id !== b.dataset.reqdel);
      emit();
    });
  }
  if (b.dataset.preview != null) window.open(api.mediaUrl(S.pid, b.dataset.preview), "_blank");
}

function drawDetail() {
  const f = (d().static || []).find((x) => x.file === selected);
  const box = els.detail;
  if (!f) {
    box.innerHTML = `<h2>Clip</h2><div class="hint">Click a clip in the library to
      inspect it, remake it, or ask for changes to it.</div>`;
    return;
  }
  const origin = originOf(f.file);
  const kb = f.bytes ? (f.bytes / 1048576).toFixed(1) + " MB" : "";
  box.innerHTML = `
    <h2>Clip <span class="sub">${f.name}</span><span class="grow"></span>
      <button class="mini" data-deselect="1">✕</button></h2>
    <img class="detailthumb" src="${api.thumbUrl(S.pid, f.file, f.mtime)}" alt="">
    <div class="hint" style="margin-top:6px">${f.file} · ${kb}</div>
    <div class="row" style="margin-top:7px">
      ${f.used_by?.length
        ? `<span class="badge o">in use</span><span class="stat">${f.used_by.join(", ")}</span>`
        : `<span class="badge">unused</span>`}
    </div>
    <div class="row" style="margin-top:9px">
      <button class="mini" data-insert="${f.file}" data-name="${f.name}">+ insert</button>
      <button class="mini" data-preview="${f.file}">open</button>
      ${origin ? `<button class="mini" data-remake="${f.file}">remake…</button>` : ""}
    </div>
    <div class="hint">${origin
      ? `made from a ${origin.kind} generator (found via ${origin.from}) — <b>remake</b>
         loads it into the form above with this clip's own settings, so generating
         overwrites it in place`
      : `no generator matches this file, so it can only be changed by request`}</div>
    <div class="fld" style="margin-top:11px"><label>ask for a change to this clip</label>
      <textarea id="chgPrompt" rows="3" placeholder="slow the first beat, drop the subtitle, end on the logo"></textarea></div>
    <div class="row"><button class="mini go" data-changereq="${f.file}">Queue change</button></div>`;
}

function render() {
  if (!S.manifest) return;
  const disc = d();
  els.genPipe.textContent = disc.clips_dir ? `clips dir: ${disc.clips_dir}` : "no render.mjs found in this project";
  if (!els.genForm.dataset.built) { drawGenForm(); els.genForm.dataset.built = "1"; }

  els.libGrid.innerHTML = disc.static.map((f) => `
    <div class="clipcard${selected === f.file ? " sel" : ""}" data-pick="${f.file}">
      <div class="thumbwrap">
        <img loading="lazy" src="${api.thumbUrl(S.pid, f.file, f.mtime)}" alt="">
        ${f.used ? `<span class="badge o" style="position:absolute;top:6px;left:6px"
          title="used by ${(f.used_by || []).join(", ")}">in use</span>` : ""}
      </div>
      <div class="ccname">${f.name}</div>
      <button class="mini" data-insert="${f.file}" data-name="${f.name}">+ insert</button>
    </div>`).join("") || `<div class="hint">nothing rendered yet — generate something above</div>`;
  const inUse = disc.static.filter((f) => f.used).length;
  els.libSum.textContent = `${disc.static.length} file(s) · ${inUse} in use`;
  drawDetail();

  els.reqList.innerHTML = S.requests.map((r) => `
    <div class="reqcard">
      <div class="row"><span class="badge ${r.status === "fulfilled" ? "g" : ""}">${r.status}</span>
        ${r.target ? `<span class="badge o" title="${escapeHtml(r.target)}">change</span>` : ""}
        <span class="grow"></span><button class="mini" data-reqdel="${r.id}">✕</button></div>
      ${r.target ? `<div class="stat">${escapeHtml(r.target.split("/").pop())}</div>` : ""}
      <div style="margin-top:4px">${escapeHtml(r.prompt)}</div>
      ${r.notes ? `<div class="hint">${escapeHtml(r.notes)}</div>` : ""}
    </div>`).join("") || `<div class="hint">no pending requests</div>`;
  els.reqSum.textContent = `${S.requests.filter((r) => r.status === "pending").length} pending`;

  els.log.textContent = (S.job.log || []).slice().reverse().join("\n") || "idle";
  els.running.textContent = S.job.running ? "· " + S.job.step : "";
  document.querySelectorAll("#pane-clips button").forEach((b) => (b.disabled = S.job.running));
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
