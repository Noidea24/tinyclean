"use strict";
// mediatool dashboard. Plain JavaScript; every bit of text from the agents goes in with textContent.

const S = { agents: [], timers: [], profile: "live" };
const TEMPLATE_DEFAULTS = {
  movie_folder: "{title} ({year}) {tmdb-{tmdb}}",
  movie_file: "{title} ({year})",
  show_folder: "{show} ({year}) {tvdb-{tvdb}}",
  season_folder: "Season {season}",
  episode_file: "{show} ({year}) - S{season}E{episode} - {episode_title}",
};
const TEMPLATE_PRESETS = {
  "Plex (default)": {},
  "Plex with IMDb ID": { movie_folder: "{title} ({year}) {imdb-{imdb}}" },
  "Name (imdb) year": { movie_folder: "{title} ({imdb}) {year}", movie_file: "{title} ({imdb}) {year}" },
  "Name year (imdb)": { movie_folder: "{title} {year} ({imdb})", movie_file: "{title} {year} ({imdb})" },
  "Jellyfin IDs": { movie_folder: "{title} ({year}) [tmdbid-{tmdb}]", show_folder: "{show} ({year}) [tvdbid-{tvdb}]" },
};
const SAMPLE = {
  movie: { title: "Oldboy", year: 2003, imdb: "tt0364569", tmdb: "670", resolution: "1080p" },
  episode: { show: "Breaking Bad", title: "Breaking Bad", year: 2008, tvdb: "81189", season: 1, episode: 5,
             episode_title: "Gray Matter", resolution: "720p" },
};

// ------------------------------------------------------------------ helpers --

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}
const $ = (id) => document.getElementById(id);
function human(b) {
  if (b == null) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (Math.abs(b) >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (i ? b.toFixed(1) : b) + " " + u[i];
}
function hms(s) {
  if (s == null) return "--";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (hh ? hh + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}
function when(t) {
  if (!t) return "";
  const d = new Date(t * 1000), now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem("mediatool." + key));
    localStorage.setItem("mediatool." + key, JSON.stringify(value));
  } catch (e) { return null; }
}
function later(fn, ms) { const t = setTimeout(fn, ms); S.timers.push(t); return t; }
function every(fn, ms) { const t = setInterval(fn, ms); S.timers.push(t); return t; }
function clearTimers() { for (const t of S.timers) { clearTimeout(t); clearInterval(t); } S.timers = []; }

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Mediatool": "1" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("log in first"); }
  if (!r.ok) { const e = new Error(data.error || r.statusText); e.status = r.status; e.data = data; throw e; }
  return data;
}
const agentApi = (agent, path, body) => api(`/api/agents/${encodeURIComponent(agent)}/${path}`, body);

function confirmBox(title, lines, word, action) {
  return new Promise((resolve) => {
    const input = h("input", { placeholder: `type ${word}`, autocomplete: "off" });
    const go = h("button", { class: "danger", disabled: true, text: action });
    const dlg = h("dialog", {},
      h("h3", { text: title }), h("div", { style: "height:8px" }),
      ...lines.map((l) => h("p", { text: l })),
      input,
      h("div", { class: "row" }, go, h("button", { class: "plain", text: "Cancel", type: "button", onclick: () => dlg.close("no") })));
    input.addEventListener("input", () => { go.disabled = input.value.trim().toLowerCase() !== word; });
    go.addEventListener("click", () => dlg.close("yes"));
    dlg.addEventListener("close", () => { dlg.remove(); resolve(dlg.returnValue === "yes"); });
    document.body.append(dlg);
    dlg.showModal();
    input.focus();
  });
}

// --------------------------------------------------------------- templates --

function render(template, values) {
  const EMPTY = "\u0000";
  let out = template.replace(/\{(\w+)(?::(\d))?\}/g, (m, name, width) => {
    const v = values[name];
    if (v == null || v === "") return name in TEMPLATE_DEFAULTS || /^(title|year|imdb|tmdb|tvdb|show|season|episode|episode_title|resolution|edition)$/.test(name) ? EMPTY : m;
    if ((name === "season" || name === "episode") && /^\d+$/.test(String(v))) return String(+v).padStart(width ? +width : 2, "0");
    return String(v);
  });
  out = out.replace(/[(\[{][^()\[\]{}\u0000]*\u0000[^()\[\]{}]*[)\]}]/g, "").replaceAll(EMPTY, "");
  out = out.replaceAll(": ", " - ").replaceAll(":", "-").replace(/[\\/]/g, "-").replace(/[*?"<>|]/g, "");
  out = out.replace(/\s+/g, " ").replace(/( - )+/g, " - ").replace(/^\s*-\s*|\s*-\s*$/g, "");
  return out.replace(/^[ .]+|[ .]+$/g, "");
}

// ------------------------------------------------------------- agents state --

async function loadAgents() {
  try {
    const data = await api("/api/agents");
    S.agents = data.agents; S.dashboard = data.dashboard || {};
  } catch (e) { /* keep the last answer */ }
  const audit = (S.agents.find((a) => a.online && a.status.audit) || { status: {} }).status.audit;
  const n = audit ? Object.entries(audit.counts || {}).filter(([k]) => k !== "other").reduce((s, [, v]) => s + v, 0) : 0;
  const badge = $("needcount");
  if (badge) { badge.textContent = n; badge.classList.toggle("hide", !n); }
  return S.agents;
}
const online = () => S.agents.filter((a) => a.online);

function workerSelect(key, filter) {
  const sel = h("select", {});
  const fill = () => {
    const keep = sel.value || store("worker." + key);
    sel.replaceChildren();
    for (const a of S.agents) {
      const ok = a.online && (!filter || filter(a));
      const busy = a.online && a.status.job;
      const label = a.name + (a.online ? (busy ? " (busy)" : "") : " (off)") + (a.online && filter && !filter(a) ? " (no Plex)" : "");
      sel.append(h("option", { value: a.name, disabled: !ok, text: label }));
    }
    const first = [...sel.options].find((o) => !o.disabled && o.value === keep)
      || [...sel.options].find((o) => !o.disabled && !o.textContent.includes("busy"))
      || [...sel.options].find((o) => !o.disabled);
    if (first) sel.value = first.value;
    else {
      sel.prepend(h("option", { value: "", text: filter ? "No worker with Plex set up is online" : "No worker is online" }));
      sel.value = "";
    }
  };
  fill();
  sel.addEventListener("change", () => store("worker." + key, sel.value));
  sel.refill = fill;
  return sel;
}

// ------------------------------------------------------------ folder picker --

function folderPicker(getAgent, opts = {}) {
  const wrap = h("div", { class: "picker" });
  const crumbs = h("div", { class: "crumbs" });
  const list = h("div", { class: "picklist" });
  const chosen = h("div", { class: "chosen muted", text: "Nothing picked yet." });
  wrap.append(crumbs, list);
  const box = h("div", {}, wrap, chosen);
  box.value = null;

  function pick(path, label) {
    box.value = path;
    chosen.textContent = "Picked: " + path;
    chosen.className = "chosen";
    for (const el of list.children) el.classList.toggle("sel", el.dataset.path === path);
    if (opts.onchange) opts.onchange(path, label);
  }
  async function open(path) {
    const agent = getAgent();
    if (!agent) { list.replaceChildren(h("div", { class: "pick muted", text: "No worker is online." })); return; }
    list.replaceChildren(h("div", { class: "pick muted", text: "Loading..." }));
    let data;
    try { data = await agentApi(agent, "browse" + (path ? "?path=" + encodeURIComponent(path) : "")); }
    catch (e) { list.replaceChildren(h("div", { class: "pick error", text: e.message })); return; }
    crumbs.replaceChildren();
    if (data.path) {
      crumbs.append(h("a", { text: "Libraries", onclick: () => open(null) }));
      const root = S.roots && S.roots.find((r) => data.path === r || data.path.startsWith(r + "/")) || "";
      let acc = root;
      crumbs.append(h("span", { class: "arrow", text: "/" }), h("a", { text: root, onclick: () => open(root) }));
      for (const part of data.path.slice(root.length).split("/").filter(Boolean)) {
        acc += "/" + part;
        const target = acc;
        crumbs.append(h("span", { class: "arrow", text: "/" }), h("a", { text: part, onclick: () => open(target) }));
      }
      pick(data.path);
    } else {
      crumbs.append(h("span", { class: "muted", text: "Pick a library, then a folder" }));
      S.roots = data.dirs.map((d) => d.path);
    }
    list.replaceChildren();
    if (data.parent) list.append(h("div", { class: "pick muted", text: ".. up", onclick: () => open(data.parent) }));
    for (const d of data.dirs) {
      list.append(h("div", { class: "pick", "data-path": d.path, onclick: () => open(d.path) },
        h("span", { text: "\u{1F4C1}" }), h("span", { text: d.name + (d.clean ? "  (Clean copies)" : "") })));
    }
    if (opts.files) for (const f of data.files) {
      list.append(h("div", { class: "pick", "data-path": f.path, onclick: () => pick(f.path) },
        h("span", { text: "\u{1F39E}" }), h("span", { text: f.name }), h("span", { class: "size", text: human(f.size) })));
    }
    if (!data.dirs.length && !(opts.files && data.files.length)) list.append(h("div", { class: "pick muted", text: "Empty." }));
  }
  box.reload = () => { box.value = null; chosen.textContent = "Nothing picked yet."; chosen.className = "chosen muted"; open(null); };
  open(store("folder." + (opts.key || "x")) || null).catch(() => open(null));
  box.remember = () => opts.key && store("folder." + opts.key, box.value);
  return box;
}

function segmented(options, value, onchange) {
  const seg = h("div", { class: "seg" });
  seg.value = value;
  for (const [label, v] of options) {
    const b = h("button", { type: "button", text: label, class: v === value ? "on" : "" });
    b.addEventListener("click", () => {
      seg.value = v;
      for (const x of seg.children) x.classList.toggle("on", x === b);
      if (onchange) onchange(v);
    });
    seg.append(b);
  }
  return seg;
}

async function startJob(agent, kind, args) {
  const job = await agentApi(agent, "jobs", { kind, args });
  location.hash = `#/job/${encodeURIComponent(agent)}/${job.id}`;
}

function errorLine() { return h("span", { class: "error" }); }

// ------------------------------------------------------------------ screens --

function progressBlock(p) {
  if (!p) return null;
  const partial = (p.active || []).reduce((s, a) => s + a.bytes * a.pct / 100, 0);
  const pct = p.bytes_total ? 100 * (p.bytes_finished + partial) / p.bytes_total : 0;
  const fill = h("div", {}); fill.style.width = pct.toFixed(1) + "%";
  return h("div", {},
    h("div", { class: "bar" }, fill),
    h("div", { class: "muted", text: `${p.files_finished} of ${p.files_total} files, ${pct.toFixed(1)}%, saved ${human(p.saved_bytes)}` +
      (p.failed ? `, ${p.failed} failed` : "") + `, about ${hms(p.eta_seconds)} left` }),
    ...(p.active || []).map((a) => h("div", { class: "file-line",
      text: `${a.file.split("/").pop()}: ${a.pct.toFixed(1)}%  ${a.speed}  ETA ${a.eta}` })));
}

function describe(job) {
  const a = job.args || {};
  const where = Array.isArray(a.paths) ? a.paths.map((p) => p.split("/").slice(-2).join("/")).join(", ") : (a.plan || a.log || "");
  const names = { audit: "Check the library", delete: "Delete", convert: "Convert", check: "Check", replace: "Replace", rename_suffix: "Give -noidea back",
                  status: "Status", organize: "Organize", apply_plan: "Apply plan", undo: "Undo" };
  return (names[job.kind] || job.kind) + (a.mode ? ` (${a.mode})` : "") + (a.dry_run ? " - dry run" : "") + (where ? ": " + where : "");
}

async function viewOverview(v) {
  const cards = h("div", { class: "cards" });
  v.append(cards);
  const draw = () => {
    cards.replaceChildren();
    if (!S.agents.length) cards.append(h("div", { class: "card muted", text: "Loading..." }));
    for (const a of S.agents) {
      if (!a.online) {
        cards.append(h("div", { class: "card off" },
          h("div", { class: "head" }, h("span", { class: "dot" }), h("h3", { text: a.name }), h("span", { class: "grow" }),
            h("span", { class: "chip off", text: "off" })),
          h("div", { class: "muted", text: a.offline ? "Not running right now (container off, or the agent stopped)." : a.error })));
        continue;
      }
      const s = a.status, job = s.job;
      const mem = s.memory || {};
      cards.append(h("div", { class: "card" },
        h("div", { class: "head" }, h("span", { class: "dot " + (job ? "busy" : "on") }), h("h3", { text: a.name }),
          h("span", { class: "grow" }), h("span", { class: "muted", text: "tinyclean " + s.version })),
        h("div", { class: "chips" },
          h("span", { class: "chip " + (s.gpu ? "ok" : "bad"), text: s.gpu ? "GPU" : "no GPU" }),
          h("span", { class: "chip " + (s.ffmpeg ? "ok" : "bad"), text: "ffmpeg " + (s.ffmpeg || "missing") }),
          h("span", { class: "chip " + (s.plex ? "ok" : ""), text: s.plex ? "Plex" : "no Plex" }),
          mem.available != null && h("span", { class: "chip", text: `RAM ${human(mem.available)} free of ${human(mem.total)}` }),
          h("span", { class: "chip", text: `load ${s.load.map((x) => x.toFixed(1)).join(" ")}` }),
          ...s.disks.map((d) => h("span", { class: "chip " + (d.missing ? "bad" : ""), text: `${d.path} ${d.missing ? "missing" : human(d.free) + " free"}` }))),
        job ? h("div", {},
          h("div", { style: "margin-top:8px" }, h("a", { href: `#/job/${encodeURIComponent(a.name)}/${job.id}`, text: describe(job) }),
            h("span", { class: "muted", text: "  started " + when(job.created) })),
          progressBlock(job.progress))
          : h("div", { class: "muted", style: "margin-top:8px", text: "Idle." })));
    }
  };
  draw();
  await loadAgents(); draw();
  every(async () => { await loadAgents(); draw(); }, 4000);

  const recent = h("section", {}, h("h2", { text: "Recent jobs" }));
  v.append(recent);
  recent.append(await jobsTable(8));
}

async function jobsTable(limit) {
  const rows = [];
  await Promise.all(online().map(async (a) => {
    try {
      for (const j of (await agentApi(a.name, "jobs?limit=" + limit)).jobs) rows.push({ ...j, agent: a.name });
    } catch (e) { /* skip that worker */ }
  }));
  rows.sort((x, y) => y.created - x.created);
  if (!rows.length) return h("div", { class: "muted", text: "No jobs yet." });
  return h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, h("th", { text: "When" }), h("th", { text: "Worker" }), h("th", { text: "Job" }), h("th", { text: "State" })),
    ...rows.slice(0, limit).map((j) => h("tr", { class: "click", onclick: () => { location.hash = `#/job/${encodeURIComponent(j.agent)}/${j.id}`; } },
      h("td", { text: when(j.created) }), h("td", { text: j.agent }), h("td", { class: "path", text: describe(j) }),
      h("td", { class: j.state === "failed" ? "error" : "", text: j.state })))));
}

async function viewConvert(v) {
  await loadAgents();
  const worker = workerSelect("convert");
  const picker = folderPicker(() => worker.value, { files: true, key: "convert" });
  worker.addEventListener("change", () => picker.reload());
  const profile = segmented([["Live action", "live"], ["Animated", "animated"]], store("profile") || "live", (p) => store("profile", p));
  const jobs = h("select", {}, ...[1, 2, 3, 4].map((n) => h("option", { value: n, text: String(n) })));
  jobs.value = store("jobs") || 1;
  const folders = segmented([["3", 3], ["5", 5], ["10", 10], ["All", 0]], 0);
  const custom = h("input", { type: "number", min: 0, placeholder: "or type a number", style: "width:150px" });
  const dry = h("input", { type: "checkbox", checked: true });
  const retry = h("input", { type: "checkbox" });
  const msg = errorLine();
  const go = h("button", { text: "Start" });
  go.addEventListener("click", async () => {
    msg.textContent = "";
    if (!picker.value) { msg.textContent = "Pick a folder or a file first."; return; }
    store("jobs", jobs.value); picker.remember();
    try {
      await startJob(worker.value, "convert", { paths: [picker.value], profile: profile.value, jobs: +jobs.value,
        folders: +(custom.value || folders.value), dry_run: dry.checked, retry_failed: retry.checked });
    } catch (e) { msg.textContent = e.message; }
  });
  v.append(h("section", {}, h("h2", { text: "Convert" }),
    h("div", { class: "grid" }, h("label", {}, "Worker", worker), h("label", {}, "Files at once", jobs)),
    h("div", { class: "row" }, h("span", { class: "muted", text: "Profile" }), profile),
    h("div", { class: "row" }, h("span", { class: "muted", text: "Show/movie folders this run" }), folders, custom),
    h("div", { style: "margin-top:12px" }, h("label", {}, "Folder or file"), picker),
    h("div", { class: "row" }, h("label", { class: "check" }, dry, "Dry run: show what each file would become, convert nothing"),
      h("label", { class: "check" }, retry, "Retry files that failed before")),
    h("div", { class: "row" }, go, msg)));
}

async function viewReview(v, params) {
  await loadAgents();
  const worker = workerSelect("convert");
  const picker = folderPicker(() => worker.value, { key: "review" });
  worker.addEventListener("change", () => picker.reload());
  const msg = errorLine();
  const check = h("button", { text: "Check Clean copies" });
  check.addEventListener("click", async () => {
    msg.textContent = "";
    if (!picker.value) { msg.textContent = "Pick a folder first."; return; }
    picker.remember();
    try { await startJob(worker.value, "check", { paths: [picker.value] }); } catch (e) { msg.textContent = e.message; }
  });
  v.append(h("section", {}, h("h2", { text: "Review and replace" }),
    h("p", { class: "muted", text: "Check compares every original with its Clean copy and changes nothing. " +
      "When the check is done, the job page offers Replace for the same folder." }),
    h("div", { class: "grid" }, h("label", {}, "Worker", worker)),
    h("div", { style: "margin-top:12px" }, h("label", {}, "Folder"), picker),
    h("div", { class: "row" }, check, msg)));
}

async function viewOrganize(v) {
  await loadAgents();
  const worker = workerSelect("organize", (a) => a.status.plex);
  const picker = folderPicker(() => worker.value, { key: "organize" });
  worker.addEventListener("change", () => picker.reload());
  const mode = segmented([["Rename files", "rename"], ["Rename and restructure", "restructure"], ["Build a new tree", "newtree"]],
    store("mode") || "rename", (m) => store("mode", m));
  const folders = segmented([["3", 3], ["5", 5], ["10", 10], ["All", 0]], 5);
  const custom = h("input", { type: "number", min: 0, placeholder: "or type a number", style: "width:150px" });
  const saved = store("templates") || {};
  const inputs = {}, previews = {};
  const preview = () => {
    const t = (k) => inputs[k].value.trim() || TEMPLATE_DEFAULTS[k];
    previews.movie.textContent = render(t("movie_folder"), SAMPLE.movie) + "/" + render(t("movie_file"), SAMPLE.movie) + "-noidea.mkv";
    previews.episode.textContent = render(t("show_folder"), SAMPLE.episode) + "/" + render(t("season_folder"), SAMPLE.episode) + "/" +
      render(t("episode_file"), SAMPLE.episode) + ".mkv";
    store("templates", Object.fromEntries(Object.keys(inputs).map((k) => [k, inputs[k].value.trim()])));
  };
  const names = { movie_folder: "Movie folder", movie_file: "Movie file", show_folder: "Show folder",
                  season_folder: "Season folder", episode_file: "Episode file" };
  for (const k of Object.keys(TEMPLATE_DEFAULTS)) {
    inputs[k] = h("input", { value: saved[k] || "", placeholder: TEMPLATE_DEFAULTS[k], oninput: preview });
  }
  previews.movie = h("div", { class: "file-line" }); previews.episode = h("div", { class: "file-line" });
  const presets = h("select", { onchange: () => {
    const p = TEMPLATE_PRESETS[presets.value];
    for (const k of Object.keys(inputs)) inputs[k].value = p[k] || "";
    preview();
  } }, h("option", { text: "Presets...", value: "" }), ...Object.keys(TEMPLATE_PRESETS).map((n) => h("option", { value: n, text: n })));
  preview();
  const msg = errorLine();
  const twoEp = h("input", { type: "checkbox", checked: store("two_episode") !== "skip",
    onchange: () => store("two_episode", twoEp.checked ? "name" : "skip") });
  const go = h("button", { text: "Dry run" });
  go.addEventListener("click", async () => {
    msg.textContent = "";
    if (!picker.value) { msg.textContent = "Pick a folder first."; return; }
    picker.remember();
    const args = { paths: [picker.value], mode: mode.value, folders: +(custom.value || folders.value),
                   two_episode: twoEp.checked ? "name" : "skip" };
    for (const k of Object.keys(inputs)) if (inputs[k].value.trim()) args[k] = inputs[k].value.trim();
    try { await startJob(worker.value, "organize", args); } catch (e) { msg.textContent = e.message; }
  });
  v.append(h("section", {}, h("h2", { text: "Organize from Plex" }),
    h("div", { class: "grid" }, h("label", {}, "Worker", worker)),
    h("div", { class: "row" }, mode),
    h("div", { class: "row" }, h("span", { class: "muted", text: "Movie or show folders" }), folders, custom),
    h("div", { style: "margin-top:12px" }, h("label", {}, "Folder"), picker),
    h("div", { style: "margin-top:14px" }, h("div", { class: "head" }, h("h2", { text: "Name templates", style: "margin:0" }), presets)),
    h("div", { class: "grid" }, ...Object.keys(inputs).map((k) => h("label", {}, names[k], inputs[k]))),
    h("div", { class: "muted", style: "margin-top:8px", text: "Preview (example titles):" }), previews.movie, previews.episode,
    h("div", { class: "row" }, h("label", { class: "check" }, twoEp,
      "Name two-episode files from the file name (S01E01-E02); off leaves them as they are")),
    h("div", { class: "row" }, go, msg,
      h("span", { class: "muted", text: "Nothing changes until you apply the plan from the job page." }))));

  const runs = h("section", {}, h("h2", { text: "Applied runs" }));
  v.append(runs);
  const w = online().find((a) => a.status.plex);
  if (!w) { runs.append(h("div", { class: "muted", text: "No worker with Plex is online." })); return; }
  const logs = (await agentApi(w.name, "undo")).logs;
  if (!logs.length) { runs.append(h("div", { class: "muted", text: "None yet." })); return; }
  runs.append(h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, h("th", { text: "Run" }), h("th", { text: "Mode" }), h("th", { text: "Changes" }), h("th", {})),
    ...logs.map((l) => h("tr", {}, h("td", { text: when(l.modified) }), h("td", { text: l.mode || "" }),
      h("td", { text: String(l.changes ?? "") }),
      h("td", {}, l.name.includes(".undone") ? h("span", { class: "muted", text: "undone" })
        : h("button", { class: "plain small", text: "Undo", onclick: async () => {
          if (!await confirmBox("Undo this run?", [`Puts back ${l.changes} change(s) from the ${l.mode} run of ${when(l.modified)}.`,
            "Hard links made by a new tree are removed only where the original is still there."], "undo", "Undo")) return;
          try { await startJob(w.name, "undo", { log: l.name, confirm: true }); } catch (e) { alert(e.message); }
        } })))))));
}

async function viewFailures(v) {
  await loadAgents();
  const box = h("section", {}, h("h2", { text: "Failed files" }), h("div", { class: "muted", text: "Reading the ledgers..." }));
  v.append(box);
  const latest = new Map();
  for (const a of online()) {
    let after = 0;
    for (;;) {
      let page;
      try { page = await agentApi(a.name, "ledger?after=" + after); } catch (e) { break; }
      for (const r of page.rows) latest.set(r.file, { ...r, agent: a.name });
      after += page.rows.length;
      if (!page.rows.length || after >= page.total) break;
    }
  }
  const failed = [...latest.values()].filter((r) => r.result === "failed").sort((x, y) => (y.time || "").localeCompare(x.time || ""));
  box.replaceChildren(h("h2", { text: `Failed files (${failed.length})` }));
  if (!failed.length) { box.append(h("div", { class: "muted", text: "Nothing has failed. (Each file counts by its latest attempt.)" })); return; }
  const profile = segmented([["Live action", "live"], ["Animated", "animated"]], store("profile") || "live", (p) => store("profile", p));
  box.append(h("div", { class: "row", style: "margin:0 0 10px" }, h("span", { class: "muted", text: "Retry with profile" }), profile));
  const redownload = (r) => /damaged or incomplete|re-download/i.test(r.notes || "");
  box.append(h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, h("th", { text: "When" }), h("th", { text: "File" }), h("th", { text: "Why" }), h("th", {})),
    ...failed.map((r) => h("tr", {},
      h("td", { text: (r.time || "").slice(5, 16) }),
      h("td", { class: "path", text: r.file.split("/").slice(-2).join("/") }),
      h("td", { class: redownload(r) ? "warn" : "", text: (redownload(r) ? "Re-download: " : "") + (r.notes || "") }),
      h("td", {}, h("button", { class: "plain small", text: "Retry", onclick: async (ev) => {
        ev.target.disabled = true;
        try { await startJob(r.agent, "convert", { paths: [r.file], profile: profile.value, jobs: 1, retry_failed: true }); }
        catch (e) { ev.target.disabled = false; alert(e.message); }
      } })))))));
}

async function viewHistory(v) {
  await loadAgents();
  const box = h("section", {}, h("h2", { text: "Jobs" }));
  v.append(box);
  box.append(await jobsTable(200));
}

async function viewJob(v, agent, id) {
  await loadAgents();
  const title = h("h3", {}), state = h("div", { class: "muted" }), prog = h("div", {});
  const out = h("pre", {});
  const outSummary = h("summary", { text: "Output" });
  const outBox = h("details", { open: true }, outSummary, out);
  const actions = h("div", { class: "row" });
  const after = h("div", {});
  v.append(h("section", {}, h("div", { class: "head" }, title), state, prog, actions, outBox), after);
  let since = 0, lines = [], shownAfter = false, current = null;
  const download = h("button", { class: "plain", text: "Download .txt", onclick: () => {
    if (!current) return;
    const stamp = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
    const head = [
      `mediatool job ${current.id} on ${agent}`,
      `${describe(current)}`,
      `state: ${current.state}` + (current.returncode != null ? `, exit code ${current.returncode}` : ""),
      `started: ${stamp(current.created)}` + (current.ended ? `, ended: ${stamp(current.ended)}` : ""),
      `settings: ${JSON.stringify(current.args)}`,
      "",
    ];
    const blob = new Blob([head.concat(lines).join("\n") + "\n"], { type: "text/plain" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `mediatool-${current.kind}-${agent}-${current.id}.txt` });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } });
  const stopNow = h("button", { class: "plain", text: "Stop now", onclick: () => agentApi(agent, `jobs/${id}/stop`, {}).catch((e) => alert(e.message)) });
  const stopAfter = h("button", { class: "plain", text: "Stop after the current file",
    onclick: () => agentApi(agent, `jobs/${id}/stop`, { after_current: true }).catch((e) => alert(e.message)) });

  async function poll() {
    let job;
    try { job = await agentApi(agent, `jobs/${id}?since=${since}`); }
    catch (e) { state.textContent = e.message; later(poll, 4000); return; }
    title.textContent = `${describe(job)}  (${agent})`;
    state.textContent = `${job.state}, started ${when(job.created)}` + (job.ended ? `, ended ${when(job.ended)}` : "") +
      (job.returncode != null && job.returncode !== 0 ? `, exit code ${job.returncode}` : "");
    state.className = job.state === "failed" ? "error" : "muted";
    if (job.lines.length) {
      const atEnd = out.scrollTop + out.clientHeight >= out.scrollHeight - 30;
      out.append(job.lines.join("\n") + "\n");
      lines = lines.concat(job.lines);
      if (atEnd) out.scrollTop = out.scrollHeight;
    }
    since = job.first_line + job.lines.length;
    prog.replaceChildren(progressBlock(job.progress) || "");
    const live = job.state === "running" || job.state === "stopping";
    current = job;
    actions.replaceChildren(...(live ? [job.kind === "convert" && stopAfter, stopNow] : []).filter(Boolean), download);
    if (live) later(poll, 1500);
    else if (!shownAfter) {
      shownAfter = true;
      out.scrollTop = out.scrollHeight;
      outSummary.textContent = `Full output (${lines.length} lines)`;
      if (lines.length > 40 && ["organize", "audit", "check", "status"].includes(job.kind)) outBox.open = false;
      await afterJob(after, agent, job, lines);
    }
  }
  poll();
}

async function afterJob(box, agent, job, lines) {
  if (job.state !== "finished") return;
  const text = lines.join("\n");
  if (job.kind === "audit") box.append(h("section", {}, h("a", { href: "#/needs", text: "Open Needs you" })));
  if (job.kind === "delete") box.append(h("section", {}, h("p", { text: "Rescan the library in Plex, then check the library again from Needs you." }),
    h("a", { href: "#/needs", text: "Back to Needs you" })));
  if (job.kind === "check") {
    const passed = (text.match(/(\d+) passed the checks/) || [])[1];
    if (!passed || passed === "0") return;
    const paths = job.args.paths;
    box.append(h("section", {}, h("h2", { text: "Replace" }),
      h("p", { text: `${passed} original(s) passed the check above. Replacing deletes each of those originals and moves its Clean copy into its place. Copies that failed are left alone.` }),
      h("button", { class: "danger", text: "Replace originals...", onclick: async () => {
        if (!await confirmBox("Replace originals?", [
          `${passed} original video(s) in ${paths.join(", ")} will be deleted, and their checked Clean copies moved in.`,
          "Deleted originals do not go to the Synology recycle bin.", "The list of files is in the check output above."],
        "replace", "Replace")) return;
        try { await startJob(agent, "replace", { paths, confirm: true }); } catch (e) { alert(e.message); }
      } })));
  }
  if (job.kind === "organize") {
    const name = (text.match(/plan-[\d-]+\.json/) || [])[0];
    if (!name) return;
    const plan = await agentApi(agent, "plans/" + name);
    const renumbered = plan.items.filter((it) => it.note).length;
    const leftovers = (plan.clean_leftovers || []).length;
    const summary = h("section", {}, h("h2", { text: "Plan" }),
      h("div", { class: "chips" },
        h("span", { class: "chip ok", text: `${plan.items.length} video(s) to ${plan.mode === "newtree" ? "link" : "rename/move"}` }),
        (plan.extras || []).length > 0 && h("span", { class: "chip", text: `${plan.extras.length} other file(s) moving along` }),
        renumbered > 0 && h("span", { class: "chip bad", text: `${renumbered} renumbered by title` }),
        leftovers > 0 && h("span", { class: "chip", text: `${leftovers} leftover folder(s) removed` }),
        h("span", { class: "chip", text: `${plan.unchanged} already right` }),
        h("span", { class: "chip", text: `${plan.skipped.length} skipped` })),
      h("p", { class: "muted", text: "Open the lists below to check them, or download the .txt above." }));
    box.append(summary);
    if (plan.items.length) {
      summary.append(h("p", { class: "muted", text: "Apply carries out exactly this plan, on files that have not changed since. Every applied run can be undone from Organize." }));
      summary.append(h("div", { class: "row" },
        h("button", { class: "danger", text: `Apply ${plan.items.length} change(s)...`, onclick: async () => {
          const verb = { rename: "renamed", restructure: "renamed and moved", newtree: "hard-linked into a new tree" }[plan.mode];
          const more = (plan.extras || []).length;
          if (!await confirmBox("Apply this plan?", [`${plan.items.length} video(s) will be ${verb}, with their subtitle files` +
            (more ? ` and ${more} other file(s) from their folders.` : "."),
            "No video is deleted" + (leftovers ? "; only the leftover folders listed are removed." : ".") +
            " Undo puts the names back."], "apply", "Apply")) return;
          try { await startJob(agent, "apply_plan", { plan: name, confirm: true }); } catch (e) { alert(e.message); }
        } })));
    }
    box.append(planTable(plan));
  }
  if (job.kind === "apply_plan") {
    const name = (text.match(/undo-[\d-]+\.json/) || [])[0];
    if (name) box.append(h("section", {}, h("p", { text: "Rescan the library in Plex, and Refresh in Sonarr/Radarr if they manage these titles." }),
      h("button", { class: "plain", text: "Undo this run...", onclick: async () => {
        if (!await confirmBox("Undo this run?", ["Puts every change from this run back."], "undo", "Undo")) return;
        try { await startJob(agent, "undo", { log: name, confirm: true }); } catch (e) { alert(e.message); }
      } })));
  }
}

function planTable(plan) {
  const rel = (p, root) => p.startsWith(root + "/") ? p.slice(root.length + 1) : p;
  const sec = h("section", {});
  const fold = (label, ...kids) => h("details", { class: "fold" }, h("summary", { text: label }), ...kids);
  if (plan.items.length) sec.append(fold(`Changes (${plan.items.length})`, h("div", { class: "tablewrap" }, h("table", {},
    h("tr", {}, h("th", { text: "Now" }), h("th", { text: "Becomes" })),
    ...plan.items.map((it) => h("tr", {}, h("td", { class: "path", text: rel(it.old, it.root) }),
      h("td", { class: "path" }, rel(it.new, plan.mode === "newtree" ? it.root.replace(/\/[^/]+$/, "") : it.root),
        it.note && h("div", { class: "file-line warn", text: it.note }),
        ...it.sidecars.map((s) => h("div", { class: "file-line", text: "+ " + s[1].split("/").pop() })))))))));
  const notes = plan.items.filter((it) => it.note);
  if (notes.length) sec.append(fold(`Renumbered by title (${notes.length})`,
    ...notes.map((it) => h("div", { class: "file-line warn", text: `${it.old.split("/").pop()}: ${it.note}` }))));
  const extras = plan.extras || [];
  if (extras.length) sec.append(fold(`Other files moving with them (${extras.length}), so the old folders end up empty`,
    h("div", { class: "tablewrap" }, h("table", {},
      h("tr", {}, h("th", { text: "Now" }), h("th", { text: "Becomes" })),
      ...extras.map((x) => h("tr", {}, h("td", { class: "path", text: rel(x.old, x.root) }),
        h("td", { class: "path", text: rel(x.new, plan.mode === "newtree" ? x.root.replace(/\/[^/]+$/, "") : x.root) })))))));
  const leftovers = plan.clean_leftovers || [];
  if (leftovers.length) sec.append(fold(`Leftover folders that will be removed (${leftovers.length})`,
    h("p", { class: "muted", text: "Clean folders holding only tinyclean's own notes (no copies), and Synology @eaDir index folders for the old names (the NAS rebuilds them), so the old folders can go once their videos have moved." }),
    ...leftovers.map((c) => h("div", { class: "file-line", text: rel(c.dir, c.root) }))));
  if (plan.skipped.length) {
    const roots = (plan.roots || []).slice().sort((a, b) => b.length - a.length);
    const groups = new Map();
    for (const [path, why] of plan.skipped) {
      const root = roots.find((r) => path.startsWith(r.replace(/\/$/, "") + "/"));
      const folder = root ? path.slice(0, root.length + 1 + path.slice(root.length + 1).split("/")[0].length) : path.replace(/\/[^/]+$/, "");
      const personal = /^(another file|name already|other files stay|stays in|the file name)/.test(why);
      const key = folder + "\u0000" + why + "\u0000" + (personal ? path : "");
      if (!groups.has(key)) groups.set(key, { folder, why, paths: [] });
      groups.get(key).paths.push(path);
    }
    sec.append(fold(`Skipped (${plan.skipped.length})`, h("div", { class: "tablewrap" }, h("table", {},
      h("tr", {}, h("th", { text: "What" }), h("th", { text: "Why" })),
      ...[...groups.values()].map((g) => h("tr", {},
        h("td", { class: "path" }, g.paths.length === 1 ? g.paths[0] : h("details", {},
          h("summary", { text: `${g.folder.split("/").pop()}: ${g.paths.length} files` }),
          ...g.paths.map((p) => h("div", { class: "file-line", text: p.split("/").pop() })))),
        h("td", { text: g.why })))))));
  }
  return sec;
}

// --------------------------------------------------------------- needs you --

function plexLink(audit, key, text) {
  if (!key || !audit.plex || !audit.plex.machine) return null;
  const url = `${audit.plex.url}/web/index.html#!/server/${audit.plex.machine}/details?key=${encodeURIComponent("/library/metadata/" + key)}`;
  return h("a", { href: url, target: "_blank", rel: "noopener", text });
}

async function viewNeeds(v) {
  await loadAgents();
  const w = online().find((a) => a.status.plex);
  if (!w) { v.append(h("section", { class: "muted", text: "No worker with Plex set up is online." })); return; }
  let audit = null;
  try { audit = await agentApi(w.name, "audit"); } catch (e) { if (e.status !== 404) throw e; }
  const check = h("button", { text: audit ? "Check again" : "Check the library", onclick: async () => {
    try { await startJob(w.name, "audit", {}); } catch (e) { alert(e.message); }
  } });
  v.append(h("section", {}, h("div", { class: "head" }, h("h3", { text: "Needs you" }), h("span", { class: "grow" }), check),
    h("p", { class: "muted", text: audit ? `Last checked ${when(audit.created)}. It reads Plex and the library and changes nothing; check again after fixing things.`
      : "Nothing checked yet. It reads Plex and the library (a minute or two) and changes nothing." })));
  if (!audit) return;
  const rel = (p) => p.split("/").slice(-2).join("/");

  // Duplicates: keep one, delete the rest (after a typed confirmation that names every file).
  const dsec = h("section", {}, h("h2", { text: `Duplicates (${audit.duplicates.length})` }));
  if (!audit.duplicates.length) dsec.append(h("div", { class: "muted", text: "None." }));
  for (const d of audit.duplicates) {
    const title = `${d.title || "?"}${d.year ? ` (${d.year})` : ""}${d.episode ? " " + d.episode : ""}`;
    dsec.append(h("div", { class: "dupe" },
      h("div", { class: "head" }, h("b", { text: title }), h("span", { class: "grow" }), plexLink(audit, d.plex_key, "Open in Plex")),
      h("div", { class: "tablewrap" }, h("table", {},
        ...d.files.map((f) => h("tr", {},
          h("td", { class: "path", text: rel(f.file) }),
          h("td", { text: human(f.size) }),
          h("td", {}, f.converted ? h("span", { class: "chip ok", text: "-noidea" }) : h("span", { class: "chip", text: "not converted" })),
          h("td", {}, h("button", { class: "plain small", text: "Keep this one", onclick: async () => {
            const others = d.files.filter((x) => x.file !== f.file);
            if (!await confirmBox(`Keep one copy of ${title}?`, [
              `Keeps: ${f.file}`,
              ...others.map((x) => `Deletes: ${x.file} (${human(x.size)}), with its subtitle files`),
              "Deleted files do not go to the Synology recycle bin."], "delete", "Delete")) return;
            try { await startJob(w.name, "delete", { paths: others.map((x) => x.file), confirm: true }); } catch (e) { alert(e.message); }
          } }))))))));
  }
  v.append(dsec);

  const usec = h("section", {}, h("h2", { text: `Not matched in Plex (${audit.unmatched.length})` }),
    h("p", { class: "muted", text: "Fix Match in Plex (… menu, Fix Match), then check again. Until then these are left as they are." }));
  if (audit.unmatched.length) usec.append(h("div", { class: "tablewrap" }, h("table", {},
    ...audit.unmatched.map((u) => h("tr", {}, h("td", { class: "path", text: u.folder.split("/").pop() }),
      h("td", { text: `${u.files} file(s)` }), h("td", {}, plexLink(audit, u.plex_key, "Fix in Plex") || ""))))));
  v.append(usec);

  const order = audit.order || [];
  const osec = h("section", {}, h("h2", { text: `Episode order differs from Plex (${order.length} shows)` }),
    h("p", { class: "muted", text: "The titles in these file names belong to other episode numbers in Plex, so Plex shows the wrong episode for them, and Organize leaves them as they are. To fix a show: open it in Plex, Edit, Advanced, Episode ordering, try TheTVDB (DVD) or TheMovieDB, save, then Check again. Shows only a few files out of order are renumbered by Organize itself and are not listed." }));
  if (order.length) osec.append(h("div", { class: "tablewrap" }, h("table", {},
    ...order.map((o) => h("tr", {},
      h("td", { class: "path" }, h("details", {},
        h("summary", { text: `${o.folder.split("/").pop()}: ${o.files} file(s)` }),
        ...o.examples.map((x) => h("div", { class: "file-line", text: `${x.code}: your file says “${x.mine}”, Plex says “${x.plex}”` })))),
      h("td", {}, plexLink(audit, o.plex_key, "Open in Plex") || ""))))));
  else osec.append(h("div", { class: "muted", text: "None." }));
  v.append(osec);

  const wsec = h("section", {}, h("h2", { text: `Clean copies waiting (${audit.waiting.length} folders)` }));
  if (audit.waiting.length) wsec.append(h("div", { class: "tablewrap" }, h("table", {},
    ...audit.waiting.map((x) => h("tr", {}, h("td", { class: "path", text: rel(x.folder) }), h("td", { text: `${x.files} file(s)` }),
      h("td", {}, h("button", { class: "plain small", text: "Review", onclick: () => { store("folder.review", x.folder); location.hash = "#/review"; } })))))));
  else wsec.append(h("div", { class: "muted", text: "None." }));
  v.append(wsec);

  const fsec = h("section", {}, h("h2", { text: `Failed conversions (${audit.failed.length})` }));
  if (audit.failed.length) fsec.append(h("div", { class: "tablewrap" }, h("table", {},
    ...audit.failed.map((r) => h("tr", {}, h("td", { class: "path", text: rel(r.file) }),
      h("td", { class: r.redownload ? "warn" : "", text: (r.redownload ? "Re-download: " : "") + r.notes }))))),
    h("div", { class: "row" }, h("a", { href: "#/failures", text: "Retry from Failures" })));
  else fsec.append(h("div", { class: "muted", text: "None." }));
  v.append(fsec);

  if (audit.other.length) v.append(h("section", {}, h("h2", { text: `Other (${audit.other.length})` }),
    h("div", { class: "tablewrap" }, h("table", {},
      ...audit.other.map((o) => h("tr", {}, h("td", { class: "path" }, h("details", {},
        h("summary", { text: `${o.folder.split("/").pop()}: ${o.files.length} file(s)` }),
        ...o.files.map((f) => h("div", { class: "file-line", text: f.split("/").pop() })))),
        h("td", { text: o.why })))))));
}

// ---------------------------------------------------------------- settings --

function newerVersion(tag, current) {
  const nums = (v) => String(v || "").replace(/^v/, "").split(/[.-]/).map((x) => parseInt(x, 10) || 0);
  const a = nums(tag), b = nums(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) > (b[i] || 0);
  }
  return false;
}

async function viewSettings(v) {
  await loadAgents();
  const box = h("div", { class: "cards" });
  v.append(h("section", {}, h("h2", { text: "Workers" }),
    h("p", { class: "muted", text: "Update installs the newest release on that worker and restarts its agent. It waits while a job is running." })), box);
  const draw = async () => {
    box.replaceChildren();
    for (const a of S.agents) {
      if (!a.online) { box.append(h("div", { class: "card off" }, h("h3", { text: a.name }), h("div", { class: "muted", text: "off" }))); continue; }
      let u = {};
      try { u = await agentApi(a.name, "update"); } catch (e) { u = { error: e.message }; }
      const s = a.status, last = u.last;
      const newer = u.latest && newerVersion(u.latest, u.current);
      const btn = h("button", { text: newer ? `Update to ${u.latest}` : "Reinstall newest", disabled: !!s.job || !u.available || u.pending,
        onclick: async () => {
          btn.disabled = true;
          try { await agentApi(a.name, "update", { tag: "latest" }); btn.textContent = "Updating..."; follow(); }
          catch (e) { alert(e.message); btn.disabled = false; }
        } });
      box.append(h("div", { class: "card" },
        h("div", { class: "head" }, h("h3", { text: a.name }), h("span", { class: "grow" }),
          h("span", { class: "chip " + (newer ? "bad" : "ok"), text: `v${u.current || s.version}` + (newer ? ` (newest ${u.latest})` : " (newest)") })),
        !u.available && h("p", { class: "muted", text: "Updates from here need the installer run once as root on this worker." }),
        h("div", { class: "row" }, btn, u.pending && h("span", { class: "muted", text: "update requested..." }),
          s.job && h("span", { class: "muted", text: "a job is running" })),
        last && h("details", { style: "margin-top:10px" },
          h("summary", { class: last.state === "failed" ? "error" : "muted", text: `Last update: ${last.state} ${last.tag || ""}, ${when(last.finished)}` }),
          h("pre", { text: (last.log || []).join("\n") })),
        h("div", { class: "muted", style: "margin-top:10px", text:
          `Organizing: ${s.plex ? "Plex set up" : "no Plex"}; ignored libraries: ${(s.plex_ignore || []).join(", ") || "none"}; ` +
          `Plex rescan after apply: ${s.plex_refresh ? "on" : "off"}; library folders: ${s.roots.join(", ")}` })));
    }
  };
  let t = 0;
  const follow = () => { t = 0; const tick = async () => { await loadAgents(); await draw(); if (++t < 40) later(tick, 3000); }; later(tick, 3000); };
  await draw();
  v.append(h("section", {}, h("h2", { text: "This page" }),
    h("p", { text: `Dashboard ${S.dashboard && S.dashboard.version ? "v" + S.dashboard.version : ""}. It updates through Komodo: ` +
      "turn on Auto Update for the mediatool stack, or Pull and Redeploy it after a release." })));
}

// ------------------------------------------------------------------ routing --

const ROUTES = { needs: viewNeeds, settings: viewSettings, overview: viewOverview, convert: viewConvert, review: viewReview, organize: viewOrganize,
                 failures: viewFailures, history: viewHistory };

async function route() {
  clearTimers();
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  const name = parts[0] || "overview";
  for (const a of $("nav").children) a.classList.toggle("on", a.getAttribute("href") === "#/" + name);
  const v = $("view");
  v.replaceChildren();
  try {
    if (name === "job") await viewJob(v, parts[1], parts[2]);
    else await (ROUTES[name] || viewOverview)(v);
  } catch (e) {
    if (e.message !== "log in first") v.append(h("section", { class: "error", text: e.message }));
  }
}

function showLogin() {
  clearTimers();
  $("app").classList.add("hide");
  $("login").classList.remove("hide");
  $("loginform").password.focus();
}
$("loginform").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("loginmsg").textContent = "";
  try {
    await api("/api/login", { password: ev.target.password.value });
    ev.target.password.value = "";
    start();
  } catch (e) { $("loginmsg").textContent = e.message; $("loginmsg").className = "error"; }
});
$("logout").addEventListener("click", async () => { await api("/api/logout", {}).catch(() => {}); showLogin(); });

async function start() {
  $("login").classList.add("hide");
  $("app").classList.remove("hide");
  route();
}
window.addEventListener("hashchange", () => { if (!$("app").classList.contains("hide")) route(); });
api("/api/session").then((s) => (s.logged_in ? start() : showLogin()));
