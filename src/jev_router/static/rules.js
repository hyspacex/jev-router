"use strict";

// Rules. The ladder is the page: the running config's rules with their hits,
// the score rulers with the window's admissions on them and the settings in a
// side column, and router.yaml as text on demand: check, preview (a pure
// replay of stored admissions) and apply. The editor is built once and never
// rebuilt by the live refresh, so a draft survives it.

const RULE_OPS = {gte: "≥", gt: ">", lte: "≤", lt: "<", eq: "=", ne: "≠", conf_gte: "confidence ≥"};
const EDITOR_LH = 20;  // px; the editor's line height, as dashboard.css sets it
const EDITOR_PAD = 10; // px; its top padding
const RATED_ORDER = [null, "too_slow", "right", "too_weak", "too_strong"];
// workbench.PREVIEW_LIMIT: a preview replays at most this many admissions.
const PREVIEW_LIMIT = 200;
const WINDOW_WORDS = {"1h": "in the last hour", "24h": "in the last 24 h", "7d": "this week", "30d": "in the last 30 days"};
// Cutoffs closer than this share of a ruler draw as one tick.
const TICK_MERGE = 0.02;
const LABEL_ROW = 12; // px between label rows under a ruler

function rulesetNow() {
  const sets = (S.rules && S.rules.rulesets) || {};
  if (!sets[S.ruleset]) S.ruleset = sets.policy ? "policy" : Object.keys(sets)[0] || "policy";
  return sets[S.ruleset] || {rules: S.rules ? S.rules.rules || [] : [], low_confidence: null, default: null, fallback: null, aliases: []};
}

function windowWords() { return WINDOW_WORDS[S.rwin] || "in the last " + S.rwin; }

function renderRules(kind) {
  const main = $("main");
  if (!S.rules) { main.replaceChildren(); return; }
  if (!S.rulesRoot) {
    const root = el("section", "rv");
    const head = el("div", "head");
    const top = el("div", "grid rules");
    const work = el("div", "rwork");
    const report = el("div", "rrep");
    S.editor = buildEditor();
    add(work, S.editor.panel, report);
    add(root, head, top, work);
    // open: the owner asked for the editor. A draft or a report keeps it open.
    S.rulesRoot = {root, head, top, work, report, open: false};
  }
  const R = S.rulesRoot;
  if (main.firstChild !== R.root || main.children.length !== 1) main.replaceChildren(R.root);
  R.top.className = "grid rules" + (kind === "data" ? " still" : "");
  R.head.replaceChildren(...rulesHead());
  R.top.replaceChildren(ladderPanel(), add(el("div", "rside"), rulersPanel(), settingsPanel()));
  syncEditor();
  R.report.replaceChildren();
  if (S.report) R.report.append(previewPanel(S.report));
  R.work.classList.toggle("split", !!S.report);
  requestAnimationFrame(stackLabels);
}

// --- the editor's visibility ----------------------------------------------------

function editorPinned() { return S.draft !== null || !!S.report; }
function editorShown() { return !!S.rulesRoot && (S.rulesRoot.open || editorPinned()); }

function showEditor(on) {
  if (!S.rulesRoot) return;
  S.rulesRoot.open = on;
  syncShown();
}

function syncShown() {
  const R = S.rulesRoot;
  if (!R) return;
  const shown = editorShown();
  R.work.hidden = !shown;
  const b = R.head.querySelector(".edtoggle");
  if (!b) return;
  b.textContent = shown ? "Hide editor" : "Edit router.yaml";
  b.setAttribute("aria-expanded", shown ? "true" : "false");
  b.disabled = shown && editorPinned();
  b.title = b.disabled ? "Stays open while there is a draft or a preview; Revert to close" : "";
}

function rulesHead() {
  const out = [el("h1", null, "Rules")];
  const sets = Object.keys((S.rules && S.rules.rulesets) || {});
  if (sets.length > 1) {
    const seg = el("div", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Ruleset");
    for (const name of sets) {
      const b = button(name === S.ruleset ? "on" : null, name, () => { S.ruleset = name; renderRules("pane"); });
      b.setAttribute("aria-pressed", name === S.ruleset ? "true" : "false");
      seg.append(b);
    }
    out.push(seg);
  }
  const toggle = button("ghost edtoggle", "Edit router.yaml", () => {
    if (editorShown()) { if (!editorPinned()) showEditor(false); return; }
    showEditor(true);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    S.editor.panel.scrollIntoView({block: "start", behavior: still ? "auto" : "smooth"});
  });
  out.push(add(el("div", "right"), windowSeg(S.rwin, rulesHash), toggle));
  return out;
}

// --- the ladder -----------------------------------------------------------------

function whenParts(when) {
  const parts = [];
  for (const [key, cond] of Object.entries(when || {})) {
    const name = String(key).replace(/^has_/, "").replaceAll("_", " ");
    if (typeof cond === "boolean") { parts.push(cond ? name : "no " + name); continue; }
    if (Array.isArray(cond)) { parts.push(name + " ∈ " + cond.join(", ")); continue; }
    if (cond === null || typeof cond !== "object") { parts.push(name + " = " + plain(cond)); continue; }
    for (const [op, v] of Object.entries(cond)) {
      if (op === "in" || op === "not_in") parts.push(name + (op === "in" ? " ∈ " : " ∉ ") + (Array.isArray(v) ? v.join(", ") : plain(v)));
      else if (RULE_OPS[op]) parts.push(name + " " + RULE_OPS[op] + " " + cfg(v));
      else parts.push(name + " " + human(op) + " " + plain(v));
    }
  }
  return parts;
}

// Where a rule sends a request: `mid_tools → glm-5.3 · high`, the route's
// primary model and effort after the route; a direct model as it stands.
// Built as pieces that never break inside: a narrow cell wraps between the
// route and its model, never inside an identifier.
function useText(use) {
  if (!use) return null;
  const pair = (model, effort) => [model ? splitModel(model)[1] : null, effort].filter(Boolean).join(" · ");
  const node = el("span");
  if (use.route) {
    const route = ((S.rules && S.rules.routes) || {})[use.route];
    const primary = route && route.primary ? pair(route.primary.model, route.primary.effort) : "";
    add(node, el("span", "nw", use.route + (primary ? " →" : "")), primary ? " " : null, primary ? el("span", "nw", primary) : null);
    return node;
  }
  const direct = pair(use.model, use.effort);
  return direct ? add(node, el("span", "nw", direct)) : null;
}

// Names for each score's levels, by question, when router.yaml gives them.
function levelLabels() {
  const out = {};
  for (const [key, q] of Object.entries((S.rules && S.rules.questions) || {})) if (q && Array.isArray(q.labels)) out[key] = q.labels;
  return out;
}

// Where a `use` lands: the route's primary entry, or the model it names.
function sendsOf(use) {
  if (!use) return null;
  if (!use.route) return {route: null, model: use.model || null, effort: use.effort || null};
  const route = ((S.rules && S.rules.routes) || {})[use.route];
  const p = route && route.primary;
  return {route: use.route, model: p ? p.model : null, effort: p ? p.effort : null};
}

// The rule card's contents for a rule of the selected ladder.
function ruleInfo(set, r) {
  const rules = Array.isArray(set.rules) ? set.rules : [];
  const at = rules.indexOf(r);
  const open = r.line ? button("lk", "router.yaml line " + r.line, () => {
    closeRuleCard();
    if (!editorShown()) showEditor(true);
    focusLine(r.line);
  }) : null;
  return {
    name: r.name, index: r.index, count: rules.length, protected: r.protected, lane: r.lane,
    description: r.description, sentence: matchWords(whenConds(r.when), levelLabels()), sendsTo: sendsOf(r.use),
    before: rules.slice(0, Math.max(0, at)).map((x) => x.name), hits: r.hits, total: set.admissions, words: windowWords(), open,
  };
}

// What the previewed draft does to the selected ladder, rule by rule:
// {name: "differs" | "removed"} and the rules the draft adds.
function draftDelta(rules) {
  const draft = S.report && S.report.draft_rules ? S.report.draft_rules[S.ruleset] : null;
  if (!Array.isArray(draft)) return null;
  const now = new Map(rules.map((r) => [r.name, r]));
  const then = new Map(draft.map((r) => [r.name, r]));
  const changed = new Map();
  for (const r of rules) {
    const d = then.get(r.name);
    if (!d) changed.set(r.name, null);
    else if (JSON.stringify(d.when || {}) !== JSON.stringify(r.when || {})) changed.set(r.name, d);
  }
  const added = draft.filter((d) => !now.has(d.name));
  return changed.size || added.length ? {changed, added, rules: draft} : null;
}

// The line of the nearest `key:` before (step -1) or after (step 1) a line.
function keyLine(key, from, step) {
  const text = S.rules && S.rules.yaml;
  if (!text || !from) return null;
  const lines = text.split("\n");
  const re = new RegExp("^\\s*" + key + ":");
  for (let i = from - 1 + step; i >= 0 && i < lines.length && Math.abs(i - from) < 400; i += step) {
    if (re.test(lines[i])) return i + 1;
  }
  return null;
}

// A row or setting that points at a line of router.yaml: click or Enter
// opens the editor there.
function pointsAt(node, line, label) {
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  if (label) node.setAttribute("aria-label", label);
  const pick = () => {
    if (!editorShown()) showEditor(true);
    focusLine(line);
    if (node.classList.contains("tr")) node.classList.add("on");
  };
  node.addEventListener("click", (event) => { if (!event.target.closest("a")) pick(); });
  node.addEventListener("keydown", (event) => {
    if (event.target !== node) return;
    if (event.key === "Enter" || event.key === " ") { pick(); event.preventDefault(); }
  });
}

function ladderPanel() {
  const set = rulesetNow();
  const rules = Array.isArray(set.rules) ? set.rules : [];
  // With more than one ladder, the count says it is this ladder's share.
  const several = Object.keys(S.rules.rulesets || {}).length > 1;
  const words = windowWords();
  let count = null;
  if (!absent(set.admissions)) {
    if (!set.admissions) count = "no admissions " + words;
    else if (several && !absent(S.rules.admissions)) count = int(set.admissions) + " of " + plural(S.rules.admissions, "admission") + " " + words;
    else count = plural(set.admissions, "admission") + " " + words;
  }
  const box = band("Ladder", "first match wins" + (count ? " · " + count : ""), "ladderb");
  const tbl = el("div", "tbl ladder");
  const th = el("div", "tr th");
  th.setAttribute("aria-hidden", "true");
  add(th, el("span"), el("span", null, "Rule"), el("span", "when", "When"), el("span", "to", "Sends to"), el("span", "num", "Hits"));
  tbl.append(th);
  const delta = draftDelta(rules);
  const draftLine = (text) => tbl.append(el("div", "ldraft", text));
  const row = (o) => {
    const zero = !absent(o.hits) && !o.hits;
    const r = el("div", "tr" + (o.cls ? " " + o.cls : zero ? " zero" : "") + (o.line ? " go" : "") + (o.line && S.hl === o.line ? " on" : ""));
    if (o.line) pointsAt(r, o.line, o.name + ", line " + o.line + " of router.yaml");
    const conds = el("span", "when");
    if (o.cond.length) o.cond.forEach((c, i) => add(conds, i ? " · " : null, c));
    else conds.append(el("span", "always", "always"));
    conds.title = conds.textContent;
    const to = el("span", "to");
    if (o.use) to.append(o.use);
    else if (o.toText) to.append(el("span", "rlane", o.toText));
    if (o.lane) add(to, " ", el("span", "rlane nw", "· " + o.lane));
    to.title = to.textContent;
    const hits = el("span", "num share");
    const fill = el("i");
    fill.style.setProperty("--w", (absent(o.share) ? 0 : Math.max(o.share > 0 ? 2 : 0, o.share * 100)) + "%");
    hits.append(fill);
    if (absent(o.hits)) hits.append(gap("not counted"));
    else if (o.hits) hits.append(link("hn", int(o.hits), listHash({rule: o.filter, window: S.rwin}), plural(o.hits, "admission") + " by " + o.name + ": open the sessions"));
    else hits.append(el("span", "hn t3", "0"));
    add(r, el("span", "i", o.index), add(el("span", "n"), o.nameNode || o.name), conds, to, hits);
    tbl.append(r);
  };
  const first = rules.length ? rules[0].line : null;
  const last = rules.length ? rules[rules.length - 1].line : null;
  const gate = set.low_confidence;
  if (gate) {
    const qs = Array.isArray(gate.questions) ? gate.questions.map(human).join(" or ") : "";
    row({index: "", name: "confidence gate", filter: "low_confidence", cond: [cap((qs ? qs + " " : "") + "confidence under " + cfg(gate.min_confidence))],
      use: useText(gate.use), hits: gate.hits, share: gate.share, line: keyLine("low_confidence", first, -1), cls: "quiet"});
  }
  const labels = levelLabels();
  for (const r of rules) {
    const nameNode = add(el("span"), ruleName(r.name, () => ruleInfo(set, r)), r.protected ? el("span", "pill", "protected") : null,
      add(el("span", "ds"), r.description ? firstSentence(r.description) : el("span", "pill", "no description")));
    row({index: String(r.index), name: r.name, nameNode, filter: r.name, cond: [matchWords(whenConds(r.when), labels)], use: useText(r.use), lane: r.lane,
      hits: r.hits, share: r.share, line: r.line});
    if (delta && delta.changed.has(r.name)) {
      const d = delta.changed.get(r.name);
      draftLine(d ? "draft differs: " + (whenParts(d.when).join(" · ") || "always") : "draft removes " + r.name);
    }
  }
  if (delta) for (const d of delta.added) draftLine("draft adds: " + d.name + " · " + (whenParts(d.when).join(" · ") || "always"));
  if (set.default) {
    row({index: "", name: "default", filter: "default", cond: [], use: useText(set.default.use), hits: set.default.hits,
      share: set.default.share, line: keyLine("default", last, 1), cls: "quiet"});
  }
  if (set.fallback && set.fallback.hits) {
    row({index: "", name: "fallback", filter: "admission_fallback", cond: ["Jev failed"], toText: "the alias's admission fallback",
      hits: set.fallback.hits, share: set.fallback.share, line: null, cls: "quiet"});
  }
  box.append(tbl);
  return box;
}

// --- rulers -----------------------------------------------------------------------

function rulerKeys(set) {
  const out = new Map();
  (set.rules || []).forEach((r, at) => {
    const conds = Object.keys(r.when || {}).length;
    for (const [key, cond] of Object.entries(r.when || {})) {
      if (!cond || typeof cond !== "object" || Array.isArray(cond)) continue;
      for (const op of ["gte", "gt", "lte", "lt"]) {
        if (typeof cond[op] !== "number") continue;
        if (!out.has(key)) out.set(key, []);
        out.get(key).push({value: cond[op], rule: r.name, conds, at});
      }
    }
  });
  return out;
}

function rulersPanel() {
  const set = rulesetNow();
  const keys = rulerKeys(set);
  const aliases = new Set(Array.isArray(set.aliases) ? set.aliases : []);
  const rated = (Array.isArray(S.rules.rated) ? S.rules.rated : []).filter((r) => !aliases.size || aliases.has(r.alias));
  const words = windowWords();
  const box = band("Where admissions landed", rated.length ? int(rated.length) + " " + words : "none " + words, "rulersb");
  if (!keys.size) {
    box.append(el("p", "empty", "No score threshold in this ladder"));
    return box;
  }
  // The previewed draft's cutoffs, drawn beside the running ones while they differ.
  const delta = draftDelta(Array.isArray(set.rules) ? set.rules : []);
  const draft = delta ? rulerKeys({rules: delta.rules}) : new Map();
  for (const key of draft.keys()) if (!keys.has(key)) keys.set(key, []);
  for (const [key, ticks] of keys) {
    const mine = new Set(ticks.map((t) => t.value + "|" + t.rule));
    const extra = (draft.get(key) || []).filter((t) => !mine.has(t.value + "|" + t.rule));
    box.append(ruler(key, ticks, rated, extra));
  }
  const legend = el("div", "legend");
  for (const v of ["right", "too_weak", "too_strong"]) legend.append(verdictDot(v));
  legend.append(el("span", "dot t3", "unrated"));
  const k = S.rules.knife_edge;
  if (k && typeof k === "object") {
    const knife = el("span", "knife");
    const near = " within " + (absent(k.margin) ? "the margin" : num2(k.margin)) + " of a threshold";
    if (k.denominator) knife.textContent = int(k.numerator) + " of " + int(k.denominator) + near;
    else {
      const by = k.unassessed_by_reason && typeof k.unassessed_by_reason === "object" ? Object.entries(k.unassessed_by_reason) : [];
      const why = "no admission assessed" + (by.length ? " · " + by.map(([r, n]) => int(n) + " " + human(r)).join(", ") : "");
      add(knife, gap(why), near);
    }
    legend.append(knife);
  }
  box.append(legend);
  return box;
}

// Near-equal cutoffs (1.399 and 1.4) draw as one tick, labelled with the
// rule that tests nothing else, the latest such in the ladder.
function mergeTicks(list, draft, top) {
  const sorted = [...list].sort((a, b) => a.value - b.value);
  const groups = [];
  for (const t of sorted) {
    const g = groups[groups.length - 1];
    if (g && t.value - g[0].value <= TICK_MERGE * top) g.push(t);
    else groups.push([t]);
  }
  return groups.map((g) => {
    const shown = g.reduce((best, t) => (cfg(t.value).length < cfg(best.value).length ? t : best), g[0]);
    const name = g.reduce((best, t) => (t.conds < best.conds || (t.conds === best.conds && t.at > best.at) ? t : best), g[0]);
    const all = [...new Set(g.map((t) => cfg(t.value) + " " + t.rule))].join(", ");
    return {v: shown.value, text: cfg(shown.value) + " " + name.rule, all, draft};
  });
}

function ruler(key, ticks, rated, draftTicks) {
  const q = (S.rules.questions || {})[key] || {};
  const top = !absent(q.max) && q.max > 0 ? q.max : Math.max(1, ...ticks.map((t) => Math.ceil(t.value)), ...draftTicks.map((t) => Math.ceil(t.value)));
  const x = (v) => Math.max(0, Math.min(100, v / top * 100));
  const blk = el("div", "rul");
  blk.append(el("div", "name", cap(key)));
  // Dots stack upward where they would touch.
  const dots = rated.filter((r) => r.scores && typeof r.scores[key] === "number")
    .sort((a, b) => RATED_ORDER.indexOf(a.verdict || null) - RATED_ORDER.indexOf(b.verdict || null) || a.scores[key] - b.scores[key]);
  const placed = [];
  const field = el("div", "rfield");
  for (const r of dots) {
    const at = x(r.scores[key]);
    let level = 0;
    while (placed.some((p) => p.level === level && Math.abs(p.at - at) < 1.4)) level += 1;
    level = Math.min(level, 7);
    placed.push({at, level});
    const verdict = VERDICT_DOTS[r.verdict] ? VERDICT_DOTS[r.verdict][0] : "v-none";
    const who = r.title || (r.session_id ? short(r.session_id) : null);
    const words = VERDICT_DOTS[r.verdict] ? VERDICT_DOTS[r.verdict][1] : "unrated";
    const label = cap(key) + " " + num2(r.scores[key]) + " · " + words + (who ? " · " + who : "");
    const cls = "rdot " + verdict + (r.knife_edge === true ? " edge" : "");
    const dot = r.session_id ? link(cls, null, sessionHash(r.session_id), label) : el("span", cls);
    dot.title = label;
    dot.style.left = at + "%";
    dot.style.bottom = (level * 8) + "px";
    field.append(dot);
  }
  const levels = placed.length ? Math.max(...placed.map((p) => p.level)) + 1 : 1;
  field.style.height = (levels * 8 + 2) + "px";
  const scale = el("div", "rscale");
  scale.append(el("div", "rline"));
  const points = [...mergeTicks(ticks, false, top), ...mergeTicks(draftTicks || [], true, top)].sort((a, b) => a.v - b.v);
  // A first guess at the label rows, by distance; stackLabels measures them.
  const lastX = [];
  for (const p of points) {
    const tk = el("i", "rtk" + (p.draft ? " draft" : ""));
    tk.style.left = x(p.v) + "%";
    let row = lastX.findIndex((e) => x(p.v) - e >= 22);
    if (row < 0) row = lastX.length;
    lastX[row] = x(p.v);
    const lb = el("span", "rlb" + (p.draft ? " draft" : "") + (row ? " row2" : ""), p.text);
    lb.title = (p.draft ? "draft · " : "") + p.all;
    lb.style.left = x(p.v) + "%";
    lb.style.top = (13 + row * LABEL_ROW) + "px";
    lb.dataset.at = String(x(p.v));
    scale.append(tk, lb);
  }
  scale.style.height = (14 + Math.max(1, lastX.length) * LABEL_ROW) + "px";
  add(blk, field, scale);
  // The level names under the scale, when router.yaml gives them and they span it.
  if (Array.isArray(q.labels) && q.labels.length - 1 === top) {
    const levels = el("div", "rlevels");
    q.labels.forEach((name, i) => {
      const s = el("span", i === 0 ? "l0" : i === q.labels.length - 1 ? "ln" : null, name);
      s.style.left = x(i) + "%";
      levels.append(s);
    });
    blk.append(levels);
  }
  return blk;
}

// Threshold labels stay inside the ruler (the first and last shift inward)
// and drop a row where they would touch, by their drawn width.
function stackLabels() {
  for (const scale of document.querySelectorAll(".rv .rscale")) {
    const width = scale.clientWidth;
    if (!width) continue;
    const ends = [];
    for (const lb of scale.querySelectorAll(".rlb")) {
      const w = lb.offsetWidth;
      const want = Number(lb.dataset.at) / 100 * width - w / 2;
      const left = Math.max(0, Math.min(width - w, want));
      let row = ends.findIndex((e) => left >= e + 8);
      if (row < 0) row = ends.length;
      ends[row] = left + w;
      lb.style.left = left + "px";
      lb.style.transform = "none";
      lb.style.top = (13 + row * LABEL_ROW) + "px";
      lb.classList.toggle("row2", row > 0);
    }
    scale.style.height = (14 + Math.max(1, ends.length) * LABEL_ROW) + "px";
  }
}

// Quota and adaptive effort, as the YAML sets them. Read-only: edit the text.
function settingsPanel() {
  const d = S.rules;
  const box = band("Settings", null, "setb");
  const kv = el("div", "kv sets");
  const line = (label, text, at) => {
    const k = el("span", "k", label);
    const v = el("span", "v", text);
    if (at) {
      pointsAt(v, at, label + ", line " + at + " of router.yaml");
      k.classList.add("go");
      v.classList.add("go");
      k.addEventListener("click", () => v.click());
    }
    add(kv, k, v);
  };
  const q = d.quota_policy || {};
  const none = Array.isArray(d.quota_sources) && !d.quota_sources.length;
  line("Quota", (none ? "no source enabled · " : "") + (q.enabled === false ? "off" : "demote above " + cfg(q.demote_above) + " · exhausted at " + cfg(q.exhausted_at) + "%"),
    keyLine("quota_policy", 1, 1) || (d.yaml && /^quota_policy:/.test(d.yaml) ? 1 : null));
  const a = d.adaptive_effort || {};
  if (a.mode === "off" || !a.mode) line("Adaptive effort", "off", a.line);
  else {
    const t = a.thresholds || {};
    const ladder = Array.isArray(a.ladder) ? a.ladder.join(" → ") : "";
    const up = [["difficulty", t.upgrade_difficulty], ["harm", t.upgrade_harm], ["corrective", t.corrective_followup]]
      .filter(([, v]) => !absent(v)).map(([k, v]) => k + " " + cfg(v)).join(", ");
    line("Adaptive effort", [a.mode, ladder, up ? "raise at " + up : null,
      absent(a.downgrade_confirmations) ? null : "lower after " + a.downgrade_confirmations].filter(Boolean).join(" · "), a.line);
  }
  box.append(kv);
  return box;
}

// --- the editor --------------------------------------------------------------------

function buildEditor() {
  const box = band("router.yaml", null, "editor");
  const head = box.firstChild;
  const hash = el("span", "mono t3 edhash");
  const tools = el("div", "acts");
  const revert = button("ghost", "Revert", () => {
    S.draft = null;
    S.edres = null;
    S.report = null;
    S.confirming = false;
    syncEditor(true);
    renderRules("pane");
  });
  const check = button("ghost", "Check", () => runCheck());
  const previewB = button("ghost", "Preview", () => runPreview());
  const apply = button("ghost primary", "Apply", () => { S.confirming = true; syncEditor(); });
  add(tools, revert, check, previewB, apply);
  add(head, hash, tools);
  const moved = el("p", "edmoved");
  const ed = el("div", "ed");
  const gut = el("pre", "gut");
  gut.setAttribute("aria-hidden", "true");
  const bar = el("div", "hlb");
  const ta = el("textarea", "yta");
  ta.spellcheck = false;
  ta.setAttribute("wrap", "off");
  ta.setAttribute("aria-label", "router.yaml");
  ta.setAttribute("autocapitalize", "off");
  ta.setAttribute("autocomplete", "off");
  ta.addEventListener("input", () => {
    S.draft = ta.value;
    if (S.rules && ta.value === S.rules.yaml) S.draft = null;
    S.hl = null;
    S.confirming = false;
    if (S.edres && S.edres.kind !== "errors") S.edres = null;
    gutter();
    syncEditor();
    const rep = document.querySelector(".rv .prv");
    if (rep) rep.classList.add("stale");
  });
  ta.addEventListener("scroll", () => { gut.scrollTop = ta.scrollTop; placeBand(); });
  add(ed, gut, bar, ta);
  const res = el("div", "edres");
  box.append(moved, ed, res);
  return {panel: box, hash, ta, gut, band: bar, moved, res, revert, check, preview: previewB, apply, lines: 0};
}

function gutter() {
  const E = S.editor;
  const n = E.ta.value.split("\n").length;
  if (n === E.lines) return;
  E.lines = n;
  const out = [];
  for (let i = 1; i <= n; i++) out.push(i);
  E.gut.textContent = out.join("\n") + "\n";
  E.gut.scrollTop = E.ta.scrollTop;
}

function placeBand() {
  const E = S.editor;
  if (!S.hl) { E.band.hidden = true; return; }
  E.band.hidden = false;
  E.band.style.top = (EDITOR_PAD + (S.hl - 1) * EDITOR_LH - E.ta.scrollTop) + "px";
}

// Scroll the editor to a line of router.yaml and mark it.
function focusLine(line) {
  const E = S.editor;
  if (!E || !line) return;
  S.hl = line;
  E.ta.scrollTop = Math.max(0, (line - 1) * EDITOR_LH - E.ta.clientHeight / 3);
  E.gut.scrollTop = E.ta.scrollTop;
  placeBand();
  for (const r of document.querySelectorAll(".rv .ladder .tr.on")) r.classList.remove("on");
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const box = E.ta.getBoundingClientRect();
  if (box.top < 0 || box.bottom > window.innerHeight) E.panel.scrollIntoView({block: "start", behavior: still ? "auto" : "smooth"});
}

function previewLabel() {
  const n = S.rules ? S.rules.admissions : null;
  if (absent(n) || !n) return "Preview";
  return "Preview on " + plural(Math.min(n, PREVIEW_LIMIT), "admission");
}

// The editor follows the server until the owner types; then it never does.
function syncEditor(force) {
  const E = S.editor;
  const d = S.rules;
  if (!E || !d) return;
  const text = typeof d.yaml === "string" ? d.yaml : null;
  if (S.draft === null) {
    if (force || E.ta.value !== (text || "")) {
      const top = E.ta.scrollTop;
      E.ta.value = text || "";
      E.ta.scrollTop = top;
    }
    S.draftBase = d.config_hash;
  }
  E.ta.disabled = text === null;
  gutter();
  placeBand();
  const dirty = S.draft !== null;
  E.hash.replaceChildren();
  if (text === null) E.hash.append(gap("text not available" + (d.yaml_source ? " · " + human(d.yaml_source) : "")));
  else add(E.hash, hash8(S.draftBase), dirty ? el("span", "edited", " · edited") : null);
  E.revert.hidden = !dirty;
  // Apply waits for a Check or Preview that passed on exactly this text.
  E.apply.disabled = !dirty || !!S.busy || S.passed !== draftText();
  E.apply.title = E.apply.disabled && dirty && !S.busy ? "Check or Preview this draft first" : "";
  E.check.disabled = E.preview.disabled = text === null || !!S.busy;
  E.check.textContent = S.busy === "check" ? "Checking…" : "Check";
  E.preview.textContent = S.busy === "preview" ? "Replaying…" : previewLabel();
  E.moved.hidden = !(dirty && d.config_hash !== S.draftBase);
  E.moved.textContent = E.moved.hidden ? "" : "config changed on the server · now " + hash8(d.config_hash);
  E.res.replaceChildren(...editorResult());
  syncShown();
}

function editorResult() {
  const out = [];
  if (S.confirming) {
    const bar = el("div", "confirm");
    add(bar, el("span", null, "Replace router.yaml and reload?"),
      button("ghost primary", S.busy === "apply" ? "Applying…" : "Apply", () => runApply(false)),
      button("ghost", "Cancel", () => { S.confirming = false; syncEditor(); }));
    out.push(bar);
  }
  const r = S.edres;
  if (!r) return out;
  if (r.kind === "valid") out.push(add(el("p", "edok"), el("span", "ok", "Valid"), el("span", "t3", " · " + hash8(r.hash))));
  if (r.kind === "errors") {
    const list = el("div", "errs");
    for (const e of r.errors || []) {
      const row = el("div", "err");
      add(row, e.line ? button("eline mono", "line " + e.line, () => focusLine(e.line)) : add(el("span", "eline"), gap("no line")), el("span", "emsg", e.message || "invalid"));
      list.append(row);
    }
    out.push(list);
  }
  if (r.kind === "applied") {
    const b = r.body || {};
    const p = el("div", "edok applied");
    if (b.applied) {
      add(p, el("span", "ok", "Applied"), el("span", "mono", " " + hash8(b.previous_hash) + " → " + hash8(b.config_hash)));
      if (b.backup_path) add(p, el("span", "t3", " · backup "), el("span", "mono path", b.backup_path));
      if (Array.isArray(b.restart_required) && b.restart_required.length) add(p, el("span", "warnt", " · restart for " + b.restart_required.join(", ")));
    } else add(p, el("span", "t3", "No change · " + hash8(b.config_hash)));
    out.push(p);
  }
  if (r.kind === "refused") {
    const p = el("div", "errs");
    const row = add(el("div", "err"), add(el("span", "eline"), r.status ? String(r.status) : gap("no status")), el("span", "emsg", (r.body && r.body.error) || "refused"));
    if (r.body && r.body.on_disk_hash) row.append(button("ghost badb", "Overwrite", () => runApply(true)));
    p.append(row);
    out.push(p);
  }
  return out;
}

function draftText() { return S.draft !== null ? S.draft : (S.rules && S.rules.yaml) || ""; }

async function runCheck() {
  if (S.busy) return;
  S.busy = "check";
  syncEditor();
  const text = draftText();
  try {
    const response = await post("/router/config/preview", {yaml: text, window: S.rwin, limit: 1});
    const body = await response.json().catch(() => ({}));
    if (response.status === 401) return unauthorized();
    if (response.ok && body.valid) {
      S.edres = {kind: "valid", hash: body.config_hash_draft};
      S.passed = text;
    }
    else if (Array.isArray(body.errors)) S.edres = {kind: "errors", errors: body.errors};
    else S.edres = {kind: "refused", status: response.status, body};
  } catch (error) {
    S.edres = {kind: "refused", status: "", body: {error: "router unreachable"}};
  } finally {
    S.busy = null;
    syncEditor();
  }
}

async function runPreview() {
  if (S.busy) return;
  S.busy = "preview";
  syncEditor();
  const text = draftText();
  try {
    const response = await post("/router/config/preview", {yaml: text, window: S.rwin});
    const body = await response.json().catch(() => ({}));
    if (response.status === 401) return unauthorized();
    if (response.ok && body.valid) {
      S.report = body;
      S.passed = text;
      S.edres = null;
    } else if (Array.isArray(body.errors)) {
      S.edres = {kind: "errors", errors: body.errors};
      S.report = null;
    } else S.edres = {kind: "refused", status: response.status, body};
  } catch (error) {
    S.edres = {kind: "refused", status: "", body: {error: "router unreachable"}};
  } finally {
    S.busy = null;
    if (S.view === "rules") renderRules("pane");
  }
}

async function runApply(force) {
  if (S.busy) return;
  S.busy = "apply";
  syncEditor();
  const body = {yaml: draftText(), base_hash: S.draftBase};
  if (force) body.force = true;
  try {
    const response = await post("/router/config/apply", body);
    const reply = await response.json().catch(() => ({}));
    if (response.status === 401) return unauthorized();
    S.confirming = false;
    if (response.ok) {
      S.edres = {kind: "applied", body: reply};
      if (reply.applied || reply.unchanged) { S.draft = null; S.report = null; S.hl = null; }
    } else if (response.status === 422 && Array.isArray(reply.errors)) S.edres = {kind: "errors", errors: reply.errors};
    else S.edres = {kind: "refused", status: response.status, body: reply};
  } catch (error) {
    S.edres = {kind: "refused", status: "", body: {error: "router unreachable"}};
  } finally {
    S.busy = null;
    syncEditor();
    if (S.edres && S.edres.kind === "applied") { S.rulesJson = ""; refresh("pane"); }
  }
}

// --- the preview report --------------------------------------------------------------

// What a change does to a rated admission: [words, colour class].
const EFFECT = {
  resolved_too_weak: ["resolved", "ok"], resolved_too_strong: ["resolved", "ok"],
  conflicts: ["conflict", "warnt"], right_changed: ["changed", "warnt"], unranked: ["unranked", "t3"],
};

// A replayed choice: rule · model effort. Given the choice before it, each
// part that moved is in the warning colour and each part that stayed muted.
function choiceText(c, was) {
  const node = el("span", "ch");
  if (!c || c.error) return add(node, gap(c && c.error ? human(c.error) : "not replayed"));
  const [, name] = splitModel(c.model);
  const tone = (key) => (was === undefined ? "was" : !was || was.error || was[key] !== c[key] ? "chg" : "same");
  const model = add(el("span", "nw"), el("span", "t3", "· "), el("span", "mono " + tone("model"), name || "?"),
    c.effort ? el("span", tone("effort"), " " + c.effort) : null);
  add(node, el("span", "mono nw " + tone("rule"), c.rule || "?"), " ", model);
  return node;
}

function previewPanel(r) {
  const box = band("Preview", "last " + (r.window || S.rwin), "prv");
  if (!r.evaluated) {
    box.append(el("p", "empty", "No admission in this window"));
    return box;
  }
  const head = el("div", "pvh");
  // The preview replays every ruleset's admissions; say so when there are several.
  const several = Object.keys((S.rules && S.rules.rulesets) || {}).length > 1;
  add(head, el("span", "pvk", int(r.changed) + " of " + int(r.evaluated)),
    el("span", "pvw", (r.evaluated === 1 ? "admission" : "admissions") + (several ? " (all rulesets)" : "") + " would change"));
  if (r.unreplayable) head.append(el("span", "t3 s12", "· " + int(r.unreplayable) + " not replayable"));
  if (Array.isArray(r.diffs) && r.diffs.length) {
    add(head, add(el("span", "pvkey"), el("span", "arrow up", "▲"), " stronger · ", el("span", "arrow down", "▼"), " weaker · ",
      el("span", "arrow", "→"), " no order"));
  }
  box.append(head);

  // Model share before → after, one row per model either side names.
  const before = r.tier_share_before || {};
  const after = r.tier_share_after || {};
  const models = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  const shares = el("div", "shr");
  for (const m of models) {
    const a = before[m] || 0;
    const b = after[m] || 0;
    const row = el("div", "shrow" + (Math.abs(a - b) > 1e-9 ? " moved" : ""));
    const bars = el("span", "shb");
    const one = el("i", "was");
    one.style.width = (a * 100) + "%";
    const two = el("i", "now");
    two.style.width = (b * 100) + "%";
    add(bars, add(el("span"), one), add(el("span"), two));
    add(row, el("span", "mono shm", m), bars, el("span", "mono shv", pct(a) + " → " + pct(b)));
    shares.append(row);
  }
  if (models.length) shares.append(add(el("div", "shrow shof"), el("span"), el("span", "t3", "of " + plural(r.evaluated, "admission")), el("span")));
  const qa = r.quota_saved_before || {};
  const qb = r.quota_saved_after || {};
  const quota = el("div", "shrow" + (qa.saved_rate !== qb.saved_rate ? " moved" : ""));
  add(quota, el("span", "shm", "Frontier quota saved"), el("span"),
    add(el("span", "mono shv"), absent(qa.saved_rate) ? gap("not recorded") : pct(qa.saved_rate), " → ", absent(qb.saved_rate) ? gap("not recorded") : pct(qb.saved_rate)));
  shares.append(quota);
  box.append(shares);

  const f = r.feedback || {};
  const marks = el("div", "fbm");
  const mark = (n, text, cls) => { if (n) marks.append(el("span", "dot " + cls, int(n) + " " + text)); };
  mark(f.resolved_too_weak, "resolved too weak", "ok");
  mark(f.resolved_too_strong, "resolved too strong", "ok");
  mark(f.conflicts, f.conflicts === 1 ? "conflict" : "conflicts", "warnt");
  mark(f.right_changed, "good changed", "warnt");
  mark(f.unranked, "unranked", "t3");
  if (marks.children.length) box.append(marks);

  const diffs = Array.isArray(r.diffs) ? r.diffs : [];
  const list = el("div", "dfl");
  if (!diffs.length) list.hidden = true;
  for (const d of diffs) {
    const row = el("div", "dfr");
    const title = d.session_id ? link("dft", d.title || short(d.session_id), sessionHash(d.session_id)) : add(el("span", "dft"), d.title || gap("no session"));
    const scores = el("span", "dfs mono");
    for (const [k, v] of Object.entries(d.answers || {})) add(scores, el("span", null, human(k) + " " + num2(v)));
    const move = el("span", "dfm");
    const dir = d.direction === "up" || d.direction === "down" ? d.direction : null;
    const arrow = dir === "up" ? "▲" : dir === "down" ? "▼" : "→";
    add(move, choiceText(d.before), add(el("span", "ch"), el("span", "arrow" + (dir ? " " + dir : ""), arrow + " "), choiceText(d.after, d.before || null)));
    const fb = el("span", "dff");
    const v = verdictDot(d.feedback_verdict);
    if (v) {
      fb.append(v);
      const e = EFFECT[d.feedback_effect];
      if (e) fb.append(el("span", "fe " + e[1], e[0]));
    }
    add(row, title, scores, move, fb);
    list.append(row);
  }
  box.append(list);
  return box;
}

// A new width moves the labels; stack them again.
window.addEventListener("resize", () => { if (S.view === "rules") requestAnimationFrame(stackLabels); });
