const state = {
  papers: [],
  byId: new Map(),
  query: "",
  category: "papers",    // "", "papers", "books" or "slides"
  tagFilter: new Set(),
  sort: { key: "year", desc: true },
  open: null,
  selected: new Set(),    // papers picked for the Compare view
  openSections: new Set(["contributions"]),  // collapsible sections kept open across re-renders
  llm: false,
  sourcesOk: true,       // false until a literature folder that exists is set
  view: "table",
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
}

// Same tag name -> same color everywhere (FNV-1a hash over the 10-color palette in style.css).
function tagClass(tag) {
  let h = 2166136261;
  for (const ch of tag) h = Math.imul(h ^ ch.codePointAt(0), 16777619);
  return `c${(h >>> 0) % 10}`;
}

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

function setPapers(papers) {
  state.papers = papers.filter((p) => !p.missing);
  state.papers.forEach(annotate);
  state.byId = new Map(state.papers.map((p) => [p.id, p]));
}

// Derived fields: in-library citation count, top-level folder, and document category.
function annotate(p) {
  p.cited_by_n = p.cited_by.length;
  p.group = p.folder ? p.folder.split("/")[0] : "(top level)";
  p.category = p.group === "books" ? "books" : p.group === "slides" ? "slides" : "papers";
}

function replacePaper(p) {
  annotate(p);
  const i = state.papers.findIndex((x) => x.id === p.id);
  if (i >= 0) state.papers[i] = p;
  state.byId.set(p.id, p);
}

function shortLabel(p) {
  const name = p.first_author ? p.first_author.split(/\s+/).pop() : p.filename.replace(/\.pdf$/i, "");
  return `${name} ${p.year ?? ""}`.trim();
}

// ---------- filtering & sorting (shared by both views) ----------

function matchesFilter(p) {
  if (state.category && p.category !== state.category) return false;
  if ([...state.tagFilter].some((t) => !p.tags.includes(t))) return false;
  const q = state.query.toLowerCase();
  if (!q) return true;
  const hay = [p.title, p.filename, p.journal, p.notes, p.authors.join(" "), p.tags.join(" "), p.year,
    p.contributions.join(" "), p.methodology.join(" ")].join(" ").toLowerCase();
  return hay.includes(q);
}

function visiblePapers() {
  const list = state.papers.filter(matchesFilter);
  const { key, desc } = state.sort;
  list.sort((a, b) => {
    const av = a[key] ?? "", bv = b[key] ?? "";
    const cmp = typeof av === "number" && typeof bv === "number" ? av - bv : String(av).localeCompare(String(bv));
    return desc ? -cmp : cmp;
  });
  return list;
}

// ---------- rendering ----------

function authorLabel(p) {
  if (!p.first_author) return '<span class="muted">—</span>';
  const etal = p.authors.length > 1 ? ' <span class="muted">et al.</span>' : "";
  return `<span title="${esc(p.authors.join(", "))}">${esc(p.first_author)}</span>${etal}`;
}

function tagChips(p, editable) {
  const chips = p.tags.map((t) =>
    `<span class="tag ${tagClass(t)}">${esc(t)}${editable ? `<span class="x" data-remove="${esc(t)}" title="Remove">×</span>` : ""}</span>`
  ).join("");
  const input = editable ? `<input class="tag-input" placeholder="+ tag" data-id="${p.id}">` : "";
  return chips + input;
}

const TAGBAR_TOP = 25;

function renderTagBar() {
  const counts = {};
  state.papers.filter((p) => !state.category || p.category === state.category)
    .forEach((p) => p.tags.forEach((t) => (counts[t] = (counts[t] || 0) + 1)));
  // Collapsed: the most used tags plus any active filter; expanded: every tag, alphabetically.
  const byUse = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  const shown = state.allTags ? byUse.slice().sort()
    : [...new Set([...byUse.slice(0, TAGBAR_TOP), ...state.tagFilter])].filter((t) => counts[t]).sort();
  const toggle = byUse.length > TAGBAR_TOP
    ? `<button class="tag-toggle" data-alltags>${state.allTags ? "Show fewer" : `All tags (${byUse.length})`}</button>` : "";
  $("#tagbar").innerHTML = shown.map((t) =>
    `<span class="tag ${tagClass(t)} ${state.tagFilter.has(t) ? "active" : ""}" data-filter="${esc(t)}">${esc(t)} <span class="count">${counts[t]}</span></span>`
  ).join("") + toggle;
  $("#tagbar").classList.toggle("filtering", state.tagFilter.size > 0);
}

function paperLinks(ids) {
  const items = ids.map((id) => state.byId.get(id)).filter(Boolean)
    .sort((a, b) => (b.year ?? 0) - (a.year ?? 0))
    .map((q) => `<li><a class="plink" data-goto="${q.id}">${esc(shortLabel(q))}</a> — ${esc(q.title || q.filename)}</li>`);
  return items.length ? `<ul class="linklist">${items.join("")}</ul>` : '<p class="muted">None in your library.</p>';
}

function section(key, title, count, body) {
  const n = count == null ? "" : ` <span class="n">(${count})</span>`;
  return `<details class="section" data-section="${key}" ${state.openSections.has(key) ? "open" : ""}>
    <summary>${title}${n}</summary>${body}</details>`;
}

function bullets(items, empty) {
  return items.length ? `<ul class="bullets">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : `<p class="muted">${empty}</p>`;
}

function detailRow(p) {
  const doi = p.doi ? `<div class="meta-line">DOI: <a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener">${esc(p.doi)}</a></div>` : "";
  const notYet = state.llm ? "Not analyzed yet." : "Not analyzed (the claude CLI was not found).";
  const analyzeBtn = state.llm ? `<button data-action="analyze">${p.analyzed_at ? "Re-analyze" : "Analyze"} with Claude</button>` : "";
  const model = p.analysis_model ? ` <span class="n">· ${esc(p.analysis_model)}</span>` : "";
  return `
    <tr class="detail"><td colspan="10">
      <div class="detail-grid">
        <div>
          <h3>Authors</h3>
          <p class="meta-line">${esc(p.authors.join(", ")) || '<span class="muted">unknown</span>'}</p>
          ${doi}
          <div class="meta-line muted">${p.folder ? esc(p.folder) + "/" : ""}${esc(p.filename)} · ${p.pages ?? "?"} pages · metadata from ${esc(p.metadata_source)}</div>
          ${p.keywords.length ? `<div class="meta-line muted">Keywords: ${esc(p.keywords.join(", "))}</div>` : ""}
          ${section("abstract", "Abstract", null, `<p>${esc(p.abstract) || '<span class="muted">No abstract found.</span>'}</p>`)}
          ${section("cites", "Cites in your library", p.cites.length, paperLinks(p.cites))}
          ${section("citedby", "Cited by in your library", p.cited_by.length, paperLinks(p.cited_by))}
          ${section("refs", "All references", p.ref_count, `<ol data-refs="${p.id}"><li>Loading…</li></ol>`)}
        </div>
        <div>
          ${section("contributions", `Key contributions${model}`, null, bullets(p.contributions, notYet))}
          ${section("methodology", "Methodology", null, bullets(p.methodology, notYet))}
          <h3 style="margin-top:16px">Tags</h3>
          <div class="tags" data-id="${p.id}">${tagChips(p, true)}</div>
          <h3 style="margin-top:16px">Your notes <span class="saved" id="saved-${p.id}"></span></h3>
          <textarea data-notes="${p.id}" placeholder="Write anything about this paper…">${esc(p.notes)}</textarea>
        </div>
      </div>
      <div class="actions" data-id="${p.id}">
        <button class="primary" data-action="pdf">Open PDF</button>
        <button data-action="graph">Show in citation graph</button>
        ${analyzeBtn}
        <button data-action="refresh" title="Re-read metadata and references from the PDF, Crossref and OpenAlex">Re-extract metadata</button>
      </div>
    </td></tr>`;
}

const PDF_ICON = `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" aria-hidden="true">
  <path d="M3.5 1.5h6l3 3v10h-9z"/><path d="M9.5 1.5v3h3"/><path d="M5.5 8h5M5.5 10h5M5.5 12h3" stroke-linecap="round"/></svg>`;

function renderTable() {
  const list = visiblePapers();
  $("#empty").hidden = state.papers.length > 0;
  $("#empty").innerHTML = state.scanning ? "<p>Building your library… papers appear here as they are processed.</p>"
    : state.sourcesOk ? "<p>No PDFs found in your literature folder. Add some and click “Rescan folder”.</p>"
    : `<p>No literature folder is set, so there is nothing to show yet.</p>
       <button class="primary" data-open-settings>Choose a folder in Settings</button>`;

  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort.key);
    th.classList.toggle("desc", th.dataset.sort === state.sort.key && state.sort.desc);
  });

  const focusedId = document.activeElement?.matches("tr.paper") ? document.activeElement.dataset.id : null;

  $("#papers tbody").innerHTML = list.map((p) => `
    <tr class="paper ${state.open === p.id ? "open" : ""}" data-id="${p.id}" tabindex="-1">
      <td class="pick"><input type="checkbox" data-pick="${p.id}" ${state.selected.has(p.id) ? "checked" : ""} title="Select for comparison">
        <a class="pdf-link" href="/api/papers/${p.id}/pdf" target="_blank" rel="noopener" title="Open PDF" aria-label="Open PDF">${PDF_ICON}</a></td>
      <td class="title">${esc(p.title || p.filename)}${p.folder ? `<span class="file">${esc(p.folder)}</span>` : ""}</td>
      <td class="author">${authorLabel(p)}</td>
      <td class="num">${p.year ?? ""}</td>
      <td class="journal">${esc(p.journal)}</td>
      <td class="num">${p.cited_by_n || '<span class="muted">0</span>'}</td>
      <td class="num">${p.cited_by_count ?? '<span class="muted">—</span>'}</td>
      <td class="num">${p.ref_count || '<span class="muted">—</span>'}</td>
      <td class="tags" data-id="${p.id}">${tagChips(p, true)}</td>
      <td class="contrib"><div class="clamp">${p.contributions.length ? `<ul>${p.contributions.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : '<span class="muted">—</span>'}</div></td>
    </tr>
    ${state.open === p.id ? detailRow(p) : ""}`).join("");

  if (focusedId) document.querySelector(`tr.paper[data-id="${focusedId}"]`)?.focus({ preventScroll: true });

  // Sections that were open before the re-render need their lazy content again.
  document.querySelectorAll("details[open] ol[data-refs]").forEach(loadRefs);
}

const VIEWS = ["table", "citations", "recommended", "compare"];

function render() {
  const n = state.papers.filter(matchesFilter).length;
  $("#count").textContent = `${n} of ${state.papers.length} documents`;
  renderTagBar();
  renderSelectionBar();
  if (state.view === "table") renderTable();
  else if (state.view === "citations") window.graphView?.render();
  else if (state.view === "recommended") window.worksView?.renderRecommended();
  else window.worksView?.renderCompare();
}

function renderSelectionBar() {
  const n = state.selected.size;
  $("#selbar").hidden = n === 0 || state.view === "compare";
  $("#selbar-n").textContent = `${n} paper${n === 1 ? "" : "s"} selected`;
  $("#selbar-compare").disabled = n < 2;
}

function toggleSelected(id, on = !state.selected.has(id)) {
  on ? state.selected.add(id) : state.selected.delete(id);
  render();
}

// ---------- views ----------

function setView(view) {
  state.view = VIEWS.includes(view) ? view : "table";
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.view === state.view));
  for (const v of VIEWS) $(`#view-${v}`).hidden = state.view !== v;
  render();
}

function openInTable(id) {
  state.open = id;
  if (location.hash !== "#table") location.hash = "#table";
  else render();
  requestAnimationFrame(() => document.querySelector(`tr.paper[data-id="${id}"]`)?.scrollIntoView({ block: "center" }));
}

// "#compare/12,34" preselects papers and "#citations/12" selects a paper in the graph,
// so both can be bookmarked.
function route() {
  const [view, arg] = location.hash.slice(1).split("/");
  if (view === "compare" && arg) arg.split(",").map(Number).filter(Boolean).forEach((id) => state.selected.add(id));
  if (view === "citations" && +arg) {
    if (window.graphView) window.graphView.select(+arg);
    else state.graphSelect = +arg; // graph.js isn't loaded yet on the first route
  }
  setView(view);
}

window.addEventListener("hashchange", route);

// ---------- actions ----------

async function saveTags(id, tags) {
  replacePaper(await api(`/api/papers/${id}`, { method: "PATCH", body: JSON.stringify({ tags }) }));
  render();
}

async function analyzePaper(id, button) {
  if (button) { button.disabled = true; button.textContent = "Analyzing…"; }
  try {
    replacePaper(await api(`/api/papers/${id}/analyze`, { method: "POST" }));
    window.graphView?.invalidate(); // references extracted by Claude can add links
  } catch (e) {
    toast(e.message, 5000);
  }
  render();
}

async function loadRefs(list) {
  if (list.dataset.loaded) return;
  list.dataset.loaded = "1";
  const refs = await api(`/api/papers/${list.dataset.refs}/citations`);
  list.innerHTML = refs.map((r) => {
    const inlib = r.cited_paper_id && state.byId.get(r.cited_paper_id);
    const text = r.raw || [r.author, r.year, r.title, r.journal].filter(Boolean).join(". ") || r.doi;
    const doi = r.doi ? ` <a href="https://doi.org/${esc(r.doi)}" target="_blank" rel="noopener">doi</a>` : "";
    const badge = inlib ? ` <a class="badge plink" data-goto="${inlib.id}">in library</a>` : "";
    return `<li class="${inlib ? "inlib" : ""}">${esc(text)}${doi}${badge}</li>`;
  }).join("") || "<li>No structured reference list available (links may still come from OpenAlex or the PDF text).</li>";
}

let settingsPrompted = false;

async function load() {
  const [papers, status] = await Promise.all([api("/api/papers"), api("/api/status")]);
  setPapers(papers);
  state.llm = status.llm_configured;
  state.sourcesOk = status.sources_ok;
  state.scanning = status.scan.running || rescanPending; // the scan may not have started yet
  if (!status.sources_ok && !settingsPrompted && !settingsDialog.open) {
    settingsPrompted = true;
    openSettings(status.sources ? `The literature folder ${status.sources} doesn't exist.` : "Choose the folder that holds your PDFs to get started.");
  }
  window.graphView?.invalidate();
  window.worksView?.invalidate();
  render();
  showProgress(status);
}

// ---------- background job progress (scan or Claude analysis) ----------

let pollTimer, lastReload = 0, rescanPending = false, watchingAnalysis = false;
function showProgress(status) {
  const { scan, analysis } = status;
  watchingAnalysis ||= analysis.running;
  const job = scan.running ? scan : analysis.running ? analysis : null;
  const bar = $("#progress");
  bar.hidden = !job;
  if (!job) return;
  const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
  const verb = job === scan ? "Processing" : "Analyzing with Claude";
  const errors = job.errors ? ` · ${job.errors} failed` : "";
  bar.querySelector(".bar").style.setProperty("--pct", `${pct}%`);
  bar.querySelector(".label").textContent = job.total
    ? `${verb} ${job.done}/${job.total}${errors}${job.current ? ` · last: ${job.current}` : ""}`
    : "Scanning folder…";
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollStatus, 3000);
}

async function pollStatus() {
  const status = await api("/api/status");
  const running = status.scan.running || status.analysis.running;
  const editing = document.activeElement?.matches("#papers input, #papers textarea");
  const rescanDone = rescanPending && !status.scan.running;
  if (!running || rescanDone || (!editing && Date.now() - lastReload > 20000)) {
    lastReload = Date.now();
    await load();
  } else {
    showProgress(status);
  }
  if (rescanDone) {
    rescanPending = false;
    await finishRescan(status.scan.last);
    downloads.later.clear(); // asking for a rescan also brings back anything put off with "Decide later"
    checkDownloads();
    return; // `status` predates any analysis the prompt just started
  }
  if (running) return;
  if (watchingAnalysis) {
    watchingAnalysis = false;
    const a = status.analysis;
    if (a.stopped) toast(`Analysis paused after ${a.done - a.errors} documents: ${a.stopped}. The rest can be analyzed from their details panels.`, 12000);
    else toast(`Analysis done: ${a.done - a.errors} analyzed${a.errors ? `, ${a.errors} failed (${a.last_error})` : ""}`, 6000);
  } else if (status.scan.last) {
    toast(`Scan done: ${status.scan.last.added} new, ${status.scan.last.links} citation links`, 4000);
  }
}

// After a manual rescan, offer to analyze just the files it found.
async function finishRescan(last) {
  if (!last) return;
  toast(`Scan done: ${last.added} new, ${last.links} citation links`, 4000);
  await offerAnalysis(last.new_ids, "Found");
}

async function offerAnalysis(ids, verb) {
  const found = ids.map((id) => state.byId.get(id)).filter(Boolean);
  if (!found.length || !state.llm) return;
  const names = found.slice(0, 10).map((p) => `• ${p.filename}`).join("\n") + (found.length > 10 ? `\n… and ${found.length - 10} more` : "");
  if (!confirm(`${verb} ${found.length} new PDF${found.length === 1 ? "" : "s"}:\n\n${names}\n\nAnalyze with Claude in the background? This uses your Claude usage.`)) return;
  await startAnalysis(found.map((p) => p.id));
}

async function startAnalysis(ids) {
  const r = await api("/api/analyze", { method: "POST", body: JSON.stringify({ ids }) });
  if (!r.started) return toast("An analysis is already running, so nothing new was started. Try again when it finishes.", 6000);
  // The job's thread may not have marked itself running yet when this response was built.
  showProgress({ scan: { running: false }, analysis: { ...r.analysis, running: true, total: r.count } });
}

// ---------- NAS backup indicator ----------

let backupTimer, backupWasRunning = false;

function renderBackup(b) {
  const btn = $("#backup"), last = b.last_result, failed = last && !last.ok;
  const date = b.last_success
    ? new Date(b.last_success).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "never";
  $("#backup-label").textContent = b.running ? "Backing up…" : date;
  btn.disabled = b.running;
  btn.hidden = !b.enabled;
  btn.classList.toggle("warn", !b.running && (failed || !b.last_success));
  btn.title = [
    b.last_success ? `Last backup: ${new Date(b.last_success).toLocaleString()}` : "No backup yet",
    failed ? `Last attempt failed (${new Date(last.at).toLocaleString()}): ${last.message}` : last ? last.message : "",
    "Click to back up now",
  ].filter(Boolean).join("\n");

  if (backupWasRunning && !b.running && last) toast(`Backup ${last.ok ? "done" : "failed"}: ${last.message}`, 6000);
  backupWasRunning = b.running;
  clearTimeout(backupTimer);
  backupTimer = setTimeout(pollBackup, b.running ? 3000 : 60000);
}

async function pollBackup() {
  renderBackup((await api("/api/status")).backup);
}

$("#backup").addEventListener("click", async () => {
  const r = await api("/api/backup", { method: "POST" });
  if (!r.started) toast("A backup is already running");
  renderBackup({ ...r.backup, running: true });
});

// ---------- settings ----------

const settingsDialog = $("#settings");
$("#settings-form").elements.backup_hour.innerHTML =
  Array.from({ length: 24 }, (_, h) => `<option value="${h}">${String(h).padStart(2, "0")}:00</option>`).join("");

async function openSettings(message = "") {
  const form = $("#settings-form");
  for (const [key, value] of Object.entries(await api("/api/config"))) {
    if (form.elements[key]) form.elements[key].value = value ?? "";
  }
  $("#settings-msg").textContent = message;
  if (dlDialog.open) dlDialog.close();
  if (!settingsDialog.open) settingsDialog.showModal();
}

$("#settings-btn").addEventListener("click", () => openSettings());
$("#empty").addEventListener("click", (e) => { if (e.target.closest("[data-open-settings]")) openSettings(); });
$("#settings-cancel").addEventListener("click", () => settingsDialog.close());

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  body.backup_hour = +body.backup_hour;
  try {
    const r = await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
    settingsDialog.close();
    toast(r.rescanning ? "Saved. Scanning the literature folder…" : "Settings saved");
    if (r.rescanning) {
      rescanPending = true;
      showProgress({ scan: { running: true, total: 0 }, analysis: { running: false } });
    }
    pollBackup();
    load();
  } catch (err) {
    $("#settings-msg").textContent = err.message;
  }
});

// ---------- articles found in the Downloads folder ----------

const downloads = { items: [], busy: new Set(), later: new Set(), added: [], polling: false };
const dlDialog = $("#downloads");

function renderDownloads() {
  const n = downloads.items.length;
  $("#dl-heading").textContent = n === 1 ? "New article in Downloads" : `${n} new articles in Downloads`;
  $("#dl-list").innerHTML = downloads.items.map((d) => {
    const busy = downloads.busy.has(d.id) ? "disabled" : "";
    return `<li data-id="${d.id}">
      <div class="dl-title">${esc(d.title)}</div>
      <div>${esc(d.author)} · ${esc(d.year)}${d.journal ? ` · <span class="muted">${esc(d.journal)}</span>` : ""}</div>
      <div class="dl-file muted">${esc(d.filename)} → ${esc(d.new_filename)}</div>
      <div class="dl-actions">
        <button class="primary" data-dl="add" ${busy}>${busy ? "Adding…" : "Add to library"}</button>
        <button data-dl="ignore" ${busy}>Don't add</button>
      </div></li>`;
  }).join("");
}

function showDownloads(items) {
  if (settingsDialog.open) return; // one dialog at a time; asked again on the next check
  const inFlight = downloads.items.filter((d) => downloads.busy.has(d.id));
  downloads.items = [...inFlight, ...items.filter((d) => !downloads.later.has(d.id) && !downloads.busy.has(d.id))];
  if (!downloads.items.length) {
    if (dlDialog.open) dlDialog.close();
    return;
  }
  renderDownloads();
  if (!dlDialog.open) {
    $("#dl-msg").textContent = "";
    dlDialog.showModal();
    dlDialog.focus(); // not the first button: a stray Enter mustn't move a file
  }
}

async function refreshDownloads(path = "/api/downloads", opts) {
  const r = await api(path, opts);
  showDownloads(r.items);
  if (r.checking && !downloads.polling) { // the first look at a new file takes a moment
    downloads.polling = true;
    setTimeout(() => { downloads.polling = false; refreshDownloads(); }, 3000);
  }
}

const checkDownloads = () => refreshDownloads("/api/downloads/check", { method: "POST" });

$("#dl-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-dl]");
  if (!btn) return;
  const id = +btn.closest("[data-id]").dataset.id, adding = btn.dataset.dl === "add";
  downloads.busy.add(id);
  renderDownloads();
  try {
    const r = await api(`/api/downloads/${id}/${btn.dataset.dl}`, { method: "POST" });
    if (adding) downloads.added.push(r.paper_id);
    $("#dl-msg").textContent = adding ? `Added as ${r.filename}` : "";
  } catch (err) {
    $("#dl-msg").textContent = err.message;
  }
  downloads.busy.delete(id);
  downloads.items = downloads.items.filter((d) => d.id !== id);
  await refreshDownloads();
  if (!dlDialog.open) finishDownloads(); // the dialog may have been closed during the add
});

$("#dl-later").addEventListener("click", () => dlDialog.close());

dlDialog.addEventListener("close", () => {
  downloads.items.forEach((d) => downloads.later.add(d.id)); // asked again the next time the page is opened
  finishDownloads();
});

async function finishDownloads() {
  const added = downloads.added;
  downloads.added = [];
  if (!added.length) return;
  await load();
  toast(`${added.length} paper${added.length === 1 ? "" : "s"} added to your library`, 4000);
  await offerAnalysis(added, "Added");
}

// Also when returning to a tab that was left open, which is otherwise never "opened" again.
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkDownloads(); });

// ---------- events ----------

$("#search").addEventListener("input", (e) => { state.query = e.target.value; render(); });

// Ctrl+F searches the library instead of opening the browser's find bar.
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== "f") return;
  e.preventDefault();
  $("#search").focus();
  $("#search").select();
});

function focusRow(row) {
  row.focus({ preventScroll: true });
  row.scrollIntoView({ block: "nearest" });
}

$("#search").addEventListener("keydown", (e) => {
  if ((e.key !== "Enter" && e.key !== "ArrowDown") || state.view !== "table") return;
  e.preventDefault();
  const first = $("#papers tbody tr.paper");
  if (first) focusRow(first);
});

// Keyboard navigation on a focused row: arrows move, p opens the PDF.
$("#papers").addEventListener("keydown", (e) => {
  if (!e.target.matches("tr.paper") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const rows = [...document.querySelectorAll("#papers tbody tr.paper")];
    const i = rows.indexOf(e.target) + (e.key === "ArrowDown" ? 1 : -1);
    if (i < 0) $("#search").focus(); // past the first row: back to typing
    else if (rows[i]) focusRow(rows[i]);
  } else if (e.key === "Enter" && !e.repeat) {
    const id = +e.target.dataset.id;
    state.open = state.open === id ? null : id;
    render(); // keeps focus on the row, but closing a row above can move it
    document.activeElement.scrollIntoView({ block: "nearest" });
  } else if (e.key.toLowerCase() === "p" && !e.repeat) {
    window.open(`/api/papers/${e.target.dataset.id}/pdf`, "_blank");
  }
});

$("#category").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cat]");
  if (!b) return;
  state.category = b.dataset.cat;
  document.querySelectorAll("#category button").forEach((x) => x.classList.toggle("active", x === b));
  render();
});

$("#scan").addEventListener("click", async () => {
  let r;
  try {
    r = await api("/api/scan", { method: "POST" });
  } catch (err) {
    return openSettings(err.message);
  }
  if (!r.started) toast("A scan is already running");
  rescanPending = true;
  showProgress({ scan: { ...r.scan, running: true }, analysis: { running: false } });
});

document.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  const numeric = ["year", "cited_by_n", "cited_by_count", "ref_count"].includes(key);
  state.sort = { key, desc: state.sort.key === key ? !state.sort.desc : numeric };
  render();
}));

$("#tagbar").addEventListener("click", (e) => {
  if (e.target.closest("[data-alltags]")) { state.allTags = !state.allTags; return render(); }
  const t = e.target.closest("[data-filter]")?.dataset.filter;
  if (!t) return;
  state.tagFilter.has(t) ? state.tagFilter.delete(t) : state.tagFilter.add(t);
  render();
});

$("#papers").addEventListener("toggle", (e) => {
  const d = e.target;
  if (!d.matches("details[data-section]")) return;
  d.open ? state.openSections.add(d.dataset.section) : state.openSections.delete(d.dataset.section);
  const list = d.querySelector("ol[data-refs]");
  if (d.open && list) loadRefs(list);
}, true);

$("#papers").addEventListener("click", async (e) => {
  const remove = e.target.closest("[data-remove]");
  if (remove) {
    e.stopPropagation();
    const id = +remove.closest("[data-id]").dataset.id;
    const p = state.byId.get(id);
    return saveTags(id, p.tags.filter((t) => t !== remove.dataset.remove));
  }
  const goto = e.target.closest("[data-goto]");
  if (goto) return openInTable(+goto.dataset.goto);
  const pick = e.target.closest("[data-pick]");
  if (pick) return toggleSelected(+pick.dataset.pick, pick.checked);
  if (e.target.closest("input, textarea, a, summary, details")) return;

  const action = e.target.closest("[data-action]");
  if (action) {
    const id = +action.closest("[data-id]").dataset.id;
    if (action.dataset.action === "pdf") window.open(`/api/papers/${id}/pdf`, "_blank");
    if (action.dataset.action === "analyze") analyzePaper(id, action);
    if (action.dataset.action === "graph") location.hash = `#citations/${id}`;
    if (action.dataset.action === "refresh") {
      action.disabled = true;
      await api(`/api/papers/${id}/refresh`, { method: "POST" });
      await load();
      toast("Metadata and references re-extracted");
    }
    return;
  }

  const row = e.target.closest("tr.paper");
  if (row) {
    const id = +row.dataset.id;
    state.open = state.open === id ? null : id;
    render();
  }
});

$("#papers").addEventListener("keydown", (e) => {
  const input = e.target.closest("input.tag-input");
  if (!input || e.key !== "Enter") return;
  const value = input.value.trim().toLowerCase();
  if (!value) return;
  const id = +input.dataset.id;
  const p = state.byId.get(id);
  const added = value.split(",").map((t) => t.trim()).filter(Boolean);
  saveTags(id, [...new Set([...p.tags, ...added])]).then(() => {
    document.querySelector(`input.tag-input[data-id="${id}"]`)?.focus();
  });
});

let notesTimer;
$("#papers").addEventListener("input", (e) => {
  const ta = e.target.closest("textarea[data-notes]");
  if (!ta) return;
  const id = +ta.dataset.notes;
  const label = $(`#saved-${id}`);
  label.textContent = "editing…";
  clearTimeout(notesTimer);
  notesTimer = setTimeout(async () => {
    replacePaper(await api(`/api/papers/${id}`, { method: "PATCH", body: JSON.stringify({ notes: ta.value }) }));
    label.textContent = "saved"; // no re-render: keep the cursor where it is
  }, 600);
});

$("#selbar-compare").addEventListener("click", () => { location.hash = "#compare"; });
$("#selbar-clear").addEventListener("click", () => { state.selected.clear(); render(); });

route();
$("#search").focus();
pollBackup();
load().then(checkDownloads);
