// Memory graph for the Control Center (Focus and Galaxy). Everything is local: graphology + sigma + graphology-library from vendor/.
// The QML side calls vexGraph.focus(data) / vexGraph.galaxy(data) / vexGraph.zoom(dir); a click sets document.title = "select:<id>".
(function () {
  "use strict";
  const COLORS = { project: "#93a8bf", person: "#cdb98a", preference: "#c8896a" };
  const FACT = "#b9bec6";
  const EDGE = "#3a3e45";
  const EDGE_DIM = "#202328";
  const EDGE_HOT = "#8d939c";
  const colorOf = (type) => COLORS[type] || FACT;
  const sizeOf = (importance, base) => base + 2.2 * Math.max(1, Math.min(5, Number(importance) || 3));
  const GraphCtor = (window.graphology && (window.graphology.Graph || window.graphology));

  let renderer = null, graph = null, mode = "focus", hovered = null, secondHop = [];
  const overlay = document.getElementById("overlay");
  const ctx = overlay.getContext("2d");

  function post(msg) { document.title = msg + "#" + Date.now(); }       // QML listens for title changes

  function reset() {
    if (renderer) { renderer.kill(); renderer = null; }
    graph = new GraphCtor({ multi: false, type: "undirected" });
    hovered = null; secondHop = [];
  }

  function makeRenderer(galaxy) {
    renderer = new Sigma(graph, document.getElementById("graph"), {
      labelFont: "Geist", labelColor: { color: "#c9ced5" }, labelSize: 13, labelWeight: "500",
      labelRenderedSizeThreshold: galaxy ? 9 : 0,                // galaxy: labels only once zoomed in
      defaultEdgeColor: EDGE, minCameraRatio: 0.08, maxCameraRatio: 6, zIndex: true, stagePadding: 90,   // room for labels at the edges
      nodeReducer: (node, data) => {
        const res = { ...data };
        if (hovered && hovered !== node && !graph.areNeighbors(hovered, node)) { res.color = "#4a4e55"; res.label = galaxy ? "" : data.label; }
        if (hovered === node) res.highlighted = true;
        return res;
      },
      edgeReducer: (edge, data) => {
        const res = { ...data };
        if (data.secondHop) { res.hidden = true; return res; }     // drawn dashed on the overlay instead
        if (hovered) {
          const [a, b] = graph.extremities(edge);
          const hot = a === hovered || b === hovered;
          res.color = hot ? EDGE_HOT : EDGE_DIM;
          res.size = hot ? 1.6 : 1;
          res.zIndex = hot ? 1 : 0;
        }
        return res;
      },
    });
    renderer.on("enterNode", ({ node }) => { hovered = node; document.body.style.cursor = "pointer"; renderer.refresh(); });
    renderer.on("leaveNode", () => { hovered = null; document.body.style.cursor = ""; renderer.refresh(); });
    renderer.on("clickNode", ({ node }) => post("select:" + node));
    renderer.on("afterRender", drawSecondHop);
  }

  function sizeOverlay() {
    const r = window.devicePixelRatio || 1;
    overlay.width = Math.round(innerWidth * r); overlay.height = Math.round(innerHeight * r);
    overlay.style.width = innerWidth + "px"; overlay.style.height = innerHeight + "px";
    ctx.setTransform(r, 0, 0, r, 0, 0);
  }

  // 2nd-hop edges: dashed and fainter (sigma has no dashed edges, so they are drawn here, under the same camera)
  function drawSecondHop() {
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (!renderer || !secondHop.length) return;
    ctx.setLineDash([4, 5]);
    ctx.lineWidth = 1;
    for (const [a, b] of secondHop) {
      if (!graph.hasNode(a) || !graph.hasNode(b)) continue;
      const pa = renderer.graphToViewport(graph.getNodeAttributes(a));
      const pb = renderer.graphToViewport(graph.getNodeAttributes(b));
      const hot = hovered && (hovered === a || hovered === b);
      ctx.strokeStyle = hot ? "rgba(141,147,156,0.85)" : (hovered ? "rgba(58,62,69,0.35)" : "rgba(58,62,69,0.7)");
      ctx.beginPath(); ctx.moveTo(pa.x, pa.y); ctx.lineTo(pb.x, pb.y); ctx.stroke();
    }
  }

  function showEmpty(empty) { document.getElementById("empty").style.display = empty ? "flex" : "none"; }

  // ---- Focus: the note in the middle, neighbours on a ring, their neighbours on an outer ring ----------------------------------
  function focus(data) {
    mode = "focus";
    reset();
    showEmpty(!data || !data.nodes || !data.nodes.length);
    if (!data || !data.nodes) return;
    const byHop = { 0: [], 1: [], 2: [] };
    data.nodes.forEach((n) => (byHop[n.hop] || byHop[2]).push(n));
    const parentOf = {};
    data.edges.forEach((e) => {
      const hopOf = (id) => (data.nodes.find((n) => n.id === id) || {}).hop;
      const [s, t] = [e.source, e.target];
      if (hopOf(s) === 1 && hopOf(t) === 2) parentOf[t] = parentOf[t] || s;
      if (hopOf(t) === 1 && hopOf(s) === 2) parentOf[s] = parentOf[s] || t;
    });
    const angle = {};
    byHop[0].forEach((n) => { graph.addNode(n.id, { x: 0, y: 0, label: n.title, size: sizeOf(n.importance, 9), color: colorOf(n.type), hop: 0 }); });
    byHop[1].forEach((n, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, byHop[1].length) - Math.PI / 2;
      angle[n.id] = a;
      graph.addNode(n.id, { x: Math.cos(a), y: Math.sin(a), label: n.title, size: sizeOf(n.importance, 5), color: colorOf(n.type), hop: 1 });
    });
    const kids = {};
    byHop[2].forEach((n) => { const p = parentOf[n.id]; (kids[p] = kids[p] || []).push(n); });
    Object.entries(kids).forEach(([p, list]) => {
      const base = angle[p] !== undefined ? angle[p] : 0, spread = Math.min(0.9, 0.35 * list.length);
      list.forEach((n, j) => {
        const a = base + (list.length > 1 ? -spread / 2 + (spread * j) / (list.length - 1) : 0);
        graph.addNode(n.id, { x: 2.05 * Math.cos(a), y: 2.05 * Math.sin(a), label: n.title, size: sizeOf(n.importance, 3), color: colorOf(n.type), hop: 2 });
      });
    });
    data.edges.forEach((e) => {
      if (!graph.hasNode(e.source) || !graph.hasNode(e.target) || e.source === e.target || graph.hasEdge(e.source, e.target)) return;
      const hops = [graph.getNodeAttribute(e.source, "hop"), graph.getNodeAttribute(e.target, "hop")];
      const second = Math.min(...hops) >= 1;                    // an edge not touching the centre
      graph.addEdge(e.source, e.target, { color: EDGE, size: 1, secondHop: second });
      if (second) secondHop.push([e.source, e.target]);
    });
    makeRenderer(false);
  }

  // ---- Galaxy: every note, laid out with ForceAtlas2, labels only when zoomed in --------------------------------------------------
  function galaxy(data) {
    mode = "galaxy";
    reset();
    showEmpty(!data || !data.nodes || !data.nodes.length);
    if (!data || !data.nodes) return;
    data.nodes.forEach((n, i) => {
      const a = i * 2.399963, r = Math.sqrt(i + 1);               // a sunflower start so ForceAtlas2 begins untangled
      graph.addNode(n.id, { x: r * Math.cos(a), y: r * Math.sin(a), label: n.title, color: n.ghost ? "#4a4e55" : colorOf(n.type),
                            size: sizeOf(n.importance, 2) + Math.min(6, n.links || 0) });
    });
    data.edges.forEach((e) => {
      if (graph.hasNode(e.source) && graph.hasNode(e.target) && e.source !== e.target && !graph.hasEdge(e.source, e.target))
        graph.addEdge(e.source, e.target, { color: EDGE, size: 0.8 });
    });
    const lib = window.graphologyLibrary;
    if (lib && lib.layoutForceAtlas2 && graph.order > 1) {
      const settings = lib.layoutForceAtlas2.inferSettings(graph);
      lib.layoutForceAtlas2.assign(graph, { iterations: 300, settings: { ...settings, gravity: 1, scalingRatio: 6 } });
    }
    makeRenderer(true);
  }

  function zoom(dir) {
    if (!renderer) return;
    const cam = renderer.getCamera();
    dir > 0 ? cam.animatedZoom({ duration: 200 }) : cam.animatedUnzoom({ duration: 200 });
  }

  document.getElementById("zin").addEventListener("click", () => zoom(1));
  document.getElementById("zout").addEventListener("click", () => zoom(-1));
  window.addEventListener("resize", () => { sizeOverlay(); if (renderer) renderer.refresh(); });
  sizeOverlay();
  window.vexGraph = { focus, galaxy, zoom, ready: true };
  post("ready");
})();
