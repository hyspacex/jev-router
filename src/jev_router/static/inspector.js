"use strict";

// The session page (#s=<id>): the title and one strip of facts with the
// feedback at its right, the admission in one sentence, the turns down the
// left and the selected turn on the right in four sections: why this rule
// (or effort), Jev's scores, the request, the calls. Uses the shared
// builders and formatters in dashboard.js. The page writes only on a
// person's click: feedback, an eval case, a sandbox probe.

(() => {
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
  // Calls shown before "N more"; the rest are one click away.
  const CALLS_SHOWN = 12;
  // Rules named in a "not reached" line before it says "and N more".
  const UNREACHED_NAMED = 6;

  // Page state that outlives a refresh: which turn is selected, what is open.
  // Which sections are open is kept for the whole page load, across sessions.
  const I = {
    sid: null, turn: null, pasted: new Set(), scroll: 0,
    open: {why: true, scores: true, request: true, calls: false},
    missed: false, allCalls: false, menu: false,
    posting: null, fbError: null, saved: null, saveError: null,
    cfg: null, cfgHash: null, cfgLoading: false,
    drawer: null, probe: null,
  };

  function sync() {
    if (I.sid === S.sid) return;
    I.sid = S.sid;
    I.turn = null;
    I.pasted = new Set();
    I.scroll = 0;
    I.missed = false;
    I.allCalls = false;
    I.menu = false;
    I.posting = null;
    I.fbError = null;
    I.saved = null;
    I.saveError = null;
    closeDrawer();
  }

  // --- small helpers ----------------------------------------------------------

  const kfmt = (n) => {
    const v = Number(n);
    if (Math.abs(v) < 1000) return String(Math.round(v));
    if (Math.abs(v) < 1e6) return (v / 1000).toFixed(Math.abs(v) < 10000 ? 1 : 0) + "k";
    return (v / 1e6).toFixed(1) + "M";
  };
  const bare = (model) => splitModel(model)[1] || model;
  function answersOf(t) { return t && t.jev && t.jev.answers && typeof t.jev.answers === "object" ? t.jev.answers : {}; }
  function valueOf(a) { return a && typeof a === "object" ? a[a.type] : null; }
  function ladderOf(t) {
    const l = t && t.plan && t.plan.evidence && t.plan.evidence.ladder;
    return Array.isArray(l) && l.length ? l : LADDER;
  }
  // "up", "down", or "" when the ladder cannot say.
  function direction(ladder, from, to) {
    const a = ladder.indexOf(from);
    const b = ladder.indexOf(to);
    if (a < 0 || b < 0 || a === b) return "";
    return b > a ? "up" : "down";
  }
  // "high → xhigh ▲", the arrow in the colour of its direction.
  function move(ladder, from, to) {
    const d = direction(ladder, from, to);
    const node = el("span", "mv");
    add(node, from + " → ", el("span", "to" + (d ? " " + d : ""), to));
    if (d) node.append(el("span", "arrow " + d, d === "up" ? " ▲" : " ▼"));
    return node;
  }
  function label(t) { return "Turn " + t.turn_index + (t.kind === "admission" ? " · admission" : ""); }
  function firstLine(text) {
    const line = String(text).split("\n").find((l) => l.trim()) || "";
    return line.trim();
  }

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
    const admTurn = turns.find((t) => t.kind === "admission") || null;
    return {session, adm, admTurn, reqs, turns, members, sel, trace: d.trace || {available: false, reason: null}, join: d.turn_join};
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

  function admDecision(ctx) { return (ctx.admTurn && ctx.admTurn.decision) || {}; }
  function fellBack(ctx) {
    return !!(admDecision(ctx).fallback || (ctx.adm && ctx.adm.fallback) || ctx.trace.reason === "fallback_admission");
  }

  // --- view ------------------------------------------------------------------

  function inspectorView(kind) {
    sync();
    const root = el("div", "insp");
    if (S.missing) {
      add(root, crumbs(), el("p", "gaprow", S.slug !== null && !S.sid ? "no bookmark named " + S.slug : "session not found"));
      return root;
    }
    if (!S.data) return root;
    loadConfig();
    const ctx = context();
    if (ctx.sel) I.turn = ctx.sel.turn_index;
    // An arrival moves; a choice inside the page moves only the turn's side;
    // the live refresh moves nothing.
    const top = kind === "view" ? "" : " still";
    const low = kind === "data" ? " still" : "";
    add(root, crumbs(), header(ctx, top), decisionLine(ctx, top));
    if (ctx.sel) {
      const split = el("div", "split");
      add(split, turnList(ctx, top), detail(ctx, ctx.sel, low));
      root.append(split);
    } else root.append(el("p", "gaprow", "no turn recorded"));
    requestAnimationFrame(afterRender);
    if (I.drawer && !I.drawer.hidden) drawerSync(ctx);
    return root;
  }

  function afterRender() {
    const list = document.querySelector(".insp .tlist");
    if (list) {
      list.scrollTop = I.scroll;
      const on = list.querySelector(".turn.on");
      if (on && list.scrollHeight > list.clientHeight) {
        const a = list.getBoundingClientRect();
        const b = on.getBoundingClientRect();
        if (b.top < a.top) list.scrollTop -= a.top - b.top + 4;
        else if (b.bottom > a.bottom) list.scrollTop += b.bottom - a.bottom + 4;
        I.scroll = list.scrollTop;
      }
    }
    layoutRulers();
  }

  // Where this session sits in the list it was opened from.
  function crumbs() {
    const nav = el("nav", "crumbs");
    nav.setAttribute("aria-label", "Session list");
    const back = link("back", "← Sessions", listHash());
    back.dataset.fk = "back";
    const summary = filterSummary();
    add(nav, back, summary ? el("span", "fsum", summary) : null);
    const right = el("div", "right");
    const list = sessions();
    const at = list.findIndex((s) => s.session_id === S.sid);
    if (at >= 0) {
      right.append(el("span", null, (at + 1) + " of " + list.length));
      const prev = at > 0 ? list[at - 1] : null;
      const next = at < list.length - 1 ? list[at + 1] : null;
      const p = prev ? link("pg", "Newer", sessionHash(prev.session_id), "Newer session (k)") : el("span", "pg off", "Newer");
      const n = next ? link("pg", "Older", sessionHash(next.session_id), "Older session (j)") : el("span", "pg off", "Older");
      p.dataset.fk = "pg-prev";
      n.dataset.fk = "pg-next";
      add(right, p, n);
    } else if (S.list) {
      right.append(el("span", null, "not in this list"));
    }
    nav.append(right);
    return nav;
  }

  function filterSummary() {
    const f = S.filters;
    const bits = [];
    if (f.q) bits.push("“" + f.q + "”");
    for (const k of ["client", "model", "rule", "state", "feedback"]) if (f[k]) bits.push(human(k) + " is " + human(f[k]));
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

  function verdictOf(session) {
    const fb = session.feedback || {};
    return fb.verdict || session.feedback_verdict || null;
  }

  function header(ctx, still) {
    const {session, reqs, turns} = ctx;
    const root = el("section", "ihead" + still);
    root.append(el("h1", null, session.title || "Session " + short(session.session_id, session.client)));

    const strip = el("div", "strip");
    // The session row carries its provider apart from the model's name.
    const model = session.model && session.provider && !String(session.model).includes("/") ? session.provider + "/" + session.model : session.model;
    strip.append(modelNode(model, session.base_effort, session.effective_effort));
    strip.append(session.client ? el("span", null, session.client) : gap("client not recorded"));
    if (session.workspace_hint) strip.append(el("span", "mono t2 ws", session.workspace_hint));
    if (!absent(session.created)) strip.append(el("span", null, day(session.created) + " " + clock(session.created).slice(0, 5)));
    strip.append(el("span", null, plural(turns.length, "turn")));
    strip.append(el("span", null, plural(reqs.length, "call")));
    if (session.adaptation_mode && session.adaptation_mode !== "off") {
      const e = session.escalation_count;
      strip.append(absent(e) ? add(el("span"), gap("escalations not recorded"), " escalations") : el("span", null, plural(e, "escalation")));
    }
    const verdict = verdictOf(session);
    strip.append(verdict ? (verdictDot(verdict) || el("span", "t2", human(verdict))) : el("span", "t3", "unrated"));
    if (session.state && session.state !== "active") strip.append(badge(session.state === "blocked" ? "warn" : "plain", session.state));
    const acts = el("div", "acts");
    add(acts, feedbackButtons(verdict), moreMenu(ctx, verdict));
    strip.append(acts);
    root.append(strip);

    const fb = session.feedback || {};
    if (fb.note) root.append(el("p", "fbnote", "“" + fb.note + "”"));
    if (I.fbError) root.append(el("p", "gaprow", I.fbError));
    if (I.saved) {
      const line = el("p", "isaved");
      const cases = plural(I.saved.total, "case");
      if (I.saved.duplicate) add(line, el("span", "t2", "Already saved"), el("span", "mono", I.saved.path), el("span", "t3", cases));
      else add(line, el("span", "ok", "Saved"), el("span", "mono", I.saved.path), el("span", "t3", cases));
      root.append(line);
    }
    if (I.saveError) root.append(el("p", "gaprow", I.saveError));
    return root;
  }

  function feedbackButtons(current) {
    const seg = el("div", "fb");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Feedback");
    for (const v of ["right", "too_weak", "too_strong"]) {
      const b = button(current === v ? "on" : "", VERDICT_WORDS[v], () => sendFeedback(v));
      b.dataset.fk = "fb-" + v;
      b.setAttribute("aria-pressed", current === v ? "true" : "false");
      b.disabled = I.posting !== null;
      seg.append(b);
    }
    return seg;
  }

  // "⋯": the two actions that are not a verdict, in a small in-page menu.
  function moreMenu(ctx, verdict) {
    const box = el("div", "more-menu");
    const toggle = button("ghost mbtn", "⋯", () => setMenu(!I.menu, true), "More actions");
    toggle.dataset.fk = "more";
    toggle.setAttribute("aria-haspopup", "menu");
    toggle.setAttribute("aria-expanded", I.menu ? "true" : "false");
    const menu = el("div", "imenu");
    menu.setAttribute("role", "menu");
    menu.hidden = !I.menu;
    const saveText = I.posting === "save" ? "Saving…" : I.saved ? (I.saved.duplicate ? "Already saved" : "Saved as eval case") : "Save as eval case";
    const save = button("mi", saveText, () => { setMenu(false); saveCase(); });
    save.setAttribute("role", "menuitem");
    save.dataset.fk = "save-case";
    save.disabled = !verdict || I.posting === "save";
    if (!verdict) {
      save.title = "Needs a verdict first";
      save.append(el("small", null, "needs a verdict"));
    }
    const open = !!(I.drawer && !I.drawer.hidden);
    const sandbox = button("mi", open ? "Close Jev sandbox" : "Jev sandbox", () => { setMenu(false); toggleDrawer(ctx); });
    sandbox.setAttribute("role", "menuitem");
    sandbox.setAttribute("aria-pressed", open ? "true" : "false");
    sandbox.dataset.fk = "sandbox";
    add(menu, save, sandbox);
    add(box, toggle, menu);
    return box;
  }

  // Opens or closes the menu in place: no render, so nothing else moves.
  function setMenu(open, focus) {
    I.menu = open;
    const menu = document.querySelector(".insp .imenu");
    const toggle = document.querySelector('.insp [data-fk="more"]');
    if (menu) menu.hidden = !open;
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (!focus) return;
    if (open && menu) {
      const first = [...menu.querySelectorAll(".mi")].find((b) => !b.disabled);
      if (first) first.focus({preventScroll: true});
    } else if (toggle) toggle.focus({preventScroll: true});
  }
  document.addEventListener("click", (e) => {
    if (I.menu && !(e.target instanceof Element && e.target.closest(".more-menu"))) setMenu(false);
  });

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
    const verdict = verdictOf(session);
    if (!verdict || I.posting) return;
    I.posting = "save";
    I.saveError = null;
    render("pane");
    try {
      const response = await post("/router/eval-cases", {session_id: sid, verdict});
      const body = await response.json().catch(() => ({}));
      if (sid !== S.sid) return;
      if (response.ok && body.saved) I.saved = {path: body.path, total: body.total_cases};
      else if (response.ok && body.duplicate) I.saved = {path: body.path, total: body.total_cases, duplicate: true};
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

  // --- the admission in one sentence ----------------------------------------------

  function decisionLine(ctx, still) {
    const {adm, trace, session} = ctx;
    const box = el("section", "decision" + still);
    if (!adm) {
      add(box, el("span", "lead", "Admission not recorded"));
      if (session.decision_rule) add(box, el("span", "why", "the session names rule"), el("b", "mono", session.decision_rule));
      return box;
    }
    const d = admDecision(ctx);
    const route = trace.route || d.route || adm.route;
    if (fellBack(ctx)) {
      add(box, el("span", "lead", "Admitted by fallback"), add(route ? el("b", "mono", route) : gap("route not recorded"), el("span", "lead", ":")),
        el("span", "why", "Jev failed, no rule ran"), badge("warn", "fallback", "The alias's admission_fallback route bound this session"));
      return box;
    }
    const rule = (trace.available && trace.matched) || d.rule || adm.rule || session.decision_rule;
    add(box, el("span", "lead", "Admitted by"), rule ? el("b", "mono", rule) : gap("rule not recorded"));
    const why = el("span", "why");
    const bits = [];
    if (trace.available) bits.push(hitWords(trace));
    else bits.push(el("span", "t3", reasonText(trace.reason)));
    if (route) bits.push("route " + route);
    const q = quotaWords(adm, session);
    if (q) bits.push("quota " + q);
    const lane = laneInfo(ctx);
    if (lane) bits.push("lane " + lane.name + (lane.word ? ", " + lane.word : ""));
    bits.forEach((b, i) => add(why, i ? " · " : null, b));
    box.append(why);
    if (trace.available && !trace.reproduces) box.append(badge("warn", "replay differs", replayDiff(trace)));
    if (trace.available) {
      const replay = button("replay", "replay", () => openReplay(ctx), "Open the rule-by-rule replay");
      replay.dataset.fk = "replay";
      box.append(replay);
    }
    return box;
  }

  // The deciding rule's conditions, in words: "difficulty 2.33 ≥ 1.4 with tools".
  function hitWords(trace) {
    const node = el("span");
    const gate = trace.gate || {};
    if (trace.matched === "low_confidence" && gate.applies && !gate.passed) return add(node, "the confidence gate fired: ", gateFired(gate));
    if (trace.matched === "default") return add(node, "no rule matched");
    const hit = (Array.isArray(trace.rules) ? trace.rules : []).find((r) => r.outcome === "hit");
    if (!hit) return add(node, gap("hit rule not in the trace"));
    const conds = Array.isArray(hit.conditions) ? hit.conditions : [];
    if (!conds.length) return add(node, el("span", "t3", "always applies"));
    const answers = conds.filter((c) => c.source !== "feature" || typeof c.value !== "boolean");
    const flags = conds.filter((c) => c.source === "feature" && typeof c.value === "boolean");
    answers.forEach((c, i) => add(node, i ? ", " : null, condText(c)));
    for (const c of flags) add(node, node.childNodes.length ? " " : null, condWords(c));
    return node;
  }

  function replayDiff(trace) {
    const rep = trace.replayed || {};
    const was = trace.stored || {};
    return "Recorded " + [was.rule, was.model, was.effort].filter(Boolean).join(" ") + "; the replay gives " + [rep.rule, rep.model, rep.effort].filter(Boolean).join(" ");
  }

  // "replay": the admission turn, its Why section open and in view.
  function openReplay(ctx) {
    I.open.why = true;
    if (ctx.admTurn && I.turn !== ctx.admTurn.turn_index) {
      I.turn = ctx.admTurn.turn_index;
      render("pane");
    }
    requestAnimationFrame(() => {
      const sec = document.querySelector('.insp details[data-sec="why"]');
      if (!sec) return;
      sec.open = true;
      sec.scrollIntoView({block: "start", behavior: "smooth"});
      const sum = sec.querySelector("summary");
      if (sum) sum.focus({preventScroll: true});
    });
  }

  function reasonText(reason) {
    if (!reason) return "not replayed";
    if (reason === "not_on_file") return "not replayed: its config is not on file";
    if (reason === "no_answers") return "not replayed: no answers stored";
    if (reason === "no_admission_row") return "not replayed: no admission row";
    if (reason === "alias_not_in_config") return "not replayed: the alias is not in its config";
    return "not replayed: " + human(reason);
  }

  // The counterfactual is the zero-pressure choice, stored on every row as
  // "model(effort or -)". It is a change only when it differs from the choice.
  function chosen(adm) { return (adm.model || "?") + "(" + (adm.effort || "-") + ")"; }
  function counterfactualDiffers(adm) { return !!adm.counterfactual && adm.counterfactual !== chosen(adm); }

  function quotaMoves(adm) {
    const moves = [];
    if (Array.isArray(adm.shifted)) for (const s of adm.shifted) moves.push(human(s.rule || "?") + " " + human(s.question || "") + " " + cfg(s.base) + " → " + cfg(s.effective) + (s.provider ? " by " + s.provider : ""));
    if (adm.reordered === true) moves.push("route reordered");
    if (counterfactualDiffers(adm)) moves.push("without quota " + adm.counterfactual);
    return moves;
  }

  // What quota did, in words, or null when nothing was recorded to say it.
  function quotaWords(adm, session) {
    const moves = quotaMoves(adm);
    if (moves.length) return "shifted: " + moves.join(", ");
    if (session.quota_changed_choice === true || adm.quota_changed_choice === true) return "steered, detail not recorded";
    // Kept is a finding only where something was recorded to compare: the
    // zero-pressure choice, or the admission's own flag.
    if (adm.counterfactual || adm.quota_changed_choice === false || session.quota_changed_choice === false) return "kept the primary";
    return null;
  }

  // The admission's lane, or null when it bound none.
  function laneInfo(ctx) {
    const {adm, session, trace} = ctx;
    if (!adm || fellBack(ctx)) return null;
    const d = admDecision(ctx);
    let lane = d.quality_lane || session.quality_lane || adm.quality_lane;
    if (trace.available && trace.replayed && "lane" in trace.replayed) lane = trace.replayed.lane;
    else {
      // A route-level answer records the route's name in the lane column.
      const lanes = I.cfg && I.cfg.quality_lanes;
      const route = trace.route || d.route || adm.route;
      if (lane && lanes && !(lane in lanes) && lane === route) lane = null;
    }
    if (!lane) return null;
    const deferred = Array.isArray(adm.exclusions) && adm.exclusions.some((x) => x && typeof x.reason === "string" && x.reason.startsWith("not qualified for lane"));
    const ev = adm.evidence;
    const word = deferred ? "deferred" : ev === "qualified" ? "qualified" : ev === "weak" ? "weak evidence" : absent(ev) ? null : human(ev);
    return {name: lane, word, ev, deferred, ref: adm.qualification_ref || null};
  }

  // --- the turn list -----------------------------------------------------------

  function turnList(ctx, still) {
    const box = band("Turns", ctx.join === "by_order" ? "calls joined by log order" : null, "turns" + still);
    const list = el("div", "tlist");
    list.addEventListener("scroll", () => { I.scroll = list.scrollTop; }, {passive: true});
    for (const t of ctx.turns) list.append(turnButton(ctx, t));
    box.append(list);
    return box;
  }

  function turnButton(ctx, t) {
    const on = ctx.sel === t;
    const corr = corrective(t);
    const b = button("turn" + (on ? " on" : "") + (corr ? " corr" : ""), "", () => pick(t.turn_index));
    b.dataset.fk = "turn-" + t.turn_index;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    const top = el("div", "t");
    top.append(el("b", null, label(t)));
    if (corr) top.append(badge("warn", "corrective", "Corrective follow-up " + num2(valueOf(answersOf(t).corrective_followup)) + " ≥ " + cfg(threshold(t, "corrective_followup"))));
    const fb = Array.isArray(t.feedback) && t.feedback.length ? t.feedback[t.feedback.length - 1].verdict : null;
    if (fb) top.append(verdictDot(fb) || el("span", "t2", human(fb)));
    top.append(el("span", "tm", absent(t.ts) ? "" : clock(t.ts).slice(0, 5)));
    const p = el("div", "p");
    const text = t.user_prompt || (t.state && (t.state.current_user_request || t.state.latest_user_request));
    if (text) p.textContent = firstLine(text);
    else p.append(gap("prompt not recorded"));
    const calls = ctx.members.get(t) || [];
    const m = el("div", "m");
    add(m, add(el("span", "ch"), changeLine(ctx, t)), el("span", "n", plural(calls.length, "call")));
    for (const [k, v] of Object.entries(problemCounts(calls))) if (v) m.append(badge(k === "unknown" ? "warn" : "bad", v + " " + PROBLEM_SHORT[k]));
    add(b, top, p, m);
    return b;
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
      if (d.fallback) return add(node, "fallback → ", d.model ? bare(d.model) : gap("model not recorded"), d.effort ? " · " + d.effort : "");
      add(node, d.rule ? d.rule : gap("rule not recorded"), " → ", d.model ? bare(d.model) : gap("model not recorded"), d.effort ? " · " + d.effort : "");
      return node;
    }
    if (t.kind === "plan") {
      const p = t.plan;
      if (!p) return add(node, gap("plan not recorded"));
      const ladder = ladderOf(t);
      const from = p.from_effort || "?";
      if (p.mode === "shadow") {
        const rec = p.recommendation || p.to_effort;
        return add(node, el("span", "t3", "would "), rec && rec !== from ? move(ladder, from, rec) : el("span", "t3", "hold " + from));
      }
      if (p.action === "change_effort") return add(node, move(ladder, from, p.to_effort || "?"));
      if (p.action === "blocked") return add(node, "blocked at " + from);
      return add(node, "held " + from);
    }
    const prev = previous(ctx, t);
    const was = prev && prev.decision ? prev.decision.effort : null;
    if (was && d.effort && was !== d.effort) return add(node, move(LADDER, was, d.effort));
    return add(node, el("span", "t3", d.effort ? "fixed " + d.effort : "no plan"));
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

  function detail(ctx, t, still) {
    const box = el("section", "band detail" + still);
    box.setAttribute("aria-label", label(t));
    box.dataset.turn = String(t.turn_index);
    add(box, whySection(ctx, t), scoresSection(ctx, t), requestSection(ctx, t), callsSection(ctx, t));
    return box;
  }

  // One folding section; whether it is open is remembered for the page load.
  function sec(key, title, note) {
    const box = el("details", "sec");
    box.dataset.sec = key;
    box.open = !!I.open[key];
    box.addEventListener("toggle", () => {
      I.open[key] = box.open;
      if (box.open) requestAnimationFrame(layoutRulers);
    });
    const sum = el("summary");
    sum.dataset.fk = "sec-" + key;
    add(sum, el("span", "st", title), note ? add(el("span", "note"), note) : null);
    const body = el("div", "body");
    box.append(sum, body);
    return [box, body];
  }

  function jevNote(t) {
    const j = t.jev;
    if (!j) return null;
    const bits = [];
    if (!absent(j.latency_ms)) bits.push("Jev " + ms(j.latency_ms));
    if (!absent(j.tokens)) bits.push(int(j.tokens) + " tokens");
    return bits.length ? bits.join(" · ") : null;
  }

  // --- 1. why this rule, or this effort ---------------------------------------------

  function whySection(ctx, t) {
    if (t.kind === "admission") {
      const note = el("span");
      const jn = jevNote(t);
      if (jn) note.append(jn + " · ");
      const hash = hash8(ctx.trace.config_hash || (ctx.adm && ctx.adm.config_hash));
      if (hash) {
        const c = el("span", null, "config " + hash);
        if (ctx.trace.available) {
          c.append(ctx.trace.source === "snapshot" ? " from snapshot" : " from running config");
          if (ctx.trace.source === "snapshot") c.title = "The running config has moved since this admission; it is replayed under the config it was taken under";
        } else if (ctx.session.config_matches === false) c.append(", not the running one");
        note.append(c);
      } else note.append(gap("config hash not recorded"));
      const [box, body] = sec("why", "Why this rule", note);
      admissionWhy(ctx, t, body);
      return box;
    }
    const [box, body] = sec("why", "Why this effort", t.kind === "plan" ? jevNote(t) : null);
    if (t.kind === "plan") planWhy(t, body);
    else fixedWhy(ctx, t, body);
    return box;
  }

  function stepRow(cls, name, text, dot) {
    const row = el("div", "step" + (cls ? " " + cls : ""));
    const w = add(el("span", "w"), text);
    w.title = w.textContent;
    add(row, el("i", "g" + (dot ? " " + dot : "")), el("span", "n", name), w);
    return row;
  }

  function routeText(ctx, t) {
    const d = t.decision || {};
    const adm = ctx.adm || {};
    const model = d.model || adm.model;
    const effort = d.effort || adm.effort;
    return (ctx.trace.route || d.route || adm.route || "direct") + " → " + (model || "?") + (effort ? " · " + effort : "");
  }

  function admissionWhy(ctx, t, body) {
    const {adm, trace} = ctx;
    if (!adm) { body.append(el("p", "gaprow", "admission not recorded")); return; }
    const d = t.decision || {};
    if (fellBack(ctx)) {
      body.append(stepRow("hit", d.rule || adm.rule || "admission_fallback", "Jev failed, no rule ran · the alias's fallback → " + routeText(ctx, t), "warn"));
    } else if (trace.available) {
      if (!trace.reproduces) body.append(add(el("p", "diff"), badge("warn", "replay differs"), " " + replayDiff(trace)));
      ruleSteps(ctx, t, body);
    } else {
      body.append(el("p", "quiet", reasonText(trace.reason)));
      body.append(stepRow("hit", d.rule || adm.rule || "?", "recorded · → " + routeText(ctx, t)));
    }
    const kv = el("div", "kv");
    add(kv, el("span", "k", "Quota"), quotaValue(adm, ctx.session));
    add(kv, el("span", "k", "Lane"), laneValue(ctx));
    body.append(kv);
  }

  // The gate, the rules missed before the hit (folded), the hit, and the rest.
  function ruleSteps(ctx, t, body) {
    const {trace} = ctx;
    const gate = trace.gate || {};
    if (gate.applies) {
      const fired = !gate.passed;
      body.append(stepRow(fired ? "fired" : null, "confidence gate", fired ? add(el("span"), "fired · ", gateFired(gate)) : gatePassed(gate), fired ? "warn" : "good"));
    }
    const rules = Array.isArray(trace.rules) ? trace.rules : [];
    const at = rules.findIndex((r) => r.outcome === "hit");
    const before = at < 0 ? rules.filter((r) => r.outcome !== "not_reached") : rules.slice(0, at);
    const after = at < 0 ? rules.filter((r) => r.outcome === "not_reached") : rules.slice(at + 1);
    if (before.length) {
      const fold = el("div", "step fold");
      const list = el("div", "missed");
      list.hidden = !I.missed;
      const toggle = button("lk", I.missed ? "hide them" : "show them", () => {
        I.missed = !I.missed;
        list.hidden = !I.missed;
        toggle.textContent = I.missed ? "hide them" : "show them";
        toggle.setAttribute("aria-expanded", I.missed ? "true" : "false");
      });
      toggle.dataset.fk = "missed";
      toggle.setAttribute("aria-expanded", I.missed ? "true" : "false");
      add(fold, el("i", "g"), add(el("span"), plural(before.length, "rule") + " missed" + (at < 0 ? "" : " before it") + " · ", toggle));
      for (const r of before) list.append(stepRow("miss", r.index + " · " + (r.name || "?"), ruleWords(r)));
      add(body, fold, list);
    }
    if (at >= 0) {
      const r = rules[at];
      body.append(stepRow("hit", r.index + " · " + (r.name || "?"), add(el("span"), ruleWords(r), " → " + routeText(ctx, t)), "good"));
    } else if (trace.matched === "low_confidence") {
      body.append(stepRow("hit", "low_confidence", "the gate's route → " + routeText(ctx, t), "good"));
    } else if (trace.matched === "default") {
      body.append(stepRow("hit", "default", "no rule matched → " + routeText(ctx, t), "good"));
    }
    if (after.length) {
      const names = after.map((r) => r.name || "?");
      const shown = names.length > UNREACHED_NAMED ? names.slice(0, UNREACHED_NAMED - 1).join(", ") + " and " + (names.length - UNREACHED_NAMED + 1) + " more" : names.join(", ");
      const fold = el("div", "step fold");
      const text = el("span", null, shown + " not reached");
      text.title = names.join(", ");
      add(fold, el("i", "g"), text);
      body.append(fold);
    }
  }

  // A feature flag in words ("no images", "with tools"); anything else as condText.
  function condWords(c) {
    if (c.source === "feature" && typeof c.value === "boolean") {
      const name = String(c.key || "?").replace(/^has_/, "").replaceAll("_", " ");
      return el("span", null, (c.value ? "with " : "no ") + name);
    }
    return condText(c);
  }

  // ruleWhy's wording, with the flags in words.
  function ruleWords(r) {
    const conds = Array.isArray(r.conditions) ? r.conditions : [];
    if (r.outcome === "hit" && conds.length) {
      const node = el("span");
      conds.forEach((c, i) => add(node, i ? " · " : null, condWords(c)));
      if (r.shift && typeof r.shift === "object") add(node, " · shifted " + cfg(r.shift.base) + " → " + cfg(r.shift.effective) + (r.shift.provider ? " by " + r.shift.provider : ""));
      return node;
    }
    const first = r.outcome === "miss" ? conds.find((c) => c.passed === false) : null;
    if (first && first.source === "feature" && typeof first.value === "boolean") return condWords(first);
    return ruleWhy(r);
  }

  function gateFired(gate) {
    const q = gate.fired_by;
    const c = q && gate.questions ? gate.questions[q] : null;
    if (!q) return el("span", null, gate.detail || "");
    return el("span", null, human(q) + " confidence " + (absent(c) ? "?" : num2(c)) + " < " + cfg(gate.min_confidence));
  }

  function gatePassed(gate) {
    if (typeof gate.detail === "string" && gate.detail.includes("stood down")) return "passed · " + gate.detail;
    const qs = Object.entries(gate.questions || {}).filter(([, c]) => !absent(c));
    if (!qs.length) return "passed";
    return "passed · " + qs.map(([q, c]) => human(q) + " " + num2(c)).join(", ") + (qs.length > 1 ? ", all ≥ " : " ≥ ") + cfg(gate.min_confidence);
  }

  // Quota: the words, then each provider's recorded reading.
  function quotaValue(adm, session) {
    const box = el("span", "iquota");
    const said = quotaWords(adm, session);
    box.append(said ? document.createTextNode(said) : gap("what quota did was not recorded"));
    const snap = adm.quota_snapshot;
    if (!snap || typeof snap !== "object" || !Object.keys(snap).length) {
      add(box, " · ", gap("no quota reading recorded"));
      return box;
    }
    const entries = Object.entries(snap);
    const fresh = entries.filter(([, q]) => q && q.status === "fresh");
    if (fresh.length) add(box, " · ", fresh.map(([p, q]) => p + " pressure " + (absent(q.pressure) ? "?" : pressureText(q.pressure))).join(", "));
    for (const [provider, q] of entries.filter(([, q]) => q && q.status === "error")) {
      add(box, " ", badge("warn", provider + " reading failed", "An errored reading contributes 0"), el("span", "t3", " (read as 0)"));
    }
    // Stale and unknown stay themselves: never read as unused capacity.
    const quiet = new Map();
    for (const [provider, q] of entries) {
      const st = q && q.status;
      if (st === "fresh" || st === "error") continue;
      const word = st || "unknown";
      if (!quiet.has(word)) quiet.set(word, []);
      quiet.get(word).push(provider);
    }
    for (const [word, names] of quiet) {
      const s = el("span", "t3", " · " + names.join(", ") + " " + word + " (read as 0)");
      s.title = "A " + word + " reading contributes 0 pressure";
      box.append(s);
    }
    return box;
  }

  function laneValue(ctx) {
    const box = el("span", "ilane");
    if (fellBack(ctx)) return add(box, el("span", "t3", "no lane · fallback"));
    const lane = laneInfo(ctx);
    if (!lane) return add(box, el("span", "t3", "none"));
    box.append(el("span", "mono", lane.name));
    if (lane.deferred) add(box, " ", badge("warn", "deferred", "Pressure found a model this lane has no evidence for; the adequate provider was kept"));
    else if (lane.ev === "qualified") add(box, " · qualified");
    else if (lane.ev === "weak") add(box, " ", badge("warn", "weak evidence", "The same adequacy under both policies cannot be claimed"));
    else if (absent(lane.ev)) add(box, " ", gap("evidence not recorded"));
    else add(box, " · " + human(lane.ev));
    if (lane.ref) box.append(el("span", "ref t3", lane.ref));
    return box;
  }

  function planWhy(t, body) {
    const p = t.plan;
    if (!p) { body.append(el("p", "gaprow", "plan not recorded")); return; }
    const ladder = ladderOf(t);
    const from = p.from_effort;
    const shadow = p.mode === "shadow";
    const to = p.action === "change_effort" ? p.to_effort : (shadow ? (p.recommendation || p.to_effort) : from);
    const plan = el("div", "plan");
    const big = el("div", "big");
    if (shadow) {
      big.append(el("span", "w", "would have "));
      if (from && to && from !== to) add(big, el("span", "w", "gone "), move(ladder, from, to));
      else add(big, el("span", "w", "held "), from || gap("effort not recorded"));
    } else if (from && to && from !== to) big.append(move(ladder, from, to));
    else if (p.action === "blocked") add(big, el("span", "w", "blocked at "), from || gap("effort not recorded"));
    else add(big, el("span", "w", "held "), from || gap("effort not recorded"));
    plan.append(big);
    const st = el("div", "pst");
    add(st, human(p.action || "?"), " · ", statusNode(p.status));
    if (shadow) add(st, " ", badge("plain", "shadow", "Recorded, never applied"));
    plan.append(st);
    if (p.reason) plan.append(el("p", "reason", p.reason));
    const ev = p.evidence || {};
    if (ev.error) plan.append(add(el("p", "reason"), badge("warn", "Jev failed"), " on this turn; the current effort was kept"));
    const th = ev.thresholds || {};
    const kv = el("div", "kv plim");
    for (const [q, key, sign] of PLAN_LIMITS) {
      // A threshold for a question this turn did not weigh says nothing here.
      if (absent(th[key]) || absent(ev[q])) continue;
      const v = ev[q];
      const crossed = !absent(v) && (sign > 0 ? v >= th[key] : v <= th[key]);
      const sym = sign > 0 ? (crossed ? "≥" : "<") : (crossed ? "≤" : ">");
      add(kv, el("span", "k", (sign > 0 ? "raise at " : "lower at ") + human(q)),
        el("span", "mono" + (crossed ? " hot" : " t2"), num2(v) + " " + sym + " " + cfg(th[key])));
    }
    if (!absent(ev.needed_confirmations) && ev.needed_confirmations > 1 && (ev.direction === "down" || (ev.confirmations || 0) > 0)) {
      add(kv, el("span", "k", "step down"), el("span", "mono", int(ev.confirmations || 0) + " of " + int(ev.needed_confirmations) + " turns"));
    }
    const rungs = el("span", "rungs");
    ladder.forEach((name, i) => add(rungs, i ? " · " : null, el("span", name === p.base_effort ? "base" : null, name + (name === p.base_effort ? " (base)" : ""))));
    add(kv, el("span", "k", "ladder"), rungs);
    plan.append(kv);
    body.append(plan);
  }

  function statusNode(status) {
    if (status === "confirmed") return el("span", "ok", STATUS_WORDS.confirmed);
    if (status === "rejected") return badge("bad", STATUS_WORDS.rejected);
    if (status === "outcome_unknown") return badge("warn", STATUS_WORDS.outcome_unknown);
    if (absent(status)) return gap("status not recorded");
    return el("span", null, STATUS_WORDS[status] || human(status));
  }

  // A user turn with no plan: the effort is the admission's, or the last feedback step.
  function fixedWhy(ctx, t, body) {
    const d = t.decision || {};
    const prev = previous(ctx, t);
    const was = prev && prev.decision ? prev.decision.effort : null;
    const moved = was && d.effort && was !== d.effort;
    const plan = el("div", "plan");
    const big = el("div", "big");
    if (moved) big.append(move(LADDER, was, d.effort));
    else big.append(d.effort ? document.createTextNode(d.effort) : el("span", "w", "no effort"));
    plan.append(big);
    plan.append(el("div", "pst", "Not assessed · Jev was not asked on this turn"));
    const fb = moved ? lastFeedbackBefore(ctx, t) : null;
    if (!moved) plan.append(el("p", "reason", "The effort is the admission's: fixed, with no plan."));
    else if (fb) plan.append(add(el("p", "reason"), "Stepped by feedback · ", verdictDot(fb.verdict) || human(fb.verdict)));
    else plan.append(add(el("p", "reason"), "Moved · ", gap("cause not recorded")));
    const rule = admDecision(ctx).rule || (ctx.adm && ctx.adm.rule) || ctx.session.decision_rule;
    const kv = el("div", "kv");
    add(kv, el("span", "k", "rule"), add(el("span"), rule ? el("span", "mono", rule) : gap("rule not recorded"), el("span", "t3", " at admission")));
    plan.append(kv);
    body.append(plan);
  }

  function lastFeedbackBefore(ctx, t) {
    let found = null;
    for (const x of ctx.turns) {
      if (x === t) break;
      for (const f of Array.isArray(x.feedback) ? x.feedback : []) if (!found || (f.ts || 0) >= (found.ts || 0)) found = f;
    }
    return found;
  }

  // --- 2. Jev scores ----------------------------------------------------------

  function scoresSection(ctx, t) {
    const [box, body] = sec("scores", "Jev scores", null);
    if (t.kind === "user_turn") { body.append(el("p", "quiet", "Not asked")); return box; }
    if (!t.jev) { body.append(el("p", "gaprow", "not recorded")); return box; }
    const ans = answersOf(t);
    const names = Object.keys(ans);
    if (!names.length) {
      const fell = t.decision && t.decision.fallback;
      body.append(el("p", fell ? "quiet" : "gaprow", fell ? "Jev failed · no answers" : "answers not recorded"));
      return box;
    }
    const ticks = t.kind === "plan" ? planTicks(t) : ruleTicks(ctx.trace);
    const order = (a) => ({choice: 0, score: 1, noul: 2}[ans[a] && ans[a].type] ?? 3);
    for (const name of names.slice().sort((a, b) => order(a) - order(b))) body.append(scoreRow(name, ans[name], ticks.get(name) || []));
    const sh = t.jev.shadow_answers;
    if (sh && typeof sh === "object" && Object.keys(sh).length) {
      const quiet = el("div", "shadowq");
      quiet.append(el("div", "note", "Shadow · measured, never read"));
      for (const [name, a] of Object.entries(sh)) quiet.append(scoreRow(name, a, []));
      body.append(quiet);
    }
    return box;
  }

  // Every numeric answer threshold the replayed ladder compared, per question.
  // A threshold that decided a rule (the hit, or the miss it alone failed) is bright.
  function ruleTicks(trace) {
    const out = new Map();
    if (!trace || !trace.available) return out;
    for (const r of Array.isArray(trace.rules) ? trace.rules : []) {
      const deciding = new Set(decidingConditions(r));
      const conds = Array.isArray(r.conditions) ? r.conditions : [];
      for (const c of conds) {
        if (c.source !== "answer" || !["gte", "gt", "lte", "lt"].includes(c.op) || typeof c.threshold !== "number") continue;
        if (!out.has(c.key)) out.set(c.key, []);
        // A rule this request fails on another condition (no images, no
        // tools, a task it excludes) could not have fired at this tick.
        const blocked = conds.some((o) => o !== c && conditionPasses(o) === false);
        out.get(c.key).push({value: c.threshold, name: r.name || "?", on: deciding.has(c), hit: r.outcome === "hit", dim: blocked && !deciding.has(c)});
      }
    }
    return out;
  }

  // The trace's own verdict on a condition, or, for a rule it never reached,
  // the same comparison made here from the recorded value.
  function conditionPasses(c) {
    if (typeof c.passed === "boolean") return c.passed;
    const v = c.value;
    const t = c.threshold;
    if (absent(v)) return null;
    switch (c.op) {
      case "eq": return v === t;
      case "ne": return v !== t;
      case "gte": return typeof v === "number" && v >= t;
      case "gt": return typeof v === "number" && v > t;
      case "lte": return typeof v === "number" && v <= t;
      case "lt": return typeof v === "number" && v < t;
      case "in": return Array.isArray(t) ? t.includes(v) : null;
      case "not_in": return Array.isArray(t) ? !t.includes(v) : null;
      default: return null;
    }
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

  // Name, value with its confidence, then a bar for a choice or a ruler for a score.
  function scoreRow(name, a, ticks) {
    const row = el("div", "score");
    const nm = el("span", "q t2", cap(name));
    nm.title = name;
    const val = el("span", "v");
    const right = el("div", "viz");
    add(row, nm, val, right);
    if (!a || typeof a !== "object") { val.append(gap("no answer recorded")); return row; }
    const v = valueOf(a);
    const conf = !absent(a.confidence) ? el("small", null, num2(a.confidence)) : null;
    if (conf) conf.title = "confidence";
    if (a.type === "choice") {
      add(val, absent(v) ? gap("no answer recorded") : human(v), conf);
      val.title = absent(v) ? "" : human(v);
      const probs = a.probabilities && typeof a.probabilities === "object" ? Object.entries(a.probabilities).filter(([, p]) => typeof p === "number" && p >= 0.01).sort((x, y) => y[1] - x[1]) : [];
      if (probs.length) {
        const stack = el("div", "stack");
        for (const [, p] of probs) {
          const seg = el("i");
          seg.style.width = (p * 100) + "%";
          stack.append(seg);
        }
        add(right, stack, el("div", "note", probs.slice(0, 3).map(([k, p]) => human(k) + " " + pct(p)).join(" · ")));
      }
      return row;
    }
    if (a.type !== "score" && a.type !== "noul") { add(val, show(v, plain)); return row; }
    add(val, show(v, num2), conf);
    right.append(ruler(questionMax(name, a, ticks), ticks, v));
    return row;
  }

  // 0 to max, the thresholds as ticks labelled "value rule", the answer as the one dot.
  function ruler(top, ticks, value) {
    const x = (v) => Math.max(0, Math.min(100, v / (top || 1) * 100));
    const box = el("div", "iruler");
    box.title = "0 to " + cfg(top);
    box.append(el("div", "rt"));
    // Near-equal cutoffs (1.399 and 1.4) are one tick. The deciding rule
    // names it; the others are counted, and listed on hover.
    const merged = [];
    for (const tk of ticks.slice().sort((p, q) => p.value - q.value)) {
      const last = merged[merged.length - 1];
      if (last && Math.abs(x(tk.value) - x(last.value)) < 1) {
        last.rules.push(tk);
        if (tk.on) last.on = true;
      } else merged.push({value: tk.value, rules: [tk], on: tk.on});
    }
    const rank = (r) => (r.on ? 0 : r.dim ? 2 : 1);
    for (const m of merged) {
      const names = [...new Map(m.rules.slice().sort((a, b) => rank(a) - rank(b)).map((r) => [r.name, r])).values()];
      const allDim = names.every((r) => r.dim);
      const tick = el("i", "rtk" + (m.on ? " on" : ""));
      tick.style.left = x(m.value) + "%";
      const lab = el("span", "rlab" + (m.on ? " on" : allDim ? " dim" : ""), cfg(Number(names[0].value.toFixed(3))) + " " + names[0].name + (names.length > 1 ? " +" + (names.length - 1) : ""));
      lab.title = cfg(m.value) + ": " + names.map((r) => r.name).join(", ");
      lab.dataset.x = String(x(m.value));
      lab.style.left = x(m.value) + "%";
      box.append(tick, lab);
    }
    if (!absent(value)) {
      const mk = el("i", "rdot");
      mk.style.left = x(value) + "%";
      mk.title = num2(value);
      box.append(mk);
    }
    return box;
  }

  // Labels sit under their tick, kept inside the ruler; one that would touch
  // another drops to the first row where it touches nothing.
  function layoutRulers() {
    for (const box of document.querySelectorAll(".insp .iruler")) {
      const width = box.clientWidth;
      if (!width) continue;
      const rows = [];
      const free = (row, left, right) => (rows[row] || []).every(([a, b]) => right + 8 <= a || left >= b + 8);
      const labels = [...box.querySelectorAll(".rlab")].sort((a, b) => Number(a.dataset.x) - Number(b.dataset.x));
      for (const lab of labels) {
        const w = lab.offsetWidth;
        const left = Math.max(0, Math.min(width - w, Number(lab.dataset.x) / 100 * width - w / 2));
        let row = 0;
        while (!free(row, left, left + w)) row += 1;
        (rows[row] = rows[row] || []).push([left, left + w]);
        lab.style.left = left + "px";
        lab.style.top = (16 + row * 13) + "px";
        lab.classList.add("placed");
      }
      box.style.height = (16 + Math.max(1, rows.length) * 13 + 2) + "px";
    }
  }
  window.addEventListener("resize", () => requestAnimationFrame(layoutRulers));

  // --- 3. the request -------------------------------------------------------------

  // Tools, images, tokens and headroom: from the admission, or the turn's first call.
  function factsOf(ctx, t) {
    if (t.kind === "admission" && ctx.adm && ctx.adm.facts && Object.keys(ctx.adm.facts).length) return ctx.adm.facts;
    const first = (ctx.members.get(t) || []).find((r) => r.facts && Object.keys(r.facts).length);
    return first ? first.facts : null;
  }

  function pastedOf(st) {
    const pasted = [];
    if (!st) return pasted;
    if (st.material_the_user_pasted) pasted.push(st.material_the_user_pasted);
    for (const q of Array.isArray(st.quoted_material) ? st.quoted_material : []) if (q && q.content) pasted.push(q.content);
    return pasted;
  }

  function requestSection(ctx, t) {
    const f = factsOf(ctx, t);
    const note = el("span");
    if (f) {
      const input = !absent(f.input_tokens) ? f.input_tokens : f.est_tokens;
      const bits = [];
      if (!absent(input)) bits.push(int(input) + " tokens in");
      if (!absent(f.headroom)) bits.push("headroom " + kfmt(f.headroom) + (absent(f.context_window) ? "" : " of " + kfmt(f.context_window)));
      note.append(bits.join(" · "));
    }
    const [box, body] = sec("request", "Request", note.textContent ? note : null);
    const st = t.state;
    const ask = (st && (st.current_user_request || st.latest_user_request)) || t.user_prompt;
    if (ask) body.append(el("div", "prompt", ask));
    else if (!st && t.state_shape) body.append(shapeBlock(t.state_shape));
    else body.append(el("p", "gaprow", "request not recorded"));
    if (st) {
      pastedOf(st).forEach((text, i) => body.append(pastedBlock(t, i, text)));
      const earlier = st.earlier_request_in_this_conversation || (st.task_request && st.task_request !== ask ? st.task_request : null);
      if (earlier) body.append(labelled("Earlier request", el("div", "prompt dim", earlier)));
      const cons = Array.isArray(st.user_constraints) ? st.user_constraints.filter(Boolean) : [];
      if (cons.length) {
        const ul = el("ul", "plainlist");
        for (const c of cons) ul.append(el("li", null, c));
        body.append(labelled("Constraints", ul));
      }
      const obs = Array.isArray(st.observations) ? st.observations.filter((o) => o && (o.summary || o.kind)) : [];
      if (obs.length) {
        const ul = el("ul", "plainlist");
        for (const o of obs) {
          const li = el("li");
          add(li, o.source ? el("span", "t3", o.source + " ") : null, o.summary || human(o.kind), o.status ? el("span", "t3", " · " + o.status) : null);
          ul.append(li);
        }
        body.append(labelled("Observations", ul));
      }
    } else if (ask && t.state_shape && t.kind !== "user_turn") {
      body.append(shapeBlock(t.state_shape));
    }
    body.append(factLine(f, t));
    return box;
  }

  function labelled(name, node) {
    return add(el("div", "lblk"), el("div", "lbl", name), node);
  }

  function pastedBlock(t, i, text) {
    const key = t.turn_index + ":" + i;
    const box = el("details", "more paste");
    box.open = I.pasted.has(key);
    box.addEventListener("toggle", () => { if (box.open) I.pasted.add(key); else I.pasted.delete(key); });
    const sum = el("summary", null, "Pasted material" + (i ? " " + (i + 1) : ""));
    sum.dataset.fk = "paste-" + key;
    add(box, sum, el("pre", "prompt", text));
    return box;
  }

  // An older row: the shape of what Jev saw, never the text.
  function shapeBlock(shape) {
    const box = el("div", "shape");
    box.append(el("div", "lbl", "What Jev saw · text not stored"));
    const kv = el("div", "kv");
    for (const [name, chars] of Object.entries(shape.parts || {})) {
      if (["schema_version", "event"].includes(name)) continue;
      add(kv, el("span", "k", human(name)), absent(chars) ? gap("not stored") : el("span", "t2", plural(chars, "char") + ", not stored"));
    }
    if (kv.children.length) box.append(kv);
    else box.append(el("p", "gaprow", "not recorded"));
    return box;
  }

  function factLine(f, t) {
    const line = el("div", "facts");
    const cell = (k, v) => line.append(add(el("span"), k + " ", add(el("b"), v)));
    const none = (what) => gap(what + " not recorded");
    if (!f) {
      for (const k of ["tools", "images", "reserve", "languages"]) cell(k, none("request facts"));
    } else {
      const names = Array.isArray(f.tool_names) && f.tool_names.length ? f.tool_names
        : (t.state && t.state.facts && Array.isArray(t.state.facts.tool_names) ? t.state.facts.tool_names : null);
      let tools;
      if (f.has_tools === false) tools = "none";
      else if (names) tools = names.join(", ");
      else if (!absent(f.tool_count)) tools = int(f.tool_count);
      else tools = none("tools");
      cell("tools", tools);
      cell("images", f.has_images === true ? "yes" : f.has_images === false ? "none" : none("images"));
      // What the window keeps back for the reply: the output ask plus the reserve.
      cell("reserve", absent(f.output_tokens) || absent(f.reserve_tokens) ? none("reserve") : int(f.output_tokens + f.reserve_tokens));
      cell("languages", Array.isArray(f.languages) ? (f.languages.length ? f.languages.join(", ") : "none") : none("languages"));
    }
    const pasted = pastedOf(t.state);
    cell("pasted material", t.state ? (pasted.length ? plural(pasted.length, "block") : "none") : gap("state not stored"));
    return line;
  }

  // --- 4. the calls ----------------------------------------------------------------

  function callsSection(ctx, t) {
    const calls = ctx.members.get(t) || [];
    const r = t.execution_rollup || {};
    const none = r.http_requests > 0 && r.usage_reported === 0;
    const partial = r.http_requests > 0 && r.usage_reported > 0 && r.usage_reported < r.http_requests;
    const bits = [String(calls.length)];
    if (!absent(r.first_byte_ms_p50)) bits.push("first byte p50 " + ms(r.first_byte_ms_p50));
    if (none) bits.push("usage not reported");
    else if (partial) bits.push(r.usage_reported + " of " + r.http_requests + " reported");
    const [box, body] = sec("calls", "Calls", bits.join(" · "));
    if (!calls.length) {
      body.append(el("p", "quiet", "No calls on this turn"));
    } else {
      const table = el("div", "tbl calls");
      const head = el("div", "tr th");
      add(head, el("span", null, "#"), el("span", null, "status"), el("span", "num", "first byte"), el("span", "num", "total"), el("span", "num", "bytes"), el("span", null, "request"));
      table.append(head);
      const shown = I.allCalls ? calls : calls.slice(0, CALLS_SHOWN);
      for (const c of shown) table.append(callRow(c));
      if (calls.length > shown.length) {
        const more = button("lk", plural(calls.length - shown.length, "more call"), () => { I.allCalls = true; render("pane"); });
        more.dataset.fk = "calls-more";
        table.append(add(el("div", "tr tmore"), more));
      }
      body.append(table);
    }
    body.append(rollup(r, none));
    const counts = problemCounts(calls);
    if (Object.values(counts).some(Boolean)) {
      const line = el("div", "probs");
      for (const [k, v] of Object.entries(counts)) if (v) line.append(badge(k === "unknown" ? "warn" : "bad", v + " " + PROBLEM_SHORT[k]));
      body.append(line);
    }
    return box;
  }

  function callRow(c) {
    const row = el("div", "tr");
    // A call that never reached the provider has nothing to time.
    const never = c.status === "rejected" && absent(c.row);
    const timed = (v) => (absent(v) ? gap(never ? "never reached the provider" : "not recorded") : document.createTextNode(ms(v)));
    const id = el("span", "t3", c.request_id || "");
    if (c.request_id) id.title = c.request_id;
    else id.append(gap("request id not recorded"));
    add(row,
      el("span", "t3", String(c.seq)),
      add(el("span", "stt"), statusCell(c)),
      add(el("span", "num"), timed(c.first_byte_ms)),
      add(el("span", "num"), timed(c.response_ms)),
      add(el("span", "num"), absent(c.response_bytes) ? gap(never ? "never reached the provider" : "not recorded") : kfmt(c.response_bytes)),
      id);
    return row;
  }

  // A completed call is its HTTP code; anything else says what happened.
  function statusCell(c) {
    const code = absent(c.upstream_status) ? "" : String(c.upstream_status);
    if (isUnknown(c.status)) return badge("warn", "unknown", "Outcome unknown" + (code ? " · HTTP " + code : ""));
    if (c.status === "rejected") return badge("bad", "rejected" + (code ? " " + code : ""));
    if (c.status === "stream_failed") return badge("bad", "stream failed", code ? "HTTP " + code : null);
    if (c.status === "completed" && code) return document.createTextNode(code);
    return document.createTextNode((code ? code + " " : "") + human(c.status || "?"));
  }

  function rollup(r, none) {
    const kv = el("div", "kv roll");
    if (none) add(kv, el("span", "k", "Tokens"), gap("usage not reported"));
    else if (!absent(r.input_tokens) || !absent(r.output_tokens)) {
      const t = el("span");
      add(t, show(r.input_tokens, int), " in · ", show(r.output_tokens, int), " out · ", show(r.cached_input_tokens, int), " cached");
      if (!absent(r.cached_input_tokens) && r.input_tokens) t.append(el("span", "t3", " (" + pct(r.cached_input_tokens / r.input_tokens) + ")"));
      add(kv, el("span", "k", "Tokens"), t);
    }
    if (!none && !absent(r.tps_p50)) add(kv, el("span", "k", "Speed"), el("span", null, Math.round(r.tps_p50) + " tok/s p50"));
    const tools = Array.isArray(r.tool_calls) ? r.tool_calls : [];
    if (tools.length) {
      const chips = el("span", "tcalls");
      for (const c of tools.slice().sort((a, b) => b.count - a.count)) chips.append(badge("plain", c.name + " × " + c.count));
      add(kv, el("span", "k", "Tool calls"), chips);
    }
    if (r.assistant_summary) add(kv, el("span", "k", "Summary"), el("blockquote", "asum", r.assistant_summary));
    return kv;
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
    if (I.drawer) I.drawer.querySelector(".dclose").focus({preventScroll: true});
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
  }

  function target(t) {
    const stateBox = I.drawer.querySelector(".dstate");
    I.probe = {
      turn: t, row: t ? t.decision_row : null, stored: !!(t && t.state),
      stateInitial: t ? stateText(t) : "", running: false, result: null, error: null, dirty: false,
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
    close.dataset.fk = "sandbox-close";
    add(head, add(el("div", "dtt"), add(el("h2", "dtitle"), "Jev sandbox ", el("span", "dtarget")),
      el("p", "dnote", "One live Jev call on the text below. Nothing is stored or rerouted.")), close);
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
    const run = button("ghost primary drun", "Run", runProbe);
    run.dataset.fk = "sandbox-run";
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
    return Object.keys(answersOf(t));
  }

  function fillQuestions() {
    if (!I.drawer || !I.probe) return;
    const qs = I.drawer.querySelector(".dqs");
    const old = new Map([...qs.querySelectorAll("textarea")].map((a) => [a.dataset.kind + ":" + a.dataset.qid, a.value]));
    const opened = new Set([...qs.querySelectorAll("details[open]")].map((d) => d.dataset.qid));
    qs.replaceChildren();
    const t = I.probe.turn;
    if (!t) return;
    for (const qid of activeQuestions(t)) {
      const q = I.cfg && I.cfg.questions ? I.cfg.questions[qid] : null;
      const lab = el("label", "dlab", cap(qid));
      const original = q && typeof q.instructions === "string" ? q.instructions : null;
      const area = editor("dq", qid, 4, cap(qid) + " instructions", original, old);
      qs.append(lab, area);
      if (original === null) qs.append(el("p", "dmiss", I.cfg ? "Wording not in the running config" : "Loading the wording…"));
      // The labels or levels Jev chooses between, as JSON, folded away.
      if (q && q.criteria && typeof q.criteria === "object") {
        const box = el("details", "dcrit");
        box.dataset.qid = qid;
        box.open = opened.has(qid);
        const sum = el("summary", null, "Criteria");
        sum.dataset.fk = "crit-" + qid;
        add(box, sum, editor("dc", qid, 10, cap(qid) + " criteria", JSON.stringify(q.criteria, null, 2), old));
        qs.append(box);
      }
    }
  }

  function editor(kind, qid, rows, name, original, old) {
    const area = el("textarea", "dta " + kind);
    area.dataset.qid = qid;
    area.dataset.kind = kind;
    area.rows = rows;
    area.spellcheck = false;
    area.setAttribute("aria-label", name);
    area.dataset.original = original === null ? "" : original;
    const key = kind + ":" + qid;
    area.value = old.has(key) ? old.get(key) : (original || "");
    area.disabled = original === null;
    area.addEventListener("input", () => { if (I.probe) I.probe.dirty = true; });
    area.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); area.blur(); } });
    return area;
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
        p.result = null;
        paintResult();
        return;
      }
    }
    const questions = {};
    const change = (qid) => (questions[qid] = questions[qid] || {});
    for (const area of I.drawer.querySelectorAll(".dq")) {
      if (!area.disabled && area.value !== area.dataset.original) change(area.dataset.qid).instructions = area.value;
    }
    for (const area of I.drawer.querySelectorAll(".dc")) {
      if (area.disabled || area.value === area.dataset.original) continue;
      try {
        change(area.dataset.qid).criteria = JSON.parse(area.value);
      } catch (error) {
        p.error = cap(area.dataset.qid) + " criteria are not JSON · " + (error && error.message ? error.message : "parse error");
        p.result = null;
        paintResult();
        return;
      }
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
      else p.error = probeError(json, response.status);
    } catch {
      if (I.probe === p) p.error = "Router unreachable";
    } finally {
      if (I.probe === p) {
        p.running = false;
        paintResult();
      }
    }
  }

  // A failed probe in a few words: where to look when Jev cannot be reached,
  // else the error's own short name. No latency: nothing was measured.
  function probeError(json, status) {
    if (json && json.reason === "unreachable") return "Jev unreachable · check settings.jev_url";
    const text = String((json && json.error) || "HTTP " + status).replace(/^jev call failed: /, "").replace(/^jev error: /, "");
    const short = text.replace(/^jev\b/, "Jev");
    return short.charAt(0).toUpperCase() + short.slice(1);
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
      box.append(el("p", "gaprow", p.error));
      return;
    }
    if (!p.result) return;
    const r = p.result;
    msNode.append(absent(r.jev_ms) ? add(el("span"), "Jev ", gap("latency not reported")) : el("span", null, "Jev " + ms(r.jev_ms)));
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
        if (absent(v)) cell.append(gap("no answer"));
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
    if (r.config_matches === false) box.append(add(el("p", "dmiss"), badge("warn", "another config"), " the decision was taken under another config"));
  }

  // --- keys ------------------------------------------------------------------------

  function inspectorKey(event) {
    const key = event.key;
    if (key === "Escape") {
      if (I.menu) setMenu(false, true);
      else if (I.drawer && !I.drawer.hidden) { closeDrawer(); render("pane"); }
      else go(listHash());
      event.preventDefault();
      return;
    }
    if (key === "j") { neighbour(1); event.preventDefault(); return; }
    if (key === "k") { neighbour(-1); event.preventDefault(); return; }
    if (!S.data) return;
    if (key === "ArrowRight" || key === "]") { step(1); event.preventDefault(); }
    else if (key === "ArrowLeft" || key === "[") { step(-1); event.preventDefault(); }
  }

  window.inspectorView = inspectorView;
  window.inspectorKey = inspectorKey;
})();
