"use strict";

// Overview. A headline band of four numbers; requests beside quota; one
// "By model" table with the task heatmap folded under it; legacy traffic
// last, only when there is any. Uses the shell's builders and formatters.
// Every top-level name here starts with "ov" (or is the old bucketLabel) so
// the other view files can never collide with it.

// Fewer calls than this and a p50 is a hint, not a measurement: its count is muted.
const OV_FEW_CALLS = 5;
// The payload's lists stop at this many entries (dashboard.py TOP).
const OV_TOP = 20;
// The heatmap's fold, kept across the live refresh so it never snaps shut.
let ovTasksOpen = false;

const OV_PROBLEMS = [["unknown", "unknown"], ["rejected", "rejected"], ["stream_failed", "stream failed"]];

// 1234 -> 1.2k, 7e7 -> 70M. Token counts read at a glance.
function ovTokens(n) {
  const v = Number(n);
  if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e7 ? 0 : 1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(v >= 1e4 ? 0 : 1) + "k";
  return String(Math.round(v));
}
function ovMoney(v, currency) {
  const n = Number(v);
  const sym = !currency || currency === "USD" ? "$" : currency + " ";
  return sym + (n >= 100 ? int(n) : n.toFixed(2));
}
function ovUntil(ts, now) {
  if (absent(ts) || absent(now)) return null;
  const t = Number(ts) - Number(now);
  if (!(t > 0)) return null;
  return t >= 86400 ? Math.round(t / 86400) + " d" : t >= 3600 ? Math.round(t / 3600) + " h" : Math.max(1, Math.round(t / 60)) + " min";
}
// "613 ms" as a number and a quieter unit, for the headline.
function ovValueUnit(text) {
  const cut = String(text).lastIndexOf(" ");
  if (cut < 0) return document.createTextNode(String(text));
  return add(el("span"), text.slice(0, cut), el("small", null, text.slice(cut + 1)));
}
function ovBold(text) { return el("b", null, text); }
// "per hour", "per 5 min": the bucket a bar stands for.
function ovPer(step) {
  if (step === 3600) return "per hour";
  if (step === 86400) return "per day";
  return "per " + windowSpan(step / 60);
}

function overviewView(kind) {
  const ov = S.overview;
  const root = el("div", "ov" + (kind === "data" ? " still" : ""));
  const w = ov.window || S.win;
  const within = (extra) => listHash(Object.assign({window: w}, extra || {}));
  const head = el("div", "head");
  add(head, el("h1", null, "Overview"), add(el("div", "right"), windowSeg(w, overviewHash)));
  root.append(head, ovHeadline(ov, within));
  root.append(add(el("div", "grid two"), ovRequests(ov), ovQuota(ov)));
  root.append(ovByModel(ov));
  const legacy = ov.legacy || {};
  if (legacy.decisions > 0) root.append(ovLegacy(legacy));
  return root;
}

// --- 1. the headline band ------------------------------------------------------

function ovStat(value, label, help, comparator) {
  const box = el("div", "stat");
  add(box, add(el("div", "v"), value), add(el("div", "l"), label, help ? helpMark(help) : null));
  if (comparator) box.append(add(el("div", "c"), ...comparator));
  return box;
}

function ovHeadline(ov, within) {
  const box = band(null, null, "headline");
  const stats = el("div", "stats");
  stats.append(ovQuotaSaved(ov), ovOverhead(ov), ovCleanTurns(ov), ovCalls(ov, within));
  box.append(stats, foot(ovCacheLine(ov), ovBreakers(ov)));
  return box;
}

function ovQuotaSaved(ov) {
  const qs = (ov.efficiency || {}).quota_saved || {};
  const counted = qs.counted || 0;
  const unknown = qs.unknown || 0;
  const calls = counted + unknown;
  const base = qs.baseline_model;
  const value = counted && !absent(qs.saved_rate) ? pct(qs.saved_rate)
    : gap(!calls ? "No call in this window" : base ? "No call could be priced against " + base : "No model in router.yaml has a quota_cost");
  const help = base
    ? "Calls that did not spend " + base + "'s quota, weighted by each model's quota_cost. A comparison against every call going to " + base + ", not a measured subscription delta."
    : "Calls that did not spend the frontier model's quota, weighted by each model's quota_cost. A comparison against every call going to the priced model with the highest quota_cost, not a measured subscription delta.";
  const comparator = calls
    ? [ovBold(int(counted) + " of " + int(calls)), " calls priced", unknown ? " · " + int(unknown) + " unknown" : null]
    : null;
  return ovStat(value, "Frontier quota saved", help, comparator);
}

function ovOverhead(ov) {
  const tax = ov.router_tax || {};
  const adm = tax.admission_ms || {};
  const value = adm.n ? ovValueUnit(ms(adm.p50)) : gap("No admission timed in this window");
  const parts = [];
  if (adm.n && !absent(adm.p95)) parts.push("p95 ", ovBold(ms(adm.p95)));
  if (!absent(tax.amortized_ms_per_request)) {
    if (parts.length) parts.push(" · ");
    parts.push(ms(tax.amortized_ms_per_request) + " per call amortised");
  } else if (tax.execution_requests) {
    if (parts.length) parts.push(" · ");
    parts.push(gap("No Jev call was timed"), " per call amortised");
  }
  const help = "The Jev call plus policy at admission, p50, fallback admissions left out. Amortised: every Jev call's time (admissions and turn plans) over every execution call.";
  return ovStat(value, "Router overhead per admission", help, parts.length ? parts : null);
}

function ovCleanTurns(ov) {
  const cf = (ov.quality || {}).corrective_followup || {};
  const ad = ((ov.sessions || {}).adaptation) || {};
  const adaptive = (ad.shadow || 0) + (ad.active || 0);
  const value = cf.denominator ? pct(1 - cf.rate) : gap("No planned turn was assessed in this window");
  const threshold = absent(cf.threshold) ? "the corrective cutoff" : num2(cf.threshold);
  const help = "Planned turns whose next prompt was not a correction (corrective_followup below " + threshold + ").";
  const comparator = [];
  if (cf.denominator) comparator.push(ovBold(int(cf.denominator - cf.numerator) + " of " + int(cf.denominator)), " planned turns");
  if (ov.sessions) {
    if (comparator.length) comparator.push(" · ");
    comparator.push(plural(adaptive, "adaptive session"));
  }
  return ovStat(value, "Clean turns", help, comparator.length ? comparator : null);
}

function ovCalls(ov, within) {
  const ex = ov.executions || {};
  const st = ex.by_status || {};
  const total = ex.total || 0;
  const done = st.completed || 0;
  const value = done === total ? int(total) : add(el("span"), int(done), el("small", null, "of " + int(total)));
  const problems = OV_PROBLEMS.reduce((sum, [key]) => sum + (st[key] || 0), 0);
  // Line one: the problems, each a link to the sessions it filters to.
  // Line two (or the same line when there is none): fallback and low confidence.
  const first = el("span", problems ? "ov-ln" : null);
  if (problems) {
    first.append(add(link("ov-link", null, within({problem: "any"}), plural(problems, "problem") + ": open the sessions"), badge("warn", plural(problems, "problem"))));
    for (const [key, words] of OV_PROBLEMS) {
      if (st[key]) add(first, " ", add(link("ov-link", null, within({problem: key})), badge("bad", int(st[key]) + " " + words)));
    }
  } else add(first, ovBold("0"), " problems · ");
  const second = el("span");
  const rel = ov.reliability || {};
  const rate = (r, key, label, warn) => {
    const part = el("span", "ov-nw", label + " ");
    if (!r || !r.denominator) part.append(gap("No admission in this window"));
    else {
      const text = int(r.numerator) + " of " + int(r.denominator);
      if (r.numerator) part.append(add(link("ov-link", null, within({problem: key})), warn ? badge("warn", text) : ovBold(text)));
      else part.append(text);
    }
    return part;
  };
  add(second, rate(rel.fallback, "fallback", "fallback", true), " · ", rate(rel.low_confidence, "low_confidence", "low confidence", false));
  const c = [first, second];
  const help = absent(ex.completion_rate) ? null
    : pct(ex.completion_rate) + " of this window's execution calls completed. Problems are calls whose outcome is unknown, rejected or whose stream failed.";
  return ovStat(value, "Calls completed", help, c);
}

// Cache hit and tokens per second, in one sentence. A value the upstream did
// not report is a gap with the count of calls that did report.
function ovCacheLine(ov) {
  const eff = ov.efficiency || {};
  const cache = eff.cache || {};
  const usage = eff.usage_reported || {};
  const pairs = ((ov.upstream || {}).pairs) || [];
  const hasTps = pairs.some((p) => p.tps && p.tps.n);
  const line = el("span");
  const reported = usage.executions
    ? "The upstream reported usage on " + int(usage.reported || 0) + " of " + plural(usage.executions, "call")
    : "No call in this window";
  const why = !usage.executions ? "no call in this window."
    : usage.reported ? "not in the usage the upstream reported (" + int(usage.reported) + " of " + plural(usage.executions, "call") + ")."
    : "not reported by the upstream on any call this window.";
  if (absent(cache.rate)) {
    if (hasTps) add(line, "Cache hit: ", gap(reported), " " + why + " Tokens per second are in the table below.");
    else add(line, "Cache hit and tokens per second: ", gap(reported), " " + why);
    return line;
  }
  add(line, "Cache hit ", ovBold(pct(cache.rate)));
  if (!absent(cache.input_tokens)) add(line, " · " + ovTokens(cache.cached_tokens || 0) + " of " + ovTokens(cache.input_tokens) + " input tokens");
  if (hasTps) add(line, " · tokens per second in the table below.");
  else add(line, " · tokens per second ", gap(reported), " not reported.");
  return line;
}

function ovBreakers(ov) {
  const breakers = (ov.reliability || {}).breakers;
  const line = el("span");
  if (!breakers || typeof breakers !== "object") return add(line, "Breakers ", gap("Breaker state was not read"), ".");
  const names = Object.entries(breakers);
  if (!names.length) return add(line, "No provider breakers.");
  const open = names.filter(([, b]) => !b || b.state !== "closed");
  if (!open.length) return add(line, names.length === 1 ? "Breaker closed on " + names[0][0] + "." : "Breakers closed on all " + names.length + " providers.");
  line.append("Breakers: ");
  open.forEach(([name, b], i) => {
    const state = b && b.state ? human(b.state) : "state unknown";
    const retry = b && !absent(b.retry_in_seconds) ? " · retry in " + Math.ceil(b.retry_in_seconds) + " s" : "";
    if (i) line.append(" ");
    line.append(badge(b && b.state ? "warn" : "plain", name + " " + state + retry));
  });
  const closed = names.length - open.length;
  if (closed) line.append(" · closed on the other " + plural(closed, "provider") + ".");
  return line;
}

// --- 2. requests ----------------------------------------------------------------

function bucketLabel(t, step) {
  if (step >= 86400) return shortDay(t);
  if (step >= 21600) return DAYS[new Date(t * 1000).getDay()] + " " + clock(t).slice(0, 5);
  return clock(t).slice(0, 5);
}

function ovRequests(ov) {
  const series = Array.isArray(ov.series) ? ov.series : [];
  const step = ov.bucket_seconds || 3600;
  const problems = series.reduce((sum, b) => sum + (b.problems || 0), 0);
  const box = band("Requests", ovPer(step) + " · " + plural(problems, "problem"), "requests");
  if (!series.length) {
    box.append(el("p", "empty", "No bucket in this window"));
    return box;
  }
  const maxReq = Math.max(1, ...series.map((b) => b.requests || 0));
  const fbs = series.map((b) => (absent(b.first_byte_p50_ms) ? null : Number(b.first_byte_p50_ms)));
  const maxFb = Math.max(0, ...fbs.filter((v) => v !== null));

  const bars = el("div", "bars");
  series.forEach((b, i) => {
    const col = el("div", "b");
    const when = bucketLabel(b.t, step);
    const fb = fbs[i] !== null ? " · first byte p50 " + ms(fbs[i]) : b.requests ? " · first byte not recorded" : "";
    col.title = when + " · " + plural(b.requests || 0, "call") + (b.problems ? " · " + plural(b.problems, "problem") : "") + fb;
    const good = Math.max(0, (b.requests || 0) - (b.problems || 0));
    const delay = Math.min(400, i * 10) + "ms";
    if (good) {
      const g = el("i");
      g.style.height = (good / maxReq * 100) + "%";
      g.style.animationDelay = delay;
      col.append(g);
    }
    if (b.problems) {
      const p = el("i", "bad");
      p.style.height = (b.problems / maxReq * 100) + "%";
      p.style.animationDelay = delay;
      col.append(p);
    }
    bars.append(col);
  });

  const chart = el("div", "chart");
  // An empty window has no top to label: only the zero line is drawn.
  const anyReq = series.some((b) => b.requests);
  add(chart, add(el("div", "y"), el("span", null, anyReq ? int(maxReq) : ""), el("span", null, "0")), bars);
  if (maxFb > 0) {
    add(chart, add(el("div", "y fby"), el("span", null, "first byte"), el("span", null, ms(maxFb))), ovFirstByteLine(fbs, maxFb, series, step));
  } else {
    add(chart, add(el("div", "y fby"), el("span", null, "first byte")),
      add(el("div", "line none"), gap(series.some((b) => b.requests) ? "No call in this window recorded a first byte" : "No call in this window"), " no first byte recorded"));
  }
  const axis = el("div", "xaxis");
  const n = series.length;
  const marks = [...new Set([0, Math.round((n - 1) / 4), Math.round((n - 1) / 2), Math.round(3 * (n - 1) / 4), n - 1])];
  for (const i of marks) axis.append(el("span", null, bucketLabel(series[i].t, step)));
  add(chart, el("div"), axis);
  box.append(chart);
  return box;
}

// The first-byte p50 per bucket as a line under the bars. A bucket with no
// first byte is skipped, never drawn as zero; each point sits at its bar's centre.
function ovFirstByteLine(fbs, maxFb, series, step) {
  const NS = "http://www.w3.org/2000/svg";
  const wrap = el("div", "line");
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 100 36");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  const n = fbs.length;
  const pts = [];
  fbs.forEach((v, i) => { if (v !== null) pts.push([(i + 0.5) / n * 100, 33 - v / maxFb * 30, i]); });
  const path = document.createElementNS(NS, "polyline");
  path.setAttribute("points", pts.map((p) => p[0].toFixed(2) + "," + p[1].toFixed(2)).join(" "));
  svg.append(path);
  wrap.append(svg);
  // Points as page dots, so they stay round however wide the chart is.
  for (const [x, y, i] of pts) {
    const d = el("i");
    d.style.left = x + "%";
    d.style.top = (y / 36 * 100) + "%";
    d.title = bucketLabel(series[i].t, step) + " · first byte p50 " + ms(fbs[i]);
    wrap.append(d);
  }
  return wrap;
}

// --- 3. quota ---------------------------------------------------------------------

function ovQuota(ov) {
  const subs = ov.subscriptions;
  const steering = subs && subs.quota_routing
    ? "steering on" + (absent(subs.demote_above) ? "" : " · demote above pressure " + pressureText(subs.demote_above))
    : subs ? "not steering" : null;
  const box = band("Quota", steering, "quota");
  if (!subs) {
    box.append(add(el("p", "empty"), gap("The quota monitor could not be read"), " Quota not readable."));
    return box;
  }
  const routed = Array.isArray(subs.routed) ? subs.routed : [];
  const tracked = Array.isArray(subs.tracked) ? subs.tracked : [];
  const list = el("div", "qrows");
  let ticks = false;
  for (const r of routed) { const [row, t] = ovQuotaRow(r, false, subs); ticks = ticks || t; list.append(row); }
  for (const r of tracked) { const [row, t] = ovQuotaRow(r, true, subs); ticks = ticks || t; list.append(row); }
  if (!routed.length && !tracked.length) list.append(el("p", "empty", "No provider configured"));
  box.append(list);
  const d = ov.decisions || {};
  const changed = d.quota_recorded
    ? add(el("span"), "Quota changed the choice on ", ovBold(int(d.quota_changed || 0) + " of " + int(d.quota_recorded)), " admissions.")
    : add(el("span"), "Quota changed the choice: ", gap("No admission in this window recorded it"), ".");
  box.append(foot(changed, ticks ? "Tick marks show expected use at this point in the window." : null));
  return box;
}

// Why a provider shows no meter. One quiet dash, never a row of empty numbers.
function readingGap(r) {
  if (r.enabled === false) return "disabled";
  if (r.status === "stale") return "stale reading";
  if (r.status === "error") return "reading failed";
  if (!r.status || r.status === "unknown") return "not polled";
  return "no reading";
}

// One meter: the used fill, the expected-use tick. Returns [node, drewTick].
function ovMeter(used, expected, cls, title, small) {
  const m = el("div", "meter" + (small ? " sm" : ""));
  m.title = title;
  if (!absent(used)) {
    const fill = el("i", cls);
    const u = Math.max(0, Math.min(100, Number(used)));
    fill.style.width = (u > 0 ? Math.max(1, u) : 0) + "%";
    m.append(fill);
  }
  if (!absent(expected)) {
    const tick = el("b");
    tick.style.left = Math.max(0, Math.min(100, Number(expected))) + "%";
    m.append(tick);
  }
  return [m, !absent(expected)];
}

function ovQuotaRow(r, tracked, subs) {
  const row = el("div", "qrow" + (tracked ? " tracked" : ""));
  const name = el("div", "p", tracked ? r.id : r.provider);
  const models = !tracked && Array.isArray(r.models) ? r.models.map((m) => m.model) : [];
  if (tracked) {
    name.append(el("small", null, "tracked"));
    name.title = "A subscription no provider links to: shown, never routed";
  }
  else {
    name.append(el("small", null, plural(models.length, "model")));
    if (models.length) name.title = models.join(" · ");
  }
  const windows = Array.isArray(r.windows) ? r.windows : [];
  const plans = tracked && Array.isArray(r.plans) ? r.plans : [];
  const exhaustedAt = absent(subs.exhausted_at) ? 100 : subs.exhausted_at;
  const demoting = !tracked && r.steering === "demoting";
  const fillCls = (used) => (absent(used) ? null : Number(used) >= exhaustedAt ? "bad" : demoting ? "warn" : null);
  // The meters: one per window, then any plan beyond the windows.
  const items = windows.map((w, i) => {
    const plan = plans[i] && !absent(plans[i].used_percent) && plans[i].used_percent === w.used_percent ? plans[i] : null;
    const label = plan && plan.title ? plan.title : !absent(w.window_minutes) ? windowSpan(w.window_minutes) + " window" : w.name ? human(w.name) : "window";
    return {label, used: w.used_percent, expected: w.expected_percent, resets: ovUntil(w.resets_at, subs.as_of) || w.reset_description || null};
  });
  for (const plan of plans.slice(windows.length)) items.push({label: plan.title || "other", used: plan.used_percent, expected: null, resets: null});
  if (!items.length && !tracked && r.status === "fresh" && !absent(r.used_percent)) {
    items.push({label: "used", used: r.used_percent, expected: null, resets: null});
  }
  let ticks = false;
  const meters = el("div", "ov-ms");
  const value = el("div", "num");
  const resets = el("div", "ov-rs t3 s12");
  if (!items.length) {
    meters.classList.add("none");
    add(meters, gap(readingGap(r)), el("span", null, readingGap(r)));
  } else {
    items.forEach((it, i) => {
      const words = absent(it.used) ? "use not reported" : Math.round(it.used) + "% used";
      const title = it.label + " · " + words + (absent(it.expected) ? "" : ", " + Math.round(it.expected) + "% expected by now") + (it.resets ? " · resets in " + it.resets : "");
      const [m, t] = ovMeter(it.used, it.expected, fillCls(it.used), title, i > 0);
      ticks = ticks || t;
      meters.append(m);
    });
    // The number beside the meters is the window nearest its limit.
    const known = items.filter((it) => !absent(it.used));
    const top = known.length ? known.reduce((a, b) => (Number(b.used) > Number(a.used) ? b : a)) : null;
    if (top) {
      value.append(Math.round(top.used) + "%");
      if (items.length > 1) value.title = top.label;
      if (top.resets) resets.append("resets in " + top.resets);
    } else value.append(gap("No window reported its use"));
  }
  const side = el("div", "steer");
  if (tracked) {
    const t = r.tokens || {};
    if (!absent(t.last_30_days_cost_usd)) {
      side.append(ovMoney(t.last_30_days_cost_usd, r.currency) + " / 30 d");
      side.title = "Cost over the last 30 days, as the subscription reports it";
    }
    if (items.length && (r.status === "stale" || r.status === "error")) side.append(el("span", "t3", (side.childNodes.length ? " · " : "") + r.status));
  } else if (r.steering && subs.quota_routing) {
    side.textContent = r.steering;
    if (r.steering === "exhausted") side.classList.add("bad");
    else if (r.steering !== "calm") side.classList.add("warn");
    if (!absent(r.pressure)) side.title = "Pressure " + pressureText(r.pressure);
  } else if (items.length && (r.status === "stale" || r.status === "error")) {
    side.textContent = r.status === "stale" ? "stale" : "failed";
    side.title = "A " + (r.status === "stale" ? "stale" : "failed") + " reading steers nothing";
  }
  add(row, name, meters, value, resets, side);
  return [row, ticks];
}

// --- 4. by model ---------------------------------------------------------------------

// The pairs the window admitted and called, joined by model and effort:
// most admissions first, then most calls.
function ovModelRows(ov) {
  const choices = ((ov.decisions || {}).choices) || [];
  const pairs = ((ov.upstream || {}).pairs) || [];
  const key = (m, e) => (m || "") + "|" + (e || "");
  const rows = new Map();
  for (const c of choices) rows.set(key(c.model, c.effort), {model: c.model, effort: c.effort, adm: c.n || 0, up: null});
  for (const p of pairs) {
    const k = key(p.model, p.effort);
    const row = rows.get(k) || {model: p.model, effort: p.effort, adm: null, up: null};
    row.up = p;
    rows.set(k, row);
  }
  // A pair missing from a list that stopped at its cap is unknown there, not zero.
  const choicesFull = choices.length >= OV_TOP;
  const pairsFull = pairs.length >= OV_TOP;
  for (const row of rows.values()) {
    if (row.adm === null && !choicesFull) row.adm = 0;
    row.calls = row.up ? row.up.n || 0 : pairsFull ? null : 0;
  }
  return [...rows.values()].sort((a, b) => (b.adm || 0) - (a.adm || 0) || (b.calls || 0) - (a.calls || 0));
}

function ovByModel(ov) {
  const d = ov.decisions || {};
  const ex = ov.executions || {};
  const admissions = d.admissions || 0;
  const box = band("By model", plural(admissions, "admission") + " · " + plural(ex.rows || 0, "call"), "bymodel");
  const rows = ovModelRows(ov);
  const hasTps = rows.some((r) => r.up && r.up.tps && r.up.tps.n);
  const table = el("div", "tbl models" + (hasTps ? " tps" : ""));
  const head = el("div", "tr th");
  add(head, el("span", null, "Model · effort"), el("span", null, "Admissions"), el("span", "num", "Calls"),
    add(el("span", "num"), "First byte p50", el("span", "p95", "p95")), hasTps ? el("span", "num", "tok/s") : null);
  table.append(head);
  if (!rows.length) table.append(el("p", "empty", "No admission or call in this window"));
  for (const r of rows) {
    const tr = el("div", "tr");
    const share = el("span", "share");
    if (r.adm === null) share.append(gap("Outside the " + OV_TOP + " most admitted pairs"));
    else {
      const bar = el("i");
      bar.style.setProperty("--w", (admissions ? r.adm / admissions * 100 : 0) + "%");
      add(share, bar, r.adm ? int(r.adm) : el("span", "t3", "0"));
    }
    const calls = r.calls === null ? gap("Outside the " + OV_TOP + " most called pairs")
      : el("span", r.calls < OV_FEW_CALLS ? "t3" : null, int(r.calls));
    if (r.calls !== null && r.calls < OV_FEW_CALLS && r.calls > 0) calls.title = "Fewer than " + OV_FEW_CALLS + " calls: the p50 is a hint";
    add(tr, modelNode(r.model, r.effort), share, add(el("span", "num"), calls), ovFirstByte(r), hasTps ? ovTps(r) : null);
    table.append(tr);
  }
  box.append(table);
  const heat = ovHeatmap(ov);
  if (heat) box.append(heat);
  return box;
}

function ovFirstByte(r) {
  const cell = el("span", "num");
  const fb = r.up && r.up.first_byte_ms;
  if (fb && fb.n) {
    add(cell, ms(fb.p50), absent(fb.p95) ? null : el("span", "p95", ms(fb.p95)));
    if (fb.n < r.calls) add(cell, " ", gap("First byte recorded on " + int(fb.n) + " of " + plural(r.calls, "call")));
  } else cell.append(gap(r.calls ? "No call recorded a first byte" : "No call in this window"));
  return cell;
}

function ovTps(r) {
  const t = r.up && r.up.tps;
  const cell = el("span", "num");
  if (t && t.n) {
    cell.append(num1(t.p50));
    if (t.n < r.calls) add(cell, " ", gap("Reported on " + int(t.n) + " of " + plural(r.calls, "call")));
  } else cell.append(gap(r.calls ? "The upstream did not report output tokens" : "No call in this window"));
  return cell;
}

// Admissions by task, folded under the table: one hue, counts in the cells,
// a corrective rate only where a planned turn was assessed.
function ovHeatmap(ov) {
  const m = ov.matrix || {};
  const pairs = Array.isArray(m.pairs) ? m.pairs : [];
  const cells = Array.isArray(m.cells) ? m.cells : [];
  const more = el("details", "more");
  more.open = ovTasksOpen;
  more.addEventListener("toggle", () => { ovTasksOpen = more.open; });
  more.append(el("summary", null, "Admissions by " + (m.question ? human(m.question) : "task")));
  if (!pairs.length || !cells.length) {
    more.append(el("p", "empty", "No admission in this window"));
    return more;
  }
  const key = (task, model, effort) => [task, model, effort].join("|");
  const at = new Map(cells.map((c) => [key(c.task, c.model, c.effort), c]));
  const order = (Array.isArray(m.tasks) ? m.tasks : []).filter((t) => cells.some((c) => c.task === t));
  if (cells.some((c) => absent(c.task))) order.push(null);
  const max = Math.max(1, ...cells.map((c) => c.n || 0));
  const scroll = el("div", "heatscroll");
  const grid = el("div", "heat");
  grid.style.gridTemplateColumns = "minmax(8rem, 150px) repeat(" + pairs.length + ", minmax(4.5rem, 1fr))";
  grid.append(el("span"));
  for (const p of pairs) {
    const [, name] = splitModel(p.model);
    const h = el("span", "ov-hh mono", (name || "?") + " · " + (p.effort || "no effort"));
    h.title = (p.model || "model not recorded") + " · " + (p.effort || "no effort");
    grid.append(h);
  }
  for (const task of order) {
    grid.append(task === null ? add(el("span", "ov-ht"), gap("Jev's task answer was not recorded"), " not recorded") : el("span", "ov-ht", human(task)));
    for (const p of pairs) {
      const c = at.get(key(task, p.model, p.effort));
      const cell = el("div", "c");
      if (c && c.n) {
        cell.classList.add("on");
        cell.style.setProperty("--a", (12 + 38 * c.n / max).toFixed(1) + "%");
        cell.append(el("span", null, int(c.n)));
        const cr = c.corrective || {};
        if (cr.denominator) cell.append(el("small", cr.numerator ? "hot" : null, pct(cr.rate) + " corrective"));
        cell.title = (task === null ? "task not recorded" : human(task)) + " · " + (p.model || "?") + " · " + (p.effort || "no effort") + " · " + plural(c.n, "admission")
          + (cr.denominator ? " · " + int(cr.numerator) + " of " + int(cr.denominator) + " planned turns corrective" : "");
      }
      grid.append(cell);
    }
  }
  scroll.append(grid);
  more.append(scroll);
  return more;
}

// --- 5. legacy alias traffic ------------------------------------------------------------

function ovLegacy(l) {
  const box = band("Legacy alias traffic", "aliases without strict sessions, counted apart", "legacy");
  const row = el("div", "ov-lrow");
  const cell = (label, value) => row.append(add(el("div", "ov-lc"), el("span", "t3 s12", label), add(el("span", "ov-lv"), value)));
  const pp = (p) => (p && p.n ? ms(p.p50) + " · " + ms(p.p95) : gap("Not recorded on any legacy row"));
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
