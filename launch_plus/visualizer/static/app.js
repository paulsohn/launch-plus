/* launch-plus visualizer — Cytoscape.js frontend */
(function () {
  "use strict";

  // --- Load graph data ---
  const raw = document.getElementById("graph-data").textContent;
  const graph = JSON.parse(raw);

  // --- Build Cytoscape elements ---
  const elements = [];

  // Groups as compound (parent) nodes
  for (const g of graph.groups) {
    // Shorten source path: keep everything from the package name onward
    // e.g. ".../src/launcher/autoware_launch/foo/launch/bar.xml" -> "foo/launch/bar.xml"
    let label = g.source || "group";
    const launchIdx = label.lastIndexOf("/launch/");
    if (launchIdx >= 0) {
      // Find the package directory (one level above "launch/")
      const prefix = label.substring(0, launchIdx);
      const pkgSlash = prefix.lastIndexOf("/");
      if (pkgSlash >= 0) {
        label = label.substring(pkgSlash + 1);
      }
    }
    elements.push({
      group: "nodes",
      data: {
        id: g.id,
        label: label,
        parent: g.parent || undefined,
        type: "group",
        source: g.source || "",
      },
    });
  }

  // Nodes (including containers, composable nodes, LCN, executables)
  for (const n of graph.nodes) {
    const label =
      n.type === "composable_node"
        ? (n.plugin || "") + (n.name ? `\n(${n.name})` : "")
        : n.type === "executable"
        ? n.name || n.cmd || "exec"
        : n.type === "load_composable_node"
        ? `load -> ${n.target || "?"}`
        : n.fqn || n.name || "node";

    elements.push({
      group: "nodes",
      data: {
        id: n.id,
        label: label,
        parent: n.parent || undefined,
        type: n.type,
        package: n.package || "",
        executable: n.executable || "",
        plugin: n.plugin || "",
        name: n.name || "",
        namespace: n.namespace || "",
        fqn: n.fqn || "",
        cmd: n.cmd || "",
        target: n.target || "",
        nodeColor: n.color || "#888",
        params: n.params || [],
        remaps: n.remaps || [],
      },
    });
  }

  // Topics
  for (const t of graph.topics) {
    elements.push({
      group: "nodes",
      data: {
        id: t.id,
        label: t.name,
        fullName: t.name,
        type: "topic",
      },
    });
  }

  // Edges
  for (const e of graph.edges) {
    elements.push({
      group: "edges",
      data: {
        source: e.source,
        target: e.target,
        type: e.type,
      },
    });
  }

  // --- Register ELK layout ---
  if (typeof cytoscapeElk !== "undefined") {
    cytoscapeElk(cytoscape, ELK);
  } else if (cytoscape.use) {
    // fallback
  }

  // --- Initialize Cytoscape ---
  const cy = cytoscape({
    container: document.getElementById("cy"),
    elements: elements,
    style: [
      // Group nodes
      {
        selector: 'node[type="group"]',
        style: {
          shape: "round-rectangle",
          "background-color": "rgba(40, 40, 80, 0.4)",
          "border-color": "#334",
          "border-width": 1,
          "border-style": "solid",
          label: "data(label)",
          "text-valign": "top",
          "text-halign": "center",
          "font-size": "10px",
          color: "#8888aa",
          "text-margin-y": 8,
          "padding": "16px",
          "text-wrap": "wrap",
        },
      },
      // Regular nodes
      {
        selector: 'node[type="node"], node[type="lifecycle_node"]',
        style: {
          shape: "round-rectangle",
          "background-color": "data(nodeColor)",
          "border-color": "#555",
          "border-width": 1,
          width: "label",
          height: "label",
          "padding": "8px",
          label: "data(label)",
          "text-valign": "center",
          "text-halign": "center",
          "font-size": "11px",
          color: "#1a1a2e",
          "font-weight": "bold",
          "text-wrap": "wrap",
        },
      },
      // Container nodes (compound)
      {
        selector: 'node[type="container"]',
        style: {
          shape: "round-rectangle",
          "background-color": "rgba(15, 52, 96, 0.5)",
          "border-color": "#2980b9",
          "border-width": 2,
          "border-style": "solid",
          label: "data(label)",
          "text-valign": "top",
          "text-halign": "center",
          "font-size": "11px",
          color: "#5dade2",
          "text-margin-y": 8,
          "padding": "12px",
          "text-wrap": "wrap",
        },
      },
      // Composable nodes
      {
        selector: 'node[type="composable_node"]',
        style: {
          shape: "round-rectangle",
          "background-color": "data(nodeColor)",
          "background-opacity": 0.8,
          "border-color": "#5dade2",
          "border-width": 1,
          width: "label",
          height: "label",
          "padding": "6px",
          label: "data(label)",
          "text-valign": "center",
          "text-halign": "center",
          "font-size": "10px",
          color: "#1a1a2e",
          "text-wrap": "wrap",
        },
      },
      // Load composable node (translucent symlink box)
      {
        selector: 'node[type="load_composable_node"]',
        style: {
          shape: "round-rectangle",
          "background-color": "rgba(93, 173, 226, 0.15)",
          "border-color": "#5dade2",
          "border-width": 2,
          "border-style": "dashed",
          label: "data(label)",
          "text-valign": "top",
          "text-halign": "center",
          "font-size": "10px",
          color: "#5dade2",
          "text-margin-y": 8,
          "padding": "10px",
          "text-wrap": "wrap",
        },
      },
      // Executable
      {
        selector: 'node[type="executable"]',
        style: {
          shape: "round-rectangle",
          "background-color": "data(nodeColor)",
          "border-color": "#555",
          "border-width": 1,
          width: "label",
          height: "label",
          "padding": "8px",
          label: "data(label)",
          "text-valign": "center",
          "text-halign": "center",
          "font-size": "10px",
          color: "#1a1a2e",
          "text-wrap": "wrap",
        },
      },
      // Topic nodes
      {
        selector: 'node[type="topic"]',
        style: {
          shape: "diamond",
          "background-color": "#e67e22",
          "border-color": "#d35400",
          "border-width": 1,
          width: "label",
          height: "label",
          "padding": "6px",
          label: "data(label)",
          "text-valign": "center",
          "text-halign": "center",
          "font-size": "9px",
          color: "#fff",
          "text-wrap": "wrap",
        },
      },
      // Remap edges
      {
        selector: 'edge[type="remap"]',
        style: {
          width: 1,
          "line-color": "#555",
          "line-style": "solid",
          "curve-style": "bezier",
          "target-arrow-shape": "none",
          opacity: 0.5,
        },
      },
      // Load target edges
      {
        selector: 'edge[type="load_target"]',
        style: {
          width: 2,
          "line-color": "#5dade2",
          "line-style": "dashed",
          "curve-style": "bezier",
          "target-arrow-shape": "triangle",
          "target-arrow-color": "#5dade2",
          "arrow-scale": 1.2,
          opacity: 0.7,
        },
      },
      // Highlighted state
      {
        selector: "node.highlighted",
        style: {
          "border-color": "#e94560",
          "border-width": 3,
        },
      },
      {
        selector: "node.faded",
        style: {
          opacity: 0.2,
        },
      },
      {
        selector: "edge.faded",
        style: {
          opacity: 0.1,
        },
      },
    ],
    layout: { name: "preset" }, // placeholder; real layout runs below
    wheelSensitivity: 0.3,
    minZoom: 0.05,
    maxZoom: 3,
  });

  // --- Run ELK layout ---
  function runLayout() {
    document.getElementById("loading").classList.remove("hidden");
    const layoutOpts = {
      name: "elk",
      elk: {
        algorithm: "layered",
        "elk.direction": "DOWN",
        "elk.spacing.nodeNode": "20",
        "elk.layered.spacing.nodeNodeBetweenLayers": "40",
        "elk.layered.spacing.edgeNodeBetweenLayers": "20",
        "elk.padding": "[top=30,left=20,bottom=20,right=20]",
        "elk.hierarchyHandling": "INCLUDE_CHILDREN",
      },
      fit: true,
      padding: 40,
    };

    try {
      cy.layout(layoutOpts).run();
      // ELK layout is synchronous in bundled mode
      document.getElementById("loading").classList.add("hidden");
    } catch (e) {
      console.error("ELK layout failed, falling back to cose:", e);
      cy.layout({
        name: "cose",
        animate: false,
        fit: true,
        padding: 40,
        nodeRepulsion: 8000,
        idealEdgeLength: 80,
      }).run();
      document.getElementById("loading").classList.add("hidden");
    }
  }

  // Defer layout to let the DOM render
  requestAnimationFrame(runLayout);

  // --- Toolbar: Fit ---
  document.getElementById("fit-btn").addEventListener("click", function () {
    cy.fit(undefined, 40);
  });

  // --- Toolbar: Collapse/Expand groups ---
  let groupsCollapsed = false;

  document
    .getElementById("collapse-btn")
    .addEventListener("click", function () {
      cy.nodes('[type="group"]').forEach(function (g) {
        g.data("_expanded", true);
        g.children().hide();
      });
      groupsCollapsed = true;
    });

  document
    .getElementById("expand-btn")
    .addEventListener("click", function () {
      cy.nodes('[type="group"]').forEach(function (g) {
        g.data("_expanded", false);
        g.children().show();
      });
      groupsCollapsed = false;
    });

  // Double-click on group to toggle collapse
  cy.on("dblclick", 'node[type="group"]', function (evt) {
    const node = evt.target;
    const expanded = !node.data("_expanded");
    node.data("_expanded", expanded);
    if (expanded) {
      node.children().hide();
    } else {
      node.children().show();
    }
  });

  // --- Search ---
  const searchInput = document.getElementById("search");
  let searchTimeout = null;

  searchInput.addEventListener("input", function () {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(doSearch, 200);
  });

  function doSearch() {
    const query = searchInput.value.trim().toLowerCase();
    if (!query) {
      cy.elements().removeClass("highlighted faded");
      return;
    }

    cy.elements().addClass("faded");

    const matched = cy.nodes().filter(function (n) {
      const d = n.data();
      return (
        (d.label && d.label.toLowerCase().includes(query)) ||
        (d.fqn && d.fqn.toLowerCase().includes(query)) ||
        (d.package && d.package.toLowerCase().includes(query)) ||
        (d.fullName && d.fullName.toLowerCase().includes(query)) ||
        (d.name && d.name.toLowerCase().includes(query))
      );
    });

    matched.removeClass("faded").addClass("highlighted");
    // Also show ancestors (parent groups)
    matched.ancestors().removeClass("faded");
    // Show connected edges and their endpoints
    matched.connectedEdges().removeClass("faded");
    matched.connectedEdges().connectedNodes().removeClass("faded");
  }

  // --- Detail panel ---
  const detailPanel = document.getElementById("detail-panel");
  const detailTitle = document.getElementById("detail-title");
  const detailBody = document.getElementById("detail-body");

  document
    .getElementById("detail-close")
    .addEventListener("click", function () {
      detailPanel.classList.add("hidden");
    });

  cy.on("tap", "node", function (evt) {
    const d = evt.target.data();
    if (d.type === "group") return; // groups don't have detail

    detailPanel.classList.remove("hidden");
    detailTitle.textContent = d.fqn || d.label || d.id;

    let html = "";

    // Basic info
    html += "<h3>Info</h3>";
    if (d.type) html += field("Type", d.type);
    if (d.package) html += field("Package", d.package);
    if (d.executable) html += field("Executable", d.executable);
    if (d.plugin) html += field("Plugin", d.plugin);
    if (d.name) html += field("Name", d.name);
    if (d.namespace) html += field("Namespace", d.namespace);
    if (d.fqn) html += field("FQN", d.fqn);
    if (d.cmd) html += field("Command", d.cmd);
    if (d.target) html += field("Target", d.target);
    if (d.fullName) html += field("Topic", d.fullName);

    // Parameters
    if (d.params && d.params.length > 0) {
      html += "<h3>Parameters (" + d.params.length + ")</h3>";
      html += "<table><tr><th>Name</th><th>Value</th></tr>";
      for (const p of d.params) {
        html +=
          "<tr><td>" + esc(p.name) + "</td><td>" + esc(p.value) + "</td></tr>";
      }
      html += "</table>";
    }

    // Remaps
    if (d.remaps && d.remaps.length > 0) {
      html += "<h3>Remaps (" + d.remaps.length + ")</h3>";
      html += "<table><tr><th>From</th><th>To</th></tr>";
      for (const r of d.remaps) {
        html +=
          "<tr><td>" + esc(r.from) + "</td><td>" + esc(r.to) + "</td></tr>";
      }
      html += "</table>";
    }

    detailBody.innerHTML = html;
  });

  // Click on background to close detail
  cy.on("tap", function (evt) {
    if (evt.target === cy) {
      detailPanel.classList.add("hidden");
    }
  });

  function field(label, value) {
    return (
      '<div class="field"><span class="field-label">' +
      esc(label) +
      ":</span> " +
      esc(String(value)) +
      "</div>"
    );
  }

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  // --- Node resize via Shift+drag on edge/corner ---
  // When Shift is held and user drags near the border of a selected node,
  // resize the node instead of moving it.
  (function initResize() {
    let resizing = null; // { node, startW, startH, startX, startY }

    cy.on("mousedown", "node", function (evt) {
      if (!evt.originalEvent.shiftKey) return;
      const node = evt.target;
      // Only allow resize on leaf nodes and compound nodes
      const pos = evt.position;
      const bb = node.boundingBox();
      const edgeThreshold = 12;
      const nearRight = Math.abs(pos.x - bb.x2) < edgeThreshold;
      const nearBottom = Math.abs(pos.y - bb.y2) < edgeThreshold;
      if (!nearRight && !nearBottom) return;

      resizing = {
        node: node,
        startW: node.width(),
        startH: node.height(),
        startX: pos.x,
        startY: pos.y,
      };
      node.ungrabify(); // prevent move while resizing
      evt.originalEvent.preventDefault();
    });

    cy.on("mousemove", function (evt) {
      if (!resizing) return;
      const pos = evt.position;
      const dx = pos.x - resizing.startX;
      const dy = pos.y - resizing.startY;
      const newW = Math.max(30, resizing.startW + dx);
      const newH = Math.max(20, resizing.startH + dy);
      resizing.node.style({ width: newW, height: newH });
    });

    function endResize() {
      if (resizing) {
        resizing.node.grabify();
        resizing = null;
      }
    }

    cy.on("mouseup", endResize);
    document.addEventListener("mouseup", endResize);
  })();
})();
