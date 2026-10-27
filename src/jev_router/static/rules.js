"use strict";

// Rules. The ladder as the running config has it, the score rulers with the
// admissions in the window on them, and router.yaml as text: check, preview
// (a pure replay of stored admissions) and apply. The editor is built once
// and never rebuilt by the live refresh, so a draft survives it.

const RULE_OPS = {gte: "≥", gt: ">", lte: "≤", lt: "<", eq: "=", ne: "≠", conf_gte: "confidence ≥"};
const EDITOR_LH = 20;  // px; the editor's line height, as dashboard.css sets it
const EDITOR_PAD = 10; // px; its top padding
const RATED_ORDER = [null, "too_slow", "right", "too_weak", "too_strong"];

function rulesetNow() {
  const sets = (S.rules && S.rules.rulesets) || {};
  if (!sets[S.ruleset]) S.ruleset = sets.policy ? "policy" : Object.keys(sets)[0] || "policy";
  return sets[S.ruleset] || {rules: S.rules ? S.rules.rules || [] : [], low_confidence: null, default: null, fallback: null, aliases: []};
}

function renderRules(kind) {
  const main = $("main");
  if (!S.rules) { main.replaceChildren(); return; }
  if (!S.rulesRoot) {
    const root = el("section", "rv");
    const head = el("div", "ovh");
    const top = el("div", "rtop");
    const work = el("div", "rwork");
    const report = el("div", "rrep");
    S.editor = buildEditor();
    add(work, S.editor.panel, report);
    add(root, head, top, work);
    S.rulesRoot = {root, head, top, work, report};
  }
  const R = S.rulesRoot;
  if (main.firstChild !== R.root || main.children.length !== 1) main.replaceChildren(R.root);
  const still = kind === "data" ? " still" : "";
  R.top.className = "rtop" + still;
  R.head.replaceChildren(...rulesHead());
  R.top.replaceChildren(ladderPanel(), add(el("div", "rside"), rulersPanel(), settingsPanel()));
  syncEditor();
  R.report.replaceChildren();
  if (S.report) R.report.append(previewPanel(S.report));
  R.work.classList.toggle("split", !!S.report);
  requestAnimationFrame(stackLabels);
}

// Threshold labels stay inside the ruler (the first and last shift inward)
// and drop a row where they would touch, by their drawn width.
function stackLabels() {
  for (const box of document.querySelectorAll(".rul .ticksx")) {
    const edge = (box.parentElement || box).getBoundingClientRect();
    const ends = [];
    for (const t of [...box.children]) {
      t.style.top = "0.4rem";
      t.style.marginLeft = "0px";
      const r = t.getBoundingClientRect();
      const shift = r.left < edge.left ? edge.left - r.left : r.right > edge.right ? Math.max(edge.left - r.left, edge.right - r.right) : 0;
      if (shift) t.style.marginLeft = shift + "px";
      const left = r.left + shift;
      let row = ends.findIndex((e) => left >= e + 8);
      if (row < 0) row = ends.length;
      ends[row] = r.right + shift;
      t.style.top = (0.4 + row * 1.2) + "rem";
    }
    box.style.height = (0.5 + Math.max(1, ends.length) * 1.2) + "rem";
  }
}

function rulesHead() {
  const out = [el("h1", "ovt", "Rules"), windowSeg(S.rwin, rulesHash)];
  const sets = Object.keys((S.rules && S.rules.rulesets) || {});
  if (sets.length > 1) {
    const seg = el("div", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Ruleset");
    for (const name of sets) {
      const b = button("segb" + (name === S.ruleset ? " on" : ""), name, () => { S.ruleset = name; renderRules("pane"); });
      b.setAttribute("aria-pressed", name === S.ruleset ? "true" : "false");
      seg.append(b);
    }
    out.push(seg);
  }
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
function useText(use) {
  if (!use) return null;
  const pair = (model, effort) => [model ? splitModel(model)[1] : null, effort].filter(Boolean).join(" · ");
  if (use.route) {
    const route = ((S.rules && S.rules.routes) || {})[use.route];
    const primary = route && route.primary ? pair(route.primary.model, route.primary.effort) : "";
    return use.route + (primary ? " → " + primary : "");
  }
  return pair(use.model, use.effort) || null;
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

function ladderPanel() {
  const set = rulesetNow();
  const rules = Array.isArray(set.rules) ? set.rules : [];
  // With more than one ladder, the count says it is this ladder's share.
  const several = Object.keys(S.rules.rulesets || {}).length > 1;
  const count = !set.admissions ? null
    : several && S.rules.admissions ? int(set.admissions) + " of " + plural(S.rules.admissions, "admission") : plural(set.admissions, "admission");
  const box = panel("Ladder", count, "ladderp");
  const list = el("div", "lad");
  const delta = draftDelta(rules);
  const draftLine = (text) => list.append(add(el("div", "ldraft"), el("span", "mono", text)));
  const within = (rule) => listHash({rule, window: S.rwin});
  const row = (index, name, cond, use, lane, hits, share, line, cls) => {
    const r = el("div", "lr" + (cls ? " " + cls : "") + (line ? " go" : "") + (line && S.hl === line ? " on" : ""));
    if (line) {
      r.tabIndex = 0;
      r.setAttribute("role", "button");
      r.setAttribute("aria-label", name + ", line " + line);
      const pick = () => { focusLine(line); r.classList.add("on"); };
      r.addEventListener("click", (event) => { if (!event.target.closest("a")) pick(); });
      r.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { pick(); event.preventDefault(); } });
    }
    const conds = el("span", "lw");
    if (cond.length) cond.forEach((c, i) => add(conds, i ? el("span", "faint", " · ") : null, c));
    else conds.append(el("span", "faint", "always"));
    const to = el("span", "lu");
    add(to, use ? el("span", "mono", use) : null, lane ? el("span", "faint", " · " + lane) : null);
    const bar = el("span", "lb");
    const fill = el("i");
    fill.style.width = (absent(share) ? 0 : Math.max(share > 0 ? 1.5 : 0, share * 100)) + "%";
    bar.append(fill);
    const n = hits ? link("ln mono", int(hits), within(name)) : el("span", "ln mono faint", "0");
    add(r, el("span", "li mono", index), el("span", "lnm mono", name), conds, to, bar, n);
    list.append(r);
  };
  const first = rules.length ? rules[0].line : null;
  const last = rules.length ? rules[rules.length - 1].line : null;
  const gate = set.low_confidence;
  if (gate) {
    const qs = Array.isArray(gate.questions) ? gate.questions.map(human).join(", ") : "";
    row("", "confidence gate", [qs + " confidence < " + cfg(gate.min_confidence)], useText(gate.use), null, gate.hits, gate.share, keyLine("low_confidence", first, -1), "gate");
  }
  for (const r of rules) {
    row(String(r.index), r.name, whenParts(r.when), useText(r.use), r.lane, r.hits, r.share, r.line, null);
    if (delta && delta.changed.has(r.name)) {
      const d = delta.changed.get(r.name);
      draftLine(d ? "draft differs: " + (whenParts(d.when).join(" · ") || "always") : "draft removes " + r.name);
    }
  }
  if (delta) for (const d of delta.added) draftLine("draft adds: " + d.name + " · " + (whenParts(d.when).join(" · ") || "always"));
  if (set.default) row("", "default", [], useText(set.default.use), null, set.default.hits, set.default.share, keyLine("default", last, 1), "dflt");
  if (set.fallback && set.fallback.hits) row("", "fallback", ["Jev failed"], null, null, set.fallback.hits, set.fallback.share, null, "dflt");
  box.append(list);
  return box;
}

// --- rulers -----------------------------------------------------------------------

function rulerKeys(set) {
  const out = new Map();
  for (const r of set.rules || []) {
    for (const [key, cond] of Object.entries(r.when || {})) {
      if (!cond || typeof cond !== "object" || Array.isArray(cond)) continue;
      for (const op of ["gte", "gt", "lte", "lt"]) {
        if (typeof cond[op] !== "number") continue;
        if (!out.has(key)) out.set(key, []);
        out.get(key).push({value: cond[op], rule: r.name});
      }
    }
  }
  return out;
}

function rulersPanel() {
  const set = rulesetNow();
  const keys = rulerKeys(set);
  const box = panel("Scores", null);
  if (!keys.size) {
    box.append(el("p", "empty", "No score threshold in this ladder"));
    return box;
  }
  const aliases = new Set(Array.isArray(set.aliases) ? set.aliases : []);
  const rated = (Array.isArray(S.rules.rated) ? S.rules.rated : []).filter((r) => !aliases.size || aliases.has(r.alias));
  // The previewed draft's cutoffs, drawn beside the running ones while they differ.
  const delta = draftDelta(Array.isArray(set.rules) ? set.rules : []);
  const draft = delta ? rulerKeys({rules: delta.rules}) : new Map();
  for (const key of draft.keys()) if (!keys.has(key)) keys.set(key, []);
  for (const [key, ticks] of keys) {
    const mine = new Set(ticks.map((t) => t.value + "|" + t.rule));
    const extra = (draft.get(key) || []).filter((t) => !mine.has(t.value + "|" + t.rule));
    box.append(ruler(key, ticks, rated, extra));
  }
  const legend = el("div", "rleg");
  for (const v of ["right", "too_weak", "too_strong"]) add(legend, add(el("span"), el("i", "rdot v-" + v), VERDICT_WORDS[v]));
  add(legend, add(el("span"), el("i", "rdot v-none"), "unrated"));
  box.append(legend);
  const k = S.rules.knife_edge;
  if (k && typeof k === "object") {
    const line = el("div", "rknife");
    if (k.denominator) {
      add(line, el("i", "rdot edge"), el("span", "mono", int(k.numerator) + " of " + plural(k.denominator, "admission")),
        " within " + num2(k.margin) + " of a threshold");
    } else line.append(gap("no admission assessed"));
    box.append(line);
  }
  return box;
}

function ruler(key, ticks, rated, draftTicks) {
  const q = (S.rules.questions || {})[key] || {};
  const top = !absent(q.max) && q.max > 0 ? q.max : Math.max(1, ...ticks.map((t) => Math.ceil(t.value)));
  const x = (v) => Math.max(0, Math.min(100, v / top * 100));
  const blk = el("div", "rul");
  blk.append(el("div", "qn", cap(key)));
  // Dots stack upward where they would touch.
  const dots = rated.filter((r) => r.scores && typeof r.scores[key] === "number")
    .sort((a, b) => RATED_ORDER.indexOf(a.verdict || null) - RATED_ORDER.indexOf(b.verdict || null) || a.scores[key] - b.scores[key]);
  const placed = [];
  const field = el("div", "rfield");
  for (const r of dots) {
    const at = x(r.scores[key]);
    let level = 0;
    while (placed.some((p) => p.level === level && Math.abs(p.at - at) < 1.6)) level += 1;
    level = Math.min(level, 7);
    placed.push({at, level});
    const verdict = r.verdict && VERDICT_WORDS[r.verdict] ? r.verdict : "none";
    const who = r.title || (r.session_id ? short(r.session_id) : null);
    const label = cap(key) + " " + num2(r.scores[key]) + (who ? " · " + who : "");
    const cls = "rdot v-" + verdict + (r.knife_edge === true ? " edge" : "");
    const dot = r.session_id ? link(cls, null, sessionHash(r.session_id), label) : el("span", cls);
    dot.title = label;
    dot.style.left = at + "%";
    dot.style.bottom = (3 + level * 9) + "px";
    field.append(dot);
  }
  const levels = placed.length ? Math.max(...placed.map((p) => p.level)) + 1 : 0;
  field.style.height = Math.max(18, 8 + levels * 9) + "px";
  const scale = el("div", "rscale");
  const merge = (list, draft) => {
    const merged = new Map();
    for (const t of list) merged.set(t.value, merged.has(t.value) ? merged.get(t.value) + ", " + t.rule : t.rule);
    return [...merged.entries()].map(([v, rule]) => ({v, rule, draft}));
  };
  const points = [...merge(ticks, false), ...merge(draftTicks || [], true)].sort((a, b) => a.v - b.v);
  const labels = el("div", "ticksx");
  const lastX = [];
  for (const {v, rule, draft} of points) {
    const mk = el("div", "mk thr" + (draft ? " draft" : ""));
    mk.style.left = x(v) + "%";
    scale.append(mk);
    let rowAt = lastX.findIndex((p) => x(v) - p >= 22);
    if (rowAt < 0) rowAt = lastX.length;
    lastX[rowAt] = x(v);
    const t = el("span", (x(v) >= 85 ? "hi" : x(v) <= 12 ? "lo" : "") + (draft ? " draft" : ""), cfg(v) + " " + rule);
    t.style.left = x(v) + "%";
    t.style.top = (0.4 + rowAt * 1.2) + "rem";
    labels.append(t);
  }
  labels.style.height = (0.5 + lastX.length * 1.2) + "rem";
  const grid = el("div", "rgrid");
  add(grid, el("span"), field, el("span"),
    el("span", "re mono", "0"), scale, el("span", "re mono", cfg(top)),
    el("span"), labels, el("span"));
  blk.append(grid);
  return blk;
}

// Quota and adaptive effort, as the YAML sets them. Read-only: edit the text.
function settingsPanel() {
  const d = S.rules;
  const box = panel("Settings", null);
  const lines = el("div", "sets");
  const line = (label, text, at) => {
    const r = el("div", "setl" + (at ? " go" : ""));
    if (at) {
      r.tabIndex = 0;
      r.setAttribute("role", "button");
      r.addEventListener("click", () => focusLine(at));
      r.addEventListener("keydown", (event) => { if (event.key === "Enter") { focusLine(at); event.preventDefault(); } });
    }
    add(r, el("span", "kl", label), add(el("span", "setv"), text));
    lines.append(r);
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
    line("Adaptive effort", a.mode + " · " + ladder + (up ? " · up at " + up : "") + (absent(a.downgrade_confirmations) ? "" : " · down after " + a.downgrade_confirmations), a.line);
  }
  box.append(lines);
  return box;
}

// --- the editor --------------------------------------------------------------------

function buildEditor() {
  const box = panel("router.yaml", null, "edp");
  const tools = el("div", "edt");
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
  const apply = button("ghost danger", "Apply", () => { S.confirming = true; syncEditor(); });
  add(tools, revert, check, previewB, apply);
  box.firstChild.append(tools);
  const moved = el("p", "edmoved");
  const ed = el("div", "ed");
  const gut = el("pre", "gut");
  gut.setAttribute("aria-hidden", "true");
  const band = el("div", "hlb");
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
    const rep = document.querySelector(".prv");
    if (rep) rep.classList.add("stale");
  });
  ta.addEventListener("scroll", () => { gut.scrollTop = ta.scrollTop; placeBand(); });
  add(ed, gut, band, ta);
  const res = el("div", "edres");
  box.append(moved, ed, res);
  return {panel: box, ta, gut, band, moved, res, revert, check, preview: previewB, apply, lines: 0};
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
  for (const r of document.querySelectorAll(".lr.on")) r.classList.remove("on");
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const box = E.ta.getBoundingClientRect();
  if (box.top < 0 || box.bottom > window.innerHeight) E.panel.scrollIntoView({block: "start", behavior: still ? "auto" : "smooth"});
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
  const note = E.panel.querySelector(".ph .note") || E.panel.firstChild.insertBefore(el("span", "note"), E.panel.firstChild.children[1]);
  note.replaceChildren();
  if (text === null) note.append(gap("text not available"));
  else add(note, el("span", "mono", hash8(S.draftBase)), dirty ? el("span", "edit", " · edited") : null);
  E.revert.hidden = !dirty;
  // Apply waits for a Check or Preview that passed on exactly this text.
  E.apply.disabled = !dirty || !!S.busy || S.passed !== draftText();
  E.check.disabled = E.preview.disabled = text === null || !!S.busy;
  E.check.textContent = S.busy === "check" ? "Checking…" : "Check";
  E.preview.textContent = S.busy === "preview" ? "Replaying…" : "Preview";
  E.moved.hidden = !(dirty && d.config_hash !== S.draftBase);
  E.moved.textContent = E.moved.hidden ? "" : "config changed on the server · now " + hash8(d.config_hash);
  E.res.replaceChildren(...editorResult());
}

function editorResult() {
  const out = [];
  if (S.confirming) {
    const bar = el("div", "confirm");
    add(bar, el("span", null, "Replace router.yaml and reload?"),
      button("danger solid", S.busy === "apply" ? "Applying…" : "Apply", () => runApply(false)),
      button("ghost", "Cancel", () => { S.confirming = false; syncEditor(); }));
    out.push(bar);
  }
  const r = S.edres;
  if (!r) return out;
  if (r.kind === "valid") out.push(add(el("p", "edok"), el("span", "ok", "Valid"), el("span", "faint", " · " + hash8(r.hash))));
  if (r.kind === "errors") {
    const list = el("div", "errs");
    for (const e of r.errors || []) {
      const row = el("div", "err");
      add(row, e.line ? button("eline mono", "line " + e.line, () => focusLine(e.line)) : el("span", "eline faint", "–"), el("span", "emsg", e.message || "invalid"));
      list.append(row);
    }
    out.push(list);
  }
  if (r.kind === "applied") {
    const b = r.body || {};
    const p = el("div", "edok applied");
    if (b.applied) {
      add(p, el("span", "ok", "Applied"), el("span", "mono", " " + hash8(b.previous_hash) + " → " + hash8(b.config_hash)));
      if (b.backup_path) add(p, el("span", "faint", " · backup "), el("span", "mono path", b.backup_path));
      if (Array.isArray(b.restart_required) && b.restart_required.length) add(p, el("span", "gap", " · restart for " + b.restart_required.join(", ")));
    } else add(p, el("span", "faint", "No change · " + hash8(b.config_hash)));
    out.push(p);
  }
  if (r.kind === "refused") {
    const p = el("div", "errs");
    const row = add(el("div", "err"), el("span", "eline gap", String(r.status || "")), el("span", "emsg", (r.body && r.body.error) || "refused"));
    if (r.body && r.body.on_disk_hash) row.append(button("danger", "Overwrite", () => runApply(true)));
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

const EFFECT = {
  resolved_too_weak: ["resolved", "ok"], resolved_too_strong: ["resolved", "ok"],
  conflicts: ["conflict", "gap"], right_changed: ["changed", "gap"], unranked: ["unranked", "faint"],
};

function choiceText(c) {
  const node = el("span", "ch");
  if (!c || c.error) return add(node, gap(c && c.error ? human(c.error) : "not replayed"));
  const [, name] = splitModel(c.model);
  add(node, el("span", "mono", c.rule || "?"), el("span", "faint", " · "), el("span", "mono", name || "?"), c.effort ? el("span", "eff", " " + c.effort) : null);
  return node;
}

function previewPanel(r) {
  const box = panel("Preview", "last " + (r.window || S.rwin), "prv");
  const head = el("div", "pvh");
  if (!r.evaluated) {
    box.append(el("p", "empty", "No admission in this window"));
    return box;
  }
  // The preview replays every ruleset's admissions; say so when there are several.
  const several = Object.keys((S.rules && S.rules.rulesets) || {}).length > 1;
  add(head, el("span", "pvk mono", int(r.changed) + " of " + int(r.evaluated)),
    el("span", "pvw", (r.evaluated === 1 ? "admission" : "admissions") + (several ? " (all rulesets)" : "") + " would change"));
  if (r.unreplayable) head.append(gap(int(r.unreplayable) + " not replayable"));
  if (Array.isArray(r.diffs) && r.diffs.length) {
    add(head, add(el("span", "pvkey"), el("span", "arr up", "▲"), " stronger · ", el("span", "arr down", "▼"), " weaker · ",
      el("span", "arr", "→"), " no order"));
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
  if (models.length) shares.append(add(el("div", "shrow shof"), el("span"), el("span", "faint", "of " + plural(r.evaluated, "admission")), el("span")));
  const qa = r.quota_saved_before || {};
  const qb = r.quota_saved_after || {};
  const quota = el("div", "shrow" + (qa.saved_rate !== qb.saved_rate ? " moved" : ""));
  add(quota, el("span", "shm kl", "Frontier quota saved"), el("span"),
    add(el("span", "mono shv"), absent(qa.saved_rate) ? gap() : pct(qa.saved_rate), " → ", absent(qb.saved_rate) ? gap() : pct(qb.saved_rate)));
  shares.append(quota);
  box.append(shares);

  const f = r.feedback || {};
  const marks = el("div", "fbm");
  const mark = (n, text, cls) => { if (n) marks.append(el("span", "fbk " + cls, int(n) + " " + text)); };
  mark(f.resolved_too_weak, "resolved " + VERDICT_WORDS.too_weak, "ok");
  mark(f.resolved_too_strong, "resolved " + VERDICT_WORDS.too_strong, "ok");
  mark(f.conflicts, f.conflicts === 1 ? "conflict" : "conflicts", "gap");
  mark(f.right_changed, VERDICT_WORDS.right + " changed", "gap");
  mark(f.unranked, "unranked", "faint");
  if (marks.children.length) box.append(marks);

  const diffs = Array.isArray(r.diffs) ? r.diffs : [];
  const list = el("div", "dfl");
  if (!diffs.length) list.hidden = true;
  for (const d of diffs) {
    const row = el("div", "dfr");
    const title = d.session_id ? link("dft", d.title || short(d.session_id), sessionHash(d.session_id)) : el("span", "dft", d.title || "–");
    const scores = el("span", "dfs mono");
    for (const [k, v] of Object.entries(d.answers || {})) add(scores, el("span", null, human(k) + " " + num2(v)));
    const move = el("span", "dfm");
    const arrow = d.direction === "up" ? "▲" : d.direction === "down" ? "▼" : "→";
    add(move, choiceText(d.before), add(el("span", "ch"), el("span", "arr" + (d.direction === "up" || d.direction === "down" ? " " + d.direction : ""), arrow + " "), choiceText(d.after)));
    const fb = el("span", "dff");
    const badge = verdictBadge(d.feedback_verdict);
    if (badge) {
      fb.append(badge);
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
