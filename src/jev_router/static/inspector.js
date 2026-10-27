"use strict";

// The session page (#s=<id>): one card per user turn, and the selected turn in
// three columns, cause to choice: what was asked, how Jev scored it, why this
// route and effort. Uses the shared builders and formatters in dashboard.js.
// The page writes only on a person's click: feedback, an eval case, a probe.

(() => {
  const VERDICT = {
    right: {badge: "✓ right", button: "Good"},
    too_weak: {badge: "↑ more", button: "Too weak"},
    too_strong: {badge: "↓ less", button: "Too strong"},
    too_slow: {badge: "slow", button: null},
  };
  const STATUS_WORDS = {planned: "planned", accepted: "sent, not confirmed", confirmed: "confirmed", rejected: "rejected", outcome_unknown: "outcome unknown"};
  // A plan's thresholds, by the question each one is weighed against.
  const PLAN_TICKS = {
    upgrade_difficulty: ["difficulty", "raise", 1], downgrade_difficulty: ["difficulty", "lower", -1],
    upgrade_harm: ["harm_if_wrong", "raise", 1], downgrade_harm: ["harm_if_wrong", "lower", -1],
    corrective_followup: ["corrective_followup", "raise", 1],
  };
  const PLAN_LIMITS = [
    ["corrective_followup", "corrective_followup", 1], ["difficulty", "upgrade_difficulty", 1],
    ["harm_if_wrong", "upgrade_harm", 1], ["p_hard", "upgrade_p_hard", 1],
    ["difficulty", "downgrade_difficulty", -1], ["harm_if_wrong", "downgrade_harm", -1],
  ];

  // Page state that outlives a refresh: which turn is selected, what is open.
  const I = {
    sid: null, turn: null, calls: false, pasted: new Set(), scroll: 0,
    posting: null, fbError: null, saved: null, saveError: null,
    cfg: null, cfgHash: null, cfgLoading: false,
    drawer: null, probe: null,
  };

  function sync() {
    if (I.sid === S.sid) return;
    I.sid = S.sid;
    I.turn = null;
    I.calls = false;
    I.pasted = new Set();
    I.scroll = 0;
    I.posting = null;
    I.fbError = null;
    I.saved = null;
    I.saveError = null;
    closeDrawer();
  }

  // --- small helpers ----------------------------------------------------------

  const kfmt = (n) => {
    const v = Number(n);
    if (v < 1000) return String(Math.round(v));
    if (v < 1e6) return (v / 1000).toFixed(v < 10000 ? 1 : 0) + "k";
    return (v / 1e6).toFixed(1) + "M";
  };
  const bare = (model) => splitModel(model)[1] || model;
  function answersOf(t) { return t && t.jev && t.jev.answers && typeof t.jev.answers === "object" ? t.jev.answers : {}; }
  function valueOf(a) { return a && typeof a === "object" ? a[a.type] : null; }
  function ladderOf(t) {
    const l = t && t.plan && t.plan.evidence && t.plan.evidence.ladder;
    return Array.isArray(l) && l.length ? l : LADDER;
  }
  function arrow(ladder, from, to) {
    const a = ladder.indexOf(from);
    const b = ladder.indexOf(to);
    if (a < 0 || b < 0 || a === b) return "";
    return b > a ? " ▲" : " ▼";
  }
  function verdictBadge(v) {
    if (!v) return null;
    const known = VERDICT[v];
    return el("span", "vb v-" + v, known ? known.badge : human(v));
  }
  function label(t) { return t.kind === "admission" ? "Admission" : "Turn " + t.turn_index; }
  function note(text) { return el("span", "note", text); }

  // --- payload ---------------------------------------------------------------

  function context() {
    const d = S.data;
    const session = d.session || {};
    const adm = d.admission || null;
    const reqs = (Array.isArray(d.requests) ? d.requests : []).slice().sort((a, b) => (a.seq || 0) - (b.seq || 0));
    const bySeq = new Map(reqs.map((r) => [r.seq, r]));
    const turns = (Array.isArray(d.turns) && d.turns.length ? d.turns.slice() : legacyTurns(adm, reqs))
      .sort((a, b) => (a.turn_index || 0) - (b.turn_index || 0));
    // The calls of each turn, from its own list; a call no turn claims goes to
    // the turn it names, else the last one that began before it.
    const members = new Map(turns.map((t) => [t, (Array.isArray(t.requests) ? t.requests : []).map((s) => bySeq.get(s)).filter(Boolean)]));
    const claimed = new Set([...members.values()].flat());
    for (const r of reqs) {
      if (claimed.has(r) || !turns.length) continue;
      let owner = turns.find((t) => t.turn_index === r.turn_index) || null;
      if (!owner) for (const t of turns) if (!absent(t.ts) && !absent(r.created) && t.ts <= r.created) owner = t;
      members.get(owner || turns[0]).push(r);
    }
    for (const list of members.values()) list.sort((a, b) => (a.seq || 0) - (b.seq || 0));
    const sel = turns.find((t) => t.turn_index === I.turn) || turns[0] || null;
    return {session, adm, reqs, turns, members, sel, trace: d.trace || {available: false, reason: null}, join: d.turn_join};
  }

  // A payload from before turns[]: one admission turn holding every call.
  function legacyTurns(adm, reqs) {
    if (!adm && !reqs.length) return [];
    return [{
      turn_index: 1, kind: "admission", ts: adm ? adm.ts : null, decision_row: adm ? adm.id : null,
      user_prompt: null, state: null, state_shape: adm ? adm.state_shape : null,
      jev: adm ? {latency_ms: adm.jev_ms, tokens: adm.jev_tokens, answers: adm.answers || {}, shadow_answers: adm.shadow_answers} : null,
      decision: adm ? {model: adm.model, effort: adm.effort, rule: adm.rule, route: adm.route, quality_lane: adm.quality_lane, fallback: !!adm.fallback} : {},
      plan: null, execution_rollup: null, requests: reqs.map((r) => r.seq), feedback: [],
    }];
  }

  function previous(ctx, t) {
    const i = ctx.turns.indexOf(t);
    return i > 0 ? ctx.turns[i - 1] : null;
  }

  function threshold(t, key) {
    const th = t.plan && t.plan.evidence && t.plan.evidence.thresholds;
    return th && !absent(th[key]) ? th[key] : null;
  }
  // Corrective when the turn's own plan says how high is corrective, and the answer is there.
  function corrective(t) {
    const v = valueOf(answersOf(t).corrective_followup);
    const limit = threshold(t, "corrective_followup");
    return !absent(v) && limit !== null && v >= limit;
  }

  function turnTokens(t) {
    const r = t.execution_rollup || {};
    if (absent(r.input_tokens) && absent(r.output_tokens)) return null;
    return (r.input_tokens || 0) + (r.output_tokens || 0);
  }

  // --- view ------------------------------------------------------------------

  function inspectorView(kind) {
    sync();
    const frag = document.createDocumentFragment();
    if (S.missing) {
      frag.append(sessionNav(), el("p", "gaprow", S.slug !== null && !S.sid ? "no bookmark named " + S.slug : "session not found"));
      return frag;
    }
    if (!S.data) return frag;
    loadConfig();
    const ctx = context();
    if (ctx.sel) I.turn = ctx.sel.turn_index;
    const top = kind === "view" ? "" : " still";
    const low = kind === "data" ? " still" : "";
    frag.append(sessionNav(), header(ctx, top), timeline(ctx, top));
    if (ctx.sel) frag.append(turnView(ctx, ctx.sel, low));
    else frag.append(el("p", "gaprow", "no turn recorded"));
    requestAnimationFrame(afterRender);
    if (I.drawer && !I.drawer.hidden) drawerSync(ctx);
    return frag;
  }

  function afterRender() {
    const strip = document.querySelector(".tstrip");
    if (strip) {
      strip.scrollLeft = I.scroll;
      const on = strip.querySelector(".tcard.sel");
      if (on) {
        const a = strip.getBoundingClientRect();
        const b = on.getBoundingClientRect();
        if (b.left < a.left || b.right > a.right) strip.scrollLeft += b.left - a.left - 8;
        I.scroll = strip.scrollLeft;
      }
    }
    layoutRulers();
  }

  // Where this session sits in the list it was opened from.
  function sessionNav() {
    const nav = el("nav", "snav");
    nav.setAttribute("aria-label", "Session list");
    const list = sessions();
    const at = list.findIndex((s) => s.session_id === S.sid);
    const back = link("back", "← Sessions", listHash());
    back.dataset.fk = "back";
    const summary = filterSummary();
    add(nav, back, summary ? el("span", "fsum", summary) : null);
    const pager = el("span", "pager");
    if (at >= 0) {
      pager.append(note((at + 1) + " of " + list.length));
      const prev = at > 0 ? list[at - 1] : null;
      const next = at < list.length - 1 ? list[at + 1] : null;
      const p = prev ? link("pg", "Newer", sessionHash(prev.session_id), "Newer session (k)") : el("span", "pg off", "Newer");
      const n = next ? link("pg", "Older", sessionHash(next.session_id), "Older session (j)") : el("span", "pg off", "Older");
      p.dataset.fk = "pg-prev";
      n.dataset.fk = "pg-next";
      add(pager, p, n);
    } else if (S.list) {
      pager.append(note("not in this list"));
    }
    nav.append(pager);
    return nav;
  }

  function filterSummary() {
    const f = S.filters;
    const bits = [];
    if (f.q) bits.push("“" + f.q + "”");
    for (const k of ["client", "model", "rule", "state", "feedback"]) if (f[k]) bits.push(human(f[k]));
    if (f.adaptive) bits.push("adaptive " + f.adaptive);
    if (f.problem) bits.push(PROBLEMS[f.problem] ? PROBLEMS[f.problem].toLowerCase() : f.problem);
    if (f.pinned) bits.push("pinned");
    if (f.window) bits.push("last " + f.window);
    return bits.join(" · ");
  }

  function neighbour(step) {
    const list = sessions();
    const at = list.findIndex((s) => s.session_id === S.sid);
    const to = at < 0 ? null : list[at + step];
    if (to) go(sessionHash(to.session_id));
  }

  // --- header ------------------------------------------------------------------

  function header(ctx, still) {
    const {session, reqs, turns} = ctx;
    const root = el("section", "ihead" + still);
    const title = el("h1", "ititle");
    title.textContent = session.title || "Session " + short(session.session_id, session.client);
    root.append(title);

    const [prov, model] = splitModel(session.model);
    const effort = session.effective_effort || session.base_effort;
    const big = el("div", "imodel");
    add(big, prov ? el("span", "prov", prov) : null, show(model));
    add(big, el("span", "sep", "·"), effort ? el("span", null, effort) : el("span", "faint", "no effort"));
    if (session.state && session.state !== "active") big.append(el("span", "istate" + (session.state === "blocked" ? " warn" : ""), session.state));
    root.append(big);

    const row = el("div", "irow");
    const facts = el("div", "ifacts");
    const bits = [];
    bits.push(session.client ? el("span", null, session.client) : gap("client"));
    if (session.workspace_hint) bits.push(el("span", "ws", session.workspace_hint));
    if (!absent(session.created)) bits.push(el("span", null, day(session.created) + " " + clock(session.created).slice(0, 5)));
    const n = turns.length;
    bits.push(el("span", null, n + (n === 1 ? " turn" : " turns")));
    bits.push(el("span", null, reqs.length + (reqs.length === 1 ? " call" : " calls")));
    if (session.adaptation_mode && session.adaptation_mode !== "off") {
      const e = session.escalation_count;
      bits.push(absent(e) ? gap("escalations") : el("span", null, e + (e === 1 ? " escalation" : " escalations")));
    }
    bits.forEach((b, i) => add(facts, i ? el("span", "dot", "·") : null, b));
    const fb = session.feedback || {};
    const verdict = fb.verdict || session.feedback_verdict;
    if (verdict) add(facts, verdictBadge(verdict));
    row.append(facts);

    const acts = el("div", "iacts");
    acts.append(feedbackButtons(verdict));
    const save = button("ghost", I.saved ? "Saved" : "Save as eval case", saveCase);
    save.dataset.fk = "save-case";
    save.disabled = !verdict || I.posting === "save";
    const box = button("ghost" + (I.drawer && !I.drawer.hidden ? " on" : ""), "Jev sandbox", () => toggleDrawer(ctx));
    box.dataset.fk = "sandbox";
    box.setAttribute("aria-pressed", I.drawer && !I.drawer.hidden ? "true" : "false");
    add(acts, save, box);
    row.append(acts);
    root.append(row);

    if (fb.note) root.append(el("p", "fbnote", "“" + fb.note + "”"));
    if (I.fbError) root.append(el("p", "gaprow", I.fbError));
    if (I.saved) {
      const line = el("p", "isaved");
      add(line, el("span", "ok", "Saved"), el("span", "mono", I.saved.path), note(int(I.saved.total) + (I.saved.total === 1 ? " case" : " cases")));
      root.append(line);
    }
    if (I.saveError) root.append(el("p", "gaprow", I.saveError));
    return root;
  }

  function feedbackButtons(current) {
    const seg = el("div", "fbseg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Feedback");
    for (const v of ["right", "too_weak", "too_strong"]) {
      const b = button("fbb v-" + v + (current === v ? " on" : ""), VERDICT[v].button, () => sendFeedback(v));
      b.dataset.fk = "fb-" + v;
      b.setAttribute("aria-pressed", current === v ? "true" : "false");
      b.disabled = I.posting !== null;
      seg.append(b);
    }
    return seg;
  }

  async function sendFeedback(verdict) {
    if (I.posting) return;
    const sid = S.sid;
    I.posting = "feedback";
    I.fbError = null;
    render("pane");
    try {
      const response = await post("/router/sessions/" + encodeURIComponent(sid) + "/feedback", {verdict, source: "dashboard"});
      if (sid !== S.sid) return;
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const err = body && body.error;
        I.fbError = "Feedback not saved · " + ((err && (err.message || err)) || "HTTP " + response.status);
      }
    } catch {
      if (sid === S.sid) I.fbError = "Feedback not saved · router unreachable";
    } finally {
      if (sid === S.sid) {
        I.posting = null;
        render("pane");
        refresh("data");
      }
    }
  }

  async function saveCase() {
    const sid = S.sid;
    const session = (S.data && S.data.session) || {};
    const verdict = (session.feedback && session.feedback.verdict) || session.feedback_verdict;
    if (!verdict || I.posting) return;
    I.posting = "save";
    I.saveError = null;
    render("pane");
    try {
      const response = await post("/router/eval-cases", {session_id: sid, verdict});
      const body = await response.json().catch(() => ({}));
      if (sid !== S.sid) return;
      if (response.ok && body.saved) I.saved = {path: body.path, total: body.total_cases};
      else I.saveError = "Not saved · " + (body.error || "HTTP " + response.status);
    } catch {
      if (sid === S.sid) I.saveError = "Not saved · router unreachable";
    } finally {
      if (sid === S.sid) {
        I.posting = null;
        render("pane");
      }
    }
  }

  // --- timeline ----------------------------------------------------------------

  function timeline(ctx, still) {
    const root = el("section", "turns" + still);
    root.setAttribute("aria-label", "Turns");
    const strip = el("div", "tstrip");
    strip.addEventListener("scroll", () => { I.scroll = strip.scrollLeft; }, {passive: true});
    ctx.turns.forEach((t, i) => strip.append(turnCard(ctx, t, i)));
    root.append(strip);
    if (ctx.join === "by_order") root.append(el("p", "tjoin", "Calls matched to turns by log order"));
    return root;
  }

  function turnCard(ctx, t, i) {
    const on = ctx.sel === t;
    const card = button("tcard" + (on ? " sel" : "") + (corrective(t) ? " corr" : ""), "", () => pick(t.turn_index));
    card.dataset.fk = "turn-" + t.turn_index;
    card.setAttribute("aria-pressed", on ? "true" : "false");
    card.style.animationDelay = Math.min(400, 60 + i * 50) + "ms";
    const head = el("span", "tch");
    add(head, el("span", "tn", label(t)), absent(t.ts) ? null : el("span", "tt", clock(t.ts).slice(0, 5)), el("span", "sp"));
    const fb = Array.isArray(t.feedback) && t.feedback.length ? t.feedback[t.feedback.length - 1].verdict : null;
    if (fb) head.append(verdictBadge(fb));
    if (corrective(t)) head.append(el("span", "cmark", "corrective"));
    const prompt = el("span", "tp");
    const text = t.user_prompt || (t.state && (t.state.current_user_request || t.state.latest_user_request));
    if (text) prompt.textContent = firstLine(text);
    else prompt.append(gap("prompt not recorded"));
    const change = el("span", "tx");
    add(change, changeLine(ctx, t));
    const calls = ctx.members.get(t) || [];
    const foot = el("span", "tk");
    const tok = turnTokens(t);
    add(foot, calls.length + (calls.length === 1 ? " call" : " calls"), " · ", absent(tok) ? gap("tokens") : kfmt(tok) + " tok");
    const bad = problemCounts(calls);
    for (const [k, v] of Object.entries(bad)) if (v) foot.append(el("span", "prob", v + " " + human(k)));
    add(card, head, prompt, change, foot);
    return card;
  }

  function firstLine(text) {
    const line = String(text).split("\n").find((l) => l.trim()) || "";
    return line.trim();
  }

  function problemCounts(calls) {
    const out = {unknown: 0, rejected: 0, stream_failed: 0};
    for (const r of calls) {
      if (isUnknown(r.status)) out.unknown += 1;
      else if (r.status === "rejected") out.rejected += 1;
      else if (r.status === "stream_failed") out.stream_failed += 1;
    }
    return out;
  }

  // What this turn did to the route or the effort, in a few words.
  function changeLine(ctx, t) {
    const d = t.decision || {};
    const node = el("span");
    if (t.kind === "admission") {
      const rule = d.rule ? el("span", d.fallback ? "gap" : "rn", d.rule) : gap("rule");
      add(node, rule, " → ", d.model ? bare(d.model) : gap("model"), d.effort ? " · " + d.effort : "");
      return node;
    }
    if (t.kind === "plan") {
      const p = t.plan;
      if (!p) return add(node, gap("plan not recorded"));
      const ladder = ladderOf(t);
      const from = p.from_effort || "?";
      if (p.mode === "shadow") {
        const rec = p.recommendation || p.to_effort;
        return add(node, el("span", "faint", rec && rec !== from ? "would " + from + " → " + rec + arrow(ladder, from, rec) : "effort held"));
      }
      if (p.action === "change_effort") {
        const to = p.to_effort || "?";
        const dir = arrow(ladder, from, to);
        return add(node, from + " → ", el("span", dir === " ▲" ? "up" : "down", to + dir));
      }
      if (p.action === "blocked") return add(node, el("span", "gap", "blocked at " + from));
      return add(node, el("span", "held", "effort held"));
    }
    const prev = previous(ctx, t);
    const was = prev && prev.decision ? prev.decision.effort : null;
    if (was && d.effort && was !== d.effort) {
      const dir = arrow(LADDER, was, d.effort);
      add(node, was + " → ", el("span", dir === " ▲" ? "up" : "down", d.effort + dir));
    }
    return node;
  }

  function pick(index) {
    if (I.turn === index) return;
    I.turn = index;
    render("pane");
  }

  function step(delta) {
    if (!S.data) return;
    const ctx = context();
    const at = ctx.turns.indexOf(ctx.sel);
    const to = ctx.turns[at + delta];
    if (to) {
      pick(to.turn_index);
      const card = document.querySelector('[data-fk="turn-' + to.turn_index + '"]');
      if (card) card.focus({preventScroll: true});
    }
  }

  // --- the selected turn ---------------------------------------------------------

  function turnView(ctx, t, still) {
    const root = el("section", "tview");
    root.setAttribute("aria-live", "polite");
    const pane = el("div", "pane" + still);
    pane.dataset.turn = String(t.turn_index);
    const cols = el("div", "tcols");
    add(cols, requestCol(ctx, t), jevCol(ctx, t), whyCol(ctx, t));
    pane.append(cols, turnFoot(ctx, t));
    root.append(pane);
    return root;
  }

  function colHead(title, corner) {
    const h = el("div", "tcolh");
    add(h, el("h2", null, title), corner ? add(el("span", "note"), corner) : null);
    return h;
  }

  // 1. What was asked, and what Jev was shown.
  function requestCol(ctx, t) {
    const col = el("div", "tcol");
    col.append(colHead("Request"));
    const st = t.state;
    const ask = (st && (st.current_user_request || st.latest_user_request)) || t.user_prompt;
    if (ask) col.append(el("div", "reqtext", ask));
    else if (!st && t.state_shape) col.append(shapeBlock(t.state_shape));
    else col.append(el("p", "gaprow", "request not recorded"));
    if (st) {
      const pasted = [];
      if (st.material_the_user_pasted) pasted.push(st.material_the_user_pasted);
      for (const q of Array.isArray(st.quoted_material) ? st.quoted_material : []) if (q && q.content) pasted.push(q.content);
      pasted.forEach((text, i) => col.append(pastedBlock(t, i, text)));
      const earlier = st.earlier_request_in_this_conversation || (st.task_request && st.task_request !== ask ? st.task_request : null);
      if (earlier) col.append(labelled("Earlier request", el("div", "reqtext dim", earlier)));
      const cons = Array.isArray(st.user_constraints) ? st.user_constraints.filter(Boolean) : [];
      if (cons.length) {
        const ul = el("ul", "plainlist");
        for (const c of cons) ul.append(el("li", null, c));
        col.append(labelled("Constraints", ul));
      }
      const obs = Array.isArray(st.observations) ? st.observations.filter((o) => o && (o.summary || o.kind)) : [];
      if (obs.length) {
        const ul = el("ul", "plainlist");
        for (const o of obs) {
          const li = el("li");
          add(li, o.source ? el("span", "faint", o.source + " ") : null, o.summary || human(o.kind), o.status ? el("span", "faint", " · " + o.status) : null);
          ul.append(li);
        }
        col.append(labelled("Observations", ul));
      }
    } else if (ask && t.state_shape && t.kind !== "user_turn") {
      col.append(shapeBlock(t.state_shape));
    }
    col.append(factStrip(ctx, t));
    return col;
  }

  function labelled(name, body) {
    return add(el("div", "lblk"), el("div", "qn", name), body);
  }

  function pastedBlock(t, i, text) {
    const key = t.turn_index + ":" + i;
    const box = el("details", "paste");
    box.open = I.pasted.has(key);
    box.addEventListener("toggle", () => { if (box.open) I.pasted.add(key); else I.pasted.delete(key); });
    const sum = el("summary", null, "Pasted");
    sum.dataset.fk = "paste-" + key;
    add(box, sum, el("pre", "pastetext", text));
    return box;
  }

  // An older row: the shape of what Jev saw, never the text.
  function shapeBlock(shape) {
    const box = el("div", "shape");
    for (const [name, chars] of Object.entries(shape.parts || {})) {
      if (["schema_version", "event"].includes(name)) continue;
      const row = el("div", "kv");
      add(row, el("span", "a", human(name)), add(el("span", "b"), gap(absent(chars) ? "not stored" : int(chars) + " chars · not stored")));
      box.append(row);
    }
    if (!box.children.length) box.append(el("p", "gaprow", "not recorded"));
    return box;
  }

  // Tools, images, tokens and headroom: from the admission, or the turn's first call.
  function factStrip(ctx, t) {
    const calls = ctx.members.get(t) || [];
    let f = null;
    if (t.kind === "admission" && ctx.adm && ctx.adm.facts && Object.keys(ctx.adm.facts).length) f = ctx.adm.facts;
    else {
      const first = calls.find((r) => r.facts && Object.keys(r.facts).length);
      f = first ? first.facts : null;
    }
    const strip = el("div", "hfacts");
    const cell = (k, v) => strip.append(add(el("div", "hf"), el("span", "k", k), add(el("span", "v"), v)));
    if (!f) {
      for (const k of ["Tools", "Images", "Tokens", "Headroom"]) cell(k, gap());
      return strip;
    }
    const names = Array.isArray(f.tool_names) && f.tool_names.length ? f.tool_names
      : (t.state && t.state.facts && Array.isArray(t.state.facts.tool_names) ? t.state.facts.tool_names : null);
    let tools;
    if (f.has_tools === false) tools = "none";
    else if (names) tools = names.join(" · ");
    else if (!absent(f.tool_count)) tools = int(f.tool_count);
    else tools = gap();
    cell("Tools", tools);
    cell("Images", f.has_images === true ? "yes" : f.has_images === false ? "none" : gap());
    cell("Tokens", show(!absent(f.input_tokens) ? f.input_tokens : f.est_tokens, int));
    const head = el("span");
    if (absent(f.headroom)) head.append(gap());
    else {
      add(head, el("span", f.headroom < 0 ? "gap" : null, int(f.headroom)));
      if (!absent(f.context_window)) head.append(el("span", "faint", " of " + kfmt(f.context_window)));
    }
    cell("Headroom", head);
    return strip;
  }

  // 2. How Jev scored it, each answer on its own ruler.
  function jevCol(ctx, t) {
    const col = el("div", "tcol");
    const jev = t.jev;
    col.append(colHead("Jev", jev && !absent(jev.latency_ms) ? ms(jev.latency_ms) : null));
    if (t.kind === "user_turn") {
      col.append(el("p", "notasked", "Not asked"));
      return col;
    }
    if (!jev) {
      col.append(el("p", "gaprow", "not recorded"));
      return col;
    }
    const ans = answersOf(t);
    const names = Object.keys(ans);
    if (!names.length) {
      const fell = t.decision && t.decision.fallback;
      col.append(el("p", "gaprow", fell ? "Jev failed · no answers" : "answers not recorded"));
      return col;
    }
    const ticks = t.kind === "plan" ? planTicks(t) : ruleTicks(ctx.trace);
    const order = (a) => ({choice: 0, score: 1, noul: 2}[ans[a] && ans[a].type] ?? 3);
    for (const name of names.slice().sort((a, b) => order(a) - order(b))) col.append(question(name, ans[name], ticks.get(name) || []));
    const sh = jev.shadow_answers;
    if (sh && typeof sh === "object" && Object.keys(sh).length) {
      const box = el("div", "shadowq");
      box.append(el("div", "qn", "Shadow"));
      for (const [name, a] of Object.entries(sh)) box.append(question(name, a, []));
      col.append(box);
    }
    return col;
  }

  // Every numeric answer threshold the replayed ladder compared, per question.
  // A threshold that decided a rule (the hit, or the miss it alone failed) is bright.
  function ruleTicks(trace) {
    const out = new Map();
    if (!trace || !trace.available) return out;
    for (const r of Array.isArray(trace.rules) ? trace.rules : []) {
      const deciding = new Set(decidingConditions(r));
      for (const c of Array.isArray(r.conditions) ? r.conditions : []) {
        if (c.source !== "answer" || !["gte", "gt", "lte", "lt"].includes(c.op) || typeof c.threshold !== "number") continue;
        if (!out.has(c.key)) out.set(c.key, []);
        out.get(c.key).push({value: c.threshold, name: r.name || "?", on: deciding.has(c), hit: r.outcome === "hit"});
      }
    }
    return out;
  }

  function planTicks(t) {
    const out = new Map();
    const ev = (t.plan && t.plan.evidence) || {};
    const th = ev.thresholds || {};
    const ans = answersOf(t);
    for (const [key, [qid, word, sign]] of Object.entries(PLAN_TICKS)) {
      if (absent(th[key])) continue;
      const v = valueOf(ans[qid]);
      const on = !absent(v) && (sign > 0 ? v >= th[key] : v <= th[key]);
      if (!out.has(qid)) out.set(qid, []);
      out.get(qid).push({value: th[key], name: word, on, hit: on});
    }
    return out;
  }

  // A threshold is deciding where its question decided the rule.
  function decidingConditions(r) {
    const conds = Array.isArray(r.conditions) ? r.conditions : [];
    if (r.outcome === "hit") return conds;
    if (r.outcome !== "miss") return [];
    const first = conds.find((c) => c.passed === false);
    if (!first || !conds.every((c) => c === first || c.passed === true)) return [];
    return [first];
  }

  function questionMax(name, a, ticks) {
    const q = I.cfg && I.cfg.questions && I.cfg.questions[name];
    if (q && !absent(q.max) && q.max > 0) return q.max;
    if (a.type === "noul") return 1;
    const probs = a.probabilities && typeof a.probabilities === "object" ? Object.keys(a.probabilities).map(Number).filter(Number.isFinite) : [];
    if (probs.length >= 2) return Math.max(...probs);
    const v = valueOf(a);
    return Math.max(1, Math.ceil(Math.max(absent(v) ? 0 : v, ...ticks.map((x) => x.value))));
  }

  function question(name, a, ticks) {
    const box = el("div", "qb");
    const head = el("div", "qh");
    const value = valueOf(a);
    const conf = a && !absent(a.confidence) ? note("conf " + num2(a.confidence)) : null;
    if (!a || typeof a !== "object") {
      add(head, el("span", "qname", cap(name)), gap());
      box.append(head);
      return box;
    }
    if (a.type === "choice") {
      add(head, el("span", "qname", cap(name)), conf, add(el("span", "qval"), absent(value) ? gap() : human(value)));
      box.append(head);
      const probs = a.probabilities && typeof a.probabilities === "object" ? Object.entries(a.probabilities).filter(([, p]) => p >= 0.01).sort((x, y) => y[1] - x[1]) : [];
      if (probs.length > 1) box.append(choiceStack(probs));
      return box;
    }
    if (a.type !== "score" && a.type !== "noul") {
      add(head, el("span", "qname", cap(name)), add(el("span", "qval"), show(value, plain)));
      box.append(head);
      return box;
    }
    add(head, el("span", "qname", cap(name)), conf, add(el("span", "qval mono"), show(value, num2)));
    box.append(head);
    const top = questionMax(name, a, ticks);
    box.append(ruler(top, ticks, value, a.type === "score" ? a.probabilities : null));
    return box;
  }

  // 0 to max, the thresholds as ticks named by their rule, the answer as the one bright mark.
  function ruler(top, ticks, value, probs) {
    const x = (v) => Math.max(0, Math.min(100, v / (top || 1) * 100));
    const box = el("div", "ruler");
    const levels = probs && typeof probs === "object" ? Object.entries(probs).map(([k, p]) => [Number(k), p]).filter(([k, p]) => Number.isFinite(k) && !absent(p)) : [];
    if (levels.length >= 2) {
      const dist = el("div", "rdist");
      for (const [k, p] of levels) {
        const bar = el("i", x(k) <= 0 ? "lo" : x(k) >= 100 ? "hi" : null);
        bar.style.left = x(k) + "%";
        bar.style.height = Math.max(4, p * 100) + "%";
        bar.title = "L" + k + " " + pct(p);
        dist.append(bar);
      }
      box.append(dist);
    }
    const track = el("div", "rtrack");
    // Near-equal cutoffs (1.399 and 1.4) are one tick.
    const merged = [];
    for (const tk of ticks.slice().sort((p, q) => p.value - q.value)) {
      const last = merged[merged.length - 1];
      if (last && Math.abs(x(tk.value) - x(last.value)) < 1) {
        last.names.push(tk.name);
        if (tk.on) { last.on = true; last.lead = tk.name; }
      } else merged.push({value: tk.value, names: [tk.name], on: tk.on, lead: tk.name});
    }
    const labels = el("div", "rlabels");
    for (const m of merged) {
      const tick = el("i", "rtick" + (m.on ? " on" : ""));
      tick.style.left = x(m.value) + "%";
      track.append(tick);
      const names = [m.lead, ...m.names.filter((n) => n !== m.lead)];
      const unique = [...new Set(names)];
      const text = cfg(Number(m.value.toFixed(2))) + " " + unique[0] + (unique.length > 1 ? " +" + (unique.length - 1) : "");
      const lab = el("span", m.on ? "on" : null, text);
      lab.dataset.x = String(x(m.value));
      lab.style.left = x(m.value) + "%";
      labels.append(lab);
    }
    if (!absent(value)) {
      const mk = el("i", "rmark");
      mk.style.left = x(value) + "%";
      track.append(mk);
    }
    for (const [v, at] of [[0, 0], [top, 100]]) {
      const end = el("span", "end", cfg(v));
      end.dataset.x = String(at);
      labels.append(end);
    }
    box.append(track, labels);
    return box;
  }

  // Labels go under their tick; one that would touch its neighbour drops a row.
  function layoutRulers() {
    for (const box of document.querySelectorAll(".rlabels")) {
      const width = box.clientWidth;
      if (!width) continue;
      // The ends hold row 0 at the edges; every other label takes the first
      // row where it touches nothing.
      const rows = [[]];
      const place = (lab, row, left) => {
        lab.style.left = left + "px";
        lab.style.top = (row * 1.15) + "rem";
        rows[row].push([left, left + lab.offsetWidth]);
      };
      const ends = [...box.querySelectorAll(".end")];
      for (const lab of ends) place(lab, 0, lab.dataset.x === "0" ? 0 : width - lab.offsetWidth);
      const items = [...box.children].filter((n) => !n.classList.contains("end")).sort((a, b) => Number(a.dataset.x) - Number(b.dataset.x));
      for (const lab of items) {
        const w = lab.offsetWidth;
        const left = Math.max(0, Math.min(width - w, Number(lab.dataset.x) / 100 * width - w / 2));
        let row = rows.findIndex((taken) => taken.every(([a, b]) => left + w + 8 <= a || left >= b + 8));
        if (row < 0) { rows.push([]); row = rows.length - 1; }
        place(lab, row, left);
      }
      box.style.height = (Math.max(1, rows.length) * 1.15 + 0.2) + "rem";
    }
  }
  window.addEventListener("resize", () => requestAnimationFrame(layoutRulers));

  function choiceStack(probs) {
    const box = el("div", "cstack");
    const bar = el("div", "cbar");
    probs.forEach(([, p], i) => {
      const seg = el("i", "c" + Math.min(i, 3));
      seg.style.width = (p * 100) + "%";
      bar.append(seg);
    });
    const legend = el("div", "clegend");
    probs.slice(0, 3).forEach(([name, p], i) => add(legend, add(el("span", "c" + i), el("b"), human(name) + " " + pct(p))));
    add(box, bar, legend);
    return box;
  }

  // 3. Why this route and effort.
  function whyCol(ctx, t) {
    const col = el("div", "tcol");
    if (t.kind === "admission") return admissionWhy(ctx, t, col);
    if (t.kind === "plan") return planWhy(ctx, t, col);
    return fixedWhy(ctx, t, col);
  }

  function admissionWhy(ctx, t, col) {
    const {adm, session, trace} = ctx;
    const d = t.decision || {};
    const stored = trace.stored || {};
    const recordedRule = d.rule || (adm && adm.rule) || stored.rule || session.decision_rule;
    let corner = null;
    if (!adm) corner = null;
    else if (trace.available && !trace.reproduces) {
      const rep = trace.replayed || {};
      corner = el("span", "gap", "replay differs · " + [rep.model, rep.effort, rep.rule].filter(Boolean).join(" "));
    } else if (!trace.available && trace.reason !== "fallback_admission") corner = gap(reasonText(trace.reason));
    col.append(colHead("Rule", corner));
    if (!adm) {
      col.append(el("p", "gaprow", "admission not recorded"));
      return col;
    }
    if (trace.available) {
      const list = el("div", "rules");
      let delay = 60;
      const line = (i, name, why, cls) => {
        const row = el("div", "r " + cls);
        row.style.animationDelay = delay + "ms";
        delay += 40;
        add(row, el("span", "i", i), el("span", "nm", name), add(el("span", "why"), why));
        list.append(row);
      };
      const gate = trace.gate || {};
      const fired = gate.applies && !gate.passed;
      if (gate.applies) {
        const q = gate.fired_by;
        const c = q && gate.questions ? gate.questions[q] : null;
        const why = fired ? "fired · " + (q ? human(q) + " " + (absent(c) ? "" : num2(c) + " < " + num2(gate.min_confidence)) : "") : "passed";
        line("", "confidence gate", why, fired ? "fired" : "miss");
      }
      for (const r of Array.isArray(trace.rules) ? trace.rules : []) {
        const cls = r.outcome === "hit" ? "hit" : r.outcome === "miss" ? "miss" : "skip";
        line(String(r.index), r.name || "?", ruleWhy(r), cls);
      }
      if (fired && trace.matched === "low_confidence") line("", "low_confidence", "the gate's route", "hit");
      if (trace.matched === "default") line("", "default", "no rule matched", "hit");
      col.append(list);
    } else {
      const box = el("div", "rules");
      const row = el("div", "r hit" + (d.fallback ? " fb" : ""));
      add(row, el("span", "i", ""), el("span", "nm", recordedRule || "?"), el("span", "why", d.fallback ? "Jev failed · the alias's fallback" : ""));
      box.append(row);
      col.append(box);
    }
    const route = el("div", "resline");
    const effort = d.effort || adm.effort;
    add(route, el("span", "k", "Route"), el("span", "mono", (trace.route || d.route || adm.route || "direct") + " → " + (d.model || adm.model || "?") + (effort ? " · " + effort : "")));
    col.append(route);
    col.append(quotaLine(adm, session), laneLine(adm, session, d));
    return col;
  }

  function reasonText(reason) {
    if (!reason) return "not replayed";
    if (reason === "not_on_file") return "not replayed · config not on file";
    if (reason === "no_answers") return "not replayed · no answers";
    if (reason === "no_admission_row") return "not replayed · no admission row";
    return "not replayed · " + human(reason);
  }

  // The counterfactual is the zero-pressure choice, stored on every row as
  // "model(effort or -)". It is a change only when it differs from the choice.
  function chosen(adm) { return (adm.model || "?") + "(" + (adm.effort || "-") + ")"; }
  function counterfactualDiffers(adm) { return !!adm.counterfactual && adm.counterfactual !== chosen(adm); }

  function quotaLine(adm, session) {
    const box = el("div", "resline");
    box.append(el("span", "k", "Quota"));
    const moves = [];
    if (Array.isArray(adm.shifted)) for (const s of adm.shifted) moves.push(human(s.rule || "?") + " " + human(s.question || "") + " " + cfg(s.base) + " → " + cfg(s.effective) + (s.provider ? " by " + s.provider : ""));
    if (adm.reordered === true) moves.push("route reordered");
    if (counterfactualDiffers(adm)) moves.push("without quota " + adm.counterfactual);
    if (moves.length) box.append(el("span", "steer", "steered"));
    else if (session.quota_changed_choice === true) box.append(gap("steered · detail not recorded"));
    else box.append(el("span", "ok", "kept primary"));
    const snap = adm.quota_snapshot;
    const prov = el("span", "pressures");
    if (!snap || typeof snap !== "object" || !Object.keys(snap).length) prov.append(gap("no reading"));
    else {
      const fresh = Object.entries(snap).filter(([, q]) => q && q.status === "fresh");
      if (!fresh.length) prov.append(gap("no fresh reading"));
      for (const [provider, q] of fresh) add(prov, el("span", null, provider + " " + pressureText(q.pressure)));
      const rest = Object.entries(snap).filter(([, q]) => !q || q.status !== "fresh");
      if (fresh.length && rest.length) for (const [provider, q] of rest) prov.append(gap(provider + " " + ((q && q.status) || "unknown")));
    }
    box.append(prov);
    for (const m of moves) box.append(el("span", "move", m));
    return box;
  }

  function laneLine(adm, session, d) {
    const lane = d.quality_lane || session.quality_lane || adm.quality_lane;
    const box = el("div", "resline");
    box.append(el("span", "k", "Lane"));
    if (!lane) { box.append(el("span", "faint", "none")); return box; }
    const ev = adm.evidence;
    add(box, el("span", "mono", lane), absent(ev) ? gap("evidence") : el("span", ev === "weak" ? "gap" : ev === "qualified" ? "ok" : "faint", ev));
    if (adm.qualification_ref) box.append(el("span", "ref mono", adm.qualification_ref));
    return box;
  }

  function planWhy(ctx, t, col) {
    const p = t.plan;
    col.append(colHead("Effort", p && p.mode === "shadow" ? "shadow" : null));
    if (!p) { col.append(el("p", "gaprow", "plan not recorded")); return col; }
    const ladder = ladderOf(t);
    const from = p.from_effort;
    const to = p.action === "change_effort" ? p.to_effort : (p.mode === "shadow" ? p.recommendation : from);
    const big = el("div", "effbig");
    if (from && to && from !== to) {
      const dir = arrow(ladder, from, to);
      add(big, from, el("span", "faint", " → "), el("span", dir === " ▲" ? "up" : "down", to + dir));
    } else add(big, from || gap(), el("span", "faint", p.action === "blocked" ? " blocked" : " held"));
    col.append(big);
    const st = el("div", "effst");
    const mark = planMark(p);
    add(st, glyph(mark.kind), human(p.action || ""), " · ", el("span", p.status === "outcome_unknown" || p.status === "rejected" ? "gap" : p.status === "confirmed" ? "ok" : null, STATUS_WORDS[p.status] || human(p.status || "")));
    col.append(st);
    if (p.reason) col.append(el("p", "reason", p.reason));
    const ev = p.evidence || {};
    if (ev.error) col.append(el("p", "gaprow", "Jev failed on this turn · the current effort was kept"));
    const rungs = el("div", "ladder");
    const base = p.base_effort;
    for (const name of ladder) {
      const lit = name === to ? mark.kind : name === from && from !== to ? "from" : null;
      const rung = el("div", "rung" + (name === base ? " base" : "") + (lit ? " lit" : ""));
      add(rung, el("i", lit ? "g-" + lit : null), el("span", null, name));
      rungs.append(rung);
    }
    col.append(rungs);
    const th = ev.thresholds || {};
    const list = el("div", "limits");
    for (const [q, key, sign] of PLAN_LIMITS) {
      if (absent(th[key])) continue;
      const v = ev[q];
      const row = el("div", "lim");
      const crossed = !absent(v) && (sign > 0 ? v >= th[key] : v <= th[key]);
      const sym = sign > 0 ? (crossed ? "≥" : "<") : (crossed ? "≤" : ">");
      add(row, el("span", "lk", (sign > 0 ? "raise · " : "lower · ") + human(q)), absent(v) ? gap() : el("span", "mono" + (crossed ? " hot" : ""), num2(v) + " " + sym + " " + num2(th[key])));
      list.append(row);
    }
    if (list.children.length) col.append(list);
    if (!absent(ev.needed_confirmations) && ev.needed_confirmations > 1 && (ev.direction === "down" || (ev.confirmations || 0) > 0)) {
      const conf = el("div", "resline");
      add(conf, el("span", "k", "Step down"), el("span", "mono", int(ev.confirmations || 0) + " of " + int(ev.needed_confirmations)));
      col.append(conf);
    }
    return col;
  }

  function planMark(p) {
    if (p.mode !== "shadow" && p.status !== "rejected") {
      if (p.confirmed_effort) return {kind: "confirmed", effort: p.confirmed_effort};
      // A keep moves nothing, so the ledger fills no confirmed column for it.
      if (p.action === "keep" && p.status === "confirmed" && p.expected_effort) return {kind: "confirmed", effort: p.expected_effort};
      if (p.expected_effort) return {kind: "expected", effort: p.expected_effort};
    }
    const r = p.recommendation || p.to_effort;
    return r ? {kind: "recommended", effort: r} : {kind: "norec", effort: null};
  }

  function glyph(kind) { return el("span", "glyph g-" + kind); }

  // A user turn with no plan: the effort is the admission's, or the last feedback step.
  function fixedWhy(ctx, t, col) {
    col.append(colHead("Effort"));
    const d = t.decision || {};
    const prev = previous(ctx, t);
    const was = prev && prev.decision ? prev.decision.effort : null;
    const big = el("div", "effbig");
    if (was && d.effort && was !== d.effort) {
      const dir = arrow(LADDER, was, d.effort);
      add(big, was, el("span", "faint", " → "), el("span", dir === " ▲" ? "up" : "down", d.effort + dir));
    } else add(big, d.effort ? d.effort : el("span", "faint", "no effort"));
    col.append(big);
    col.append(el("p", "fixed", "Fixed effort · no plan"));
    const admTurn = ctx.turns.find((x) => x.kind === "admission");
    const rule = (admTurn && admTurn.decision && admTurn.decision.rule) || (ctx.adm && ctx.adm.rule) || ctx.session.decision_rule;
    const why = el("div", "resline");
    add(why, el("span", "k", "Rule"), rule ? el("span", "mono", rule) : gap(), note("at admission"));
    col.append(why);
    if (was && d.effort && was !== d.effort) {
      const fb = lastFeedbackBefore(ctx, t);
      const line = el("div", "resline");
      add(line, el("span", "k", "Changed by"), fb ? add(el("span"), "feedback ", verdictBadge(fb.verdict)) : gap("cause not recorded"));
      col.append(line);
    }
    return col;
  }

  function lastFeedbackBefore(ctx, t) {
    let found = null;
    for (const x of ctx.turns) {
      if (x === t) break;
      for (const f of Array.isArray(x.feedback) ? x.feedback : []) if (!found || (f.ts || 0) >= (found.ts || 0)) found = f;
    }
    return found;
  }

  // --- under the columns: how the turn ran -------------------------------------

  function turnFoot(ctx, t) {
    const foot = el("div", "tfoot");
    const calls = ctx.members.get(t) || [];
    const r = t.execution_rollup || {};
    const roll = el("div", "roll");
    const stat = (v, unit) => roll.append(add(el("span", "st"), add(el("b"), v), unit ? " " + unit : null));
    stat(String(calls.length), calls.length === 1 ? "call" : "calls");
    // No call reported usage: one gap says so, not five.
    const none = r.http_requests > 0 && r.usage_reported === 0;
    const partial = r.http_requests && r.usage_reported > 0 && r.usage_reported < r.http_requests;
    if (!none) {
      stat(show(r.input_tokens, kfmt), "in");
      stat(show(r.cached_input_tokens, kfmt), "cached");
      stat(show(r.output_tokens, kfmt), "out");
      const rate = !absent(r.cached_input_tokens) && r.input_tokens ? r.cached_input_tokens / r.input_tokens : null;
      stat(show(rate, pct), "cache");
    }
    stat(show(r.first_byte_ms_p50, ms), "first byte");
    if (!none) stat(show(r.tps_p50, (v) => Math.round(v)), "tok/s");
    if (none) roll.append(gap("usage not reported"));
    if (partial) roll.append(gap("usage on " + r.usage_reported + " of " + r.http_requests));
    foot.append(roll);
    const tools = Array.isArray(r.tool_calls) ? r.tool_calls : [];
    if (tools.length) {
      const chips = el("div", "chips");
      for (const c of tools.slice().sort((a, b) => b.count - a.count)) chips.append(el("span", "chip", c.name + " × " + c.count));
      foot.append(chips);
    }
    if (r.assistant_summary) foot.append(el("blockquote", "asum", r.assistant_summary));
    if (calls.length) foot.append(callTable(calls));
    return foot;
  }

  function callTable(calls) {
    const box = el("details", "calls");
    box.open = I.calls;
    box.addEventListener("toggle", () => { I.calls = box.open; });
    const sum = el("summary", null, "HTTP calls");
    sum.dataset.fk = "calls";
    box.append(sum);
    const table = el("div", "ctable");
    const headRow = el("div", "crow chead");
    for (const h of ["#", "Time", "Status", "First byte", "Total", "In", "Out"]) headRow.append(el("span", null, h));
    table.append(headRow);
    for (const r of calls) {
      const row = el("div", "crow");
      const bad = isUnknown(r.status) || r.status === "rejected" || r.status === "stream_failed";
      const status = el("span", bad ? "gap" : null);
      // A completed call is its HTTP code; anything else says what happened.
      const ok = r.status === "completed" && !absent(r.upstream_status);
      add(status, absent(r.upstream_status) ? "" : String(r.upstream_status), ok ? null : (absent(r.upstream_status) ? "" : " ") + human(r.status || "?"));
      add(row,
        el("span", "faint", String(r.seq)),
        add(el("span"), absent(r.created) ? gap() : clock(r.created)),
        status,
        add(el("span"), show(r.first_byte_ms, ms)),
        add(el("span"), show(r.response_ms, ms)),
        add(el("span"), tokenCell(r, "input_tokens")),
        add(el("span"), tokenCell(r, "output_tokens")));
      table.append(row);
    }
    box.append(table);
    return box;
  }

  // A call that never reached the provider has no usage; one that did and
  // reported none is a gap.
  function tokenCell(r, key) {
    if (!absent(r[key])) return document.createTextNode(int(r[key]));
    if (r.status === "rejected" && absent(r.row)) return el("span", "faint", "–");
    return gap();
  }

  // --- the Jev sandbox ------------------------------------------------------------

  function loadConfig() {
    const hash = S.data && S.data.config_hash;
    if (I.cfgLoading || (I.cfg && I.cfgHash === hash)) return;
    I.cfgLoading = true;
    api("/router/config").then(async (response) => {
      if (response.ok) {
        I.cfg = await response.json();
        I.cfgHash = hash;
        if (S.view === "session" && S.data) render("data");
        if (I.drawer && !I.drawer.hidden && I.probe) fillQuestions();
      }
    }).catch(() => {}).finally(() => { I.cfgLoading = false; });
  }

  function sandboxTurn(ctx) {
    if (ctx.sel && ctx.sel.jev && ctx.sel.kind !== "user_turn") return ctx.sel;
    return ctx.turns.find((t) => t.kind === "admission") || null;
  }

  function toggleDrawer(ctx) {
    if (I.drawer && !I.drawer.hidden) { closeDrawer(); render("pane"); return; }
    openDrawer(ctx);
    render("pane");
  }

  function closeDrawer() {
    if (I.drawer) I.drawer.hidden = true;
    document.body.classList.remove("drawer-open");
    I.probe = null;
  }

  function stateText(t) {
    if (t.state && typeof t.state === "object") return JSON.stringify(t.state, null, 2);
    // Nothing stored: start from what the turn's prompt says.
    return JSON.stringify({latest_user_request: t.user_prompt || ""}, null, 2);
  }

  function openDrawer(ctx) {
    const t = sandboxTurn(ctx);
    if (!I.drawer) I.drawer = buildDrawer();
    I.drawer.hidden = false;
    document.body.classList.add("drawer-open");
    target(t);
    I.drawer.querySelector(".dclose").focus({preventScroll: true});
  }

  function target(t) {
    const stateBox = I.drawer.querySelector(".dstate");
    I.probe = {
      turn: t, row: t ? t.decision_row : null, stored: !!(t && t.state),
      stateInitial: t ? stateText(t) : "", running: false, result: null, error: null, errorMs: null, dirty: false,
    };
    I.drawer.querySelector(".dtarget").textContent = t ? label(t) : "";
    stateBox.value = I.probe.stateInitial;
    stateBox.disabled = !t;
    I.drawer.querySelector(".dqs").replaceChildren();
    fillQuestions();
    paintResult();
  }

  // Follow the selected turn while nothing has been edited or run.
  function drawerSync(ctx) {
    if (!I.probe) return;
    const t = sandboxTurn(ctx);
    if (!t) return;
    const same = I.probe.turn && I.probe.turn.turn_index === t.turn_index;
    if (!same && !I.probe.dirty && !I.probe.result && !I.probe.error && !I.probe.running) target(t);
    else if (same) I.probe.turn = t;
  }

  function buildDrawer() {
    const box = el("aside", "drawer");
    box.setAttribute("aria-label", "Jev sandbox");
    box.hidden = true;
    const head = el("div", "dhead");
    const close = button("ghost dclose", "Close", () => { closeDrawer(); render("pane"); });
    add(head, add(el("h2", "dtitle"), "Jev sandbox ", el("span", "dtarget")), close);
    const form = el("div", "dbody");
    const st = el("label", "dlab", "State");
    const area = el("textarea", "dta dstate");
    area.spellcheck = false;
    area.rows = 12;
    area.setAttribute("aria-label", "State");
    area.addEventListener("input", () => { if (I.probe) I.probe.dirty = true; });
    area.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); area.blur(); } });
    const qs = el("div", "dqs");
    const actions = el("div", "dact");
    const run = button("link drun", "Run", runProbe);
    add(actions, run, el("span", "dms"));
    const result = el("div", "dres");
    add(form, st, area, qs);
    add(box, head, form, add(el("div", "dfoot"), actions, result));
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && e.target.tagName !== "TEXTAREA") { closeDrawer(); render("pane"); e.stopPropagation(); }
    });
    document.body.append(box);
    return box;
  }

  function activeQuestions(t) {
    const names = Object.keys(answersOf(t));
    return names;
  }

  function fillQuestions() {
    if (!I.drawer || !I.probe) return;
    const qs = I.drawer.querySelector(".dqs");
    const old = new Map([...qs.querySelectorAll("textarea")].map((a) => [a.dataset.qid, a.value]));
    qs.replaceChildren();
    const t = I.probe.turn;
    if (!t) return;
    for (const qid of activeQuestions(t)) {
      const q = I.cfg && I.cfg.questions ? I.cfg.questions[qid] : null;
      const lab = el("label", "dlab", cap(qid));
      const area = el("textarea", "dta dq");
      area.dataset.qid = qid;
      area.rows = 4;
      area.spellcheck = false;
      area.setAttribute("aria-label", cap(qid) + " instructions");
      const original = q && typeof q.instructions === "string" ? q.instructions : null;
      area.dataset.original = original === null ? "" : original;
      area.value = old.has(qid) ? old.get(qid) : (original || "");
      area.disabled = original === null;
      area.addEventListener("input", () => { if (I.probe) I.probe.dirty = true; });
      area.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); area.blur(); } });
      qs.append(lab, area);
      if (original === null) qs.append(gap(I.cfg ? "wording not in the running config" : "loading"));
    }
  }

  async function runProbe() {
    const p = I.probe;
    if (!p || p.running || !p.turn || absent(p.row)) return;
    const body = {decision_row: p.row};
    const text = I.drawer.querySelector(".dstate").value;
    // An untouched stored state is sent as nothing, so Jev sees the stored one exactly.
    if (!p.stored || text !== p.stateInitial) {
      try {
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("state must be an object");
        body.state = parsed;
      } catch (error) {
        p.error = "State is not a JSON object · " + (error && error.message ? error.message : "parse error");
        p.errorMs = null;
        p.result = null;
        paintResult();
        return;
      }
    }
    const questions = {};
    for (const area of I.drawer.querySelectorAll(".dq")) {
      if (!area.disabled && area.value !== area.dataset.original) questions[area.dataset.qid] = {instructions: area.value};
    }
    if (Object.keys(questions).length) body.questions = questions;
    p.running = true;
    p.error = null;
    p.result = null;
    paintResult();
    try {
      const response = await post("/router/sandbox/jev", body);
      const json = await response.json().catch(() => ({}));
      if (I.probe !== p) return;
      if (response.ok) p.result = json;
      else {
        p.error = json.error || "HTTP " + response.status;
        p.errorMs = absent(json.jev_ms) ? null : json.jev_ms;
      }
    } catch {
      if (I.probe === p) p.error = "router unreachable";
    } finally {
      if (I.probe === p) {
        p.running = false;
        paintResult();
      }
    }
  }

  function paintResult() {
    if (!I.drawer) return;
    const p = I.probe;
    const run = I.drawer.querySelector(".drun");
    const msNode = I.drawer.querySelector(".dms");
    const box = I.drawer.querySelector(".dres");
    box.replaceChildren();
    msNode.replaceChildren();
    run.disabled = !p || p.running || !p.turn || absent(p.row);
    run.textContent = p && p.running ? "Running…" : "Run";
    if (!p) return;
    if (p.error) {
      const line = el("p", "gaprow");
      add(line, p.error);
      box.append(line);
      if (!absent(p.errorMs)) msNode.append(el("span", "mono", "Jev " + ms(p.errorMs)));
      return;
    }
    if (!p.result) return;
    const r = p.result;
    msNode.append(el("span", "mono", "Jev " + (absent(r.jev_ms) ? "–" : ms(r.jev_ms))));
    const orig = r.original_answers || {};
    const sand = r.sandbox_answers || {};
    const qids = [...new Set([...(Array.isArray(r.questions) ? r.questions : []), ...Object.keys(orig), ...Object.keys(sand)])];
    const withRoute = !!(r.original_route || r.sandbox_route);
    const grid = el("div", "dgrid");
    grid.style.gridTemplateColumns = "4.6rem repeat(" + qids.length + ", minmax(0, 1fr))";
    grid.append(el("span"));
    for (const q of qids) grid.append(el("span", "dh", human(q)));
    const row = (name, answers, route, other, otherRoute) => {
      grid.append(el("span", "dn", name));
      for (const q of qids) {
        const a = answers[q];
        const v = valueOf(a);
        const ov = valueOf(other ? other[q] : null);
        const changed = other && !absent(v) && v !== ov;
        const cell = el("span", "dv" + (changed ? " chg" : "") + (typeof v === "number" ? " num" : ""));
        if (absent(v)) cell.append(gap());
        else {
          cell.append(typeof v === "number" ? num2(v) : human(v));
          if (changed && typeof v === "number" && typeof ov === "number") cell.append(el("span", "delta", (v > ov ? " +" : " ") + num2(v - ov)));
        }
        grid.append(cell);
      }
      if (withRoute) {
        const cell = el("span", "dv droute");
        if (!route || route.error) cell.append(gap(route && route.error ? human(route.error) : "no route"));
        else {
          const same = otherRoute && !otherRoute.error && otherRoute.rule === route.rule && otherRoute.model === route.model && otherRoute.effort === route.effort;
          if (otherRoute && !same) cell.classList.add("chg");
          cell.append(el("span", "mono", (route.rule || "?") + " → " + bare(route.model || "?") + (route.effort ? " · " + route.effort : "")));
        }
        grid.append(cell);
      }
    };
    row("Original", orig, r.original_route, null, null);
    row("Sandbox", sand, r.sandbox_route, orig, r.original_route);
    box.append(grid);
    if (r.config_matches === false) box.append(gap("decision taken under another config"));
  }

  // --- keys ------------------------------------------------------------------------

  function inspectorKey(event) {
    const key = event.key;
    if (key === "j") { neighbour(1); event.preventDefault(); return; }
    if (key === "k") { neighbour(-1); event.preventDefault(); return; }
    if (!S.data) return;
    if (key === "ArrowRight" || key === "]") { step(1); event.preventDefault(); }
    else if (key === "ArrowLeft" || key === "[") { step(-1); event.preventDefault(); }
    else if (key === "Escape") {
      if (I.drawer && !I.drawer.hidden) { closeDrawer(); render("pane"); }
      else go(listHash());
      event.preventDefault();
    }
  }

  window.inspectorView = inspectorView;
  window.inspectorKey = inspectorKey;
})();
