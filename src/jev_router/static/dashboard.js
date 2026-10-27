"use strict";

// Router inspector. Read-only. Builds every node with createElement and
// textContent; dynamic sizes go through element.style (CSSOM), never markup.

const LADDER = ["none", "low", "medium", "high", "xhigh", "max"];
const STAGES = ["Jev", "Constraints", "Rule & quota", "Binding", "Outcome"];
const REFRESH_MS = 5000;
const LIST_LIMIT = 200;
const WINDOWS = ["1h", "24h", "7d", "30d"];
const FILTER_KEYS = ["q", "client", "model", "rule", "state", "adaptive", "problem", "pinned", "window"];
const STATES = ["active", "prepared", "blocked", "closed"];
const MODES = ["off", "shadow", "active"];
const PROBLEMS = {
  any: "Any problem", unknown: "Outcome unknown", rejected: "Rejected",
  stream_failed: "Stream failed", fallback: "Fallback admission", low_confidence: "Low-confidence gate",
};
const PROBLEM_SHORT = {unknown: "unknown", rejected: "rejected", stream_failed: "stream failed", fallback: "fallback", low_confidence: "low conf"};

const S = {
  token: "",
  connected: false,
  paused: false,
  view: "overview",   // overview | sessions | session
  win: "24h",
  filters: {},
  overview: null,
  overviewJson: "",
  list: null,
  listJson: "",
  listKey: null,
  cfgHash: null,
  sid: null,
  slug: null,
  data: null,
  dataJson: "",
  missing: false,
  sel: null,          // request seq, or null for the admission
  stage: 1,
  timer: null,
  controller: null,
  gen: 0,
  toolbar: null,
  facetsJson: "",
  searchTimer: null,
};

const $ = (id) => document.getElementById(id);

// --- small builders ------------------------------------------------------

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function add(parent, ...children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return parent;
}
function absent(v) { return v === null || v === undefined || (typeof v === "number" && !Number.isFinite(v)); }
function gap(text) { return el("span", "gapchip", text || "not recorded"); }
// A value, or a visible amber gap. Never a guess.
function show(v, format) {
  if (absent(v) || v === "") return gap();
  return document.createTextNode(format ? format(v) : String(v));
}
function button(cls, text, onClick, label) {
  const b = el("button", cls, text);
  b.type = "button";
  if (label) b.setAttribute("aria-label", label);
  b.addEventListener("click", onClick);
  return b;
}
function link(cls, text, hash, label) {
  const a = el("a", cls, text);
  a.href = hash;
  if (label) a.setAttribute("aria-label", label);
  return a;
}

// --- formatting ----------------------------------------------------------

const int = (n) => Math.round(Number(n)).toLocaleString("en-US");
const num2 = (n) => Number(n).toFixed(2);
const secs = (ms) => (Number(ms) / 1000).toFixed(2) + " s";
const size = (b) => (b >= 1e6 ? (b / 1e6).toFixed(2) + " MB" : Math.max(1, Math.round(b / 1000)) + " KB");
const num1 = (n) => { const v = Number(n); return v > 0 && v < 0.1 ? "<0.1" : v.toFixed(v >= 10 ? 0 : 1); };
const pct = (r) => { const v = Number(r) * 100; return (v > 0 && v < 1 ? "<1" : v > 99 && v < 100 ? ">99" : Math.round(v)) + "%"; };
// Milliseconds as the reader wants them: ms under a second, seconds above.
const ms = (v) => (Number(v) < 1000 ? Math.round(Number(v)) + " ms" : secs(v));
// Pressure is 0 to 1, not a share of the allowance.
const pressureText = (p) => { const v = Number(p); return v > 0 && v < 0.01 ? "<0.01" : v.toFixed(2); };
const windowSpan = (m) => (m >= 1440 ? Math.round(m / 1440) + " d" : m >= 60 ? Math.round(m / 60) + " h" : Math.round(m) + " min");
const human = (s) => String(s).replaceAll("_", " ");
const cap = (s) => { const t = String(s).replaceAll("_", " ").replaceAll("-", " "); return t.charAt(0).toUpperCase() + t.slice(1); };
function pad(x) { return String(x).padStart(2, "0"); }
function clock(ts) { const d = new Date(ts * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); }
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function day(ts) { const d = new Date(ts * 1000); return DAYS[d.getDay()] + " " + d.getDate() + " " + MONTHS[d.getMonth()]; }
function shortDay(ts) { const d = new Date(ts * 1000); return d.getDate() + " " + MONTHS[d.getMonth()]; }
// Today's times bare; anything older with its date.
function stamp(ts) {
  const d = new Date(ts * 1000);
  const hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
  return d.toDateString() === new Date().toDateString() ? hm : shortDay(ts) + " " + hm;
}
function zone(ts) {
  try {
    const part = new Intl.DateTimeFormat("en-US", {timeZoneName: "short"}).formatToParts(new Date(ts * 1000)).find((p) => p.type === "timeZoneName");
    return part ? part.value : "";
  } catch { return ""; }
}
function rel(t) { return t < 60 ? "+" + t.toFixed(1) + " s" : "+" + Math.floor(t / 60) + " m " + Math.round(t % 60) + " s"; }
function span(t) { return t < 60 ? t.toFixed(0) + " s" : Math.floor(t / 60) + " m " + Math.round(t % 60) + " s"; }
function short(id, client) {
  if (!id) return "";
  let rest = String(id);
  if (client && rest.startsWith(client + "-")) rest = rest.slice(client.length + 1);
  else { const m = /^[a-z][a-z0-9]*[-_](.+)$/i.exec(rest); if (m && m[1].length >= 4) rest = m[1]; }
  return rest.slice(0, 4) + "…";
}
function splitModel(model) {
  if (!model) return ["", null];
  const i = String(model).lastIndexOf("/");
  return i < 0 ? ["", model] : [model.slice(0, i + 1), model.slice(i + 1)];
}
function hash8(h) { return h ? String(h).slice(0, 8) : null; }
function cfg(v) { return typeof v === "number" ? String(v) : plain(v); }
function bool(v) { return v === true ? "yes" : v === false ? "no" : v; }
function plain(v) {
  if (typeof v === "boolean") return bool(v);
  if (typeof v === "number") return Number.isInteger(v) ? int(v) : num2(v);
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}
// The ledger says "unknown"; older payloads said "outcome_unknown".
function isUnknown(status) { return status === "unknown" || status === "outcome_unknown"; }

// --- routes ----------------------------------------------------------------
//
// #overview[?w=7d]  ·  #sessions[?client=pi&problem=any…]  ·  #s=<id>  ·  #<slug>

function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  const cut = raw.indexOf("?");
  const head = cut < 0 ? raw : raw.slice(0, cut);
  const params = new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1));
  let name = head;
  try { name = decodeURIComponent(head); } catch { /* keep it raw */ }
  if (!name || name === "overview") {
    const w = params.get("w");
    return {view: "overview", win: WINDOWS.includes(w) ? w : "24h"};
  }
  if (name === "sessions") {
    const filters = {};
    for (const key of FILTER_KEYS) { const v = params.get(key); if (v) filters[key] = v; }
    return {view: "sessions", filters};
  }
  if (name.startsWith("s=")) return {view: "session", sid: name.slice(2) || null, slug: null};
  return {view: "session", sid: null, slug: name};
}
function filterQuery(filters) {
  const q = new URLSearchParams();
  for (const key of FILTER_KEYS) if (filters[key]) q.set(key, filters[key]);
  return q.toString();
}
function listHash(filters) { const q = filterQuery(filters || S.filters); return "#sessions" + (q ? "?" + q : ""); }
function overviewHash(win) { return "#overview" + ((win || S.win) === "24h" ? "" : "?w=" + (win || S.win)); }
function sessionHash(id) { return "#s=" + encodeURIComponent(id); }

// --- networking ----------------------------------------------------------

async function api(path) {
  const current = new AbortController();
  const timeout = setTimeout(() => current.abort(), 10000);
  S.controller = current;
  try {
    return await fetch(path, {
      headers: S.token ? {"X-Router-Admin-Token": S.token} : {},
      cache: "no-store",
      signal: current.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
}

// A person's write: feedback, an eval case, a Jev probe. Same credential and
// timeout handling as api(); a probe can take a while, so the timeout is longer.
async function post(path, body) {
  const current = new AbortController();
  const timeout = setTimeout(() => current.abort(), 30000);
  try {
    return await fetch(path, {
      method: "POST",
      headers: Object.assign({"Content-Type": "application/json"}, S.token ? {"X-Router-Admin-Token": S.token} : {}),
      body: JSON.stringify(body),
      cache: "no-store",
      signal: current.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
}

function stopTimer() {
  clearTimeout(S.timer);
  S.timer = null;
}
function schedule() {
  stopTimer();
  if (S.connected && !S.paused && !document.hidden) S.timer = setTimeout(() => refresh("data"), REFRESH_MS);
}

function fail(message) {
  const box = $("error");
  box.hidden = !message;
  box.textContent = message || "";
}

function resetSession() {
  S.data = null;
  S.dataJson = "";
  S.missing = false;
  S.sel = null;
}

// One refresh per tick, for the view on screen. A navigation bumps S.gen, and
// an answer that arrives for an older view is dropped.
async function refresh(kind) {
  stopTimer();
  const gen = S.gen;
  let changed = false;
  let refused = null;
  try {
    if (S.view === "overview") {
      const response = await api("/router/audit/overview?window=" + encodeURIComponent(S.win));
      if (gen !== S.gen) return;
      if (response.status === 401) return unauthorized();
      if (!response.ok) throw new Error("HTTP " + response.status);
      const text = await response.text();
      if (gen !== S.gen) return;
      if (text !== S.overviewJson) {
        S.overviewJson = text;
        S.overview = JSON.parse(text);
        changed = true;
      }
      S.cfgHash = S.overview.config_hash;
    } else {
      const key = filterQuery(S.filters);
      const response = await api("/router/audit/sessions?limit=" + LIST_LIMIT + (key ? "&" + key : ""));
      if (gen !== S.gen) return;
      if (response.status === 401) return unauthorized();
      if (response.status === 400) {
        // A hand-edited link with a filter the router does not accept.
        const body = await response.json().catch(() => ({}));
        refused = "Filter not accepted · " + (body.error || "bad request");
        S.list = {sessions: [], bookmarks: [], facets: {}, matched: 0};
        S.listJson = "";
        S.listKey = key;
        changed = true;
      } else {
        if (!response.ok) throw new Error("HTTP " + response.status);
        const text = await response.text();
        if (gen !== S.gen) return;
        if (text !== S.listJson || key !== S.listKey) {
          S.listJson = text;
          S.listKey = key;
          S.list = JSON.parse(text);
          changed = true;
        }
        S.cfgHash = S.list.config_hash;
      }
      if (S.view === "session") {
        let wanted = S.sid;
        if (S.slug !== null) {
          const book = bookmarks().find((b) => b.slug === S.slug);
          wanted = book ? book.session_id : null;
        }
        if (wanted !== S.sid) {
          S.sid = wanted;
          resetSession();
          kind = "view";
        }
        if (!S.sid) {
          if (!S.missing) changed = true;
          S.missing = true;
        } else {
          const response2 = await api("/router/audit/sessions/" + encodeURIComponent(S.sid));
          if (gen !== S.gen) return;
          if (response2.status === 404) {
            if (!S.missing) changed = true;
            S.missing = true;
            S.data = null;
            S.dataJson = "";
          } else if (!response2.ok) {
            throw new Error("HTTP " + response2.status);
          } else {
            const text = await response2.text();
            if (gen !== S.gen) return;
            S.missing = false;
            if (text !== S.dataJson) {
              S.dataJson = text;
              S.data = JSON.parse(text);
              changed = true;
              if (S.sel !== null && !requests().some((r) => r.seq === S.sel)) S.sel = null;
            }
          }
        }
      }
    }
    S.connected = true;
    fail(refused);
    chrome();
    if (changed || kind !== "data") render(kind);
  } catch (error) {
    if (gen !== S.gen) return;
    fail((error && error.name === "AbortError" ? "Refresh timed out" : "Router unreachable") + " · showing the last data");
  } finally {
    if (gen === S.gen) schedule();
  }
}

function unauthorized() {
  S.connected = false;
  chrome();
  render("view");
  fail("Admin token required");
}

function bookmarks() { return (S.list && Array.isArray(S.list.bookmarks)) ? S.list.bookmarks : []; }
function sessions() { return (S.list && Array.isArray(S.list.sessions)) ? S.list.sessions : []; }
function requests() { return (S.data && Array.isArray(S.data.requests)) ? S.data.requests : []; }
function plans() { return (S.data && Array.isArray(S.data.plans)) ? S.data.plans : []; }

// --- header and navigation --------------------------------------------------

function chrome() {
  $("auth").hidden = S.connected;
  $("pause").hidden = !S.connected;
  $("disconnect").hidden = !S.connected;
  $("views").hidden = !S.connected;
  $("pause").textContent = S.paused ? "Resume" : "Pause";
  const cfgNode = $("cfg");
  cfgNode.replaceChildren();
  if (S.connected && S.cfgHash !== null) {
    const dot = el("span", "dot" + (S.paused ? " off" : ""));
    dot.title = S.paused ? "paused" : "live · every 5 s";
    add(cfgNode, dot, show(hash8(S.cfgHash)));
  }
  const over = $("v-overview");
  const list = $("v-sessions");
  over.href = overviewHash();
  list.href = listHash();
  over.toggleAttribute("aria-current", S.view === "overview");
  list.toggleAttribute("aria-current", S.view !== "overview");
  if (S.view === "overview") over.setAttribute("aria-current", "page");
  else list.setAttribute("aria-current", "page");
}

function go(hash) {
  if (location.hash === hash || (hash === "#overview" && !location.hash)) return;
  location.hash = hash;
}

function onHash() {
  const route = parseHash();
  S.gen += 1;
  S.view = route.view;
  if (route.view === "overview") {
    if (route.win !== S.win) {
      S.win = route.win;
      S.overview = null;
      S.overviewJson = "";
    }
  } else if (route.view === "sessions") {
    S.filters = route.filters;
  } else if (route.sid !== S.sid || route.slug !== S.slug) {
    S.slug = route.slug;
    if (route.sid !== null || S.slug === null) S.sid = route.sid;
    else {
      const book = bookmarks().find((b) => b.slug === S.slug);
      S.sid = book ? book.session_id : null;
    }
    resetSession();
  }
  chrome();
  if (!S.connected) return;
  // Show what is cached for this view at once; the fetch follows.
  render("view");
  window.scrollTo(0, 0);
  refresh("view");
}

// --- render --------------------------------------------------------------

// kind: "view" (arrived: entrance motion), "pane" (a choice inside the view),
// "data" (the live refresh: nothing replays).
function render(kind) {
  const main = $("main");
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.fk : null;
  if (!S.connected) { main.replaceChildren(); return; }
  if (S.view === "sessions") {
    renderSessions(kind);
  } else {
    main.replaceChildren();
    if (S.view === "overview") {
      if (S.overview) main.append(overviewView(kind));
    } else {
      main.append(inspectorView(kind));
    }
  }
  if (focused) {
    const again = document.querySelector('[data-fk="' + CSS.escape(focused) + '"]');
    if (again && again !== document.activeElement) again.focus({preventScroll: true});
  }
}

// --- overview ----------------------------------------------------------------

function overviewView(kind) {
  const ov = S.overview;
  const root = el("section", "ov" + (kind === "data" ? " still" : ""));
  const w = ov.window || S.win;
  const within = (extra) => listHash(Object.assign({window: w}, extra || {}));

  const head = el("div", "ovh");
  const seg = el("div", "seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Window");
  for (const option of WINDOWS) {
    const a = link("segb" + (option === w ? " on" : ""), option, overviewHash(option));
    if (option === w) a.setAttribute("aria-current", "true");
    seg.append(a);
  }
  const since = el("span", "note");
  if (!absent(ov.since)) since.textContent = "since " + day(ov.since) + " " + clock(ov.since).slice(0, 5) + " " + zone(ov.since);
  add(head, el("h1", "ovt", "Overview"), seg, since);
  root.append(head);

  // Volume: every count links to the sessions behind it.
  const ex = ov.executions || {};
  const st = ex.by_status || {};
  const kpis = el("div", "kpis");
  const kpi = (label, value, sub, hash, warn) => {
    const box = hash ? link("kpi" + (warn ? " warn" : ""), null, hash) : el("div", "kpi");
    add(box, el("span", "kl", label), add(el("span", "kpv"), value), sub ? add(el("span", "ks"), sub) : null);
    kpis.append(box);
  };
  const ss = ov.sessions || {};
  kpi("Sessions admitted", int(ss.admitted || 0), modeLine(ss), within());
  kpi("Executions", int(ex.total || 0), absent(ex.completion_rate) ? null : pct(ex.completion_rate) + " completed", null);
  kpi("Outcome unknown", int(st.unknown || 0), null, st.unknown ? within({problem: "unknown"}) : null, st.unknown > 0);
  kpi("Rejected", int(st.rejected || 0), null, st.rejected ? within({problem: "rejected"}) : null, st.rejected > 0);
  kpi("Stream failed", int(st.stream_failed || 0), null, st.stream_failed ? within({problem: "stream_failed"}) : null, st.stream_failed > 0);
  const lat = ov.latency || {};
  kpi("Jev at admission", pctl(lat.jev_admission_ms), pctlSub(lat.jev_admission_ms), null);
  root.append(kpis);

  const grid = el("div", "ovgrid");
  grid.append(seriesPanel(ov), modelsPanel(ov), decisionsPanel(ov, within), quotaPanel(ov), effortPanel(ov, within), legacyPanel(ov));
  root.append(grid);
  return root;
}

function modeLine(ss) {
  const a = ss.adaptation || {};
  const bits = [];
  if (a.shadow) bits.push(a.shadow + " shadow");
  if (a.active) bits.push(a.active + " adaptive");
  if (!bits.length && ss.admitted) bits.push("effort fixed");
  return bits.join(" · ") || null;
}

// p50 of a percentile record, or a gap that says why there is none.
function pctl(p) {
  if (!p || !p.n) return el("span", "none", "–");
  return document.createTextNode(ms(p.p50));
}
function pctlSub(p) { return p && p.n ? "p95 " + ms(p.p95) + " · n " + int(p.n) : null; }
function pairCell(p, rows) {
  const cell = el("span", "pp");
  if (p && p.n) {
    add(cell, ms(p.p50), el("span", "p95", ms(p.p95)));
    if (rows && p.n < rows) add(cell, " ", gap(int(p.n) + " of " + int(rows) + " recorded"));
  } else add(cell, rows ? gap("not recorded") : el("span", "none", "–"));
  return cell;
}

function panel(title, note, cls) {
  const box = el("section", "panel" + (cls ? " " + cls : ""));
  const h = el("div", "ph");
  add(h, el("h2", "pt", title), note ? add(el("span", "note"), note) : null);
  box.append(h);
  return box;
}

function bucketLabel(t, step) {
  if (step >= 86400) return shortDay(t);
  if (step >= 21600) return DAYS[new Date(t * 1000).getDay()] + " " + clock(t).slice(0, 5);
  return clock(t).slice(0, 5);
}

function seriesPanel(ov) {
  const series = Array.isArray(ov.series) ? ov.series : [];
  const step = ov.bucket_seconds || 3600;
  const box = panel("Requests and p50 response", "per " + windowSpan(step / 60), "wide");
  const maxReq = Math.max(1, ...series.map((b) => b.requests || 0));
  const maxMs = Math.max(1, ...series.map((b) => b.response_p50_ms || 0));
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
    if (!absent(b.response_p50_ms)) {
      bar.style.height = Math.max(2, b.response_p50_ms / maxMs * 100) + "%";
      m.title = when + " · p50 " + ms(b.response_p50_ms) + " over " + b.timed;
    } else if (b.requests > 0) {
      // Requests ran, no time was recorded for them: a gap, not a zero.
      bar.className = "gapbar";
      m.title = when + " · response time not recorded";
    }
    bar.style.animationDelay = Math.min(400, i * 10) + "ms";
    m.append(bar);
    msRow.append(m);
  });
  const axis = el("div", "saxis");
  if (series.length) {
    add(axis, el("span", null, bucketLabel(series[0].t, step)), el("span", null, bucketLabel(series[Math.floor(series.length / 2)].t, step)), el("span", null, bucketLabel(series[series.length - 1].t, step)));
  }
  const legend = el("div", "slegend");
  add(legend, el("span", null, "max " + maxReq + " requests"), el("span", "badkey", "problems"), el("span", null, "p50 up to " + (maxMs > 1 ? ms(maxMs) : "–")));
  box.append(reqRow, msRow, axis, legend);
  return box;
}

function modelsPanel(ov) {
  const lat = ov.latency || {};
  const ex = ov.executions || {};
  const models = Array.isArray(lat.models) ? lat.models : [];
  const note = el("span");
  if (ex.rows && ex.usage_reported < ex.rows) add(note, gap("usage not reported on " + int(ex.rows - ex.usage_reported) + " of " + int(ex.rows)));
  const box = panel("Response time by model", note, "wide");
  const table = el("div", "mt");
  const header = el("div", "mr mh");
  add(header, el("span", null, "Model"), el("span", "n", "Executions"), el("span", null, "First byte p50 · p95"), el("span", null, "Total p50 · p95"));
  table.append(header);
  if (!models.length) table.append(el("p", "empty", "No executions in this window"));
  for (const m of models) {
    const row = el("div", "mr");
    const name = m.model ? link("mono", m.model, listHash({model: m.model, window: ov.window})) : gap("model not recorded");
    add(row, add(el("span", "mm"), name), el("span", "n mono", int(m.n)), pairCell(m.first_byte_ms, m.n), pairCell(m.response_ms, m.n));
    table.append(row);
  }
  const jev = el("div", "jevline");
  add(jev, el("span", "kl", "Jev per turn plan"), add(el("span", "mono"), pctl(lat.jev_plan_ms)), lat.jev_plan_ms && lat.jev_plan_ms.n ? el("span", "note", pctlSub(lat.jev_plan_ms)) : el("span", "note", "no plans in this window"));
  box.append(table, jev);
  return box;
}

function distribution(rows, label, hashOf) {
  const list = el("div", "dist");
  const total = rows.reduce((a, r) => a + (r.n || 0), 0) || 1;
  if (!rows.length) list.append(el("p", "empty", "None in this window"));
  for (const r of rows) {
    const name = label(r);
    const hash = hashOf(r);
    const row = hash ? link("dr", null, hash) : el("div", "dr");
    const fill = el("i");
    fill.style.width = Math.max(1, r.n / total * 100) + "%";
    add(row, add(el("span", "dn"), name), add(el("span", "dt2"), fill), el("span", "dc", int(r.n)), el("span", "dp", pct(r.n / total)));
    list.append(row);
  }
  return list;
}

function decisionsPanel(ov, within) {
  const d = ov.decisions || {};
  const box = panel("Admission decisions", d.admissions ? int(d.admissions) + " admissions" : null, "wide");
  const cols = el("div", "dcols");
  const rules = add(el("div"), el("div", "qn", "Rule"), distribution(d.rules || [], (r) => r.rule || gap(), (r) => r.rule ? within({rule: r.rule}) : null));
  const choices = add(el("div"), el("div", "qn", "Model · effort"), distribution(d.choices || [], (r) => add(el("span"), r.model ? r.model : gap(), r.effort ? " · " + r.effort : " · no effort"), (r) => r.model ? within({model: r.model}) : null));
  add(cols, rules, choices);
  box.append(cols);
  const facts = el("div", "dfacts");
  const fact = (label, n, of, hash, warn) => {
    const node = hash && n ? link("df" + (warn && n ? " warn" : ""), null, hash) : el("div", "df");
    add(node, el("span", "kl", label), el("span", "dv", int(n || 0) + (of ? " of " + int(of) : "")), of ? el("span", "note", pct((n || 0) / of)) : null);
    facts.append(node);
  };
  fact("Low-confidence gate", d.low_confidence, d.admissions, within({problem: "low_confidence"}), false);
  fact("Fallback (Jev failed)", d.fallback, d.admissions, within({problem: "fallback"}), true);
  const qc = el("div", "df");
  add(qc, el("span", "kl", "Quota changed the choice"), el("span", "dv", int(d.quota_changed || 0) + " of " + int(d.quota_recorded || 0)));
  if ((d.quota_recorded || 0) < (ov.sessions && ov.sessions.admitted || 0)) qc.append(gap("not recorded on " + ((ov.sessions.admitted || 0) - (d.quota_recorded || 0))));
  facts.append(qc);
  box.append(facts);
  return box;
}

function quotaPanel(ov) {
  const q = ov.quota;
  const note = q && !absent(q.ts) ? "as of " + stamp(q.ts) : null;
  const box = panel("Quota", note);
  if (!q || !q.providers || !Object.keys(q.providers).length) {
    box.append(el("p", "gaprow", "no quota reading on any decision"));
    return box;
  }
  const list = el("div", "qlist");
  for (const [provider, p] of Object.entries(q.providers)) {
    const row = el("div", "qp");
    const head = el("div", "qph");
    const status = p && p.status;
    add(head, el("span", "mono", provider));
    if (status === "fresh") add(head, el("span", "qs ok", "fresh"), add(el("span", "qpr"), "pressure ", show(p.pressure, pressureText)));
    else add(head, el("span", "qs gap", (status || "unknown") + " · counted as 0"));
    row.append(head);
    // Each window is its own record: its usage beside its own reset.
    for (const w of Array.isArray(p && p.windows) ? p.windows : []) {
      const line = el("div", "qw");
      const name = [w.name, absent(w.window_minutes) ? null : windowSpan(w.window_minutes)].filter(Boolean).join(" · ") || "window";
      const track = el("span", "qt");
      if (!absent(w.used_percent)) {
        const fill = el("i");
        fill.style.width = Math.max(0.5, Math.min(100, w.used_percent)) + "%";
        track.append(fill);
      }
      add(line, el("span", "qn2", name), track,
        absent(w.used_percent) ? gap("usage not reported") : el("span", "mono", num1(w.used_percent) + "%"),
        el("span", "note", absent(w.resets_at) ? "" : "resets " + stamp(w.resets_at)));
      row.append(line);
    }
    list.append(row);
  }
  box.append(list);
  return box;
}

function effortPanel(ov, within) {
  const e = ov.effort || {};
  const ss = (ov.sessions && ov.sessions.adaptation) || {};
  const box = panel("Adaptive effort", null);
  if (!(e.shadow && (e.shadow.plans || ss.shadow)) && !(e.active && (e.active.plans || ss.active))) {
    box.append(el("p", "empty", "No adaptive-effort session or plan in this window"));
    return box;
  }
  const cols = el("div", "ecols");
  for (const mode of ["shadow", "active"]) {
    const m = e[mode] || {};
    if (!m.plans && !ss[mode]) continue;
    const col = el("div", "ec");
    const h = el("div", "ech");
    const title = mode === "shadow" ? "Shadow" : "Active";
    add(h, ss[mode] ? link("ect", title, within({adaptive: mode})) : el("span", "ect", title), el("span", "note", int(ss[mode] || 0) + (ss[mode] === 1 ? " session · " : " sessions · ") + int(m.plans || 0) + (m.plans === 1 ? " plan" : " plans")));
    col.append(h);
    const funnel = el("div", "funnel");
    const step = (label, n, extra) => funnel.append(add(el("div", "fs"), el("span", "fv2", int(n || 0)), el("span", "kl", label), extra || null));
    step("recommended a change", m.recommended);
    step("applied", m.applied, mode === "shadow" ? el("span", "note", "never, by design") : null);
    step("confirmed", m.confirmed);
    col.append(funnel);
    const statuses = m.by_status || {};
    const odd = Object.entries(statuses).filter(([s, n]) => n && s !== "confirmed");
    if (odd.length) {
      const line = el("div", "estat");
      for (const [s, n] of odd) add(line, el("span", s === "outcome_unknown" || s === "rejected" ? "gap" : null, int(n) + " " + human(s)));
      col.append(line);
    }
    cols.append(col);
  }
  box.append(cols);
  return box;
}

function legacyPanel(ov) {
  const l = ov.legacy || {};
  const box = panel("Legacy alias traffic", "no session · kept out of every number above", "wide legacy");
  if (!l.decisions) { box.append(el("p", "empty", "None in this window")); return box; }
  const row = el("div", "lrow");
  const cell = (label, value) => row.append(add(el("div", "lc"), el("span", "kl", label), add(el("span", "lval"), value)));
  cell("Decisions", int(l.decisions));
  cell("Pinned", int(l.pinned || 0));
  cell("Fallback", int(l.fallback || 0));
  cell("Upstream errors", int(l.upstream_errors || 0));
  cell("Jev p50 · p95", pairCell(l.jev_ms, 0));
  cell("First byte p50 · p95", pairCell(l.first_byte_ms, l.decisions));
  cell("Total p50 · p95", pairCell(l.response_ms, l.decisions));
  box.append(row);
  return box;
}

// --- sessions ----------------------------------------------------------------

function renderSessions(kind) {
  const main = $("main");
  if (!S.toolbar) S.toolbar = buildToolbar();
  syncToolbar();
  const table = sessionTable(kind);
  if (main.firstChild === S.toolbar && main.children.length === 2) main.lastChild.replaceWith(table);
  else main.replaceChildren(S.toolbar, table);
}

function setFilter(key, value, typing) {
  const next = Object.assign({}, S.filters);
  if (value) next[key] = value; else delete next[key];
  const hash = listHash(next);
  if (typing) {
    // Typing replaces the entry rather than piling up history.
    history.replaceState(null, "", hash);
    onHash();
  } else go(hash);
}

function select(id, label, onChange) {
  const s = el("select", "fsel");
  s.id = id;
  s.setAttribute("aria-label", label);
  s.addEventListener("change", () => onChange(s.value));
  return s;
}
function fillSelect(s, options, current) {
  const values = options.map((o) => o[0]);
  if (current && !values.includes(current)) options = options.concat([[current, current]]);
  s.replaceChildren(...options.map(([value, text]) => { const o = el("option", null, text); o.value = value; return o; }));
  s.value = current || "";
}

function buildToolbar() {
  const bar = el("div", "toolbar");
  const search = el("input", "fsearch");
  search.id = "q";
  search.type = "search";
  search.placeholder = "Session id";
  search.autocomplete = "off";
  search.spellcheck = false;
  search.setAttribute("aria-label", "Search by session id");
  search.addEventListener("input", () => {
    clearTimeout(S.searchTimer);
    S.searchTimer = setTimeout(() => {
      const v = search.value.trim();
      if (!v || /^[A-Za-z0-9_.:-]{1,64}$/.test(v)) setFilter("q", v, true);
    }, 250);
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { search.blur(); event.stopPropagation(); }
    if (event.key === "Enter" || event.key === "ArrowDown") {
      const first = document.querySelector(".srow");
      if (first) { first.focus(); event.preventDefault(); }
    }
  });
  const pinned = button("pinb", "Pinned", () => setFilter("pinned", S.filters.pinned === "1" ? "" : "1"));
  pinned.id = "pinned";
  const clear = button("ghost clear", "Clear", () => go("#sessions"));
  clear.id = "clear";
  const count = el("span", "count");
  count.id = "count";
  add(bar,
    search,
    select("f-client", "Client", (v) => setFilter("client", v)),
    select("f-model", "Model", (v) => setFilter("model", v)),
    select("f-rule", "Rule", (v) => setFilter("rule", v)),
    select("f-state", "State", (v) => setFilter("state", v)),
    select("f-adaptive", "Adaptive effort", (v) => setFilter("adaptive", v)),
    select("f-problem", "Problems", (v) => setFilter("problem", v)),
    select("f-window", "Last seen", (v) => setFilter("window", v)),
    pinned, clear, count);
  return bar;
}

// A toolbar control, attached to the page or not yet.
function tb(id) { return S.toolbar.querySelector("#" + id); }

function syncToolbar() {
  const f = S.filters;
  const facets = (S.list && S.list.facets) || {};
  const json = JSON.stringify(facets) + "|" + JSON.stringify(f);
  if (json !== S.facetsJson) {
    S.facetsJson = json;
    const opts = (name, all) => [["", all]].concat((facets[name] || []).map((x) => [x.value, x.value]));
    fillSelect(tb("f-client"), opts("client", "All clients"), f.client);
    fillSelect(tb("f-model"), opts("model", "All models"), f.model);
    fillSelect(tb("f-rule"), opts("rule", "All rules"), f.rule);
    fillSelect(tb("f-state"), [["", "Any state"]].concat(STATES.map((s) => [s, cap(s)])), f.state);
    fillSelect(tb("f-adaptive"), [["", "Any effort mode"]].concat(MODES.map((m) => [m, m === "off" ? "Adaptive off" : "Adaptive " + m])), f.adaptive);
    fillSelect(tb("f-problem"), [["", "Any outcome"]].concat(Object.entries(PROBLEMS)), f.problem);
    fillSelect(tb("f-window"), [["", "Any time"]].concat(WINDOWS.map((w) => [w, "Last " + w])), f.window);
  }
  for (const id of ["f-client", "f-model", "f-rule", "f-state", "f-adaptive", "f-problem", "f-window"]) {
    tb(id).classList.toggle("set", !!tb(id).value);
  }
  const search = tb("q");
  if (document.activeElement !== search && search.value !== (f.q || "")) search.value = f.q || "";
  const pinned = tb("pinned");
  pinned.setAttribute("aria-pressed", f.pinned === "1" ? "true" : "false");
  const books = bookmarks().length;
  pinned.textContent = "Pinned" + (books ? " " + books : "");
  tb("clear").hidden = !FILTER_KEYS.some((k) => f[k]);
  const shown = sessions().length;
  const matched = S.list && typeof S.list.matched === "number" ? S.list.matched : shown;
  tb("count").textContent = S.list ? (matched > shown ? shown + " of " + int(matched) : int(shown)) + (matched === 1 ? " session" : " sessions") : "";
}

const COLUMNS = ["Last seen", "Session", "Model · effort", "Rule", "Adaptive", "Requests", "Problems", "p50"];

function sessionTable(kind) {
  const root = el("section", "stable" + (kind === "data" ? " still" : ""));
  const rows = sessions();
  if (!S.list) return root;
  if (!rows.length) {
    root.append(el("p", "empty", FILTER_KEYS.some((k) => S.filters[k]) ? "No session matches these filters" : "No sessions yet"));
    return root;
  }
  const head = el("div", "srow shead");
  head.setAttribute("aria-hidden", "true");
  COLUMNS.forEach((c, i) => head.append(el("span", i >= 5 && i !== 6 ? "c" + i + " num" : "c" + i, c)));
  root.append(head);
  rows.forEach((s, i) => root.append(sessionRow(s, i)));
  return root;
}

function sessionRow(s, i) {
  const row = link("srow", null, sessionHash(s.session_id));
  row.dataset.fk = "row-" + s.session_id;
  row.dataset.sid = s.session_id;
  row.style.animationDelay = Math.min(300, i * 12) + "ms";
  const when = el("span", "c0 mono");
  add(when, absent(s.last_seen) ? (absent(s.created) ? gap() : stamp(s.created)) : stamp(s.last_seen));
  const who = el("span", "c1");
  if (s.pinned) who.append(el("span", "pin", "◆"));
  add(who, s.client ? el("span", null, s.client) : gap("client"), el("span", "sid mono", short(s.session_id, s.client)));
  if (s.pinned) who.append(el("span", "plabel", s.pinned.label));
  if (s.tombstone) who.append(el("span", "faint", "pruned"));
  else if (s.state && s.state !== "active") who.append(el("span", "faint", s.state));
  const mdl = el("span", "c2 mono");
  add(mdl, show(s.model), s.effort ? el("span", "faint", " · " + s.effort) : null);
  const rl = add(el("span", "c3 mono"), show(s.rule));
  const mode = el("span", "c4");
  if (absent(s.adaptation_mode)) mode.append(gap());
  else if (s.adaptation_mode !== "off") mode.append(el("span", "mode", s.adaptation_mode));
  const counts = s.requests || {};
  const total = Object.values(counts).reduce((a, n) => a + (Number(n) || 0), 0);
  const n = el("span", "c5 num mono", total ? (counts.completed || 0) + " / " + total : "0");
  const probs = el("span", "c6");
  for (const p of Array.isArray(s.problems) ? s.problems : []) {
    const k = p === "unknown" ? counts.unknown : p === "rejected" ? counts.rejected : p === "stream_failed" ? counts.stream_failed : null;
    probs.append(el("span", "prob", (k ? k + " " : "") + (PROBLEM_SHORT[p] || human(p))));
  }
  const rt = s.response_ms || {};
  const p50 = el("span", "c7 num mono");
  if (!absent(rt.p50)) p50.textContent = secs(rt.p50);
  else if (counts.completed) p50.append(gap());
  else p50.append(el("span", "faint", "–"));
  add(row, when, who, mdl, rl, mode, n, probs, p50);
  return row;
}

// --- rule wording, shared with the session page ---------------------------------

const OPS = {gte: ["≥", "<"], gt: [">", "≤"], lte: ["≤", ">"], lt: ["<", "≥"], eq: ["=", "≠"], ne: ["≠", "="], conf_gte: ["≥", "<"]};

function condText(c) {
  const node = el("span");
  const name = String(c.key || "?").replace(/^has_/, "").replaceAll("_", " ") + (c.op === "conf_gte" ? " confidence" : "");
  if (absent(c.value)) { add(node, name + " ", gap()); return node; }
  const v = typeof c.value === "number" ? num2(c.value) : plain(c.value);
  if (typeof c.value === "boolean" || (c.op === "eq" && typeof c.threshold === "boolean")) { add(node, name + " " + bool(c.value)); return node; }
  const thr = Array.isArray(c.threshold) ? c.threshold.join(" / ") : cfg(c.threshold);
  const failed = c.passed === false;
  if (c.op === "in" || c.op === "not_in") {
    const inside = (c.op === "in") !== failed;
    add(node, name + " " + v + (c.op === "in" && !failed ? "" : (inside ? " ∈ " : " ∉ ") + thr));
    return node;
  }
  const sym = OPS[c.op] ? OPS[c.op][failed ? 1 : 0] : c.op;
  add(node, name + " " + v + " " + sym + " " + thr);
  return node;
}

function ruleWhy(r) {
  const node = el("span");
  const conds = Array.isArray(r.conditions) ? r.conditions : [];
  if (r.outcome === "not_reached") return add(node, "not reached");
  if (r.outcome === "skipped_experiment") return add(node, "experimental · skipped");
  if (r.outcome === "hit") {
    if (!conds.length) return add(node, "always");
    conds.forEach((c, i) => add(node, i ? " · " : null, condText(c)));
  } else {
    const first = conds.find((c) => c.passed === false);
    add(node, first ? condText(first) : gap());
  }
  if (r.shift && typeof r.shift === "object") {
    const s = r.shift;
    add(node, " · shifted " + cfg(s.base) + " → " + cfg(s.effective) + (s.provider ? " by " + s.provider : ""));
  }
  return node;
}

// --- input -------------------------------------------------------------------

$("auth").addEventListener("submit", (event) => {
  event.preventDefault();
  S.token = $("token").value;
  $("token").value = "";
  S.listJson = "";
  S.overviewJson = "";
  S.dataJson = "";
  refresh("view");
});
$("disconnect").addEventListener("click", () => {
  stopTimer();
  S.gen += 1;
  if (S.controller) S.controller.abort();
  S.token = "";
  S.connected = false;
  S.list = null;
  S.listJson = "";
  S.overview = null;
  S.overviewJson = "";
  S.cfgHash = null;
  S.data = null;
  S.dataJson = "";
  S.toolbar = null;
  S.facetsJson = "";
  $("token").value = "";
  fail("");
  chrome();
  render("view");
});
$("pause").addEventListener("click", () => {
  S.paused = !S.paused;
  if (S.paused) stopTimer();
  chrome();
  if (!S.paused) refresh("data");
});
$("home").addEventListener("click", () => go("#overview"));
window.addEventListener("hashchange", onHash);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopTimer();
  else if (S.connected && !S.paused) refresh("data");
});

// Move through the list's rows with j and k; Enter opens one.
function stepRow(step) {
  const rows = [...document.querySelectorAll(".srow[href]")];
  if (!rows.length) return;
  const at = rows.indexOf(document.activeElement);
  const to = rows[at < 0 ? (step > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, at + step))];
  to.focus();
  to.scrollIntoView({block: "nearest"});
}

document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const target = event.target;
  if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable)) return;
  if (!S.connected) return;
  const key = event.key;
  if (key === "o") { go(overviewHash()); event.preventDefault(); return; }
  if (key === "s") { go(listHash()); event.preventDefault(); return; }
  if (key === "/") {
    event.preventDefault();
    if (S.view !== "sessions") go(listHash());
    // The toolbar exists once the list view has rendered.
    setTimeout(() => { const q = $("q"); if (q) q.focus(); }, S.view === "sessions" ? 0 : 60);
    return;
  }
  if (S.view === "sessions") {
    if (key === "j") { stepRow(1); event.preventDefault(); }
    else if (key === "k") { stepRow(-1); event.preventDefault(); }
    return;
  }
  if (S.view !== "session") return;
  inspectorKey(event);
});

onHash();
chrome();
refresh("view");
