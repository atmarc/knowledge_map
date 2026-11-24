// Citations view: force-directed graph of papers, with an arrow from each paper to the papers it cites.
// Shares `state`, `matchesFilter`, `api`, `esc`, `shortLabel`, `tagClass`, `openInTable` with app.js.

(() => {
  const g = { sig: null, nodes: [], links: [], selected: null, sim: null, fitted: false, building: null };

  const svg = d3.select("#graph");
  const wrap = document.querySelector(".graph-wrap");
  const panel = document.querySelector("#g-panel");
  const tooltip = document.querySelector("#g-tooltip");
  const opt = {
    isolated: document.querySelector("#g-isolated"),
    byYear: document.querySelector("#g-byyear"),
    color: document.querySelector("#g-color"),
  };

  const defs = svg.append("defs");
  for (const id of ["arrow", "arrow-out", "arrow-in"]) {
    defs.append("marker").attr("id", id).attr("viewBox", "0 -4 8 8").attr("refX", 8).attr("refY", 0)
      .attr("markerWidth", 7).attr("markerHeight", 7).attr("markerUnits", "userSpaceOnUse").attr("orient", "auto")
      .append("path").attr("d", "M0,-4L8,0L0,4Z");
  }
  const root = svg.append("g");
  const axisG = root.append("g").attr("class", "year-axis");
  const linkG = root.append("g");
  const nodeG = root.append("g");

  const zoom = d3.zoom().scaleExtent([0.08, 6]).on("zoom", (e) => {
    root.attr("transform", e.transform);
    if (e.sourceEvent) g.fitted = true; // the user took over; stop auto-fitting
    updateLabels();
  });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("click", (e) => { if (e.target === svg.node()) select(null); });

  const radius = (p) => 4 + 2.4 * Math.sqrt(p.cited_by_n || 0);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---------- colors ----------

  function colorScale() {
    const mode = opt.color.value;
    const papers = g.nodes.map((n) => n.p);
    if (mode === "year") {
      const years = papers.map((p) => p.year).filter(Boolean);
      const scale = d3.scaleSequential(d3.interpolateViridis).domain(years.length ? d3.extent(years) : [2000, 2025]);
      return { color: (p) => (p.year ? scale(p.year) : "#999"), legend: () => {
        const [a, b] = scale.domain();
        const stops = d3.range(0, 1.01, 0.1).map((t) => d3.interpolateViridis(t)).join(",");
        return `${a}<span class="ramp" style="background:linear-gradient(90deg,${stops})"></span>${b}`;
      } };
    }
    if (mode === "folder") {
      const folders = [...new Set(papers.map((p) => p.group))].sort();
      const scale = d3.scaleOrdinal(d3.schemeTableau10).domain(folders);
      return { color: (p) => scale(p.group), legend: () => folders.map((f) =>
        `<span><span class="sw" style="background:${scale(f)}"></span>${esc(f)}</span>`).join("") };
    }
    const tagColor = (t) => css(`--tag${tagClass(t).slice(1)}-fg`);
    const top = d3.rollups(papers.filter((p) => p.tags[0]), (v) => v.length, (p) => p.tags[0])
      .sort((a, b) => b[1] - a[1]).slice(0, 8);
    return { color: (p) => (p.tags[0] ? tagColor(p.tags[0]) : "#999"), legend: () => top.map(([t]) =>
      `<span><span class="sw" style="background:${tagColor(t)}"></span>${esc(t)}</span>`).join("") +
      (top.length ? "" : '<span class="muted">No tags yet</span>') };
  }

  // ---------- build ----------

  async function build() {
    const { links } = await api("/api/graph");
    const linked = new Set(links.flatMap((l) => [l.citing_id, l.cited_id]));
    const papers = state.papers.filter((p) => opt.isolated.checked || linked.has(p.id));
    const ids = new Set(papers.map((p) => p.id));
    const edges = links.filter((l) => ids.has(l.citing_id) && ids.has(l.cited_id));

    // Paper objects are replaced on every reload, so always refresh the reference.
    const old = new Map(g.nodes.map((n) => [n.id, n]));
    g.nodes = papers.map((p) => Object.assign(old.get(p.id) || {}, { id: p.id, p }));
    const sig = [...ids].join(",") + "|" + edges.map((l) => `${l.citing_id}>${l.cited_id}`).join(",");
    const changed = sig !== g.sig;
    g.sig = sig;
    g.links = edges.map((l) => ({ source: l.citing_id, target: l.cited_id, method: l.method }));
    draw(changed);
  }

  function yearScale() {
    const years = g.nodes.map((n) => n.p.year).filter(Boolean);
    const [a, b] = years.length ? d3.extent(years) : [2000, 2025];
    const width = Math.max(900, g.nodes.length * 5);
    return d3.scaleLinear().domain([a, b]).range([-width / 2, width / 2]);
  }

  function setForces() {
    const sim = g.sim;
    if (opt.byYear.checked) {
      const xs = yearScale();
      sim.force("x", d3.forceX((n) => (n.p.year ? xs(n.p.year) : xs.range()[0] - 80)).strength(0.7));
      sim.force("y", d3.forceY(0).strength(0.06));
      const [a, b] = xs.domain();
      const ticks = d3.range(Math.ceil(a / 5) * 5, b + 1, 5);
      axisG.selectAll("g").data(ticks, (d) => d).join((en) => {
        const t = en.append("g");
        t.append("line").attr("y1", -2000).attr("y2", 2000);
        t.append("text").attr("y", -14).attr("text-anchor", "middle");
        return t;
      }).attr("transform", (d) => `translate(${xs(d)},0)`).select("text").text((d) => d);
      axisG.attr("display", null);
    } else {
      sim.force("x", d3.forceX(0).strength(0.04));
      sim.force("y", d3.forceY(0).strength(0.04));
      axisG.attr("display", "none");
    }
  }

  function draw(restart) {
    if (!g.sim) {
      g.sim = d3.forceSimulation()
        .force("charge", d3.forceManyBody().strength(-260).distanceMax(700))
        .force("collide", d3.forceCollide((n) => radius(n.p) + 22).iterations(2))  // room for the labels
        .on("tick", tick)
        .on("end", () => { if (!g.fitted) fit(); });
    }
    g.sim.nodes(g.nodes);
    g.sim.force("link", d3.forceLink(g.links).id((n) => n.id).distance(110).strength(0.12));
    setForces();
    if (restart) {
      if (!g.fitted) {
        // Center the origin until the layout settles, then fit it to the viewport.
        svg.call(zoom.transform, d3.zoomIdentity.translate(wrap.clientWidth / 2, wrap.clientHeight / 2).scale(0.6));
        clearTimeout(g.fitTimer);
        g.fitTimer = setTimeout(() => { if (!g.fitted) fit(); }, 1500);
      }
      g.sim.alpha(0.9).restart();
    }

    linkG.selectAll("line").data(g.links, (l) => `${l.source.id ?? l.source}>${l.target.id ?? l.target}`)
      .join("line").attr("class", (l) => `edge ${l.method === "author-year" ? "weak" : ""}`);

    const node = nodeG.selectAll("g.node").data(g.nodes, (n) => n.id).join((en) => {
      const ng = en.append("g").attr("class", "node");
      ng.append("circle");
      ng.append("text").attr("dy", "0.32em");
      ng.call(d3.drag()
        .on("start", (e, n) => { if (!e.active) g.sim.alphaTarget(0.2).restart(); n.fx = n.x; n.fy = n.y; })
        .on("drag", (e, n) => { n.fx = e.x; n.fy = e.y; })
        .on("end", (e, n) => { if (!e.active) g.sim.alphaTarget(0); n.fx = null; n.fy = null; }));
      ng.on("click", (e, n) => { e.stopPropagation(); select(n.id); })
        .on("dblclick", (e, n) => window.open(`/api/papers/${n.id}/pdf`, "_blank"))
        .on("mousemove", (e, n) => showTooltip(e, n.p))
        .on("mouseleave", () => (tooltip.hidden = true));
      return ng;
    });
    node.select("circle").attr("r", (n) => radius(n.p));
    node.select("text").attr("x", (n) => radius(n.p) + 3).text((n) => shortLabel(n.p));
    style();
  }

  function tick() {
    linkG.selectAll("line").each(function (l) {
      const dx = l.target.x - l.source.x, dy = l.target.y - l.source.y;
      const d = Math.hypot(dx, dy) || 1, rt = radius(l.target.p) + 2;
      this.setAttribute("x1", l.source.x);
      this.setAttribute("y1", l.source.y);
      this.setAttribute("x2", l.target.x - (dx / d) * rt);
      this.setAttribute("y2", l.target.y - (dy / d) * rt);
    });
    nodeG.selectAll("g.node").attr("transform", (n) => `translate(${n.x},${n.y})`);
  }

  function fit() {
    if (!g.nodes.length) return;
    const w = wrap.clientWidth, h = wrap.clientHeight;
    const [x0, x1] = d3.extent(g.nodes, (n) => n.x), [y0, y1] = d3.extent(g.nodes, (n) => n.y);
    const k = Math.min(2, 0.9 / Math.max((x1 - x0 + 80) / w, (y1 - y0 + 80) / h));
    svg.transition().duration(500).call(zoom.transform,
      d3.zoomIdentity.translate(w / 2, h / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
    g.fitted = true;
  }

  // ---------- styling, selection, labels ----------

  function neighbors(id) {
    const p = state.byId.get(id);
    return p ? new Set([id, ...p.cites, ...p.cited_by]) : new Set();
  }

  function style() {
    const { color, legend } = colorScale();
    document.querySelector("#g-legend").innerHTML = legend();
    const sel = g.selected, near = sel ? neighbors(sel) : null;
    const filtering = state.query || state.category || state.tagFilter.size;
    const visible = (p) => (near ? near.has(p.id) : !filtering || matchesFilter(p));

    nodeG.selectAll("g.node")
      .classed("dim", (n) => !visible(n.p))
      .classed("selected", (n) => n.id === sel)
      .select("circle").attr("fill", (n) => color(n.p));

    linkG.selectAll("line").each(function (l) {
      const out = sel && l.source.id === sel, inn = sel && l.target.id === sel;
      const dim = sel ? !(out || inn) : !(visible(l.source.p) && visible(l.target.p));
      this.classList.toggle("out", !!out);
      this.classList.toggle("in", !!inn);
      this.classList.toggle("dim", dim);
      this.setAttribute("marker-end", `url(#${out ? "arrow-out" : inn ? "arrow-in" : "arrow"})`);
    });
    updateLabels();
  }

  function updateLabels() {
    const k = d3.zoomTransform(svg.node()).k;
    const threshold = d3.quantile(g.nodes.map((n) => n.p.cited_by_n || 0).sort(d3.ascending), 0.92) || 1;
    const near = g.selected ? neighbors(g.selected) : null;
    nodeG.selectAll("g.node").select("text").attr("display", (n) =>
      k >= 1.4 || (n.p.cited_by_n >= Math.max(1, threshold)) || (near && near.has(n.id)) ? null : "none");
  }

  function linkList(ids, cls, title) {
    const items = ids.map((id) => state.byId.get(id)).filter(Boolean).sort((a, b) => (b.year ?? 0) - (a.year ?? 0));
    return `<h3 class="${cls}">${title} (${items.length})</h3>` + (items.length
      ? `<ul class="linklist">${items.map((q) => `<li><a data-gsel="${q.id}" title="${esc(q.title)}">${esc(shortLabel(q))}</a> — ${esc((q.title || q.filename).slice(0, 80))}</li>`).join("")}</ul>`
      : '<p class="muted">None in your library.</p>');
  }

  function renderPanel() {
    const p = g.selected && state.byId.get(g.selected);
    panel.hidden = !p;
    if (!p) return;
    const authors = p.authors.length > 3 ? `${p.authors.slice(0, 3).join(", ")} et al.` : p.authors.join(", ");
    panel.innerHTML = `
      <button class="close" title="Close">×</button>
      <h2>${esc(p.title || p.filename)}</h2>
      <div class="muted">${esc(authors)}${p.year ? ` · ${p.year}` : ""}${p.journal ? ` · ${esc(p.journal)}` : ""}</div>
      <div style="margin-top:8px">${p.tags.map((t) => `<span class="tag ${tagClass(t)}">${esc(t)}</span>`).join("")}</div>
      ${p.contributions.length ? `<h3>Key contributions</h3><ul class="bullets">${p.contributions.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      ${linkList(p.cites, "out", "Cites")}
      ${linkList(p.cited_by, "in", "Cited by")}
      <div class="actions">
        <button class="primary" data-gact="pdf">Open PDF</button>
        <button data-gact="table">Show in table</button>
        <button data-gact="compare">${state.selected.has(p.id) ? "Remove from" : "Add to"} comparison</button>
      </div>`;
  }

  function select(id, center = false) {
    g.selected = id;
    renderPanel();
    style();
    const n = id && g.nodes.find((x) => x.id === id);
    if (n && center) {
      const go = () => svg.transition().duration(500).call(zoom.translateTo, n.x, n.y);
      n.x === undefined ? setTimeout(go, 800) : go();
      g.fitted = true;
    }
  }

  function showTooltip(e, p) {
    const [x, y] = d3.pointer(e, wrap);
    tooltip.innerHTML = `<b>${esc(shortLabel(p))}</b> — ${esc(p.title || p.filename)}<br>` +
      `cites ${p.cites.length} · cited by ${p.cited_by.length} in library`;
    tooltip.style.left = `${Math.min(x + 14, wrap.clientWidth - 330)}px`;
    tooltip.style.top = `${y + 14}px`;
    tooltip.hidden = false;
  }

  panel.addEventListener("click", (e) => {
    if (e.target.closest(".close")) return select(null);
    const s = e.target.closest("[data-gsel]");
    if (s) return select(+s.dataset.gsel, true);
    const act = e.target.closest("[data-gact]")?.dataset.gact;
    if (act === "pdf") window.open(`/api/papers/${g.selected}/pdf`, "_blank");
    if (act === "table") openInTable(g.selected);
    if (act === "compare") toggleSelected(g.selected);
  });

  opt.isolated.addEventListener("change", () => { g.fitted = false; build(); });
  opt.byYear.addEventListener("change", () => { g.fitted = false; setForces(); g.sim.alpha(0.8).restart(); });
  opt.color.addEventListener("change", style);

  // ---------- interface used by app.js ----------

  let pendingSelect = state.graphSelect || null;
  window.graphView = {
    render() {
      if (g.sig === null || g.dirty) {
        g.dirty = false;
        g.building = build().then(() => {
          // The first build can run before the papers have loaded: keep waiting for the paper.
          if (pendingSelect && state.byId.has(pendingSelect)) { select(pendingSelect, true); pendingSelect = null; }
        });
      } else {
        renderPanel();
        style();
      }
    },
    invalidate() { g.dirty = true; },
    select(id) {
      if (g.sig === null || g.dirty) pendingSelect = id;
      else select(id, true);
    },
  };
})();
