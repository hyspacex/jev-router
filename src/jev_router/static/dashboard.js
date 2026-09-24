"use strict";

const $ = (id) => document.querySelector(`#${CSS.escape(id)}`);
let snapshot = {decisions: []};
let selected = null;
let groupFilter = "";
let token = "";
let paused = false;
let disconnected = false;
let controller = null;
let timer = null;

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}
function present(value) { return value === null || value === undefined || value === "" ? "—" : String(value); }
function group(row) {
  if (row.session_id) return `session:${row.session_id}`;
  if (row.conversation_key) return `conversation:${row.conversation_key}`;
  return "unlinked";
}
function groupLabel(key) {
  if (key === "unlinked") return "No session identity";
  const [kind, ...parts] = key.split(":");
  const id = parts.join(":");
  return kind === "session" ? id : `Legacy · ${id.slice(0, 12)}`;
}
function eventType(row) { return row.event_type || "alias_request"; }
function fallback(row) { return Boolean(row.fallback || row.fallback_index); }
function outcome(row) {
  const parts = [];
  if (row.upstream_status) parts.push(`HTTP ${row.upstream_status}`);
  if (row.stream_state) parts.push(row.stream_state);
  if (!parts.length) {
    if (row.event_type === "admission") parts.push("Binding prepared");
    else if (row.event_type === "effort_plan") parts.push("Plan recorded");
    else parts.push("No outcome recorded yet");
  }
  if (fallback(row)) parts.push("fallback");
  return parts.join(" · ");
}
function addPair(target, label, value) {
  const line = node("div", undefined, "pair");
  line.append(node("dt", label), node("dd", present(value)));
  target.append(line);
}
function jsonSection(target, title, value) {
  if (value === null || value === undefined || (typeof value === "object" && !Object.keys(value).length)) return;
  const details = node("details");
  details.append(node("summary", title), node("pre", JSON.stringify(value, null, 2)));
  target.append(details);
}
function choices(id, field, title) {
  const el = $(id);
  const previous = el.value;
  const values = [...new Set(snapshot.decisions.map(field).filter(Boolean))].sort();
  if (previous && !values.includes(previous)) values.push(previous);
  el.replaceChildren(new Option(title, ""), ...values.map(value => new Option(value, value)));
  el.value = previous;
}
function humanize(value) { return String(value).replaceAll("_", " "); }
function meter(target, label, value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) return;
  const line = node("div", undefined, "judgment-meter");
  const heading = node("div", undefined, "meter-heading");
  heading.append(node("span", label), node("strong", `${Math.round(value * 100)}%`));
  const bar = node("progress");
  bar.max = 1;
  bar.value = value;
  bar.setAttribute("aria-label", label);
  line.append(heading, bar);
  target.append(line);
}
function judgments(target, answers) {
  const grid = node("div", undefined, "judgments");
  for (const [name, answer] of Object.entries(answers || {})) {
    if (!answer || typeof answer !== "object") continue;
    const card = node("article", undefined, "judgment-card");
    card.append(node("span", humanize(name), "judgment-name"));
    const value = answer[answer.type];
    if (answer.type === "choice") card.append(node("strong", humanize(present(value)), "judgment-value"));
    else if (answer.type === "score" && typeof value === "number") {
      card.append(node("strong", String(Number(value.toFixed(2))), "judgment-value"), node("small", "Score · question-specific scale, not a percentage"));
    } else if (answer.type === "noul") {
      meter(card, "Judgment probability", value);
    } else card.append(node("strong", "Unrecognized answer", "judgment-value"));
    if (answer.type !== "noul") meter(card, "Confidence", answer.confidence);
    if (answer.probabilities && Object.keys(answer.probabilities).length) {
      const distribution = node("details");
      distribution.append(node("summary", "Answer distribution"));
      for (const [label, probability] of Object.entries(answer.probabilities)) meter(distribution, humanize(label), probability);
      card.append(distribution);
    }
    grid.append(card);
  }
  target.append(grid);
}
function ruleSummary(target, row) {
  const card = node("section", undefined, "rule-card");
  card.append(node("span", "ROUTING RULE", "eyebrow"), node("h3", humanize(row.rule || "Not recorded")));
  const definition = row.rule_definition;
  if (definition?.when && Object.keys(definition.when).length) {
    const list = node("ul", undefined, "conditions");
    const operators = {gte: "≥", lte: "≤", gt: ">", lt: "<", eq: "=", in: "is one of", conf_gte: "confidence ≥"};
    for (const [name, condition] of Object.entries(definition.when)) {
      let text = `${humanize(name)} = ${present(condition)}`;
      if (condition && typeof condition === "object" && !Array.isArray(condition)) {
        text = `${humanize(name)} ${Object.entries(condition).map(([op, value]) => `${operators[op] || humanize(op)} ${Array.isArray(value) ? value.join(", ") : present(value)}`).join("; ")}`;
      }
      list.append(node("li", text));
    }
    card.append(list, node("small", "Configured conditions. Any recorded quota shifts are listed separately below; this is not a new policy evaluation."));
  } else if (definition?.min_confidence !== undefined) {
    card.append(node("p", `Confidence gate · minimum ${definition.min_confidence}`));
  } else if (definition?.default) card.append(node("p", "The configured default route was selected."));
  else card.append(node("p", "No matching rule definition is available for this event.", "muted"));
  if (row.route) card.append(node("p", `Route → ${row.route}`));
  if (row.quality_lane) card.append(node("p", `Quality lane → ${row.quality_lane}`));
  target.append(card);
}
function renderDetail() {
  const target = $("detail");
  const expanded = new Set();
  if (target.dataset.event === String(selected)) {
    for (const details of target.querySelectorAll("details[open]")) expanded.add(details.querySelector("summary")?.textContent);
  }
  target.dataset.event = String(selected);
  target.replaceChildren();
  if (selected === null) {
    target.append(node("p", "Select an event to inspect its Jev answers, identity and routing evidence.", "muted"));
    return;
  }
  const row = snapshot.decisions.find(item => item.id === selected);
  if (!row) {
    target.append(node("p", "This event has left the loaded window. Increase the window or inspect the historical CLI log.", "muted"));
    return;
  }
  const hero = node("section", undefined, "decision-hero");
  hero.append(node("span", "RECORDED MODEL", "eyebrow"), node("h3", present(row.model)));
  const badges = node("div", undefined, "badges");
  badges.append(node("span", `Effort · ${present(row.effort)}${row.event_type === "execution" ? " (base)" : ""}`, "badge"), node("span", present(row.mode), "badge"));
  if (row.pinned) badges.append(node("span", "Pin reused", "badge"));
  if (fallback(row)) badges.append(node("span", "Fallback", "badge warning"));
  hero.append(badges, node("p", outcome(row), "outcome-summary"));
  target.append(hero);
  const identity = node("dl");
  for (const [label, key] of [["Log row (unique event)", "id"], ["Decision (may span turns)", "decision_id"], ["Client", "client"], ["Strict session", "session_id"], ["Conversation hash", "conversation_key"], ["Request", "request_id"], ["Turn", "turn_id"], ["Alias", "alias"], ["Rule", "rule"], ["Route", "route"], ["Mode", "mode"], ["Quality lane", "quality_lane"]]) addPair(identity, label, row[key]);
  const identifiers = node("details");
  identifiers.append(node("summary", "Request & session identifiers"), identity);
  if (row.pinned) target.append(node("p", "Pin reused: this request did not ask Jev to choose again.", "notice"));
  if (row.event_type === "execution") target.append(node("p", "Strict execution uses the existing binding, not a new model decision. Effort here is the logged base setting, not proof of an adaptive transition. Select the admission event in this session for its classification.", "notice"));
  if (row.event_type === "effort_plan") target.append(node("p", "An effort plan is a recommendation, not proof the provider applied it. The model stays fixed; use the session/plan ledger to inspect confirmation.", "notice"));
  if (row.fallback) target.append(node("p", "Classifier fallback was used. Recorded answers, if present, must not be treated as a successful Jev classification.", "notice"));
  if (row.mode === "shadow") target.append(node("p", "Shadow mode: a recorded policy choice is not proof that choice served the request. Check the outcome and intended-model fields.", "notice"));
  target.append(node("h3", "Jev judgments"), node("p", "Structured answers, not a written reasoning trace. Shadow answers are measured separately and do not drive the active choice.", "muted"));
  if (!row.answers || !Object.keys(row.answers).length) target.append(node("p", "No active classifier answers recorded for this event.", "muted"));
  judgments(target, row.answers);
  ruleSummary(target, row);
  jsonSection(target, "Shadow answers (not used)", row.shadow_answers);
  target.append(node("p", row.config_hash === snapshot.config_hash ? "Recorded config matches the running config." : "Config differs (or is unknown). Current rules cannot explain this historical decision exactly; use decisions replay with the matching config.", "notice"));
  jsonSection(target, "Matching configured rule / gate", row.rule_definition);
  jsonSection(target, "Recorded quota pressure", row.pressures);
  jsonSection(target, "Recorded threshold shifts", row.shifted);
  jsonSection(target, "Capability exclusions", row.exclusions);
  jsonSection(target, "Experimental routes", row.experiment_routes);
  const evidence = node("dl");
  for (const [label, key] of [["Counterfactual", "counterfactual"], ["Evidence strength", "evidence"], ["Qualification", "qualification_ref"], ["Intended model", "intended_model"], ["Intended effort", "intended_effort"], ["Fallback reason", "fallback_reason"], ["Jev latency (ms)", "jev_ms"], ["Response latency (ms)", "response_ms"], ["Input estimate (tokens)", "est_tokens"], ["Messages", "message_count"], ["Config hash", "config_hash"]]) addPair(evidence, label, row[key]);
  const evidenceDetails = node("details");
  evidenceDetails.append(node("summary", "Timing, fallback & evidence"), evidence);
  target.append(identifiers, evidenceDetails);
  const details = node("details");
  details.append(node("summary", "Full audit metadata"), node("pre", JSON.stringify(row, null, 2)));
  target.append(details);
  for (const panel of target.querySelectorAll("details")) panel.open = expanded.has(panel.querySelector("summary")?.textContent);
}
function render() {
  const rows = snapshot.decisions;
  $("total").textContent = rows.length;
  $("groups").textContent = new Set(rows.map(group).filter(key => key !== "unlinked")).size;
  $("models").textContent = new Set(rows.map(row => row.model).filter(Boolean)).size;
  $("fallbacks").textContent = rows.filter(fallback).length;
  $("window").textContent = `Latest ${snapshot.limit || $("limit").value} · ${snapshot.mode || "unknown mode"} · refreshes every 2s`;
  choices("alias", row => row.alias, "All aliases");
  choices("model", row => row.model, "All models");
  choices("event", eventType, "All events");
  const search = $("search").value.toLowerCase().trim();
  const filtered = rows.filter(row => (!$("alias").value || row.alias === $("alias").value) && (!$("model").value || row.model === $("model").value) && (!$("event").value || eventType(row) === $("event").value) && (!search || [row.client, row.session_id, row.request_id, row.turn_id, row.decision_id, row.conversation_key, row.model, row.rule, row.id].some(value => String(value ?? "").toLowerCase().includes(search))));
  const grouped = new Map();
  for (const row of filtered) grouped.set(group(row), (grouped.get(group(row)) || 0) + 1);
  const list = $("session-list");
  list.replaceChildren();
  const all = node("button", `All groups · ${filtered.length}`, groupFilter ? "group" : "group selected");
  all.type = "button";
  all.addEventListener("click", () => { groupFilter = ""; render(); });
  list.append(all);
  if (groupFilter && !grouped.has(groupFilter)) grouped.set(groupFilter, 0);
  for (const [key, count] of grouped) {
    const button = node("button", `${groupLabel(key)} · ${count}`, key === groupFilter ? "group selected" : "group");
    button.type = "button";
    button.title = key;
    button.addEventListener("click", () => { groupFilter = key; render(); });
    list.append(button);
  }
  const visible = filtered.filter(row => !groupFilter || group(row) === groupFilter);
  $("count").textContent = `${visible.length} of ${rows.length} loaded events`;
  $("empty").hidden = visible.length > 0;
  $("empty").textContent = rows.length ? "No events match these filters." : "No logged events yet. Concrete-model passthrough is not included.";
  const body = $("rows");
  body.replaceChildren();
  for (const row of visible) {
    const tr = node("tr", undefined, row.id === selected ? "selected" : "");
    const cells = [
      [new Date(row.ts * 1000).toLocaleTimeString(), `${eventType(row)} · #${row.id}`],
      [row.client || "Unlabelled client", groupLabel(group(row))],
      [present(row.model), `${present(row.effort)}${row.event_type === "execution" ? " · base" : ""}${row.pinned ? " · pinned" : ""}`],
      [present(row.rule), row.quality_lane || row.route || ""],
      [outcome(row), row.response_ms == null ? "" : `${Math.round(row.response_ms)} ms`],
    ];
    for (const [primary, secondary] of cells) {
      const td = node("td");
      td.append(node("span", primary), node("small", secondary));
      tr.append(td);
    }
    const td = node("td");
    const button = node("button", "Inspect");
    button.type = "button";
    button.setAttribute("aria-label", `Inspect event ${row.id}`);
    button.addEventListener("click", () => { selected = row.id; render(); });
    td.append(button);
    tr.append(td);
    body.append(tr);
  }
  renderDetail();
}
function cancel() {
  clearTimeout(timer);
  if (controller) controller.abort();
  controller = null;
}
async function refresh() {
  cancel();
  const current = new AbortController();
  controller = current;
  const timeout = setTimeout(() => current.abort(), 10000);
  try {
    const response = await fetch(`/router/audit?limit=${$("limit").value}`, {headers: token ? {"X-Router-Admin-Token": token} : {}, cache: "no-store", signal: current.signal});
    if (response.status === 401) throw new Error("Authentication required. Enter the router admin token and Connect.");
    if (!response.ok) throw new Error(`Router returned HTTP ${response.status}.`);
    const data = await response.json();
    if (controller !== current) return;
    snapshot = data;
    render();
    $("error").hidden = true;
    $("connection").textContent = `${paused ? "Paused · fetched" : "Live · updated"} ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (controller !== current) return;
    $("error").hidden = false;
    $("error").textContent = `${error.name === "AbortError" ? "Refresh timed out." : error.message} Displayed data may be stale.`;
    $("connection").textContent = "Disconnected / stale";
  } finally {
    clearTimeout(timeout);
    if (controller === current) {
      controller = null;
      if (!paused && !disconnected && !document.hidden) timer = setTimeout(refresh, 2000);
    }
  }
}
for (const id of ["search", "alias", "model", "event"]) $(id).addEventListener("input", render);
$("limit").addEventListener("change", () => { if (!disconnected) refresh(); });
$("reset").addEventListener("click", () => { for (const id of ["search", "alias", "model", "event"]) $(id).value = ""; groupFilter = ""; render(); });
$("connect").addEventListener("click", () => {
  token = $("token").value;
  $("token").value = "";
  disconnected = false;
  snapshot = {decisions: []}; selected = null; render();
  refresh();
});
$("disconnect").addEventListener("click", () => {
  disconnected = true; cancel(); token = ""; $("token").value = "";
  snapshot = {decisions: []}; selected = null; groupFilter = ""; render();
  $("connection").textContent = "Disconnected"; $("error").hidden = true;
});
$("pause").addEventListener("click", () => {
  paused = !paused;
  $("pause").textContent = paused ? "Resume" : "Pause";
  if (paused) { cancel(); $("connection").textContent = "Paused"; }
  else if (!disconnected) refresh();
});
$("refresh").addEventListener("click", () => { if (!disconnected) refresh(); });
document.addEventListener("visibilitychange", () => {
  if (document.hidden) cancel();
  else if (!paused && !disconnected) refresh();
});
refresh();
