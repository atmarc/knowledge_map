// Recommended and Compare views. Shares `state`, `api`, `esc`, `shortLabel`, `matchesFilter`,
// `openInTable`, `toggleSelected`, `render` with app.js.

(() => {
  const PAGE = 50;
  const rec = { items: null, loading: false, retry: null };
  const cit = { data: null, loading: false, retry: null };
  const shown = { cited: PAGE, citing: PAGE };
  const cmp = { key: null, items: null, retry: null };
  const $ = (sel) => document.querySelector(sel);

  // Which of the user's papers count as citers: category + tag filters, not the search box
  // (in these views the search box filters the cited works instead).
  function citerFilter(p) {
    if (state.category && p.category !== state.category) return false;
    return [...state.tagFilter].every((t) => p.tags.includes(t));
  }

  function workLine(w) {
    const authors = w.authors.length > 2 ? `${w.authors[0]} et al.` : w.authors.join(" & ");
    const title = w.title || w.raw || (w.doi ? `doi:${w.doi}` : "Untitled reference");
    const titleHtml = w.paper_id
      ? `<a class="plink" data-goto="${w.paper_id}">${esc(title)}</a>`
      : w.doi ? `<a href="https://doi.org/${esc(w.doi)}" target="_blank" rel="noopener">${esc(title)}</a>` : esc(title);
    const meta = [authors, w.year, w.journal].filter(Boolean).map(esc).join(" · ");
    return `<div class="wtitle">${titleHtml}</div><div class="wmeta">${meta}${w.pending ? " <span class=\"muted\">(looking up details…)</span>" : ""}</div>`;
  }

  function citerChips(ids, max = 8) {
    const papers = ids.map((id) => state.byId.get(id)).filter(Boolean).sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
    const chip = (p) => `<span class="chip" data-goto="${p.id}" title="${esc(p.title || p.filename)}">${esc(shortLabel(p))}</span>`;
    if (papers.length <= max) return papers.map(chip).join("");
    return papers.slice(0, max).map(chip).join("") +
      `<details><summary>+${papers.length - max} more</summary>${papers.slice(max).map(chip).join("")}</details>`;
  }

  function matchesQuery(w) {
    const q = state.query.toLowerCase();
    return !q || [w.title, w.raw, w.journal, w.year, w.doi, w.authors.join(" ")].join(" ").toLowerCase().includes(q);
  }

  // ---------- Recommended ----------

  async function loadRecommended() {
    rec.loading = true;
    try {
      const r = await api("/api/recommended?limit=400");
      rec.items = r.items;
      clearTimeout(rec.retry);
      // Titles for DOI-only references are fetched in the background; refresh until done.
      if (r.resolving && state.view === "recommended") rec.retry = setTimeout(loadRecommended, 4000);
    } finally {
      rec.loading = false;
    }
    if (state.view === "recommended") renderRecommended();
  }

  function renderRecommended() {
    const body = $("#recommended tbody");
    if (!rec.items) {
      body.innerHTML = '<tr><td colspan="4" class="muted">Loading…</td></tr>';
      if (!rec.loading) loadRecommended();
      return;
    }
    const rows = rec.items
      .map((w) => ({ w, citers: w.cited_by.filter((id) => state.byId.has(id) && citerFilter(state.byId.get(id))) }))
      .filter(({ w, citers }) => citers.length >= 2 && matchesQuery(w))
      .sort((a, b) => b.citers.length - a.citers.length);
    moreButton("cited", rows.length);
    body.innerHTML = rows.slice(0, shown.cited).map(({ w, citers }, i) => `
      <tr>
        <td class="num muted">${i + 1}</td>
        <td class="num"><b>${citers.length}</b></td>
        <td class="work">${workLine(w)}</td>
        <td class="citers">${citerChips(citers)}</td>
      </tr>`).join("") || '<tr><td colspan="4" class="muted">No work is cited by two or more of the selected papers.</td></tr>';
    renderCiting();
  }

  function moreButton(which, total) {
    const btn = document.querySelector(`[data-more="${which}"]`);
    btn.hidden = total <= shown[which];
    btn.textContent = `Show ${Math.min(PAGE, total - shown[which])} more (${total} in total)`;
  }

  // ---------- Citing your library ----------

  async function loadCiting() {
    cit.loading = true;
    try {
      cit.data = await api("/api/citing");
    } finally {
      cit.loading = false;
    }
    clearTimeout(cit.retry);
    if (cit.data.running && state.view === "recommended") cit.retry = setTimeout(loadCiting, 5000);
    if (state.view === "recommended") renderCiting();
  }

  const day = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

  function renderCiting() {
    const body = $("#citing tbody");
    if (!cit.data) {
      body.innerHTML = '<tr><td colspan="4" class="muted">Loading…</td></tr>';
      if (!cit.loading) loadCiting();
      return;
    }
    const d = cit.data, last = d.last_result;
    const failed = last && !last.ok ? ` · last attempt failed (${day(last.at)}): ${last.message}` : "";
    $("#citing-status").textContent = d.running ? "· updating…" : d.last_success ? `· updated ${day(d.last_success)}${failed}` : failed || "· not computed yet";
    $("#citing-coverage").textContent = `${d.screened} of your ${d.papers} documents are known to OpenAlex and can be screened.`;

    const rows = d.items
      .map((w) => ({ w, citers: w.cites.filter((id) => state.byId.has(id) && citerFilter(state.byId.get(id))) }))
      .filter(({ w, citers }) => citers.length >= 2 && matchesQuery(w))
      .sort((a, b) => b.citers.length - a.citers.length || (b.w.cited_by_count ?? 0) - (a.w.cited_by_count ?? 0));
    moreButton("citing", rows.length);
    const empty = d.items.length ? "No work cites two or more of the selected papers."
      : d.running ? "Collecting citing works from OpenAlex… this takes a few minutes." : "Nothing yet: the first daily update hasn't finished.";
    body.innerHTML = rows.slice(0, shown.citing).map(({ w, citers }, i) => `
      <tr>
        <td class="num muted">${i + 1}</td>
        <td class="num"><b>${citers.length}</b></td>
        <td class="work">${workLine(w)}${w.cited_by_count != null ? `<div class="wmeta">cited ${w.cited_by_count}× worldwide</div>` : ""}</td>
        <td class="citers">${citerChips(citers)}</td>
      </tr>`).join("") || `<tr><td colspan="4" class="muted">${empty}</td></tr>`;
  }

  // ---------- Compare ----------

  function renderSelectedPapers() {
    const papers = [...state.selected].map((id) => state.byId.get(id)).filter(Boolean);
    $("#cmp-selected").innerHTML = papers.map((p) => `
      <span class="cmp-paper"><a class="plink" data-goto="${p.id}">${esc(shortLabel(p))}</a>
        <span class="t" title="${esc(p.title || p.filename)}">${esc(p.title || p.filename)}</span>
        <span class="x" data-unpick="${p.id}" title="Remove">×</span></span>`).join("")
      || '<span class="muted">No papers selected. Tick papers in the Table, use “Add to comparison” in the graph, or add them here.</span>';

    $("#cmp-papers").innerHTML = state.papers.filter((p) => p.category === "papers" && !state.selected.has(p.id))
      .map((p) => `<option value="${esc(`${shortLabel(p)} — ${p.title || p.filename}`)}"></option>`).join("");

    const n = papers.length, sel = $("#cmp-min");
    const current = +sel.value || n;
    sel.innerHTML = Array.from({ length: Math.max(0, n - 1) }, (_, i) => n - i)
      .map((k) => `<option value="${k}" ${k === Math.min(current, n) ? "selected" : ""}>${k === n ? `all ${n}` : `at least ${k}`} papers</option>`).join("");
    $("#cmp-min-wrap").hidden = n < 3;
    return papers;
  }

  async function renderCompare() {
    const papers = renderSelectedPapers();
    const out = $("#cmp-body");
    if (papers.length < 2) {
      out.innerHTML = '<p class="empty-state">Select at least two papers to see the references they have in common.</p>';
      return;
    }
    const min = papers.length >= 3 ? +$("#cmp-min").value || papers.length : papers.length;
    const key = `${papers.map((p) => p.id).sort().join(",")}|${min}`;
    if (cmp.key !== key) {
      cmp.key = key;
      cmp.items = null;
      out.innerHTML = '<p class="empty-state">Loading…</p>';
      const r = await api(`/api/compare?ids=${papers.map((p) => p.id).join(",")}&min_shared=${min}`);
      if (cmp.key !== key) return; // selection changed meanwhile
      cmp.items = r.items;
      clearTimeout(cmp.retry);
      if (r.resolving) cmp.retry = setTimeout(() => { cmp.key = null; if (state.view === "compare") renderCompare(); }, 4000);
    }
    if (!cmp.items) return;

    const items = cmp.items.filter(matchesQuery);
    const inLib = items.filter((w) => w.paper_id), ext = items.filter((w) => !w.paper_id);
    const showWho = papers.length >= 3 && min < papers.length;
    const li = (w, cls) => `<li class="${cls}">${workLine(w)}
      ${showWho ? `<div style="margin-top:4px">Cited by: ${citerChips(w.cited_by, 20)}</div>` : ""}
      ${!w.paper_id && w.library_citations > w.cited_by.length ? `<div class="wmeta">cited by ${w.library_citations} papers in your library</div>` : ""}</li>`;
    const what = min === papers.length ? (papers.length === 2 ? "both papers" : `all ${papers.length} papers`) : `at least ${min} of the ${papers.length} papers`;
    out.innerHTML = `
      <p class="cmp-summary"><b>${items.length}</b> works are cited by ${what}: <b>${inLib.length}</b> in your library, <b>${ext.length}</b> not.</p>
      <div class="cmp-cols">
        <div><h2>In your library (${inLib.length})</h2><ul>${inLib.map((w) => li(w, "inlib")).join("") || '<li class="muted">None</li>'}</ul></div>
        <div><h2>Not in your library (${ext.length})</h2><ul>${ext.map((w) => li(w, "ext")).join("") || '<li class="muted">None</li>'}</ul></div>
      </div>`;
  }

  // ---------- events ----------

  for (const id of ["#view-recommended", "#view-compare"]) {
    $(id).addEventListener("click", (e) => {
      const goto = e.target.closest("[data-goto]");
      if (goto) return openInTable(+goto.dataset.goto);
      const unpick = e.target.closest("[data-unpick]");
      if (unpick) toggleSelected(+unpick.dataset.unpick, false);
    });
  }

  $("#cmp-add").addEventListener("change", (e) => {
    const label = e.target.value;
    const p = state.papers.find((q) => `${shortLabel(q)} — ${q.title || q.filename}` === label);
    e.target.value = "";
    if (p) toggleSelected(p.id, true);
  });
  $("#cmp-min").addEventListener("change", () => renderCompare());
  $("#view-recommended").addEventListener("click", (e) => {
    const more = e.target.closest("[data-more]");
    if (!more) return;
    shown[more.dataset.more] += PAGE;
    renderRecommended();
  });

  window.worksView = {
    renderRecommended,
    renderCompare,
    invalidate() { rec.items = null; cit.data = null; cmp.key = null; },
  };
})();
