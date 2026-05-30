"use strict";

// --------------------------------------------------------------------------- //
// Small helpers
// --------------------------------------------------------------------------- //

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v !== null && v !== undefined) {
      node.setAttribute(k, v);
    }
  }
  for (const c of children) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const secs = Math.floor((Date.now() - d.getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

const STATUS_BADGE = {
  live: "badge-live",
  rolling: "badge-rolling",
  rolledback: "badge-rolledback",
  "rolled back": "badge-rolledback",
  pending: "badge-pending",
  approved: "badge-approved",
  "dry-run": "badge-dryrun",
};
function statusClass(status) {
  return STATUS_BADGE[(status || "").toLowerCase()] || "badge-unknown";
}
// A dry-run deploy is "Live" as a Temper entity but was NOT applied to the
// cluster — show "DRY-RUN" as its status so it's unambiguous, not "LIVE".
function displayStatus(d) {
  return d && d.dry_run ? "DRY-RUN" : d && d.status ? d.status : "Unknown";
}

const LINEAGE = {
  throughput: { cls: "chip-throughput", icon: "🚀", label: "throughput" },
  latency: { cls: "chip-latency", icon: "⚡", label: "latency" },
  baseline: { cls: "chip-baseline", icon: "○", label: "baseline" },
};
function lineageChip(lineage) {
  const l = LINEAGE[lineage] || LINEAGE.baseline;
  return el("span", { class: `chip ${l.cls}` }, `${l.icon} ${l.label}`);
}

// --------------------------------------------------------------------------- //
// API
// --------------------------------------------------------------------------- //

async function api(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`${path} → ${resp.status}`);
  return resp.json();
}

// --------------------------------------------------------------------------- //
// History list
// --------------------------------------------------------------------------- //

let SELECTED = null;

function renderHistory(deployments) {
  const list = $("#history-list");
  list.innerHTML = "";
  $("#deploy-count").textContent = deployments.length;

  if (!deployments.length) {
    list.appendChild(el("div", { class: "loading" }, "No deployments found."));
    return;
  }

  for (const d of deployments) {
    const card = el(
      "div",
      { class: "deploy-card", "data-id": d.id, onClick: () => selectDeploy(d.id) },
      el("div", { class: "card-row card-top" },
        el("div", {},
          el("div", { class: "card-tag", title: d.image_tag || "" }, d.image_tag || "(no tag)"),
          el("div", { class: "card-id" }, d.id)
        ),
        el("span", { class: `badge ${statusClass(displayStatus(d))}` }, displayStatus(d))
      ),
      el("div", { class: "card-meta" },
        lineageChip(d.lineage),
        el("span", { class: "card-when", title: d.deployed_at || "" },
          d.deployed_at ? relTime(d.deployed_at) : "not applied")
      ),
      d.result_summary
        ? el("div", { class: "card-result" }, d.result_summary)
        : null
    );
    if (d.id === SELECTED) card.classList.add("active");
    list.appendChild(card);
  }
}

// --------------------------------------------------------------------------- //
// Detail view
// --------------------------------------------------------------------------- //

// Governed flow we render the stepper against (canonical order).
const FLOW = ["Created", "Request", "Approve", "Apply", "RecordResult", "MarkLive"];
const GATE_ACTIONS = new Set(["Approve"]);

function renderTimeline(timeline) {
  if (!timeline || !timeline.length) {
    return el("div", { class: "timeline-empty" },
      "No governed events recorded for this deploy (timeline empty).");
  }
  const wrap = el("div", { class: "timeline" });
  timeline.forEach((ev) => {
    const isGate = GATE_ACTIONS.has(ev.action);
    const dotCls = isGate ? "tl-dot gate" : "tl-dot done";
    const transition =
      ev.from_status && ev.to_status
        ? `${ev.from_status || "∅"} → ${ev.to_status}`
        : ev.to_status || "";
    const paramKeys = ev.params ? Object.keys(ev.params).filter((k) => k !== "id") : [];
    const paramStr = paramKeys.length
      ? paramKeys.map((k) => `${k}=${JSON.stringify(ev.params[k])}`).join("  ")
      : "";

    wrap.appendChild(
      el("div", { class: "tl-step" },
        el("div", { class: dotCls }),
        el("div", { class: "tl-action" },
          ev.action || "—",
          isGate ? el("span", { class: "tl-gate-tag" }, "human gate") : null
        ),
        transition ? el("div", { class: "tl-transition" }, transition) : null,
        el("div", { class: "tl-time" }, fmtTime(ev.timestamp)),
        paramStr ? el("div", { class: "tl-params" }, paramStr) : null
      )
    );
  });
  return wrap;
}

function changelogPanel(cl) {
  const body = el("div", { class: "panel-body" });
  if (!cl || cl.missing) {
    body.appendChild(el("div", { class: "cl-muted" },
      `No linked improvement issue${cl && cl.issue_id ? ` (${cl.issue_id})` : ""}.`));
    return panel("Changelog", body);
  }

  const f = (label, value, opts = {}) => {
    if (!value) return null;
    return el("div", { class: "cl-field" },
      el("div", { class: "cl-label" }, label),
      el("div", { class: opts.cls || "cl-value" }, value)
    );
  };

  if (cl.title) body.appendChild(el("div", { class: "cl-field" },
    el("div", { class: "cl-issue-title" }, cl.title)));
  body.appendChild(f("Hypothesis", cl.hypothesis));
  body.appendChild(f("Plan", cl.plan, { cls: "cl-plan" }));
  if (cl.target_file) {
    body.appendChild(el("div", { class: "cl-field" },
      el("div", { class: "cl-label" }, "Target file"),
      el("span", { class: "cl-target" }, cl.target_file)));
  }
  body.appendChild(f("Acceptance criteria", cl.acceptance_criteria));

  const meta = [];
  if (cl.issue_id) meta.push(`issue: ${cl.issue_id}`);
  if (cl.issue_status) meta.push(`status: ${cl.issue_status}`);
  if (cl.ci_status) meta.push(`ci: ${cl.ci_status}`);
  if (cl.branch_name) meta.push(`branch: ${cl.branch_name}`);
  if (meta.length) {
    body.appendChild(el("div", { class: "cl-field cl-muted" }, meta.join("   ·   ")));
  }
  return panel("Changelog", body);
}

function panel(title, bodyNode, headExtra) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, title, headExtra || null),
    bodyNode
  );
}

async function selectDeploy(id) {
  SELECTED = id;
  document.querySelectorAll(".deploy-card").forEach((c) =>
    c.classList.toggle("active", c.dataset.id === id));

  $("#empty-state").classList.add("hidden");
  const content = $("#detail-content");
  content.classList.remove("hidden");
  content.innerHTML = "";
  content.appendChild(el("div", { class: "loading" }, "Loading detail…"));

  let detail;
  try {
    detail = await api(`/api/deployments/${encodeURIComponent(id)}`);
  } catch (err) {
    content.innerHTML = "";
    content.appendChild(el("div", { class: "error-box" }, `Failed to load: ${err.message}`));
    return;
  }

  content.innerHTML = "";

  // Header
  content.appendChild(
    el("div", { class: "detail-header" },
      el("div", { class: "detail-title" },
        el("span", { class: "detail-tag" }, detail.image_tag || "(no tag)"),
        el("span", { class: `badge ${statusClass(displayStatus(detail))}` }, displayStatus(detail)),
        lineageChip(detail.lineage),
        detail.dry_run ? el("span", { class: "chip chip-dryrun", title: "rollout simulated; not applied to the live cluster" }, "simulated") : null
      ),
      el("div", { class: "detail-sub" },
        `${detail.id}` +
        (detail.namespace ? `  ·  ns: ${detail.namespace}` : "") +
        (detail.deployed_at ? `  ·  deployed ${fmtTime(detail.deployed_at)}` : "")
      )
    )
  );

  // Changelog
  content.appendChild(changelogPanel(detail.changelog));

  // Governed timeline
  content.appendChild(panel("Governed Timeline", el("div", { class: "panel-body" },
    renderTimeline(detail.timeline))));

  // Diff panel (placeholder, then async fill)
  const diffMeta = el("div", { class: "diff-meta" }, "Resolving diff…");
  const diffTarget = el("div", { id: "diff-target" });
  const toolbar = el("div", { class: "diff-toolbar" },
    el("button", { class: "diff-btn active", id: "view-side", onClick: () => setDiffView("side-by-side") }, "Side-by-side"),
    el("button", { class: "diff-btn", id: "view-line", onClick: () => setDiffView("line-by-line") }, "Unified")
  );
  content.appendChild(panel("Code Diff", el("div", {}, diffMeta, diffTarget), toolbar));

  loadDiff(id, diffMeta, diffTarget);
}

// --------------------------------------------------------------------------- //
// Diff rendering (diff2html)
// --------------------------------------------------------------------------- //

let CURRENT_DIFF = null; // {text, target}

function setDiffView(mode) {
  if (!CURRENT_DIFF || !CURRENT_DIFF.text) return;
  $("#view-side").classList.toggle("active", mode === "side-by-side");
  $("#view-line").classList.toggle("active", mode === "line-by-line");
  drawDiff(CURRENT_DIFF.target, CURRENT_DIFF.text, mode);
}

function drawDiff(target, diffText, mode) {
  const ui = new Diff2HtmlUI(target, diffText, {
    drawFileList: true,
    matching: "lines",
    outputFormat: mode,
    highlight: true,
    colorScheme: "dark",
  });
  ui.draw();
  ui.highlightCode();
}

async function loadDiff(id, metaNode, targetNode) {
  let diff;
  try {
    diff = await api(`/api/deployments/${encodeURIComponent(id)}/diff`);
  } catch (err) {
    metaNode.innerHTML = `<span class="cl-muted">diff unavailable: ${esc(err.message)}</span>`;
    return;
  }

  if (!diff.available || !diff.unified_diff) {
    metaNode.innerHTML = "";
    targetNode.innerHTML = "";
    targetNode.appendChild(el("div", { class: "diff-none" },
      diff.reason ? `No diff: ${diff.reason}.` : "No code diff available for this deployment."));
    return;
  }

  const statSummary = (diff.stat || [])
    .map((s) => `${s.file} (+${s.additions} −${s.deletions})`)
    .join("   ");
  metaNode.innerHTML =
    `<b>base</b> ${esc(diff.base)}  →  <b>branch</b> ${esc(diff.branch)}` +
    (statSummary ? `<br>${esc(statSummary)}` : "");

  CURRENT_DIFF = { text: diff.unified_diff, target: targetNode };
  drawDiff(targetNode, diff.unified_diff, "side-by-side");
}

// --------------------------------------------------------------------------- //
// Control plane — POST helper
// --------------------------------------------------------------------------- //

async function post(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  let data = {};
  try { data = await resp.json(); } catch { /* empty */ }
  return { ok: resp.ok, status: resp.status, data };
}

// --------------------------------------------------------------------------- //
// Control plane — cluster status card
// --------------------------------------------------------------------------- //

let SCALE_TARGET = 3;

function podDot(p) {
  const cls = p.ready ? "ready" : (p.phase === "Pending" ? "pending" : "notready");
  return el("span", { class: `pod-dot ${cls}`, title: `${p.name}: ${p.phase}` }, p.name);
}

function renderCluster(c) {
  const pill = $("#cluster-pill");
  const body = $("#cluster-body");
  body.innerHTML = "";

  if (!c || !c.reachable) {
    pill.className = "pill pill-bad";
    pill.textContent = "unreachable";
    body.appendChild(el("div", { class: "error-box" },
      `Cluster unreachable: ${esc((c && c.error) || "kubectl failed")}`));
    return;
  }

  const ready = c.ready_replicas || 0;
  const want = c.configured_replicas ?? ready;
  pill.className = ready === want && ready > 0 ? "pill pill-ok" : "pill pill-muted";
  pill.textContent = `${ready}/${want} ready`;

  body.appendChild(
    el("div", { class: "cluster-line" },
      el("div", { class: "cluster-metric" },
        el("div", { class: "m-label" }, "Image tag"),
        el("div", { class: "m-value tag" }, c.image_tag || "—")),
      lineageChip(c.lineage),
      el("div", { class: "cluster-metric" },
        el("div", { class: "m-label" }, "Replicas"),
        el("div", { class: "m-value" }, `${ready} / ${want}`)),
    )
  );

  const dots = el("div", { class: "pod-dots" });
  (c.pods || []).forEach((p) => dots.appendChild(podDot(p)));
  if (!c.pods || !c.pods.length) dots.appendChild(el("span", { class: "cl-muted" }, "no pods"));
  body.appendChild(dots);

  body.appendChild(el("div", { class: "cluster-repo" }, c.image || ""));

  // Keep the scale stepper anchored to reality on (re)load.
  SCALE_TARGET = want || ready || 3;
  const n = $("#scale-n");
  if (n) n.textContent = SCALE_TARGET;
}

async function refreshCluster() {
  try {
    const c = await api("/api/cluster");
    renderCluster(c);
    return c;
  } catch (err) {
    renderCluster({ reachable: false, error: err.message });
    return null;
  }
}

// --------------------------------------------------------------------------- //
// Control plane — inline governed-flow rendering
// --------------------------------------------------------------------------- //

function flowGate(slotId, kind, id, onApprove) {
  const slot = $("#" + slotId);
  slot.innerHTML = "";
  slot.appendChild(
    el("div", { class: "cp-gate" },
      el("div", { class: "cp-gate-title" }, "Pending your approval"),
      el("div", { class: "cp-gate-sub" },
        "This is the Cedar human gate. Clicking Approve = you acting as the verified supervisor."),
      el("div", { class: "cp-gate-id" }, `${kind} entity: ${id}  ·  state: Requested/Pending`),
      el("div", { class: "cp-gate-actions" },
        el("button", { class: "cp-btn cp-approve", onClick: onApprove }, "Approve"),
        el("button", { class: "cp-btn cp-deny", onClick: () => { slot.innerHTML = ""; } }, "Deny")
      )
    )
  );
}

function flowStage(slotId, text) {
  const slot = $("#" + slotId);
  slot.innerHTML = "";
  slot.appendChild(
    el("div", { class: "cp-stage" },
      el("div", { class: "spinner" }),
      el("div", { class: "cp-stage-text" }, text)
    )
  );
}

function flowResult(slotId, res) {
  const slot = $("#" + slotId);
  slot.innerHTML = "";
  const ok = res.ok;
  const node = el("div", { class: `cp-result ${ok ? "ok" : "fail"}` },
    el("div", { class: "cp-result-head" },
      ok ? "✓" : "✕",
      `${res.kind || "action"} → ${res.status || (ok ? "Done" : "Failed")}`),
    res.rollout_cmd || res.scale_cmd
      ? el("div", { class: "cp-result-cmd" }, res.rollout_cmd || res.scale_cmd)
      : null,
    res.output ? el("div", { class: "cp-result-out" }, res.output) : null
  );
  slot.appendChild(node);
}

function flowError(slotId, res) {
  const slot = $("#" + slotId);
  slot.innerHTML = "";
  const d = res.data || {};
  const lines = [];
  if (d.step) lines.push(`step: ${d.step}`);
  if (d.decision_id) lines.push(`decision: ${d.decision_id} (approve in the Observe UI)`);
  lines.push(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail || d));
  slot.appendChild(el("div", { class: "error-box" },
    `Action blocked (HTTP ${res.status}). ${lines.join(" · ")}`));
}

// --------------------------------------------------------------------------- //
// Control plane — deploy / scale / rollback wiring
// --------------------------------------------------------------------------- //

async function startDeploy() {
  const tag = $("#image-select").value;
  if (!tag) return;
  $("#deploy-btn").disabled = true;
  flowStage("deploy-flow", `Creating governed Deploy of ${tag}…`);
  const res = await post("/api/actions/deploy", { image_tag: tag });
  $("#deploy-btn").disabled = false;
  if (!res.ok) return flowError("deploy-flow", res);

  flowGate("deploy-flow", "Deploy", res.data.id, async () => {
    flowStage("deploy-flow", `Approved. Rolling ${tag} to the cluster (kubectl set image)…`);
    const ap = await post(`/api/actions/deploy/${encodeURIComponent(res.data.id)}/approve`, {});
    if (!ap.ok) return flowError("deploy-flow", ap);
    flowResult("deploy-flow", ap.data);
    if (ap.data.cluster) renderCluster(ap.data.cluster); else refreshCluster();
  });
}

async function rollbackLive() {
  // Roll back whatever is live: drive a governed rollback on the newest deploy.
  $("#rollback-btn").disabled = true;
  flowStage("deploy-flow", "Resolving live deploy to roll back…");
  let deploys;
  try {
    deploys = await api("/api/deployments");
  } catch (err) {
    $("#rollback-btn").disabled = false;
    return flowError("deploy-flow", { status: 502, data: { detail: err.message } });
  }
  const live = (deploys.deployments || []).find((d) => d.status === "Live") ||
    (deploys.deployments || [])[0];
  if (!live) {
    $("#rollback-btn").disabled = false;
    flowError("deploy-flow", { status: 404, data: { detail: "no deploy to roll back" } });
    return;
  }
  flowStage("deploy-flow", `Governed rollback (kubectl rollout undo) via ${live.id}…`);
  const res = await post(`/api/actions/deploy/${encodeURIComponent(live.id)}/rollback`, {});
  $("#rollback-btn").disabled = false;
  if (!res.ok) return flowError("deploy-flow", res);
  flowResult("deploy-flow", res.data);
  if (res.data.cluster) renderCluster(res.data.cluster); else refreshCluster();
}

async function startScale() {
  $("#scale-btn").disabled = true;
  flowStage("scale-flow", `Creating governed ScaleOp to ${SCALE_TARGET} replicas…`);
  const res = await post("/api/actions/scale", { target_replicas: SCALE_TARGET });
  $("#scale-btn").disabled = false;
  if (!res.ok) return flowError("scale-flow", res);

  flowGate("scale-flow", "ScaleOp", res.data.id, async () => {
    flowStage("scale-flow", `Approved. Scaling to ${SCALE_TARGET} (kubectl scale)…`);
    const ap = await post(`/api/actions/scale/${encodeURIComponent(res.data.id)}/approve`, {});
    if (!ap.ok) return flowError("scale-flow", ap);
    flowResult("scale-flow", ap.data);
    if (ap.data.cluster) renderCluster(ap.data.cluster); else refreshCluster();
  });
}

let IMAGES = {}; // tag -> {description, lineage, has_code, branch}

async function loadImages() {
  const sel = $("#image-select");
  try {
    const data = await api("/api/images");
    sel.innerHTML = "";
    IMAGES = {};
    (data.images || []).forEach((img) => {
      IMAGES[img.tag] = img;
      sel.appendChild(el("option", { value: img.tag }, img.tag));
    });
    updateImageDesc();
  } catch {
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "" }, "(no images)"));
  }
}

// Show the selected image's description + a Browse-code link; hide the code
// browser when the selection changes.
function updateImageDesc() {
  const tag = $("#image-select").value;
  const img = IMAGES[tag] || {};
  $("#image-desc-text").textContent = img.description || "";
  const link = $("#browse-code");
  link.style.display = img.has_code ? "" : "none";
  $("#code-browser").classList.add("hidden");
  $("#code-browser").innerHTML = "";
}

// Open the code browser for the selected tag: diff by default, with a
// "view full file" toggle per changed file.
async function browseCode(ev) {
  if (ev) ev.preventDefault();
  const tag = $("#image-select").value;
  const box = $("#code-browser");
  box.classList.remove("hidden");
  box.innerHTML = '<div class="cp-muted">Loading diff…</div>';
  let d;
  try {
    d = await api(`/api/images/${encodeURIComponent(tag)}/diff`);
  } catch (e) {
    box.innerHTML = `<div class="cp-muted">Could not load diff: ${esc(e.message)}</div>`;
    return;
  }
  if (d.no_diff) {
    box.innerHTML = `<div class="cp-muted">${esc(d.reason || "No code diff available.")}</div>`;
    return;
  }
  box.innerHTML = "";
  box.appendChild(el("div", { class: "cp-code-head" },
    el("span", {}, `${tag} · base `),
    el("code", {}, (d.base || "").slice(0, 8)),
    el("span", {}, " → branch "),
    el("code", {}, d.branch || "")));

  // The whole unified diff (small, single-file in practice) rendered once via
  // the same Diff2HtmlUI the history view uses, with a per-tag "view full file"
  // toggle.
  const region = el("div", { class: "cp-diff-region" });
  const f0 = (d.stat || [])[0] || {};
  const toggle = el("button", { class: "cp-filetoggle" }, "view full file");
  let showingFull = false;

  const renderDiff = () => {
    region.innerHTML = "";
    try {
      const ui = new Diff2HtmlUI(region, d.unified_diff, {
        drawFileList: false, matching: "lines",
        outputFormat: "side-by-side", highlight: true, colorScheme: "dark",
      });
      ui.draw();
    } catch {
      const pre = el("pre", { class: "cp-fullfile" });
      pre.textContent = d.unified_diff || "";
      region.appendChild(pre);
    }
  };
  const renderFull = async () => {
    region.innerHTML = '<div class="cp-muted">Loading file…</div>';
    try {
      const fr = await api(`/api/images/${encodeURIComponent(tag)}/file?path=${encodeURIComponent(f0.file)}`);
      region.innerHTML = "";
      const pre = el("pre", { class: "cp-fullfile" });
      pre.textContent = fr.content || "";
      region.appendChild(pre);
    } catch (e) {
      region.innerHTML = `<div class="cp-muted">Could not load file: ${esc(e.message)}</div>`;
    }
  };
  toggle.addEventListener("click", () => {
    showingFull = !showingFull;
    toggle.textContent = showingFull ? "view diff" : "view full file";
    showingFull ? renderFull() : renderDiff();
  });

  if (f0.file) {
    box.appendChild(el("div", { class: "cp-file-head" },
      el("span", { class: "cp-file-name" }, `${f0.file} (+${f0.additions} −${f0.deletions})`),
      toggle));
  }
  box.appendChild(region);
  renderDiff();
}

function wireControlPlane() {
  $("#deploy-btn").addEventListener("click", startDeploy);
  $("#image-select").addEventListener("change", updateImageDesc);
  $("#browse-code").addEventListener("click", browseCode);
  $("#rollback-btn").addEventListener("click", rollbackLive);
  $("#scale-btn").addEventListener("click", startScale);
  $("#scale-inc").addEventListener("click", () => {
    SCALE_TARGET = Math.min(10, SCALE_TARGET + 1);
    $("#scale-n").textContent = SCALE_TARGET;
  });
  $("#scale-dec").addEventListener("click", () => {
    SCALE_TARGET = Math.max(0, SCALE_TARGET - 1);
    $("#scale-n").textContent = SCALE_TARGET;
  });
}

// --------------------------------------------------------------------------- //
// Workloads tab
// --------------------------------------------------------------------------- //

const WL_BADGE = {
  active: "badge-live", running: "badge-live",
  defined: "badge-pending", stopped: "badge-rolledback", deleted: "badge-rolledback",
};
function wlStatusClass(s) { return WL_BADGE[(s || "").toLowerCase()] || "badge-unknown"; }

function wlPodDots(pods) {
  const dots = el("div", { class: "pod-dots wl-pods" });
  (pods || []).forEach((p) => {
    const cls = p.ready ? "ready" : (p.phase === "Pending" ? "pending" : "notready");
    dots.appendChild(el("span", {
      class: `pod-dot ${cls}`,
      title: `${p.name}: ${p.phase}${p.reason ? " (" + p.reason + ")" : ""}`,
    }, p.name.split("-").slice(-1)[0]));
  });
  if (!pods || !pods.length) dots.appendChild(el("span", { class: "cl-muted" }, "no pods"));
  return dots;
}

function wlRow(item, kind, meta) {
  const running = (item.status || "").toLowerCase() === "running";
  const row = el("div", { class: "wl-item" },
    el("div", { class: "wl-item-top" },
      el("div", {},
        el("span", { class: "wl-item-id" }, item.id),
        el("span", { class: "wl-item-meta" }, meta)
      ),
      el("span", { class: `badge ${wlStatusClass(item.status)}` }, item.status || "—")
    )
  );
  if (kind !== "queue") {
    row.appendChild(wlPodDots(item.pods));
    if (running) {
      row.appendChild(el("div", { class: "wl-item-actions" },
        el("button", {
          class: "cp-btn cp-btn-ghost wl-stop",
          onClick: () => stopWorkload(kind, item.id),
        }, "Stop")));
    }
  }
  if (item.result) {
    row.appendChild(el("div", { class: "wl-item-result", title: item.result }, item.result));
  }
  return row;
}

let WL_BUSY = false;

async function loadWorkloads() {
  let d;
  try {
    d = await api("/api/workloads");
  } catch (err) {
    ["#wl-queues", "#wl-producers", "#wl-consumers"].forEach((s) => {
      $(s).innerHTML = "";
      $(s).appendChild(el("div", { class: "error-box" }, `Failed: ${err.message}`));
    });
    return;
  }
  $("#wl-brokers").textContent = d.brokers || "brokers";

  // Queues
  const ql = $("#wl-queues"); ql.innerHTML = "";
  if (!d.queues.length) ql.appendChild(el("div", { class: "cl-muted" }, "No queues yet."));
  d.queues.forEach((q) => ql.appendChild(
    wlRow(q, "queue", `topic ${q.name}  ·  ${q.partitions} partitions`)));

  // Producer/consumer queue dropdowns (only Active queues are targetable).
  const activeQueues = d.queues.filter((q) => (q.status || "").toLowerCase() === "active");
  [["#p-queue", "p-queue"], ["#c-queue", "c-queue"]].forEach(([sel]) => {
    const cur = $(sel).value;
    $(sel).innerHTML = "";
    if (!activeQueues.length) {
      $(sel).appendChild(el("option", { value: "" }, "(create a queue first)"));
    } else {
      activeQueues.forEach((q) => $(sel).appendChild(el("option", { value: q.name }, q.name)));
      if (activeQueues.some((q) => q.name === cur)) $(sel).value = cur;
    }
  });

  // Producers
  const pl = $("#wl-producers"); pl.innerHTML = "";
  if (!d.producers.length) pl.appendChild(el("div", { class: "cl-muted" }, "No producers yet."));
  d.producers.forEach((p) => pl.appendChild(
    wlRow(p, "producer",
      `→ ${p.queue}  ·  ${p.rate_per_sec}/s  ·  ${p.msg_size}B  ·  conc ${p.concurrency}`)));

  // Consumers
  const cl = $("#wl-consumers"); cl.innerHTML = "";
  if (!d.consumers.length) cl.appendChild(el("div", { class: "cl-muted" }, "No consumers yet."));
  d.consumers.forEach((c) => cl.appendChild(
    wlRow(c, "consumer",
      `← ${c.queue}  ·  ${c.process_ms}ms/msg  ·  conc ${c.concurrency}`)));
}

async function createQueue() {
  const name = $("#q-name").value.trim();
  const partitions = parseInt($("#q-parts").value, 10) || 1;
  if (!name) return;
  WL_BUSY = true; $("#q-create").disabled = true;
  flowStage("q-flow", `Creating governed Queue ${name}…`);
  const res = await post("/api/workloads/queue", { name, partitions });
  $("#q-create").disabled = false; WL_BUSY = false;
  if (!res.ok) return flowError("q-flow", res);
  flowResult("q-flow", {
    ok: true, kind: "Queue", status: res.data.status,
    output: `${res.data.id} → Active (topic ${name}, ${partitions} partitions)`
  });
  $("#q-name").value = "";
  loadWorkloads();
}

async function createProducer() {
  const queue = $("#p-queue").value;
  if (!queue) return flowError("p-flow", { status: 400, data: { detail: "create + select a queue first" } });
  const body = {
    queue,
    rate_per_sec: parseInt($("#p-rate").value, 10) || 500,
    msg_size: parseInt($("#p-size").value, 10) || 512,
    concurrency: parseInt($("#p-conc").value, 10) || 1,
  };
  WL_BUSY = true; $("#p-create").disabled = true;
  flowStage("p-flow", `Creating governed Producer → deploying pod(s) (pip-install + connect)…`);
  const res = await post("/api/workloads/producer", body);
  $("#p-create").disabled = false; WL_BUSY = false;
  if (!res.ok) return flowError("p-flow", res);
  flowResult("p-flow", {
    ok: res.data.ok, kind: "Producer", status: res.data.status,
    output: `${res.data.id} → ${res.data.deployment}  ${res.data.note || ""}\n${res.data.result || ""}`
  });
  loadWorkloads();
}

async function createConsumer() {
  const queue = $("#c-queue").value;
  if (!queue) return flowError("c-flow", { status: 400, data: { detail: "create + select a queue first" } });
  const body = {
    queue,
    process_ms: parseInt($("#c-proc").value, 10) || 0,
    concurrency: parseInt($("#c-conc").value, 10) || 1,
  };
  WL_BUSY = true; $("#c-create").disabled = true;
  flowStage("c-flow", `Creating governed Consumer → deploying pod(s)…`);
  const res = await post("/api/workloads/consumer", body);
  $("#c-create").disabled = false; WL_BUSY = false;
  if (!res.ok) return flowError("c-flow", res);
  flowResult("c-flow", {
    ok: res.data.ok, kind: "Consumer", status: res.data.status,
    output: `${res.data.id} → ${res.data.deployment}  ${res.data.note || ""}\n${res.data.result || ""}`
  });
  loadWorkloads();
}

async function stopWorkload(kind, id) {
  const slot = kind === "producer" ? "p-flow" : "c-flow";
  flowStage(slot, `Stopping ${kind} ${id} (governed Stop → kubectl delete)…`);
  const res = await post(`/api/workloads/${kind}/${encodeURIComponent(id)}/stop`, {});
  if (!res.ok) return flowError(slot, res);
  flowResult(slot, {
    ok: res.data.ok, kind, status: res.data.status,
    output: `${res.data.deployment} deleted`
  });
  loadWorkloads();
}

function wireWorkloads() {
  $("#q-create").addEventListener("click", createQueue);
  $("#p-create").addEventListener("click", createProducer);
  $("#c-create").addEventListener("click", createConsumer);
  $("#wl-refresh").addEventListener("click", loadWorkloads);
}

// Poll live status while the Workloads tab is open (pods change state).
let WL_TIMER = null;
function startWorkloadsPolling() {
  stopWorkloadsPolling();
  WL_TIMER = setInterval(() => { if (!WL_BUSY) loadWorkloads(); }, 5000);
}
function stopWorkloadsPolling() {
  if (WL_TIMER) { clearInterval(WL_TIMER); WL_TIMER = null; }
}

// --------------------------------------------------------------------------- //
// Tabs / view switching
// --------------------------------------------------------------------------- //

let HISTORY_LOADED = false;
let WORKLOADS_WIRED = false;

// --------------------------------------------------------------------------- //
// Hash router: every view + detail page is a hash route so the browser
// Back/Forward buttons (and deep links) work. navigate() updates the hash;
// the hashchange listener (renderRoute) does the actual rendering. This keeps a
// single render path and real history entries.
// --------------------------------------------------------------------------- //

// Navigate to a route, pushing a browser history entry (default) or replacing
// it. Routes: "clusters" | "symphony" | "control" | "workloads" | "observe"
//          | "cluster/<id>" | "issue/<id>"
function navigate(route, { replace = false } = {}) {
  const target = "#" + route;
  if (location.hash === target) {
    // Same route (e.g. re-clicking the active tab) — just re-render.
    renderRoute();
    return;
  }
  if (replace) history.replaceState(null, "", target);
  else location.hash = target; // triggers hashchange -> renderRoute
  if (replace) renderRoute();
}

function renderRoute() {
  const raw = (location.hash || "#clusters").slice(1);
  const [head, ...rest] = raw.split("/");
  const id = rest.join("/");
  if (head === "cluster" && id) { closeModal(); showClusterDetail(decodeURIComponent(id)); return; }
  if (head === "issue" && id) { switchView("symphony"); openSymphonyDetail(decodeURIComponent(id)); return; }
  // Any other route is a top-level view — make sure no modal is left open
  // (e.g. pressing Back out of an issue modal).
  closeModal();
  const known = ["clusters", "breeds", "runs", "symphony", "control", "workloads", "observe", "evolution"];
  switchView(known.includes(head) ? head : "clusters");
}

function switchView(view) {
  // Leaving the cluster-detail sub-view returns to a real tab.
  $("#view-cluster-detail").classList.add("hidden");
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === view));
  $("#view-clusters").classList.toggle("hidden", view !== "clusters");
  $("#view-breeds").classList.toggle("hidden", view !== "breeds");
  $("#view-runs").classList.toggle("hidden", view !== "runs");
  $("#view-symphony").classList.toggle("hidden", view !== "symphony");
  $("#view-control").classList.toggle("hidden", view !== "control");
  $("#view-workloads").classList.toggle("hidden", view !== "workloads");
  $("#view-observe").classList.toggle("hidden", view !== "observe");
  $("#view-evolution").classList.toggle("hidden", view !== "evolution");

  if (view !== "workloads") stopWorkloadsPolling();
  if (view === "evolution") startEvolutionPolling();
  else stopEvolutionPolling();

  if (view === "clusters") {
    if (!CLUSTERS_WIRED) { wireClusters(); CLUSTERS_WIRED = true; }
    loadClustersView();
  }
  if (view === "breeds") {
    if (!BREEDS_WIRED) { wireBreeds(); BREEDS_WIRED = true; }
    loadBreeds();
  }
  if (view === "runs") {
    if (!RUNS_WIRED) { wireRuns(); RUNS_WIRED = true; }
    loadRuns();
  }
  if (view === "symphony") {
    if (!SYMPHONY_WIRED) { wireSymphony(); SYMPHONY_WIRED = true; }
    loadSymphony();
  }
  if (view === "observe" && !HISTORY_LOADED) {
    HISTORY_LOADED = true;
    loadHistory();
  }
  if (view === "control") refreshCluster();
  if (view === "workloads") {
    if (!WORKLOADS_WIRED) { wireWorkloads(); WORKLOADS_WIRED = true; }
    loadWorkloads();
    startWorkloadsPolling();
  }
}

async function loadHistory() {
  try {
    const data = await api("/api/deployments");
    renderHistory(data.deployments || []);
    if (data.deployments && data.deployments.length) {
      selectDeploy(data.deployments[0].id);
    }
  } catch (err) {
    $("#history-list").innerHTML = "";
    $("#history-list").appendChild(
      el("div", { class: "error-box" }, `Failed to load deployments: ${err.message}`));
  }
}

// --------------------------------------------------------------------------- //
// Boot
// --------------------------------------------------------------------------- //

async function boot() {
  // health pill
  try {
    const h = await api("/api/health");
    const pill = $("#health-pill");
    if (h.temper_reachable) {
      pill.className = "pill pill-ok";
      pill.textContent = "temper: connected";
    } else {
      pill.className = "pill pill-bad";
      pill.textContent = "temper: unreachable";
    }
  } catch {
    const pill = $("#health-pill");
    pill.className = "pill pill-bad";
    pill.textContent = "backend error";
  }

  // Tabs navigate via the hash router (creates history entries -> Back works).
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => navigate(t.dataset.view)));

  // Browser Back/Forward (and any hash change) re-render the matching route.
  window.addEventListener("hashchange", renderRoute);

  // Escape dismisses any open modal (standard modal UX).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modalIsOpen()) { e.preventDefault(); dismissModal(); }
  });

  // Control plane wiring is needed even though Clusters is the home view, so
  // deploy/scale/images work the moment the user switches to it.
  wireControlPlane();
  loadImages();

  // Render whatever route the URL points at (deep link), defaulting to
  // Clusters. replace:true so the initial load isn't a spurious history entry.
  if (!location.hash) navigate("clusters", { replace: true });
  else renderRoute();
}

// =========================================================================== //
// Clusters view (ADR-0098): LTR git/worktree tree + clusters table + modals
// + cluster detail page.
// =========================================================================== //

let CLUSTERS_WIRED = false;
let CLUSTERS_CACHE = [];

function wireClusters() {
  $("#tree-refresh").addEventListener("click", loadTree);
  $("#cluster-create-btn").addEventListener("click", openCreateClusterModal);
  // In-app back mirrors the browser Back button. If the user deep-linked
  // straight to a detail page (no history to pop), fall back to the list.
  $("#detail-back").addEventListener("click", () => {
    if (history.length > 1) history.back();
    else navigate("clusters", { replace: true });
  });
  $("#modal-backdrop").addEventListener("click", dismissModal);
}

async function loadClustersView() {
  await Promise.all([loadTree(), loadClustersTable()]);
}

// ---------------- Modal host ----------------

function openModal(node, { wide = false } = {}) {
  const panel = $("#modal-panel");
  panel.innerHTML = "";
  panel.classList.toggle("modal-wide", !!wide);
  panel.appendChild(node);
  $("#modal-host").classList.remove("hidden");
}
function modalIsOpen() { return !$("#modal-host").classList.contains("hidden"); }
// Dismiss the modal. If it's the issue-detail modal (routed at #issue/<id>),
// go back so the hash returns to #symphony; otherwise just close.
function dismissModal() {
  if (location.hash.startsWith("#issue/")) {
    if (history.length > 1) history.back();
    else navigate("symphony", { replace: true });
  } else {
    closeModal();
  }
}
function closeModal() {
  $("#modal-host").classList.add("hidden");
  $("#modal-panel").innerHTML = "";
  $("#modal-panel").classList.remove("modal-wide");
}

function modalField(label, inputNode) {
  // Wrap the input INSIDE the <label> so the caption is programmatically
  // associated with the control (clickable label + screen-reader name), and
  // mirror the caption into aria-label as a belt-and-suspenders.
  inputNode.setAttribute("aria-label", label);
  return el("label", { class: "modal-field" }, el("span", { class: "modal-field-cap" }, label), inputNode);
}

// ---------------- LTR git/worktree tree (dagre + d3) ----------------

const STATE_DOT = {
  Live: "state-live", Building: "state-building", Deploying: "state-deploying",
  Defined: "state-defined", Approved: "state-approved", Failed: "state-failed",
  Torndown: "state-torndown",
};

async function loadTree() {
  const canvas = $("#tree-canvas");
  try {
    const data = await api("/api/tree");
    renderTree(data.nodes || []);
  } catch (err) {
    canvas.innerHTML = "";
    canvas.appendChild(el("div", { class: "error-box" }, `Failed to load lineage: ${err.message}`));
  }
}

// Orthogonal (right-angle, boxy) LTR layout: rank by depth from the root via
// link_parents, columns = rank (x), rows = siblings (y); connectors are
// horizontal-out / vertical-jump / horizontal-in elbows. No dagre/curves.
function renderTree(nodes) {
  const canvas = $("#tree-canvas");
  canvas.innerHTML = "";
  if (!nodes.length) {
    canvas.appendChild(el("div", { class: "loading" }, "No relevant lineage to show."));
    return;
  }
  if (typeof d3 === "undefined") {
    canvas.appendChild(el("div", { class: "error-box" }, "Graph library failed to load (offline?)."));
    return;
  }

  const NW = 220, NH = 58, COL = 280, ROWH = 74, MX = 20, MY = 18;
  const bySha = {};
  nodes.forEach((n) => { bySha[n.sha] = n; });
  const parentOf = (n) => (n.link_parents || []).find((p) => bySha[p]) || null;

  // Rank = longest chain from a root (node with no kept parent).
  const rank = {};
  function computeRank(sha, guard) {
    if (rank[sha] != null) return rank[sha];
    if (guard.has(sha)) return 0;
    guard.add(sha);
    const p = parentOf(bySha[sha]);
    const r = p ? computeRank(p, guard) + 1 : 0;
    rank[sha] = r;
    return r;
  }
  nodes.forEach((n) => computeRank(n.sha, new Set()));

  // Group by rank, assign a row per node within ascending rank order.
  const byRank = {};
  nodes.forEach((n) => { (byRank[rank[n.sha]] ||= []).push(n); });
  const pos = {}; // sha -> {x, y}
  let rowCursor = 0;
  const maxRank = Math.max(...nodes.map((n) => rank[n.sha]));
  // Lay out by walking children of each node so siblings stack near their parent.
  const childrenOf = {};
  nodes.forEach((n) => { const p = parentOf(n); if (p) (childrenOf[p] ||= []).push(n); });
  const roots = nodes.filter((n) => !parentOf(n));
  function place(n) {
    pos[n.sha] = { x: MX + rank[n.sha] * COL, y: MY + rowCursor * ROWH };
    rowCursor++;
    (childrenOf[n.sha] || []).forEach(place);
  }
  roots.forEach(place);
  // Any node not reached (cycle guard) gets appended.
  nodes.forEach((n) => { if (!pos[n.sha]) { pos[n.sha] = { x: MX + rank[n.sha] * COL, y: MY + rowCursor * ROWH }; rowCursor++; } });

  const W = MX * 2 + (maxRank + 1) * COL;
  const H = MY * 2 + rowCursor * ROWH;
  const svg = d3.select(canvas).append("svg").attr("width", Math.max(W, 600)).attr("height", Math.max(H, 200));

  // Orthogonal connectors: from parent right-mid -> elbow -> child left-mid.
  const edgeG = svg.append("g");
  nodes.forEach((n) => {
    const p = parentOf(n);
    if (!p || !pos[p]) return;
    const a = pos[p], b = pos[n.sha];
    const x1 = a.x + NW, y1 = a.y + NH / 2;   // parent right-middle
    const x2 = b.x, y2 = b.y + NH / 2;        // child left-middle
    const midX = x1 + Math.max(20, (x2 - x1) / 2);
    const d = `M ${x1} ${y1} H ${midX} V ${y2} H ${x2}`;
    edgeG.append("path").attr("class", "tree-edge").attr("d", d);
  });

  const onMain = (n) => (n.branches || []).includes("main");
  const hasCluster = (n) => (n.clusters || []).length > 0;

  const node = svg.append("g").selectAll("g").data(nodes).enter().append("g")
    .attr("class", (n) => `tree-node-card ${hasCluster(n) ? "clickable" : ""}`)
    .attr("transform", (n) => `translate(${pos[n.sha].x},${pos[n.sha].y})`)
    .style("cursor", (n) => hasCluster(n) ? "pointer" : "default")
    .on("click", (_ev, n) => {
      const cl = (n.clusters || [])[0];
      if (cl) {
        if (["Building", "Deploying", "queued", "worktree"].includes(cl.status)) openClusterLogs(cl.id, cl.name);
        else openClusterDetail(cl.id);
      }
    });

  // Native hover tooltip with the full sha + branch + subject (subjects are
  // truncated in the box, so this lets you read the whole thing).
  node.append("title").text((n) => {
    const b = (n.branches || []).join(", ") || (n.worktree && n.worktree.branch) || "";
    const cl = (n.clusters || [])[0];
    return `${n.sha}${b ? "  (" + b + ")" : ""}\n${n.subject || ""}${cl ? `\n→ helix-${cl.name} · ${cl.status}` : ""}`;
  });

  node.append("rect")
    .attr("class", (n) => `tnc-box ${hasCluster(n) ? "has-cluster" : ""} ${onMain(n) ? "on-main" : ""}`)
    .attr("width", NW).attr("height", NH).attr("rx", 6);

  node.append("text").attr("class", "tnc-sha").attr("x", 12).attr("y", 19).text((n) => n.sha);
  node.append("text").attr("class", "tnc-branch").attr("x", 66).attr("y", 19)
    .text((n) => {
      const b = (n.branches || []).find((x) => x !== "main" && !x.startsWith("df-cluster/"))
        || (n.worktree && n.worktree.branch) || (n.branches || [])[0] || "";
      return (n.worktree ? "⊟ " : "") + b;
    });
  node.append("text").attr("class", "tnc-subject").attr("x", 12).attr("y", 37)
    .text((n) => { const s = n.subject || ""; return s.length > 32 ? s.slice(0, 31) + "…" : s; });
  node.each(function (n) {
    const cls = n.clusters || [];
    if (!cls.length) return;
    const c = cls[0];
    const dotClass = STATE_DOT[c.status] || "state-defined";
    const sel = d3.select(this);
    sel.append("circle").attr("cx", 17).attr("cy", 49).attr("r", 4)
      .attr("class", `tnc-dot ${dotClass}`).attr("fill", "currentColor");
    sel.append("text").attr("class", "tnc-cluster").attr("x", 27).attr("y", 52)
      .text(`helix-${c.name}${cls.length > 1 ? ` +${cls.length - 1}` : ""} · ${c.status}`);
  });
}

// ---------------- Clusters table ----------------

function countWorkloads(workloads, kind, queue) {
  // workloads from /api/workloads; filter to this cluster's queue prefix when known.
  if (!workloads) return 0;
  const list = workloads[kind] || [];
  return list.length;
}

async function loadClustersTable() {
  const tbody = $("#clusters-tbody");
  try {
    const [data, wl] = await Promise.all([
      api("/api/clusters"),
      api("/api/workloads").catch(() => ({})),
    ]);
    // De-dupe Defined shells by name (keep the most recent / most-advanced).
    const seen = new Map();
    for (const c of data.clusters || []) {
      const prev = seen.get(c.name);
      if (!prev || c.id > prev.id) seen.set(c.name, c);
    }
    CLUSTERS_CACHE = [...seen.values()].filter((c) => c.status !== "Torndown");
    renderClustersTable(CLUSTERS_CACHE, wl);
  } catch (err) {
    tbody.innerHTML = "";
    tbody.appendChild(el("tr", {}, el("td", { colspan: "6", class: "error-box" },
      `Failed to load clusters: ${err.message}`)));
  }
}

function renderClustersTable(clusters, wl) {
  const tbody = $("#clusters-tbody");
  tbody.innerHTML = "";
  if (!clusters.length) {
    tbody.appendChild(el("tr", {}, el("td", { colspan: "6", class: "loading" },
      "No clusters yet — click “+ Create cluster”.")));
    return;
  }
  const allProducers = (wl && wl.producers) || [];
  const allConsumers = (wl && wl.consumers) || [];
  const allQueues = (wl && wl.queues) || [];

  const isBuilding = (c) => ["Building", "Deploying", "queued"].includes(c.status) || ["building", "deploying", "queued", "worktree"].includes(c.job_phase || "");

  // Each workload carries a `cluster` field ("helix" = baseline). A cluster row
  // shows ONLY its own queues/producers/consumers.
  const clusterMatches = (wlCluster, c) => (wlCluster || "helix") === (c.name || "");

  for (const c of clusters) {
    const dotCls = STATE_DOT[c.status] || "state-defined";

    // Filter workloads to THIS cluster, then group by queue (topic).
    const producers = allProducers.filter((p) => clusterMatches(p.cluster, c));
    const consumers = allConsumers.filter((cn) => clusterMatches(cn.cluster, c));
    const queues = allQueues.filter((q) => clusterMatches(q.cluster, c));
    const byQueue = {};
    const ensure = (topic) => (byQueue[topic] ||= { producers: [], consumers: [] });
    queues.forEach((q) => { if (q.name) ensure(q.name); });
    producers.forEach((p) => { if (p.queue) ensure(p.queue).producers.push(p); });
    consumers.forEach((cn) => { if (cn.queue) ensure(cn.queue).consumers.push(cn); });
    const allTopics = Object.keys(byQueue);

    // Workloads cell: one line per queue with producer/consumer chips.
    const wlCell = el("td", { class: "cl-wl-cell" });
    if (!allTopics.length) {
      wlCell.appendChild(el("span", { class: "cl-count-zero" }, "no queues"));
    } else {
      const isRunning = (w) => (w.status || "") === "Running";
      for (const topic of allTopics) {
        const grp = byQueue[topic];
        // A queue is "migrated away" from THIS cluster when it has no Running
        // producers or consumers here — its workloads were stopped by a migration
        // and recreated on a niche cluster. Show only the RUNNING chips; dim the
        // whole line + tag it when nothing is running here.
        const runProds = grp.producers.filter(isRunning);
        const runCons = grp.consumers.filter(isRunning);
        const migratedAway = (grp.producers.length || grp.consumers.length) && !runProds.length && !runCons.length;
        const line = el("div", { class: `cl-qline${migratedAway ? " cl-qline-migrated" : ""}` },
          el("span", { class: "cl-qname" }, topic),
          el("span", { class: "cl-qmeta" },
            ...runProds.map((p) => el("span", { class: "cl-chip cl-chip-prod", title: `producer ${p.id} (${p.shape || "stable"})` }, `▶ ${p.rate_per_sec || "?"}/s${p.shape && p.shape !== "stable" ? " · " + p.shape : ""}`)),
            ...runCons.map((cn) => el("span", { class: "cl-chip cl-chip-cons", title: `consumer ${cn.id} (${cn.shape || "stable"})` }, `◀ ${cn.process_ms ?? "?"}ms${cn.shape && cn.shape !== "stable" ? " · " + cn.shape : ""}`)),
            migratedAway ? el("span", { class: "cl-chip cl-chip-migrated", title: "Workloads stopped here and migrated to a niche cluster" }, "migrated →") : null,
            (!migratedAway && !runProds.length && !runCons.length) ? el("span", { class: "cl-count-zero" }, "idle") : null,
          ));
        wlCell.appendChild(line);
      }
    }

    const stateCell = el("td", {}, el("span", { class: `state-dot ${dotCls}` }), c.status || "—");
    if (isBuilding(c)) {
      stateCell.classList.add("cl-clickable-state");
      stateCell.title = "Click to view build logs";
    }

    const row = el("tr", {
      onClick: () => { if (isBuilding(c)) openClusterLogs(c.id, c.name); else openClusterDetail(c.id); },
    },
      el("td", {},
        el("span", { class: "cl-name" }, `helix-${c.name}`,
          // Sub-line shows the source branch/worktree (distinct info), not a
          // redundant repeat of the StatefulSet name.
          el("span", { class: "cl-ss" }, c.branch || c.worktree || "—"))),
      stateCell,
      el("td", {}, el("span", { class: "cl-version" }, c.image_tag || "—")),
      el("td", { class: "cl-count" }, String(c.replicas ?? "—")),
      wlCell,
      el("td", { class: "cl-row-actions" },
        c.status === "Defined"
          ? el("button", { class: "cl-mini-btn", onClick: (e) => { e.stopPropagation(); approveCluster(c.id); } }, "Approve")
          : isBuilding(c)
            ? el("button", { class: "cl-mini-btn", onClick: (e) => { e.stopPropagation(); openClusterLogs(c.id, c.name); } }, "Logs")
            : el("button", { class: "cl-mini-btn", onClick: (e) => { e.stopPropagation(); openEditClusterModal(c); } }, "Edit"),
        el("button", { class: "cl-mini-btn cl-mini-danger", title: "Tear down this cluster (StatefulSet + Services + worktree)", onClick: (e) => { e.stopPropagation(); tearDownCluster(c); } }, "Teardown")),
    );
    tbody.appendChild(row);
  }
}

async function tearDownCluster(c) {
  if (!confirm(`Tear down helix-${c.name}? This deletes its StatefulSet + Services and prunes its worktree.`)) return;
  const r = await post(`/api/clusters/${encodeURIComponent(c.id)}/teardown`, {});
  if (!(r.ok && r.data.ok)) {
    alert("Teardown failed: " + (r.data.detail || (r.data.output || "").slice(0, 300) || `HTTP ${r.status}`));
  }
  await loadClustersView();
}

// ---------------- Create / approve / edit ----------------

function openCreateClusterModal() {
  const nameI = el("input", { type: "text", placeholder: "exp-linger", id: "ncl-name" });
  const baseI = el("input", { type: "text", value: "champion-metrics-pranav-clone", id: "ncl-base" });
  const repI = el("input", { type: "number", min: "1", max: "5", value: "3", id: "ncl-rep" });
  const msg = el("div", { class: "modal-note" });

  const submit = el("button", {
    class: "cp-btn cp-btn-primary", onClick: async () => {
      msg.textContent = "Creating governed Cluster…";
      const res = await post("/api/clusters", {
        name: nameI.value.trim(), base: baseI.value.trim() || "main",
        replicas: parseInt(repI.value, 10) || 3,
      });
      if (res.ok && res.data.ok) {
        closeModal();
        await loadClustersView();
      } else {
        msg.textContent = "Denied: " + (res.data.detail || res.data.step || `HTTP ${res.status}`);
      }
    }
  }, "Create");

  openModal(el("div", {},
    el("h3", { class: "modal-title" }, "Create Helix cluster"),
    el("p", { class: "modal-sub" }, "Provisions a governed Cluster from its own git worktree. Approve (human gate) then builds from the worktree and deploys helix-<name>."),
    modalField("Name (→ helix-<name>)", nameI),
    el("div", { class: "modal-row" }, modalField("Base branch", baseI), modalField("Replicas", repI)),
    msg,
    el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Cancel"), submit),
  ));
}

async function approveCluster(id) {
  const res = await post(`/api/clusters/${encodeURIComponent(id)}/approve`, {});
  if (!(res.ok && res.data.ok)) {
    alert("Approve denied: " + (res.data.detail || `HTTP ${res.status}`));
  }
  await loadClustersView();
}

// Shape presets -> the param inputs each one reveals. Keys map to shape_params.
const PRODUCER_SHAPES = {
  stable: { label: "Stable (steady rate)", params: [] },
  bursty: { label: "Bursty (burst then idle)", params: [["burst_size", "Burst size", 1000], ["idle_ms", "Idle ms", 3000]] },
  ramp: { label: "Ramp (rate ramps up)", params: [["ramp_to", "Ramp to /s", 2000], ["ramp_period_ms", "Ramp period ms", 60000]] },
  batch: { label: "Batch (large periodic flush)", params: [["batch_period_ms", "Batch period ms", 2000]] },
};
const CONSUMER_SHAPES = {
  stable: { label: "Stable (1 msg at a time)", params: [] },
  low_latency: { label: "Low latency (fetch ASAP)", params: [] },
  batch: { label: "Batch (process N at once)", params: [["batch_size", "Batch size", 100], ["fetch_wait_ms", "Fetch wait ms", 500]] },
  bursty: { label: "Bursty (consume then idle)", params: [["burst_period_ms", "Burst ms", 2000], ["idle_ms", "Idle ms", 4000]] },
  spiky: { label: "Spiky latency (p50/p99 tail)", params: [["p50_ms", "p50 ms", 5], ["p99_ms", "p99 ms", 200]] },
};

// Build a shape <select> + a params container that re-renders on change.
// Returns { select, paramsBox, read() -> {shape, shape_params{}} }.
function shapePicker(shapeDefs) {
  const paramsBox = el("div", { class: "modal-row ecl-shape-params" });
  const inputs = {};
  function render(shape) {
    paramsBox.innerHTML = ""; for (const k in inputs) delete inputs[k];
    for (const [key, label, def] of (shapeDefs[shape] || {}).params || []) {
      const inp = el("input", { type: "number", min: "0", max: "600000", value: String(def) });
      inputs[key] = inp;
      paramsBox.appendChild(modalField(label, inp));
    }
  }
  const select = el("select", {
    class: "cp-select",
    onChange: (e) => render(e.target.value)
  });
  for (const [val, def] of Object.entries(shapeDefs)) select.appendChild(el("option", { value: val }, def.label));
  render(select.value);
  return {
    select, paramsBox, read: () => {
      const sp = {}; for (const k in inputs) { const v = parseInt(inputs[k].value, 10); if (!isNaN(v)) sp[k] = v; }
      return { shape: select.value, shape_params: sp };
    }
  };
}

function openEditClusterModal(c) {
  // Add a queue / producer / consumer to THIS cluster.
  const qName = el("input", { type: "text", placeholder: "orders", id: "ecl-q" });
  const pRate = el("input", { type: "number", value: "500", min: "1", max: "5000" });
  const pSize = el("input", { type: "number", value: "512", min: "1", max: "65536" });
  const cProc = el("input", { type: "number", value: "5", min: "0", max: "60000" });
  const msg = el("div", { class: "modal-note" });
  const queueForWl = el("input", { type: "text", placeholder: "orders", id: "ecl-wq" });
  const prodShape = shapePicker(PRODUCER_SHAPES);
  const consShape = shapePicker(CONSUMER_SHAPES);

  // Live list of this cluster's existing workloads, each removable.
  const existing = el("div", { class: "ecl-existing" }, el("div", { class: "loading" }, "Loading workloads…"));
  const matches = (wlCluster) => (wlCluster || "helix") === (c.name || "");
  async function refreshExisting() {
    existing.innerHTML = "";
    let wl;
    try { wl = await api("/api/workloads"); } catch { existing.appendChild(el("div", { class: "cl-count-zero" }, "could not load")); return; }
    const queues = (wl.queues || []).filter((q) => matches(q.cluster) && q.status !== "Deleted");
    const producers = (wl.producers || []).filter((p) => matches(p.cluster) && p.status !== "Stopped");
    const consumers = (wl.consumers || []).filter((cn) => matches(cn.cluster) && cn.status !== "Stopped");
    if (!queues.length && !producers.length && !consumers.length) {
      existing.appendChild(el("div", { class: "cl-count-zero" }, "no workloads on this cluster yet"));
      return;
    }
    const rowFor = (label, sub, onRemove) => el("div", { class: "ecl-row" },
      el("div", {}, el("span", { class: "ecl-row-name" }, label), el("span", { class: "ecl-row-sub" }, sub)),
      el("button", {
        class: "cl-mini-btn cl-mini-danger", onClick: async (e) => {
          e.target.disabled = true; e.target.textContent = "…";
          await onRemove(); await refreshExisting(); loadClustersTable();
        }
      }, "Remove"));
    queues.forEach((q) => existing.appendChild(rowFor(`queue ${q.name}`, `${q.partitions ?? "?"} partitions`,
      () => post(`/api/workloads/queue/${encodeURIComponent(q.id)}/delete`, {}))));
    producers.forEach((p) => existing.appendChild(rowFor(`producer → ${p.queue}`, `▶ ${p.rate_per_sec || "?"}/s · ${p.shape || "stable"}`,
      () => post(`/api/workloads/producer/${encodeURIComponent(p.id)}/stop`, {}))));
    consumers.forEach((cn) => existing.appendChild(rowFor(`consumer ← ${cn.queue}`, `◀ ${cn.process_ms ?? "?"}ms · ${cn.shape || "stable"}`,
      () => post(`/api/workloads/consumer/${encodeURIComponent(cn.id)}/stop`, {}))));
  }
  refreshExisting();

  openModal(el("div", {},
    el("h3", { class: "modal-title" }, `Edit helix-${c.name}`),
    el("p", { class: "modal-sub" }, "Add or remove governed workloads on this cluster. Each routes through a Temper Queue/Producer/Consumer entity."),

    el("div", { class: "modal-section-title" }, "Current workloads"),
    existing,

    el("div", { class: "modal-section-title" }, "Add queue"),
    modalField("Topic name", qName),
    el("button", {
      class: "cp-btn", onClick: async () => {
        msg.textContent = "Creating queue…";
        const topic = qName.value.trim();
        const r = await post("/api/workloads/queue", { name: topic, queue: topic, partitions: 3, cluster: c.name });
        msg.textContent = r.ok && r.data.ok ? "Queue created." : "Denied: " + (r.data.detail || r.status);
        loadClustersTable();
      }
    }, "Add queue"),

    el("div", { class: "modal-section-title" }, "Add producer"),
    modalField("Queue", queueForWl),
    el("div", { class: "modal-row" },
      modalField("Rate /s", pRate), modalField("Msg size (B)", pSize),
      modalField("Shape", prodShape.select)),
    prodShape.paramsBox,
    el("button", {
      class: "cp-btn", onClick: async () => {
        msg.textContent = "Adding producer…";
        const sh = prodShape.read();
        const r = await post("/api/workloads/producer", {
          queue: queueForWl.value.trim(), rate_per_sec: parseInt(pRate.value, 10) || 500,
          msg_size: parseInt(pSize.value, 10) || 512, concurrency: 1, cluster: c.name,
          shape: sh.shape, shape_params: sh.shape_params
        });
        msg.textContent = r.ok && r.data.ok ? `Producer added (${sh.shape}).` : "Denied: " + (r.data.detail || r.status);
        refreshExisting(); loadClustersTable();
      }
    }, "Add producer"),

    el("div", { class: "modal-section-title" }, "Add consumer"),
    el("div", { class: "modal-row" },
      modalField("Process ms", cProc), modalField("Shape", consShape.select)),
    consShape.paramsBox,
    el("button", {
      class: "cp-btn", onClick: async () => {
        msg.textContent = "Adding consumer…";
        const sh = consShape.read();
        const r = await post("/api/workloads/consumer", {
          queue: queueForWl.value.trim(), process_ms: parseInt(cProc.value, 10) || 5,
          concurrency: 1, start_offset: "beginning", cluster: c.name,
          shape: sh.shape, shape_params: sh.shape_params
        });
        msg.textContent = r.ok && r.data.ok ? `Consumer added (${sh.shape}).` : "Denied: " + (r.data.detail || r.status);
        refreshExisting(); loadClustersTable();
      }
    }, "Add consumer"),
    msg,
    el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Done")),
  ));
}

// ---------------- Cluster detail page ----------------

// Navigate to the cluster-detail route (pushes history -> Back works).
function openClusterDetail(id) { navigate("cluster/" + encodeURIComponent(id)); }

// Actually render the cluster-detail page (called by the router on #cluster/<id>).
async function showClusterDetail(id) {
  $("#view-clusters").classList.add("hidden");
  $("#view-symphony").classList.add("hidden");
  $("#view-control").classList.add("hidden");
  $("#view-workloads").classList.add("hidden");
  $("#view-observe").classList.add("hidden");
  $("#view-cluster-detail").classList.remove("hidden");
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.view === "clusters"));
  const body = $("#cluster-detail-body");
  body.innerHTML = "";
  body.appendChild(el("div", { class: "loading" }, "Loading cluster…"));

  try {
    const data = await api("/api/clusters");
    const c = (data.clusters || []).find((x) => x.id === id);
    if (!c) { body.innerHTML = ""; body.appendChild(el("div", { class: "error-box" }, "Cluster not found.")); return; }
    const deploys = await api("/api/deployments").catch(() => ({ deployments: [] }));
    const wl = await api("/api/workloads").catch(() => ({}));
    renderClusterDetail(c, deploys.deployments || [], wl);
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("div", { class: "error-box" }, `Failed: ${err.message}`));
  }
}

function renderClusterDetail(c, deployments, wl) {
  const body = $("#cluster-detail-body");
  body.innerHTML = "";
  const dotCls = STATE_DOT[c.status] || "state-defined";

  body.appendChild(el("div", { class: "detail-head" },
    el("span", { class: "detail-title" }, `helix-${c.name}`),
    el("span", {}, el("span", { class: `state-dot ${dotCls}` }), c.status || "—")));
  body.appendChild(el("div", { class: "detail-meta" },
    `${c.branch || "—"}  ·  ${c.image_tag || "no image"}  ·  ${c.replicas ?? "?"} replicas  ·  ${c.worktree || ""}`));

  // Deployment history for this cluster's image lineage.
  const relevant = deployments.filter((d) =>
    !c.image_tag || (d.image_tag && (d.image_tag === c.image_tag || d.image_tag.includes(c.name))));
  const rollbackBtn = el("button", {
    class: "cp-btn cp-btn-ghost",
    title: c.status === "Live" ? "Roll this cluster's StatefulSet back to its previous image" : "Only a Live cluster can be rolled back",
    onClick: async (e) => {
      if (c.status !== "Live") { alert("Cluster must be Live to roll back (is " + c.status + ")."); return; }
      if (!confirm(`Roll helix-${c.name} back to its previous image?`)) return;
      e.target.disabled = true; e.target.textContent = "Rolling back…";
      const r = await post(`/api/clusters/${encodeURIComponent(c.id)}/rollback`, {});
      if (r.ok && r.data.ok) {
        alert(`Rolled back: ${r.data.from || "?"} → ${r.data.to}`);
        openClusterDetail(c.id);
      } else {
        alert("Rollback failed: " + (r.data.detail || (r.data.output || "").slice(0, 300) || `HTTP ${r.status}`));
        e.target.disabled = false; e.target.textContent = "Rollback";
      }
    },
  }, "Rollback");
  const teardownBtn = el("button", {
    class: "cp-btn cp-btn-ghost cl-mini-danger",
    title: "Tear down this cluster (StatefulSet + Services + worktree)",
    onClick: async (e) => {
      if (!confirm(`Tear down helix-${c.name}? This deletes its StatefulSet + Services and prunes its worktree.`)) return;
      e.target.disabled = true; e.target.textContent = "Tearing down…";
      const r = await post(`/api/clusters/${encodeURIComponent(c.id)}/teardown`, {});
      if (r.ok && r.data.ok) { switchView("clusters"); }
      else { alert("Teardown failed: " + (r.data.detail || (r.data.output || "").slice(0, 300) || `HTTP ${r.status}`)); e.target.disabled = false; e.target.textContent = "Teardown"; }
    },
  }, "Teardown");
  const panel = el("section", { class: "panel" },
    el("div", { class: "panel-head" }, "Deployment history",
      el("span", { class: "detail-head-actions" }, rollbackBtn, teardownBtn)));
  const pbody = el("div", { class: "panel-body detail-timeline" });
  if (!relevant.length) {
    pbody.appendChild(el("div", { class: "loading" }, "No deployments recorded for this cluster yet."));
  } else {
    for (const d of relevant.slice(0, 20)) {
      pbody.appendChild(el("div", { class: "detail-deploy" },
        el("div", { class: "card-tag" }, d.image_tag || d.id),
        el("div", { class: "card-id" }, `${displayStatus(d)} · ${fmtTime(d.created_at || d.when)}`),
        d.result ? el("div", { class: "card-result" }, d.result) : null));
    }
  }
  panel.appendChild(pbody);
  body.appendChild(panel);

  // ---- Workloads on this cluster (queues + producers/consumers per queue) ----
  const matches = (wlCluster) => (wlCluster || "helix") === (c.name || "");
  const producers = ((wl && wl.producers) || []).filter((p) => matches(p.cluster) && p.status !== "Stopped");
  const consumers = ((wl && wl.consumers) || []).filter((cn) => matches(cn.cluster) && cn.status !== "Stopped");
  const queues = ((wl && wl.queues) || []).filter((q) => matches(q.cluster) && q.status !== "Deleted");

  const byQueue = {};
  const ensure = (t) => (byQueue[t] ||= { producers: [], consumers: [] });
  queues.forEach((q) => { if (q.name) ensure(q.name); });
  producers.forEach((p) => { if (p.queue) ensure(p.queue).producers.push(p); });
  consumers.forEach((cn) => { if (cn.queue) ensure(cn.queue).consumers.push(cn); });
  const topics = Object.keys(byQueue);

  const wlPanel = el("section", { class: "panel" },
    el("div", { class: "panel-head" }, "Workloads",
      el("button", {
        class: "cp-btn cp-btn-ghost",
        title: c.status === "Live" ? "Add/remove producers, consumers, queues" : "Cluster must be Live to manage workloads",
        onClick: () => { if (c.status === "Live") openEditClusterModal(c); else alert("Cluster must be Live to manage workloads (is " + c.status + ")."); }
      },
        "Manage")));
  const wbody = el("div", { class: "panel-body" });

  // One expanded row per producer/consumer: direction, knobs, shape, pods, Stop.
  const podSummary = (w) => {
    const pods = w.pods || [];
    if (!pods.length) return "no pods";
    const ready = pods.filter((p) => p.ready).length;
    return `${ready}/${pods.length} ready`;
  };
  const stopBtn = (kind, w) => el("button", {
    class: "cl-mini-btn cl-mini-danger",
    onClick: async (e) => {
      e.stopPropagation();
      if (!confirm(`Stop ${kind} ${w.id}?`)) return;
      e.target.disabled = true; e.target.textContent = "…";
      await post(`/api/workloads/${kind}/${encodeURIComponent(w.id)}/stop`, {});
      openClusterDetail(c.id);
    }
  }, "Stop");

  const wlRow = (kind, w) => {
    const dir = kind === "producer" ? "▶" : "◀";
    const knobs = kind === "producer"
      ? `${w.rate_per_sec ?? "?"}/s · ${w.msg_size ?? "?"}B`
      : `${w.process_ms ?? "?"}ms/msg`;
    return el("div", { class: `wl-detail-row wl-${kind}` },
      el("span", { class: "wl-dir" }, dir),
      el("div", { class: "wl-main" },
        el("div", { class: "wl-line1" },
          el("span", { class: "wl-id" }, w.id),
          el("span", { class: `wl-shape wl-shape-${w.shape || "stable"}` }, w.shape || "stable")),
        el("div", { class: "wl-line2" }, `${knobs} · conc ${w.concurrency ?? 1} · ${podSummary(w)}`)),
      stopBtn(kind, w));
  };

  if (!topics.length) {
    wbody.appendChild(el("div", { class: "cl-count-zero" }, "No queues / producers / consumers on this cluster."));
  } else {
    for (const topic of topics) {
      const grp = byQueue[topic];
      const block = el("div", { class: "wl-queue-block" },
        el("div", { class: "wl-queue-head" }, el("span", { class: "cl-qname" }, topic),
          el("span", { class: "wl-queue-count" }, `${grp.producers.length} prod · ${grp.consumers.length} cons`)));
      grp.producers.forEach((p) => block.appendChild(wlRow("producer", p)));
      grp.consumers.forEach((cn) => block.appendChild(wlRow("consumer", cn)));
      if (!grp.producers.length && !grp.consumers.length) {
        block.appendChild(el("div", { class: "cl-count-zero wl-detail-row" }, "queue only — no producers/consumers"));
      }
      wbody.appendChild(block);
    }
  }
  wlPanel.appendChild(wbody);
  body.appendChild(wlPanel);
}

// ---------------- Build logs (Building/Deploying clusters) ----------------

let LOGS_TIMER = null;

function openClusterLogs(id, name) {
  if (LOGS_TIMER) { clearInterval(LOGS_TIMER); LOGS_TIMER = null; }
  const phaseEl = el("span", { class: "logs-phase" }, "…");
  const pre = el("pre", { class: "logs-pre" }, "Loading build logs…");
  const panel = el("div", {},
    el("h3", { class: "modal-title" }, `Provisioning helix-${name}`),
    el("p", { class: "modal-sub" }, "Live build & deploy logs — Cloud Build from the worktree, then kubectl rollout."),
    el("div", { class: "logs-statusbar" }, el("span", { class: "logs-label" }, "Phase: "), phaseEl),
    pre,
    el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: () => { if (LOGS_TIMER) clearInterval(LOGS_TIMER); LOGS_TIMER = null; closeModal(); } }, "Close")),
  );
  openModal(panel);

  const tick = async () => {
    try {
      const d = await api(`/api/clusters/${encodeURIComponent(id)}/logs`);
      phaseEl.textContent = d.phase ? `${d.phase}${d.detail ? " — " + d.detail : ""}` : (d.note || "—");
      phaseEl.className = "logs-phase " + (STATE_DOT[(d.phase || "").replace(/^\w/, (c) => c.toUpperCase())] || "");
      const log = (d.log || []).join("\n");
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      pre.textContent = log || "(no output yet)";
      if (atBottom) pre.scrollTop = pre.scrollHeight;
      if (["live", "failed"].includes(d.phase)) { if (LOGS_TIMER) clearInterval(LOGS_TIMER); LOGS_TIMER = null; }
    } catch (err) {
      pre.textContent = "Failed to load logs: " + err.message;
    }
  };
  tick();
  LOGS_TIMER = setInterval(tick, 2000);
}

// =========================================================================== //
// Symphony pipeline board (ADR-0093): ImprovementIssues across the loop, with
// Symphony's worktree/branch/PR on Implementing+ cards.
// =========================================================================== //

let SYMPHONY_WIRED = false;

function wireSymphony() {
  $("#sym-refresh").addEventListener("click", loadSymphony);
}

const SYM_STAGE_LABEL = {
  Observed: "Observed", Researching: "Researching", Planned: "Planned",
  Implementing: "Implementing", Verifying: "Verifying", Deploying: "Deploying",
  Done: "Done",
};

async function loadSymphony() {
  const board = $("#sym-board");
  try {
    const data = await api("/api/symphony/issues");
    renderSymphonyBoard(data);
  } catch (err) {
    board.innerHTML = "";
    board.appendChild(el("div", { class: "error-box" }, `Failed to load pipeline: ${err.message}`));
  }
}

function symProgressDots(c) {
  // plan -> approved -> branch -> pr -> ci passed
  const steps = [c.has_plan, c.plan_approved, c.has_branch, c.has_pr, c.ci_passed];
  return el("div", { class: "sym-progress", title: "plan · approved · branch · PR · CI" },
    ...steps.map((on) => el("div", { class: `sym-pdot ${on ? "on" : ""}` })));
}

// A speciation ticket marks its target as "speciation://<cluster>". Render it as
// a clear infra-placement badge rather than the raw marker string.
function symTargetEl(target) {
  if (!target) return null;
  if (target.startsWith("speciation://")) {
    const cluster = target.slice("speciation://".length);
    return el("div", { class: "sym-card-file sym-card-speciation" },
      `🧬 speciation → cluster ${cluster}`);
  }
  if (target.startsWith("opt://")) {
    const cluster = target.slice("opt://".length);
    return el("div", { class: "sym-card-file sym-card-optimizer" },
      `⚙ optimize → cluster ${cluster}`);
  }
  if (target.startsWith("meta://")) {
    const agent = target.slice("meta://".length);
    return el("div", { class: "sym-card-file sym-card-meta" },
      `📊 prompt-tune → ${agent} agent`);
  }
  return el("div", { class: "sym-card-file" }, target);
}

function symCard(c) {
  const children = [
    el("div", { class: "sym-card-title" }, c.title),
    symTargetEl(c.target_file),
  ];

  // Agent chips: planner + assignee (Symphony is usually the assignee).
  const agents = [];
  if (c.planner_id) agents.push(el("span", { class: "sym-agent-chip" }, `plan: ${c.planner_id}`));
  if (c.assignee_id) agents.push(el("span", { class: "sym-agent-chip" }, `impl: ${c.assignee_id}`));
  if (agents.length) children.push(el("div", { class: "sym-card-agents" }, ...agents));

  // Symphony's output: worktree branch + PR (shown once it has a branch).
  if (c.branch_name || c.pr_url) {
    const symBlock = el("div", { class: "sym-card-symphony" });
    if (c.branch_name) symBlock.appendChild(el("div", { class: "sym-branch" }, c.branch_name));
    if (c.pr_url) {
      const isSim = c.pr_simulated;
      const label = isSim ? "simulated PR" : "PR";
      symBlock.appendChild(
        isSim
          ? el("span", { class: "sym-pr sym-pr-sim", title: c.pr_url }, label)
          : el("a", { class: "sym-pr", href: c.pr_url, target: "_blank", rel: "noopener", title: c.pr_url }, label)
      );
    }
    if (c.ci_status) {
      symBlock.appendChild(el("div", { class: `sym-ci ${c.ci_passed ? "sym-ci-passed" : "sym-ci-failed"}` },
        `CI: ${c.ci_status}`));
    }
    children.push(symBlock);
  }

  children.push(symProgressDots(c));

  return el("div", { class: "sym-card", onClick: () => navigate("issue/" + encodeURIComponent(c.id)) }, ...children);
}

function renderSymphonyBoard(data) {
  const board = $("#sym-board");
  board.innerHTML = "";
  const stages = data.stages || [];
  const columns = data.columns || {};

  for (const stage of stages) {
    const cards = columns[stage] || [];
    const isImpl = stage === data.implementing_stage;
    const col = el("div", { class: `sym-col ${isImpl ? "sym-col-implementing" : ""}` },
      el("div", { class: "sym-col-head" },
        el("span", {}, SYM_STAGE_LABEL[stage] || stage),
        el("span", { class: "sym-col-count" }, String(cards.length))),
    );
    const body = el("div", { class: "sym-col-body" });
    if (!cards.length) {
      body.appendChild(el("div", { class: "sym-col-empty" }, "—"));
    } else {
      for (const c of cards) body.appendChild(symCard(c));
    }
    col.appendChild(body);
    board.appendChild(col);
  }

  // Failed issues (surfaced below the board).
  const failedWrap = $("#sym-failed");
  failedWrap.innerHTML = "";
  const failed = data.failed || [];
  if (failed.length) {
    failedWrap.appendChild(el("div", { class: "section-title" }, "Failed",
      el("span", { class: "count-badge" }, String(failed.length))));
    for (const c of failed) {
      failedWrap.appendChild(el("div", { class: "sym-failed-card" },
        el("div", { class: "sym-card-title" }, c.title),
        symTargetEl(c.target_file)));
    }
  }
}

// Render a ticket's Plan. Optimizer/speciation tickets carry a JSON payload
// (machine-readable for the executor) — render it as structured cards instead of
// dumping the raw escaped JSON. Normal code-PR plans render as plain text.
function symPlanEl(plan) {
  let obj = null;
  if (typeof plan === "string") {
    try { obj = JSON.parse(plan); } catch { obj = null; }
  } else if (typeof plan === "object") {
    obj = plan;
  }
  // Optimizer plan {cluster, workload_summary, proposals:[...]} OR meta plan
  // {agent, trace_summary, proposals:[{target_file, change, evidence, expected_effect}]}.
  if (obj && Array.isArray(obj.proposals)) {
    const wrap = el("div", { class: "spec-proposals" });
    const summary = obj.workload_summary || obj.trace_summary;
    if (summary) {
      const label = obj.trace_summary ? "traces" : "workload";
      wrap.appendChild(el("div", { class: "opt-workload" }, `${label}: ${summary}`));
    }
    for (const p of obj.proposals) {
      wrap.appendChild(el("div", { class: "spec-proposal" },
        el("div", { class: "opt-prop-file" }, `⚙ ${p.target_file || "?"}`),
        p.change ? el("div", { class: "opt-prop-change" }, p.change) : null,
        p.rationale ? el("div", { class: "opt-prop-rat" }, p.rationale) : null,
        p.evidence ? el("div", { class: "opt-prop-rat" }, `evidence: ${p.evidence}`) : null,
        (p.expected_metric || p.expected_effect)
          ? el("div", { class: "opt-prop-expect" }, `→ ${p.expected_metric || ""} ${p.expected_effect || ""}`.trim())
          : null));
    }
    return wrap;
  }
  // Speciation plan: { cluster, niche, genome, queues }
  if (obj && obj.niche && Array.isArray(obj.queues)) {
    return el("div", { class: "spec-proposal" },
      el("div", { class: "spec-prop-head" },
        el("span", { class: "run-agent agent-speciation" }, obj.niche),
        el("span", { class: "spec-prop-arrow" }, "→"),
        el("span", { class: "spec-prop-cluster" }, `🧬 ${obj.cluster || ""}`)),
      el("div", { class: "spec-prop-queues" }, ...obj.queues.map((q) => el("span", { class: "spec-queue-chip" }, q))),
      Object.keys(obj.genome || {}).length
        ? el("div", { class: "opt-prop-expect" }, `genome: ${JSON.stringify(obj.genome)}`) : null);
  }
  // Fallback: plain text plan (normal code-change tickets).
  return el("div", { class: "cl-plan" }, typeof plan === "string" ? plan : JSON.stringify(plan));
}

async function openSymphonyDetail(id) {
  try {
    const c = await api(`/api/symphony/issues/${encodeURIComponent(id)}`);
    const body = el("div", {},
      el("h3", { class: "modal-title" }, c.title),
      el("p", { class: "modal-sub" }, `${c.status}${c.target_file ? " · " + c.target_file : ""}`),
    );
    if (c.hypothesis) body.appendChild(el("div", { class: "cl-field" },
      el("div", { class: "cl-label" }, "Hypothesis"), el("div", { class: "cl-value" }, c.hypothesis)));
    if (c.plan) body.appendChild(el("div", { class: "cl-field" },
      el("div", { class: "cl-label" }, "Plan"), symPlanEl(c.plan)));
    const meta = [];
    if (c.planner_id) meta.push(`planner: ${c.planner_id}`);
    if (c.assignee_id) meta.push(`implementer: ${c.assignee_id}`);
    if (c.branch_name) meta.push(`branch: ${c.branch_name}`);
    if (c.ci_status) meta.push(`ci: ${c.ci_status}`);
    if (meta.length) body.appendChild(el("div", { class: "modal-note" }, meta.join("  ·  ")));
    if (c.worktree) body.appendChild(el("div", { class: "modal-note" },
      `worktree: ${c.worktree.path} @ ${c.worktree.sha}`));
    if (c.pr_url) {
      body.appendChild(el("div", { class: "cl-field" },
        el("div", { class: "cl-label" }, c.pr_simulated ? "Simulated PR" : "Pull request"),
        c.pr_simulated
          ? el("span", { class: "cl-target" }, c.pr_url)
          : el("a", { class: "sym-pr", href: c.pr_url, target: "_blank", rel: "noopener" }, c.pr_url)));
    }
    body.appendChild(el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: dismissModal }, "Close")));
    openModal(body);
  } catch (err) {
    // A stale deep-link (e.g. an issue id from before a server restart) returns
    // 404 — surface it inline + drop back to the board instead of a blocking alert.
    if (location.hash.startsWith("#issue/")) navigate("symphony", { replace: true });
    openModal(el("div", {},
      el("h3", { class: "modal-title" }, "Issue unavailable"),
      el("p", { class: "modal-sub" }, `Could not load this issue (${err.message}). It may have been cleared.`),
      el("div", { class: "modal-actions" },
        el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Close"))));
  }
}

// =========================================================================== //
// Breeds view (ADR-0099): global selective pressure + the telemetry firewall
// (proven live) + per-cluster niches with their breeds + results.
// =========================================================================== //

let BREEDS_WIRED = false;

function wireBreeds() {
  $("#breeds-refresh").addEventListener("click", loadBreeds);
  $("#goal-add-btn").addEventListener("click", () => openGoalModal(null));
  $("#mig-add-btn").addEventListener("click", openMigrateModal);
}

// Start a governed queue migration from the UI. Populates queue + target-cluster
// dropdowns from live workloads/clusters; the source cluster is inferred from
// the chosen queue's current placement.
async function openMigrateModal() {
  const [wl, clusters] = await Promise.all([
    api("/api/workloads").catch(() => ({})),
    api("/api/clusters").catch(() => ({ clusters: [] })),
  ]);
  // queue -> the cluster it currently runs on (from a running producer/consumer)
  const queueCluster = {};
  for (const kind of ["producers", "consumers"]) {
    for (const w of (wl[kind] || [])) {
      if (w.status === "Running" && w.queue) queueCluster[w.queue] = w.cluster || "helix";
    }
  }
  const queues = Object.keys(queueCluster).sort();
  const liveClusters = (clusters.clusters || [])
    .filter((c) => c.status === "Live")
    .map((c) => c.name);

  if (!queues.length) {
    openModal(el("div", {},
      el("h3", { class: "modal-title" }, "Migrate a queue"),
      el("p", { class: "modal-sub" }, "No running queues to migrate. Add a producer/consumer first."),
      el("div", { class: "modal-actions" },
        el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Close"))));
    return;
  }

  const queueSel = el("select", {}, ...queues.map((q) => el("option", { value: q }, q)));
  const fromLabel = el("div", { class: "mig-from-note" });
  const updateFrom = () => { fromLabel.textContent = `currently on helix-${queueCluster[queueSel.value] || "helix"}`; };
  queueSel.addEventListener("change", () => { updateFrom(); syncTargets(); });

  const toSel = el("select", {});
  function syncTargets() {
    const src = queueCluster[queueSel.value] || "helix";
    toSel.innerHTML = "";
    const targets = liveClusters.filter((c) => c !== src);
    if (!targets.length) {
      toSel.appendChild(el("option", { value: "" }, "(no other live cluster — create one first)"));
    } else {
      for (const c of targets) toSel.appendChild(el("option", { value: c }, `helix-${c}`));
    }
  }
  updateFrom(); syncTargets();

  const reasonI = el("textarea", {
    class: "goal-english", rows: "2",
    placeholder: "Why move it? e.g. “latency-sensitive niche — move off the throughput-tuned shared cluster”"
  });
  const msg = el("div", { class: "modal-note" });

  const submit = el("button", {
    class: "cp-btn cp-btn-primary", onClick: async () => {
      const to_cluster = toSel.value;
      if (!to_cluster) { msg.textContent = "No target cluster available — create a Live cluster first."; return; }
      msg.textContent = "Migrating (recreating workloads on the target, stopping the old)…";
      submit.disabled = true;
      const res = await post("/api/migrations", {
        queue: queueSel.value,
        from_cluster: queueCluster[queueSel.value] || "helix",
        to_cluster,
        reason: reasonI.value.trim() || "manual migration via control plane",
      });
      if (res.ok && res.data.ok) {
        msg.innerHTML = "";
        msg.appendChild(el("span", { class: "goal-en-ok" },
          `✓ ${res.data.status} — moved ${(res.data.moved || []).length} workload(s) to helix-${to_cluster}`));
        setTimeout(() => { closeModal(); loadBreeds(); }, 900);
      } else {
        submit.disabled = false;
        msg.textContent = "✕ " + (res.data.detail || res.data.step || `HTTP ${res.status}`);
      }
    }
  }, "Migrate");

  openModal(el("div", {},
    el("h3", { class: "modal-title" }, "Migrate a queue"),
    el("p", { class: "modal-sub" }, "Relocate a queue's producers + consumers to a niche cluster. The topic is recreated on the target and the workloads re-pointed; the source sheds the load. Governed + recorded."),
    modalField("Queue", queueSel),
    el("div", { class: "mig-from-row" }, fromLabel),
    modalField("Target cluster", toSel),
    modalField("Reason", reasonI),
    msg,
    el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Cancel"), submit)));
}

async function loadBreeds() {
  try {
    const data = await api("/api/breeds/board");
    renderGlobalGoals(data.global_goals || []);
    renderFirewall(data.firewall || {});
    renderBreedClusters(data.clusters || []);
  } catch (err) {
    $("#breeds-clusters").innerHTML = "";
    $("#breeds-clusters").appendChild(el("div", { class: "error-box" }, `Failed to load breeds: ${err.message}`));
  }
  loadMigrations();
}

const MIG_STATUS_CLS = {
  Done: "mig-done", Failed: "mig-failed",
  Migrating: "mig-migrating", Requested: "mig-requested", Approved: "mig-approved",
};

async function loadMigrations() {
  const root = $("#breeds-migrations");
  if (!root) return;
  try {
    const data = await api("/api/migrations");
    const migs = data.migrations || [];
    root.innerHTML = "";
    if (!migs.length) {
      root.appendChild(el("div", { class: "mig-empty" },
        "No migrations yet — a migration moves a queue's workloads to a niche cluster (speciation)."));
      return;
    }
    for (const m of migs) root.appendChild(migrationCard(m));
  } catch (err) {
    root.innerHTML = "";
    root.appendChild(el("div", { class: "error-box" }, `Failed to load migrations: ${err.message}`));
  }
}

function migrationCard(m) {
  const cls = MIG_STATUS_CLS[m.status] || "mig-requested";
  const moved = (m.moved ? String(m.moved).split(",").map((s) => s.trim()).filter(Boolean) : []);
  return el("div", { class: "mig-card" },
    el("div", { class: "mig-top" },
      el("span", { class: "mig-queue" }, m.queue || "(queue)"),
      el("span", { class: "mig-route" },
        el("span", { class: "mig-from" }, `helix-${m.from_cluster || "?"}`),
        el("span", { class: "mig-arrow" }, "→"),
        el("span", { class: "mig-to" }, `helix-${m.to_cluster || "?"}`)),
      el("span", { class: `mig-status ${cls}` }, m.status || "—")),
    m.reason ? el("div", { class: "mig-reason" }, m.reason) : null,
    moved.length ? el("div", { class: "mig-moved" },
      ...moved.map((w) => el("span", { class: "mig-moved-chip" }, w))) : null);
}

function renderGlobalGoals(goals) {
  const root = $("#breeds-global-goals");
  root.innerHTML = "";
  if (!goals.length) {
    root.appendChild(el("div", { class: "niche-empty" }, "No global fitness goals set."));
    return;
  }
  for (const g of goals) {
    root.appendChild(el("div", { class: "goal-card goal-global" },
      el("div", { class: "goal-metric" }, g.metric || "—",
        el("span", { class: "goal-scope-pill goal-scope-global" }, "global")),
      el("div", { class: "goal-dir" }, `${g.direction || "?"}${g.target ? " → " + g.target : ""}`),
      el("div", { class: "goal-meta" }, `${g.status || ""}${g.generation ? " · gen " + g.generation : ""}`),
      el("div", { class: "goal-actions" },
        el("button", { class: "cl-mini-btn", onClick: () => openGoalModal(g) }, "Edit"),
        el("button", { class: "cl-mini-btn goal-remove", onClick: () => removeGoal(g) }, "Remove"))));
  }
}

// Add (g=null) or Edit (g=existing) a global goal. Edit = governed retire+recreate.
function openGoalModal(g) {
  const editing = !!g;
  const metricI = el("input", { type: "text", placeholder: "throughput", value: g ? (g.metric || "") : "" });
  const dirSel = el("select", {},
    el("option", { value: "maximize" }, "maximize"),
    el("option", { value: "minimize" }, "minimize"));
  if (g && g.direction) dirSel.value = g.direction;
  const targetI = el("input", { type: "text", placeholder: "60000 / min-pods", value: g ? (g.target || "") : "" });
  const msg = el("div", { class: "modal-note" });

  const submit = el("button", {
    class: "cp-btn cp-btn-primary", onClick: async () => {
      const body = {
        metric: metricI.value.trim(), direction: dirSel.value,
        target: targetI.value.trim(), scope: "global"
      };
      msg.textContent = editing ? "Superseding goal (retire + recreate)…" : "Creating goal…";
      const res = editing
        ? await fetch(`/api/goals/${encodeURIComponent(g.id)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
        }).then(r => r.json().then(d => ({ ok: r.ok, data: d })))
        : await post("/api/goals", body);
      if (res.ok && res.data.ok) { closeModal(); loadBreeds(); }
      else msg.textContent = "Denied: " + (res.data.detail || res.data.step || `HTTP ${res.status}`);
    }
  }, editing ? "Save (supersede)" : "Add goal");

  const children = [
    el("h3", { class: "modal-title" }, editing ? "Edit global goal" : "Add global goal"),
    el("p", { class: "modal-sub" }, editing
      ? "Changing selective pressure retires this goal and creates a new one — a governed, auditable supersede."
      : "A system-wide fitness goal the breeder optimizes the whole population toward."),
  ];

  // Intent-driven entry (add only): state an OPEN-ENDED goal; the agent discovers
  // which existing Datadog metrics serve it, flags any blindspot, and — for a
  // blindspot — can instrument a new metric for real (ADR-0101).
  if (!editing) {
    const intentI = el("textarea", {
      class: "goal-english", rows: "2",
      placeholder: "Describe the goal in plain English — e.g. “make Helix cheaper to run, less CPU per message” or “keep p95 latency under 40ms”. The agent finds (or creates) the metrics to measure it."
    });
    const cov = el("div", { class: "goal-coverage" });   // discovered metrics + blindspot
    const enMsg = el("div", { class: "modal-note" });

    const enBtn = el("button", {
      class: "cp-btn cp-btn-primary", onClick: async () => {
        const text = intentI.value.trim();
        if (!text) { enMsg.textContent = "Describe a goal first."; return; }
        enMsg.textContent = "Agent analyzing telemetry coverage…";
        cov.innerHTML = "";
        const res = await post("/api/goals/intent", { intent: text });
        if (!(res.ok && res.data.ok)) { enMsg.textContent = "✕ " + (res.data.detail || `HTTP ${res.status}`); return; }
        const d = res.data;
        enMsg.innerHTML = "";
        enMsg.appendChild(el("span", { class: "goal-en-ok" }, `✓ goal set — ${d.echo || ""}`));
        // Show what the agent discovered.
        const tracked = d.tracked_metrics || [];
        cov.appendChild(el("div", { class: "cov-row" },
          el("span", { class: "cov-label" }, "tracking"),
          ...(tracked.length
            ? tracked.map((m) => el("span", { class: "cov-metric" }, m))
            : [el("span", { class: "cov-none" }, "no existing metric covers this")])));
        if (d.primary && d.primary.metric) {
          cov.appendChild(el("div", { class: "cov-row" },
            el("span", { class: "cov-label" }, "primary"),
            el("span", { class: "cov-primary" }, `${d.primary.metric} · ${d.primary.direction}`)));
        }
        if (d.blindspot) {
          const pm = d.proposed_metric || {};
          const blindBox = el("div", { class: "cov-blindspot" },
            el("div", { class: "cov-blind-title" }, "⚠ Blindspot — this goal needs a metric that doesn’t exist yet"),
            el("div", { class: "cov-blind-reason" }, d.blindspot_reason || ""),
            pm.name ? el("div", { class: "cov-proposed" }, `proposed: `,
              el("span", { class: "cov-metric" }, pm.name),
              el("span", { class: "cov-proposed-desc" }, pm.desc ? ` — ${pm.desc}` : "")) : null);
          const instBtn = el("button", { class: "cp-btn cp-btn-primary cov-instrument" }, "Instrument it (live)");
          const instMsg = el("div", { class: "modal-note" });
          instBtn.addEventListener("click", async () => {
            instMsg.textContent = "Editing helix metrics.rs + cargo check (this is a real code change)…";
            instBtn.disabled = true;
            const ir = await post("/api/goals/instrument",
              { metric_name: pm.name, rationale: d.blindspot_reason, goal_id: d.id });
            if (ir.ok && ir.data.ok) {
              instMsg.innerHTML = "";
              instMsg.appendChild(el("span", { class: "goal-en-ok" },
                `✓ edit compiles (${ir.data.cargo_check}); building + deploying to helix-cluster-001 in the background → see Agent Runs`));
            } else {
              instBtn.disabled = false;
              instMsg.textContent = "✕ " + (ir.data.detail || ir.data.step || `HTTP ${ir.status}`);
            }
          });
          blindBox.appendChild(el("div", { class: "cov-instrument-row" }, instBtn, instMsg));
          cov.appendChild(blindBox);
        }
        loadBreeds();  // the new goal is live; refresh the board behind the modal
      }
    }, "Set goal from intent");

    children.push(
      el("div", { class: "goal-english-block" },
        el("div", { class: "modal-field" },
          el("span", { class: "modal-field-cap" }, "Goal (plain English)"), intentI),
        el("div", { class: "goal-en-actions" }, enBtn, enMsg),
        cov),
      el("div", { class: "goal-or" }, el("span", {}, "or set the structured fields directly")));
  }

  children.push(
    modalField("Metric", metricI),
    el("div", { class: "modal-row" }, modalField("Direction", dirSel), modalField("Target", targetI)),
    msg,
    el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Cancel"), submit));

  openModal(el("div", {}, ...children));
}

async function removeGoal(g) {
  if (!confirm(`Remove global goal "${g.metric}/${g.direction}"? This retires it (the breeder stops optimizing toward it).`)) return;
  const res = await post(`/api/goals/${encodeURIComponent(g.id)}/retire`, {});
  if (!(res.ok && res.data.ok)) alert("Remove denied: " + (res.data.detail || `HTTP ${res.status}`));
  loadBreeds();
}

function renderFirewall(fw) {
  const root = $("#breeds-firewall");
  root.innerHTML = "";
  if (!fw.available) {
    root.appendChild(el("div", { class: "fw-note" }, fw.note || "Firewall status unavailable."));
    return;
  }
  for (const c of fw.checks || []) {
    root.appendChild(el("div", { class: `fw-item ${c.denied ? "fw-denied" : "fw-allowed"}` },
      el("span", { class: "fw-icon" }, c.denied ? "🔒" : "⚠"),
      `read ${c.entity} → ${c.code}${c.denied ? " denied" : ""}`));
  }
  root.appendChild(el("div", { class: "fw-note" },
    fw.all_denied
      ? "Breeder is Cedar-forbidden from reading workload shapes (403 + GovernanceDecision). It infers niches from Datadog telemetry alone."
      : "⚠ Firewall not fully enforced — breeder can read some workload entities."));
}

const BREED_STATUS_CLS = {
  Promoted: "breed-promoted", Culled: "breed-culled",
  Verifying: "breed-verifying", Building: "breed-building", Proposed: "breed-proposed",
};

function renderBreedClusters(clusters) {
  const root = $("#breeds-clusters");
  root.innerHTML = "";
  if (!clusters.length) {
    root.appendChild(el("div", { class: "niche-empty" }, "No clusters or breeds yet."));
    return;
  }
  for (const c of clusters) {
    const head = el("div", { class: "niche-head" },
      el("span", { class: "niche-name" }, `helix-${c.name}`),
      c.niche ? el("span", { class: "niche-label" }, c.niche) : null,
      el("span", { class: "niche-goals" },
        (c.goals || []).length
          ? (c.goals || []).map((g) => `${g.metric} ${g.direction}`).join("  ·  ")
          : "no per-niche goal"));
    const body = el("div", { class: "niche-body" });
    const breeds = c.breeds || [];
    if (!breeds.length) {
      body.appendChild(el("div", { class: "niche-empty" }, "No breeds produced for this cluster yet."));
    } else {
      for (const b of breeds) body.appendChild(breedCard(b));
    }
    root.appendChild(el("div", { class: "niche-card" }, head, body));
  }
}

function breedCard(b) {
  const cls = BREED_STATUS_CLS[b.status] || "breed-proposed";
  // Stop link clicks from bubbling to the card's onClick (which opens the diff
  // modal). Without this, clicking the Symphony link would open both views.
  const issueLink = b.issue_id
    ? el("a", {
      class: "breed-issue-link", href: `#issue/${encodeURIComponent(b.issue_id)}`,
      title: "open the Symphony ticket",
      onClick: (e) => e.stopPropagation(),
    }, `▸ ${b.issue_id}`)
    : null;
  const top = el("div", { class: "breed-top" },
    el("span", { class: "breed-gene" }, b.gene || "(gene)"),
    el("span", { class: `breed-status ${cls}` }, b.status || "Proposed"),
    issueLink);

  const children = [top];
  if (b.motivation) children.push(el("div", { class: "breed-motivation" }, b.motivation));

  // Result row: perf delta, CI, or cull reason.
  const result = el("div", { class: "breed-result" });
  if (b.perf_delta) {
    // A latency reduction (e.g. "-22% p95") is GOOD; "improvement" should be
    // colored by outcome, not by sign. Treat unsafe/regress/fail/culled as bad,
    // otherwise a promoted breed (or explicit improvement) as good.
    const s = String(b.perf_delta).toLowerCase();
    const bad = /regress|slower|unsafe|worse|fail|↓/.test(s) || b.status === "Culled";
    const good = !bad && (b.status === "Promoted" || /improv|faster|better|↑/.test(s) || /-\d/.test(s));
    result.appendChild(el("span", { class: good ? "breed-perf-up" : "breed-perf-down" }, `Δ ${b.perf_delta}`));
  }
  if (b.ci_status) {
    const pass = /pass|ok|green/i.test(String(b.ci_status));
    result.appendChild(el("span", { class: pass ? "breed-ci-pass" : "breed-ci-fail" }, `CI: ${b.ci_status}`));
  }
  if (b.status === "Culled" && b.cull_reason) {
    result.appendChild(el("span", { class: "breed-cull-reason" }, `✕ ${b.cull_reason}`));
  }
  if (result.childNodes.length) children.push(result);

  return el("div", {
    class: "breed-card",
    title: "Click to view this variant's code diff",
    onClick: () => openBreedDiff(b),
  }, ...children);
}

// --------------------------------------------------------------------------- //
// Per-variant code diff modal: shows the unified diff between the variant's
// commit (`df-cluster/<cluster_id>`) and its parent on the lineage tip. Renders
// with the same diff2html infrastructure as the cluster-detail Code Diff panel.
// --------------------------------------------------------------------------- //

let BREED_DIFF = null; // {text, target}

function setBreedDiffView(mode) {
  if (!BREED_DIFF || !BREED_DIFF.text) return;
  $("#breed-view-side").classList.toggle("active", mode === "side-by-side");
  $("#breed-view-line").classList.toggle("active", mode === "line-by-line");
  drawDiff(BREED_DIFF.target, BREED_DIFF.text, mode);
}

async function openBreedDiff(b) {
  BREED_DIFF = null;
  const breedId = b.id || "(unknown)";
  const titleNode = el("div", { class: "modal-title" }, `Variant: ${b.gene || breedId}`);
  const subBits = [breedId];
  if (b.cluster_id && b.cluster_id !== "none") subBits.push(`cluster: ${b.cluster_id}`);
  if (b.status) subBits.push(`status: ${b.status}`);
  if (b.perf_delta) subBits.push(`Δ ${b.perf_delta}`);
  const subNode = el("div", { class: "modal-sub" }, subBits.join("  ·  "));
  const motivationNode = b.motivation
    ? el("div", { class: "breed-motivation", style: "margin-bottom:14px;" }, b.motivation)
    : null;

  const toolbar = el("div", { class: "diff-toolbar", style: "margin-bottom:8px;" },
    el("button", { class: "diff-btn active", id: "breed-view-side",
      onClick: () => setBreedDiffView("side-by-side") }, "Side-by-side"),
    el("button", { class: "diff-btn", id: "breed-view-line",
      onClick: () => setBreedDiffView("line-by-line") }, "Unified"));
  const meta = el("div", { class: "diff-meta" }, "Loading diff…");
  const target = el("div", { id: "breed-diff-target" });
  const closeRow = el("div", { class: "modal-actions" },
    el("button", { class: "cp-btn", onClick: closeModal }, "Close"));

  const children = [titleNode, subNode];
  if (motivationNode) children.push(motivationNode);
  children.push(toolbar, meta, target, closeRow);
  openModal(el("div", {}, ...children), { wide: true });

  let d;
  try {
    d = await api(`/api/breeds/${encodeURIComponent(breedId)}/diff`);
  } catch (err) {
    meta.innerHTML = `<span class="cl-muted">diff unavailable: ${esc(err.message)}</span>`;
    return;
  }

  if (!d.available || !d.unified_diff) {
    meta.innerHTML = "";
    target.innerHTML = "";
    target.appendChild(el("div", { class: "diff-none" },
      d.reason ? `No diff: ${d.reason}.` : "No code diff available for this variant."));
    return;
  }

  const statSummary = (d.stat || [])
    .map((s) => `${s.file} (+${s.additions} −${s.deletions})`)
    .join("   ");
  meta.innerHTML =
    `<b>base</b> ${esc(d.base)}  →  <b>branch</b> ${esc(d.branch)}` +
    (statSummary ? `<br>${esc(statSummary)}` : "");

  BREED_DIFF = { text: d.unified_diff, target };
  drawDiff(target, d.unified_diff, "side-by-side");
}

// =========================================================================== //
// Agent Runs view (ADR-0100): reverse-chron timeline of every loop-agent pass
// with a summary + links to what each run produced.
// =========================================================================== //

let RUNS_WIRED = false;
let RUNS_FILTER = "all";
let RUNS_CACHE = [];

let SCHED_TIMER = null;

function wireRuns() {
  $("#runs-refresh").addEventListener("click", loadRuns);
  $("#sched-toggle").addEventListener("click", toggleScheduler);
  loadScheduler();
  // poll scheduler + runs while the Runs tab is open (countdowns + new ticks).
  if (SCHED_TIMER) clearInterval(SCHED_TIMER);
  SCHED_TIMER = setInterval(() => {
    if (!$("#view-runs").classList.contains("hidden")) { loadScheduler(); loadRuns(); }
  }, 5000);
}

async function loadScheduler() {
  try {
    const s = await api("/api/scheduler");
    renderScheduler(s);
  } catch { /* scheduler optional */ }
}

// Agents shown in the Agent Runs tab. The other scheduler agents
// (observer/researcher/symphony/breeder/evolution) still exist in the backend
// but aren't used in this demo, so they're hidden from the UI. Frontend-only —
// the backend scheduler config is untouched.
const SHOWN_AGENTS = new Set(["speciation", "speciation-executor", "optimizer", "optimizer-executor", "meta-optimizer"]);

function renderScheduler(s) {
  const statusEl = $("#sched-status");
  statusEl.textContent = s.running ? "running" : "stopped";
  statusEl.className = `sched-status ${s.running ? "sched-on" : "sched-off"}`;
  const tog = $("#sched-toggle");
  tog.textContent = s.running ? "Stop loop" : "Start loop";
  tog.className = `cp-btn ${s.running ? "cp-btn-ghost" : "cp-btn-primary"}`;

  const root = $("#sched-agents");
  root.innerHTML = "";
  for (const a of (s.agents || []).filter((a) => SHOWN_AGENTS.has(a.agent))) {
    const stat = a.ticking ? "ticking" : (a.last_status || "—");
    const statCls = a.ticking ? "sched-laststat-ticking"
      : a.last_status === "ok" ? "sched-laststat-ok"
        : a.last_status === "failed" ? "sched-laststat-failed" : "";
    const intervalI = el("input", {
      type: "number", min: "30", value: String(a.interval),
      title: "interval seconds", onChange: async (e) => {
        await post(`/api/scheduler/agent/${a.agent}`, { interval: parseInt(e.target.value, 10) });
        loadScheduler();
      }
    });
    const enableCb = el("input", { type: "checkbox", title: "enabled" });
    enableCb.checked = !!a.enabled;
    enableCb.addEventListener("change", async () => {
      await post(`/api/scheduler/agent/${a.agent}`, { enabled: enableCb.checked });
      loadScheduler();
    });
    root.appendChild(el("div", { class: "sched-row" },
      el("span", { class: `sched-agent-name run-agent ${AGENT_CLS[a.agent] || "agent-symphony"}` }, a.agent),
      el("span", { class: "sched-interval" }, "every ", intervalI, "s"),
      el("span", { class: "sched-next" },
        s.running && a.enabled ? (a.ticking ? "running now…" : `next in ${a.next_in}s`) : "—",
        a.last_status ? el("span", { class: `sched-laststat ${statCls}`, style: "margin-left:10px" }, `last: ${stat}`) : null),
      el("label", { class: "sched-enable" }, enableCb, "on"),
      el("button", {
        class: "sched-runnow", onClick: async (e) => {
          e.target.disabled = true; e.target.textContent = "…";
          await post(`/api/scheduler/agent/${a.agent}/run-now`, {});
          setTimeout(() => { loadScheduler(); loadRuns(); }, 1500);
        }
      }, "run now")));
  }
}

async function toggleScheduler() {
  const s = await api("/api/scheduler").catch(() => ({ running: false }));
  if (s.running) {
    await post("/api/scheduler/stop", {});
  } else {
    if (!confirm("Start the autonomous loop?\n\nWhile running, the agents will edit Helix code and deploy to GKE on their intervals, unattended. Stop it anytime.")) return;
    await post("/api/scheduler/start", {});
  }
  loadScheduler();
}

const AGENT_CLS = {
  observer: "agent-observer", researcher: "agent-researcher",
  symphony: "agent-symphony", breeder: "agent-breeder",
  evolution: "agent-evolution", speciation: "agent-speciation",
  optimizer: "agent-optimizer", "optimizer-executor": "agent-optimizer",
  "speciation-executor": "agent-speciation", "meta-optimizer": "agent-meta",
};
const AGENT_DESC = {
  speciation: "Reads telemetry, proposes niche-tuned cluster placements, and migrates queues onto them.",
};
const RUN_STATUS_CLS = { Succeeded: "run-succeeded", Failed: "run-failed", Running: "run-running" };

// Map a produced-entity ref to the right in-app deep link.
function producedLink(ref) {
  if (!ref) return null;
  if (ref.startsWith("breed-")) return { href: "#breeds", label: ref };          // breeds tab
  if (ref.startsWith("deploy-")) return { href: "#observe", label: ref };        // deployment history
  // ImprovementIssue ids -> the Symphony issue detail
  return { href: `#issue/${encodeURIComponent(ref)}`, label: ref };
}

async function loadRuns() {
  try {
    const data = await api("/api/runs");
    RUNS_CACHE = data.runs || [];
    renderRunsFilter(data.agents || []);
    renderRuns();
  } catch (err) {
    $("#runs-timeline").innerHTML = "";
    $("#runs-timeline").appendChild(el("div", { class: "error-box" }, `Failed to load runs: ${err.message}`));
  }
}

function renderRunsFilter(agents) {
  const root = $("#runs-filter");
  root.innerHTML = "";
  const mk = (key, label) => el("button", {
    class: `runs-filter-btn ${RUNS_FILTER === key ? "active" : ""}`,
    onClick: () => { RUNS_FILTER = key; renderRunsFilter(agents); renderRuns(); },
  }, label);
  root.appendChild(mk("all", "All"));
  // Only show filter chips for the agents we surface in this demo.
  for (const a of agents.filter((a) => SHOWN_AGENTS.has(a))) root.appendChild(mk(a, a));
}

function renderRuns() {
  const root = $("#runs-timeline");
  root.innerHTML = "";
  // Hide runs from agents not used in this demo (observer/researcher/etc.).
  const runs = RUNS_CACHE
    .filter((r) => SHOWN_AGENTS.has(r.agent_type))
    .filter((r) => RUNS_FILTER === "all" || r.agent_type === RUNS_FILTER);
  if (!runs.length) {
    root.appendChild(el("div", { class: "niche-empty" }, "No agent runs recorded yet."));
    return;
  }
  for (const r of runs) root.appendChild(runCard(r));
}

function runCard(r) {
  const agentCls = AGENT_CLS[r.agent_type] || "agent-symphony";
  const statusCls = RUN_STATUS_CLS[r.status] || "run-running";

  const left = el("div", { class: "run-left" },
    el("span", { class: `run-agent ${agentCls}` }, r.agent_type || "agent"),
    el("span", { class: `run-status ${statusCls}` }, r.status || "Running"),
    el("span", { class: "run-when" }, r.started_at ? fmtTime(r.started_at) : ""));

  const right = el("div", { class: "run-right" });
  if (r.trigger) right.appendChild(el("div", { class: "run-trigger" }, `▸ ${r.trigger}`));
  if (r.summary) right.appendChild(el("div", { class: "run-summary" }, r.summary));

  // Metrics chips
  const m = r.metrics || {};
  const mkeys = Object.keys(m);
  if (mkeys.length) {
    const mrow = el("div", { class: "run-metrics" });
    for (const k of mkeys) mrow.appendChild(el("span", { class: "run-metric" }, `${k}: ${m[k]}`));
    right.appendChild(mrow);
  }

  // Produced-entity links
  if ((r.produced || []).length) {
    const prow = el("div", { class: "run-produced" },
      el("span", { class: "run-produced-label" }, "produced"));
    for (const ref of r.produced) {
      const lk = producedLink(ref);
      if (lk) prow.appendChild(el("a", {
        class: "run-link", href: lk.href,
        onClick: (e) => e.stopPropagation()
      }, lk.label));
    }
    right.appendChild(prow);
  }

  return el("div", { class: "run-card", onClick: () => openRunDetail(r.id) }, left, right);
}

// Parse the speciation summary's "niche→cluster←[q1,q2]; ..." proposals into
// structured rows so the run detail reads as a plan, not a run-on paragraph.
function parseSpeciationProposals(summary) {
  if (!summary) return null;
  const ix = summary.indexOf("ticket(s):");
  if (ix < 0) return null;
  const tail = summary.slice(ix + "ticket(s):".length);
  const items = [];
  for (const chunk of tail.split(";")) {
    // Format: "<niche>→<cluster>←[q1,q2]". Split ONLY on the arrow glyphs
    // (→ and ←), never on hyphens — niche/cluster names contain hyphens.
    const m = chunk.match(/\s*([^→]+?)\s*→\s*([^←]+?)\s*←\s*\[(.*?)\]/);
    if (!m) continue;
    items.push({
      niche: m[1].trim(),
      cluster: m[2].trim(),
      queues: m[3].split(",").map((q) => q.trim()).filter(Boolean),
    });
  }
  return items.length ? items : null;
}

function speciationProposalsEl(items) {
  const wrap = el("div", { class: "spec-proposals" });
  for (const p of items) {
    wrap.appendChild(el("div", { class: "spec-proposal" },
      el("div", { class: "spec-prop-head" },
        el("span", { class: "run-agent agent-speciation" }, p.niche),
        el("span", { class: "spec-prop-arrow" }, "→"),
        el("span", { class: "spec-prop-cluster" }, `🧬 ${p.cluster}`)),
      el("div", { class: "spec-prop-queues" },
        ...p.queues.map((q) => el("span", { class: "spec-queue-chip" }, q)))));
  }
  return wrap;
}

async function openRunDetail(id) {
  try {
    const r = await api(`/api/runs/${encodeURIComponent(id)}`);
    const body = el("div", {},
      el("h3", { class: "modal-title" }, `${r.agent_type} run`),
      el("p", { class: "modal-sub" }, `${r.status}${r.started_at ? " · " + fmtTime(r.started_at) : ""}`));
    if (r.trigger) body.appendChild(el("div", { class: "cl-field" },
      el("div", { class: "cl-label" }, "Trigger"), el("div", { class: "cl-value" }, r.trigger)));
    const proposals = parseSpeciationProposals(r.summary);
    if (proposals) {
      body.appendChild(el("div", { class: "cl-field" },
        el("div", { class: "cl-label" }, `Placement proposals (${proposals.length})`),
        speciationProposalsEl(proposals)));
    } else if (r.summary) {
      body.appendChild(el("div", { class: "cl-field" },
        el("div", { class: "cl-label" }, "Summary"), el("div", { class: "cl-plan" }, r.summary)));
    }
    const m = r.metrics || {};
    if (Object.keys(m).length) {
      const mrow = el("div", { class: "run-metrics" });
      for (const k of Object.keys(m)) mrow.appendChild(el("span", { class: "run-metric" }, `${k}: ${m[k]}`));
      body.appendChild(el("div", { class: "cl-field" }, el("div", { class: "cl-label" }, "Metrics"), mrow));
    }
    if ((r.produced || []).length) {
      const prow = el("div", { class: "run-produced" });
      for (const ref of r.produced) {
        const lk = producedLink(ref);
        if (lk) prow.appendChild(el("a", { class: "run-link", href: lk.href, onClick: closeModal }, lk.label));
      }
      body.appendChild(el("div", { class: "cl-field" }, el("div", { class: "cl-label" }, "Produced"), prow));
    }
    body.appendChild(el("div", { class: "modal-actions" },
      el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Close")));
    openModal(body);
  } catch (err) {
    openModal(el("div", {},
      el("h3", { class: "modal-title" }, "Run unavailable"),
      el("p", { class: "modal-sub" }, `Could not load this run (${err.message}).`),
      el("div", { class: "modal-actions" },
        el("button", { class: "cp-btn cp-btn-ghost", onClick: closeModal }, "Close"))));
  }
}

// --------------------------------------------------------------------------- //
// Evolution progress panel
// --------------------------------------------------------------------------- //

let _evoTimer = null;

function startEvolutionPolling() {
  loadEvolutionStatus();
  if (!_evoTimer) _evoTimer = setInterval(loadEvolutionStatus, 5000);
}

function stopEvolutionPolling() {
  if (_evoTimer) { clearInterval(_evoTimer); _evoTimer = null; }
}

async function loadEvolutionStatus() {
  let data;
  try {
    data = await api("/api/evolution/status");
  } catch {
    return;
  }
  renderEvolutionStatus(data);
}

const _OUTCOME_STYLE = {
  survived:       { bg: "#d1fae5", color: "#065f46", icon: "✓" },
  dst_culled:     { bg: "#fee2e2", color: "#991b1b", icon: "✗ DST" },
  fitness_culled: { bg: "#fef3c7", color: "#92400e", icon: "✗ fit" },
  no_data:        { bg: "#f3f4f6", color: "#6b7280", icon: "–" },
  unknown:        { bg: "#f3f4f6", color: "#6b7280", icon: "?" },
};

const _PHASE_PILLS = {
  idle:        { cls: "pill-muted",   label: "Idle" },
  running:     { cls: "pill-ok",      label: "Running" },
  building:    { cls: "pill-pending", label: "Building" },
  observing:   { cls: "pill-pending", label: "Observing" },
  champion:    { cls: "pill-ok",      label: "Champion!" },
  cooldown:    { cls: "pill-muted",   label: "Cooldown" },
  no_champion: { cls: "pill-muted",   label: "No Champion" },
  error:       { cls: "pill-fail",    label: "Error" },
};

function renderEvolutionStatus(d) {
  const phase      = d.phase || "idle";
  const isIdle     = phase === "idle";
  const badgeInfo  = _PHASE_PILLS[phase] || { cls: "pill-muted", label: phase };

  // Phase badge + message
  const badge = $("#evo-phase-badge");
  badge.className  = `pill ${badgeInfo.cls}`;
  badge.textContent = badgeInfo.label;
  $("#evo-message").textContent = d.message || "";

  if (d.updated_at) {
    const ts = new Date(d.updated_at);
    $("#evo-updated-at").textContent = `Last updated: ${ts.toLocaleTimeString()}`;
  }

  if (isIdle || !d.stage) {
    $("#evo-stage-block").classList.add("hidden");
    $("#evo-champion-banner").classList.add("hidden");
    $("#evo-history-block").classList.add("hidden");
    return;
  }

  // Stage progress block
  $("#evo-stage-block").classList.remove("hidden");
  const varCount   = d.variant_count || 0;
  const maxVar     = d.max_variants  || 12;
  const pct        = Math.min(100, Math.round(varCount / maxVar * 100));
  $("#evo-progress-bar").style.width  = pct + "%";
  $("#evo-stage-label").textContent   = `Stage ${d.stage}`;
  $("#evo-variant-count").textContent = `${varCount} / ${maxVar} variants`;
  $("#evo-baseline").textContent      = d.baseline ? `Baseline: ${d.baseline}` : "";

  const linPill = $("#evo-lineage-label");
  linPill.textContent = `${d.lineage || "?"} lineage`;
  linPill.className   = "pill " + (d.lineage === "latency" ? "pill-pending" : "pill-ok");

  const goalPill = $("#evo-goal-label");
  goalPill.textContent = `${d.direction || "?"} ${d.metric || ""}`;
  goalPill.className   = "pill pill-muted";

  // Champion banner
  if (d.champion_found) {
    $("#evo-champion-banner").classList.remove("hidden");
    const delta = d.champion_delta_pct != null ? `${d.champion_delta_pct > 0 ? "+" : ""}${d.champion_delta_pct.toFixed(1)}%` : "";
    $("#evo-champion-title").textContent = `Champion found! Variant ${d.champion_variant_num}${delta ? " · " + delta : ""}`;
    $("#evo-champion-sub").textContent = `Merged to champion-metrics-pranav-clone`;
  } else {
    $("#evo-champion-banner").classList.add("hidden");
  }

  // History table
  const history = d.history || [];
  if (history.length) {
    $("#evo-history-block").classList.remove("hidden");
    const tbody = $("#evo-history-body");
    tbody.innerHTML = "";
    // Newest first
    for (const row of [...history].reverse().slice(0, 40)) {
      const style = _OUTCOME_STYLE[row.outcome] || _OUTCOME_STYLE.unknown;
      const delta = row.delta_pct != null ? `${row.delta_pct > 0 ? "+" : ""}${row.delta_pct.toFixed(1)}%` : "–";
      const tr = document.createElement("tr");
      tr.style.borderBottom = "1px solid #f3f4f6";
      tr.innerHTML = `
        <td style="padding:5px 8px;font-weight:500;">v${row.variant}</td>
        <td style="padding:5px 8px;color:#6b7280;">S${row.stage}</td>
        <td style="padding:5px 8px;">
          <span style="background:${style.bg};color:${style.color};border-radius:4px;padding:2px 6px;font-size:11px;">${style.icon} ${row.outcome}</span>
        </td>
        <td style="padding:5px 8px;font-variant-numeric:tabular-nums;">${delta}</td>
        <td style="padding:5px 8px;color:#374151;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${row.description || ''}">${row.description || "–"}</td>
      `;
      tbody.appendChild(tr);
    }
  } else {
    $("#evo-history-block").classList.add("hidden");
  }
}

// Boot last, after all module-level declarations are initialized.
boot();
