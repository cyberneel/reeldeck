// Thin wrapper over the HTTP API. Every reeldeck endpoint, one place.

async function req(method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); } catch { return { error: "can't reach the ReelDeck server", offline: true }; }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await r.json() : null;
  if (!r.ok && data && !data.error) data.error = `http ${r.status}`;
  return data ?? { error: `http ${r.status}` };
}

export const api = {
  app: (wallet) => req("GET", "/api/app" + (wallet ? "?wallet=1" : "")),
  aiPress: (action) => req("POST", "/api/ai/press", { action }),
  aiSettings: (body) => req("POST", "/api/ai/settings", body),
  aiChooseAgain: () => req("POST", "/api/ai/choose-again", {}),
  assist: (body) => req("POST", "/api/ai/assist", body),
  // the Agent tab: changes proposed for an ask, then the ticked ones applied
  agent: (pid, ask) => req("POST", `/api/p/${pid}/agent`, { ask }),
  agentApply: (pid, changes, rev) => req("POST", `/api/p/${pid}/agent/apply`, { changes, _rev: rev }),
  getScript: (pid) => req("GET", `/api/p/${pid}/script`),
  putScript: (pid, doc) => req("PUT", `/api/p/${pid}/script`, doc),
  snapshotScript: (pid, label) => req("POST", `/api/p/${pid}/script/versions`, { label }),
  scriptVersion: (pid, id) => req("GET", `/api/p/${pid}/script/versions/${id}`),

  listProjects: () => req("GET", "/api/projects"),
  browseProjects: () => req("GET", "/api/projects?detail=1"),
  createProject: (name, folder) => req("POST", "/api/projects", { name, folder }),
  registerProject: (dir, name, folder) => req("POST", "/api/projects", { dir, name, folder }),
  renameProject: (pid, name) => req("PUT", `/api/projects/${pid}`, { name }),
  setFolder: (pid, folder) => req("PUT", `/api/projects/${pid}`, { folder }),
  deleteProject: (pid) => req("DELETE", `/api/projects/${pid}`),

  getProject: (pid) => req("GET", `/api/p/${pid}/project`),
  putProject: (pid, manifest, rev) =>
    req("PUT", `/api/p/${pid}/project`, { ...manifest, _rev: rev }),

  status: (pid) => req("GET", `/api/p/${pid}/status`),
  // one take through the Voice sliders; the settings ride in the query
  cleanPreview: (pid, file, tone) =>
    req("GET", `/api/p/${pid}/clean?` + new URLSearchParams({ file, ...tone })),
  taildrop: (pid) => req("GET", `/api/p/${pid}/taildrop`),
  sendTaildrop: (pid, file, target) => req("POST", `/api/p/${pid}/taildrop`, { file, target }),
  align: (pid) => req("GET", `/api/p/${pid}/align`),
  run: (pid, steps) => req("POST", `/api/p/${pid}/run`, { steps }),
  renderLine: (pid, i, force) => req("POST", `/api/p/${pid}/line`, { i, force }),
  generate: (pid, spec) => req("POST", `/api/p/${pid}/generate`, spec),

  // The file goes up as the raw body rather than a multipart form: fetch can
  // post a File directly, and the server never has to parse MIME boundaries.
  // The name rides in the query string because that is the only part of it the
  // server needs.
  upload: async (pid, file) => {
    const r = await fetch(
      `/api/p/${pid}/upload?name=${encodeURIComponent(file.name)}`,
      { method: "POST", body: file });
    const data = await r.json().catch(() => null);
    return data ?? { error: `http ${r.status}` };
  },

  // same raw-body trick as upload(): the recorded blob goes up as the body and
  // the playhead position it was captured at rides in the query string
  take: async (pid, blob, at, trim) => {
    const r = await fetch(`/api/p/${pid}/take?at=${at.toFixed(3)}${trim ? "&trim=1" : ""}`,
                          { method: "POST", body: blob });
    const data = await r.json().catch(() => null);
    return data ?? { error: `http ${r.status}` };
  },

  // how a take was delivered next to its line's TTS read (delivery.py)
  deliveries: (pid) => req("GET", `/api/p/${pid}/delivery`),
  delivery: (pid, i, file) => req("POST", `/api/p/${pid}/delivery`, { i, file }),

  addRequest: (pid, prompt, duration_hint, notes, target) =>
    req("POST", `/api/p/${pid}/requests`, { prompt, duration_hint, notes, target }),
  deleteRequest: (pid, rid) => req("DELETE", `/api/p/${pid}/requests/${rid}`),

  // `rel` is always project-root-relative — the same paths the manifest
  // (segment.src) and discovery already use, so nothing has to guess a prefix.
  // `v` is the file's mtime: same path + new bytes has to be a new URL, or the
  // browser keeps the clip it already decoded and a re-render looks like a no-op.
  mediaUrl: (pid, rel, v) => `/media/p/${pid}/${rel}` + (v ? `?v=${v}` : ""),
  // `at` (seconds) picks the frame; the default is just past the first
  thumbUrl: (pid, rel, v, at) =>
    `/media/p/${pid}/__thumb__?src=${encodeURIComponent(rel)}` + (v ? `&v=${v}` : "") +
    (at != null ? `&at=${at}` : ""),

  // the Publish tab and publish_mcp.py: every reel's Instagram post, one record each
  publishList: () => req("GET", "/api/publish"),
  publishQuota: () => req("GET", "/api/publish/quota"),
  publishEdit: (pid, body) => req("PUT", `/api/publish/${pid}`, body),
  publishAct: (pid, act, body) => req("POST", `/api/publish/${pid}/${act}`, body),
  publishToken: (token) => req("POST", "/api/publish/token", { token }),
  // the Comments tab: what's unanswered on Instagram, and a public reply under one comment
  // how: "" asks Instagram, "fresh" distrusts the server's memory, "seen" is that memory alone
  comments: (how) => req("GET", "/api/comments" + (how ? `?${how}=1` : "")),
  reelComments: (media) => req("GET", `/api/comments/${media}`),
  commentReply: (id, text, dm, need_dm) => req("POST", `/api/comments/${id}/reply`, { text, dm, need_dm }),
  commentAuto: (b) => req("POST", "/api/comments/auto", b),   // { replies } the bank, or { reel, on, word, dm }
  // helpers for the handful of files that live at a fixed name under `out`
  outRel: (m, name) => `${m.out || "out"}/${name}`,
  outUrl: (pid, m, name, v) => `/media/p/${pid}/${(m.out || "out")}/${name}` + (v ? `?v=${v}` : ""),
};
