"use strict";

// Overview. Four rows: quota, four numbers, tasks beside latency, requests
// over time with reliability. Uses the shell's builders and formatters.

const STEER_CLASS = {calm: "calm", elevated: "elevated", demoting: "demoting", exhausted: "exhausted"};

// 1234 -> 1.2k, 7e7 -> 70M. Token counts read at a glance.
function ovTokens(n) {
  const v = Number(n);
  if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(v >= 1e4 ? 0 : 1) + "k";
  return String(Math.round(v));
}
function ovSigned(d) {
  const v = Math.round(Number(d));
  return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v);
}
function ovMoney(v, currency) {
  const n = Number(v);
  const sym = !currency || currency === "USD" ? "$" : currency + " ";
  return sym + (n >= 100 ? int(n) : n.toFixed(2));
}
function ovUntil(ts, now) {
  const t = Number(ts) - Number(now);
  if (!(t > 0)) return null;
  return t >= 86400 ? Math.round(t / 86400) + " d" : t >= 3600 ? Math.round(t / 3600) + " h" : Math.max(1, Math.round(t / 60)) + " min";
}

function overviewView(kind) {
  const ov = S.overview;
  const root = el("section", "ov" + (kind === "data" ? " still" : ""));
  const w = ov.window || S.win;
  const within = (extra) => listHash(Object.assign({window: w}, extra || {}));
  const head = el("div", "ovh");
  add(head, el("h1", "ovt", "Overview"), windowSeg(w, overviewHash));
  root.append(head, quotaPanel(ov), kpiStrip(ov));
  root.append(add(el("div", "ovrow"), matrixPanel(ov), latencyPanel(ov)));
  root.append(seriesPanel(ov, within));
  const legacy = ov.legacy || {};
  if (legacy.decisions > 0) root.append(legacyPanel(legacy));
  return root;
}

// --- 1. quota -------------------------------------------------------------------

function quotaPanel(ov) {
  const subs = ov.subscriptions;
  const box = panel("Quota", subs && subs.quota_routing === false ? "not steering" : null, "quota2");
  if (!subs) {
    box.append(el("p", "gaprow", "quota not readable"));
    return box;
  }
  const routed = Array.isArray(subs.routed) ? subs.routed : [];
  const tracked = Array.isArray(subs.tracked) ? subs.tracked : [];
  const list = el("div", "subs");
  for (const r of routed) list.append(subRow(r, false, subs));
  for (const t of tracked) list.append(subRow(t, true, subs));
  if (!routed.length && !tracked.length) list.append(el("p", "empty", "No provider"));
  box.append(list);
  return box;
}

// Why a provider shows no bars. One chip, never a row of empty numbers.
function readingGap(r) {
  if (r.enabled === false) return "not polled";
  if (r.status === "stale") return "stale reading";
  if (r.status === "error") return "reading failed";
  return "no reading";
}

function subRow(r, tracked, subs) {
  const row = el("div", "sub" + (tracked ? " tracked" : ""));
  const name = el("div", "subn");
  name.append(el("span", "subp", tracked ? r.id : r.provider));
  if (!tracked && Array.isArray(r.models) && r.models.length) {
    name.append(el("span", "subm mono", r.models.map((m) => m.model).join(" · ")));
  }
  const bars = el("div", "subw");
  const windows = Array.isArray(r.windows) ? r.windows : [];
  const plans = tracked && Array.isArray(r.plans) ? r.plans : [];
  windows.forEach((w, i) => {
    // A plan that reads the same as its window names it.
    const plan = plans[i] && !absent(plans[i].used_percent) && plans[i].used_percent === w.used_percent ? plans[i] : null;
    const label = plan && plan.title ? plan.title : !absent(w.window_minutes) ? windowSpan(w.window_minutes) : w.name ? human(w.name) : "window";
    bars.append(quotaBar(label, w.used_percent, w.expected_percent, w.pace_delta, ovUntil(w.resets_at, subs.as_of), subs.exhausted_at));
  });
  for (const plan of plans.slice(windows.length)) {
    bars.append(quotaBar(plan.title || "other", plan.used_percent, null, null, null, subs.exhausted_at));
  }
  if (!windows.length && !plans.length) {
    if (!tracked && r.status === "fresh" && !absent(r.used_percent)) bars.append(quotaBar("used", r.used_percent, null, null, null, subs.exhausted_at));
    else bars.append(add(el("div", "subgap"), gap(readingGap(r))));
  }
  const side = el("div", "subs2");
  if (tracked) {
    const t = r.tokens || {};
    if (!absent(t.last_30_days_cost_usd)) add(side, el("span", "cost mono", ovMoney(t.last_30_days_cost_usd, r.currency)), el("span", "faint", " / 30 d"));
    if (r.status === "stale" || r.status === "error") side.append(el("span", "steer demoting", r.status));
  } else if (r.steering && subs.quota_routing !== false) {
    side.append(el("span", "steer " + (STEER_CLASS[r.steering] || ""), r.steering));
  } else if ((windows.length || !absent(r.used_percent)) && (r.status === "stale" || r.status === "error")) {
    side.append(el("span", "steer demoting", r.status));
  }
  add(row, name, bars, side);
  return row;
}

function quotaBar(label, used, expected, delta, resets, exhaustedAt) {
  const line = el("div", "qbar");
  const track = el("span", "qtrack");
  if (!absent(used)) {
    const fill = el("i", Number(used) >= (exhaustedAt || 100) ? "full" : null);
    fill.style.width = Math.max(0.5, Math.min(100, used)) + "%";
    track.append(fill);
  }
  if (!absent(expected)) {
    const tick = el("b");
    tick.style.left = Math.max(0, Math.min(100, expected)) + "%";
    track.append(tick);
  }
  const value = absent(used) ? gap() : el("span", "qv mono", Math.round(used) + "%");
  const pace = el("span", "qd mono");
  if (!absent(delta)) {
    pace.classList.add(delta > 0 ? "over" : "under");
    add(pace, ovSigned(delta), el("span", "faint", " vs pace"));
  }
  add(line, el("span", "ql", label), track, value, pace, el("span", "qr faint", resets ? "resets in " + resets : ""));
  return line;
}

// --- 2. four numbers ---------------------------------------------------------------

function kpiStrip(ov) {
  const strip = el("div", "big4");
  const big = (label, value, sub) => strip.append(add(el("div", "bigk"), el("span", "kl", label), add(el("span", "bigv"), value), sub ? add(el("span", "ks"), sub) : null));
  const eff = ov.efficiency || {};
  const qs = eff.quota_saved || {};
  const calls = (qs.priced || 0) + (qs.unpriced || 0);
  big("Quota saved",
    absent(qs.saved_rate) ? gap(calls ? "no priced model" : "no calls") : pct(qs.saved_rate),
    calls ? int(qs.priced || 0) + " of " + int(calls) + " calls priced" : null);
  const tax = ov.router_tax || {};
  const adm = tax.admission_ms || {};
  big("Router tax",
    adm.n ? ms(adm.p50) : gap("no admission timed"),
    absent(tax.amortized_ms_per_request) ? (tax.execution_requests ? gap("per call not recorded") : null)
      : ms(tax.amortized_ms_per_request) + " per call · " + int(tax.execution_requests) + " calls");
  const cf = (ov.quality || {}).corrective_followup || {};
  big("Clean turns",
    cf.denominator ? pct(1 - cf.rate) : gap("no turn assessed"),
    cf.denominator ? int(cf.denominator - cf.numerator) + " of " + int(cf.denominator) + " turns" : null);
  const cache = eff.cache || {};
  big("Cache hit",
    absent(cache.rate) ? gap("not reported") : pct(cache.rate),
    absent(cache.input_tokens) ? null : ovTokens(cache.cached_tokens || 0) + " of " + ovTokens(cache.input_tokens) + " input tokens");
  return strip;
}

// --- 3. tasks beside latency ---------------------------------------------------------

function ovPair(model, effort) {
  const node = el("span", "pair");
  const [prov, name] = splitModel(model);
  add(node, prov ? el("span", "faint", prov) : null, model ? name : gap("model"), el("span", "eff", effort ? " · " + effort : " · no effort"));
  return node;
}

function matrixPanel(ov) {
  const m = ov.matrix || {};
  const box = panel(m.question ? cap(m.question) : "Task", null);
  const pairs = Array.isArray(m.pairs) ? m.pairs : [];
  const cells = Array.isArray(m.cells) ? m.cells : [];
  if (!pairs.length || !cells.length) {
    box.append(el("p", "empty", "No admission in this window"));
    return box;
  }
  const key = (task, model, effort) => [task, model, effort].join("|");
  const at = new Map(cells.map((c) => [key(c.task, c.model, c.effort), c]));
  const order = (Array.isArray(m.tasks) ? m.tasks : []).filter((t) => cells.some((c) => c.task === t));
  if (cells.some((c) => absent(c.task))) order.push(null);
  const max = Math.max(1, ...cells.map((c) => c.n || 0));
  const scroll = el("div", "mxs");
  const grid = el("div", "mx");
  grid.style.gridTemplateColumns = "minmax(9.5rem, 1.1fr) repeat(" + pairs.length + ", minmax(4.6rem, 1fr))";
  grid.append(el("span", "mxh"));
  for (const p of pairs) {
    const h = el("span", "mxh mxc");
    const [, name] = splitModel(p.model);
    add(h, el("span", "mono", name || "?"), el("span", "eff", p.effort || "no effort"));
    h.title = (p.model || "") + " · " + (p.effort || "no effort");
    grid.append(h);
  }
  for (const task of order) {
    grid.append(add(el("span", "mxt"), task === null ? gap("not recorded") : human(task)));
    for (const p of pairs) {
      const c = at.get(key(task, p.model, p.effort));
      const cell = el("span", "mxv");
      if (c && c.n) {
        cell.style.backgroundColor = "rgba(123, 224, 195, " + (0.1 + 0.55 * c.n / max).toFixed(3) + ")";
        cell.append(el("span", "mxn mono", int(c.n)));
        const cr = c.corrective || {};
        if (cr.denominator) cell.append(el("span", "mxr mono" + (cr.numerator ? " hot" : ""), pct(cr.rate)));
      }
      grid.append(cell);
    }
  }
  scroll.append(grid);
  box.append(scroll);
  return box;
}

// A p50 with its own count: fewer recorded than ran is a gap beside it.
function ovP50(p, rows, format) {
  const cell = el("span", "num mono");
  if (p && p.n) {
    add(cell, format(p.p50));
    if (rows && p.n < rows) add(cell, " ", gap(int(p.n) + " of " + int(rows)));
  } else add(cell, rows ? gap() : el("span", "none", "–"));
  return cell;
}

function latencyPanel(ov) {
  const pairs = ((ov.upstream || {}).pairs) || [];
  const box = panel("Latency", null);
  const table = el("div", "lt");
  const head = el("div", "ltr lth");
  add(head, el("span", null, "Model · effort"), el("span", "num", "Calls"), el("span", "num", "First byte"), el("span", "num", "tok/s"));
  table.append(head);
  if (!pairs.length) table.append(el("p", "empty", "No calls in this window"));
  for (const p of pairs) {
    const row = el("div", "ltr");
    add(row, ovPair(p.model, p.effort), el("span", "num mono", int(p.n || 0)),
      ovP50(p.first_byte_ms, p.n, ms), ovP50(p.tps, p.n, (v) => num1(v)));
    table.append(row);
  }
  box.append(table);
  return box;
}

// --- 4. requests over time, reliability ------------------------------------------------

function bucketLabel(t, step) {
  if (step >= 86400) return shortDay(t);
  if (step >= 21600) return DAYS[new Date(t * 1000).getDay()] + " " + clock(t).slice(0, 5);
  return clock(t).slice(0, 5);
}

function seriesPanel(ov, within) {
  const series = Array.isArray(ov.series) ? ov.series : [];
  const step = ov.bucket_seconds || 3600;
  const box = panel("Requests", "per " + windowSpan(step / 60), "wide");
  const maxReq = Math.max(1, ...series.map((b) => b.requests || 0));
  const maxMs = Math.max(1, ...series.map((b) => b.first_byte_p50_ms || 0));
  const reqRow = el("div", "sbars");
  const msRow = el("div", "sbars ms");
  series.forEach((b, i) => {
    const when = bucketLabel(b.t, step);
    const col = el("div", "sb");
    col.title = when + " · " + b.requests + " requests" + (b.problems ? " · " + b.problems + " problems" : "");
    const good = el("i");
    good.style.height = ((b.requests - b.problems) / maxReq * 100) + "%";
    const bad = el("i", "bad");
    bad.style.height = (b.problems / maxReq * 100) + "%";
    good.style.animationDelay = bad.style.animationDelay = Math.min(400, i * 10) + "ms";
    add(col, good, bad);
    reqRow.append(col);
    const m = el("div", "sb");
    const bar = el("i");
    if (!absent(b.first_byte_p50_ms)) {
      bar.style.height = Math.max(2, b.first_byte_p50_ms / maxMs * 100) + "%";
      m.title = when + " · first byte p50 " + ms(b.first_byte_p50_ms);
    } else if (b.requests > 0) {
      // Requests ran and no first byte was recorded: a gap, not a zero.
      bar.className = "gapbar";
      m.title = when + " · first byte not recorded";
    }
    bar.style.animationDelay = Math.min(400, i * 10) + "ms";
    m.append(bar);
    msRow.append(m);
  });
  const axis = el("div", "saxis");
  if (series.length) {
    add(axis, el("span", null, bucketLabel(series[0].t, step)), el("span", null, bucketLabel(series[Math.floor(series.length / 2)].t, step)), el("span", null, bucketLabel(series[series.length - 1].t, step)));
  }
  const chart = el("div", "schart");
  add(chart,
    add(el("div", "sl"), el("span", "mono faint", int(maxReq)), el("span", null, "Calls")), reqRow,
    add(el("div", "sl"), el("span", "mono faint", maxMs > 1 ? ms(maxMs) : "–"), el("span", null, "First byte")), msRow,
    el("div"), axis);
  box.append(chart, reliabilityLine(ov, within));
  return box;
}

function reliabilityLine(ov, within) {
  const line = el("div", "rel");
  const fact = (label, value, hash, warn) => {
    const node = hash ? link("rf" + (warn ? " warn" : ""), null, hash) : el("span", "rf" + (warn ? " warn" : ""));
    add(node, el("span", "kl", label), add(el("span", "mono"), value));
    line.append(node);
  };
  const ex = ov.executions || {};
  const st = ex.by_status || {};
  fact("Calls", int(ex.total || 0) + (absent(ex.completion_rate) ? "" : " · " + pct(ex.completion_rate) + " done"), null, false);
  for (const [key, label] of [["unknown", "Unknown"], ["rejected", "Rejected"], ["stream_failed", "Stream failed"]]) {
    if (st[key]) fact(label, int(st[key]), within({problem: key}), true);
  }
  const rel = ov.reliability || {};
  const rate = (r) => (r && r.denominator ? int(r.numerator) + " of " + int(r.denominator) : "0");
  fact("Fallback", rate(rel.fallback), rel.fallback && rel.fallback.numerator ? within({problem: "fallback"}) : null, !!(rel.fallback && rel.fallback.numerator));
  fact("Low confidence", rate(rel.low_confidence), rel.low_confidence && rel.low_confidence.numerator ? within({problem: "low_confidence"}) : null, false);
  const breakers = rel.breakers;
  const br = el("span", "rf");
  br.append(el("span", "kl", "Breakers"));
  if (!breakers || typeof breakers !== "object") br.append(gap());
  else {
    const names = Object.entries(breakers);
    const open = names.filter(([, b]) => b && b.state !== "closed");
    if (!names.length) br.append(el("span", "faint", "none"));
    else if (!open.length) br.append(el("span", "ok", "closed"));
    for (const [name, b] of open) br.append(el("span", "gap mono", name + " " + human(b.state || "?") + (absent(b.retry_in_seconds) ? "" : " · " + Math.ceil(b.retry_in_seconds) + " s")));
  }
  line.append(br);
  return line;
}

function legacyPanel(l) {
  const box = panel("Legacy alias traffic", null, "wide legacy");
  const row = el("div", "lrow");
  const cell = (label, value) => row.append(add(el("div", "lc"), el("span", "kl", label), add(el("span", "lval"), value)));
  const pp = (p) => (p && p.n ? ms(p.p50) + " · " + ms(p.p95) : gap());
  cell("Decisions", int(l.decisions));
  cell("Pinned", int(l.pinned || 0));
  cell("Fallback", int(l.fallback || 0));
  cell("Upstream errors", int(l.upstream_errors || 0));
  cell("Jev p50 · p95", pp(l.jev_ms));
  cell("First byte p50 · p95", pp(l.first_byte_ms));
  cell("Total p50 · p95", pp(l.response_ms));
  box.append(row);
  return box;
}
