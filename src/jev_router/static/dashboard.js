"use strict";

// Router inspector. Read-only. Builds every node with createElement and
// textContent; dynamic sizes go through element.style (CSSOM), never markup.

const LADDER = ["none", "low", "medium", "high", "xhigh", "max"];
const STAGES = ["Jev", "Constraints", "Rule & quota", "Binding", "Outcome"];
const REFRESH_MS = 5000;
const LIST_LIMIT = 200;
const WINDOWS = ["1h", "24h", "7d", "30d"];
const FILTER_KEYS = ["q", "client", "model", "rule", "state", "adaptive", "problem", "feedback", "pinned", "window"];
const STATES = ["active", "prepared", "blocked", "closed"];
const MODES = ["off", "shadow", "active"];
const PROBLEMS = {
  any: "Any problem", unknown: "Outcome unknown", rejected: "Rejected",
  stream_failed: "Stream failed", fallback: "Fallback admission", low_confidence: "Low-confidence gate",
};
const PROBLEM_SHORT = {unknown: "unknown", rejected: "rejected", stream_failed: "stream failed", fallback: "fallback", low_confidence: "low conf"};
// Feedback verdicts as the owner says them. too_weak: the model needed more.
const VERDICT_WORDS = {too_weak: "↑ more", too_strong: "↓ less", right: "✓ right", too_slow: "slow"};
const FEEDBACK_OPTIONS = [["too_weak", "↑ Needed more"], ["too_strong", "↓ Needed less"], ["right", "✓ Right"], ["too_slow", "Too slow"], ["unrated", "Unrated"]];
// Search accepts what the router's `q` filter accepts: up to 80 plain characters.
const SEARCH_OK = /^[\p{L}\p{N}_ .,:;!?'"()[\]/#@+=&*-]{1,80}$/u;

const S = {
  token: "",
  connected: false,
  paused: false,
  view: "overview",   // overview | sessions | session | rules
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
  // rules: the running config, and the draft the owner is editing
  rwin: "7d",
  rules: null,
  rulesJson: "",
  ruleset: "policy",
  draft: null,        // the edited text, or null while the editor follows the server
  draftBase: null,    // the config hash the draft was started from
  report: null,       // the last preview's body, or null
  edres: null,        // what the last check or apply said, shown under the editor
  confirming: false,  // the in-page "apply?" question is open
  busy: null,         // "check" | "preview" | "apply" while one is in flight
  hl: null,           // the editor line a ladder row pointed at
  editor: null,       // the editor panel, built once so a refresh never touches it
  rulesRoot: null,
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
function panel(title, note, cls) {
  const box = el("section", "panel" + (cls ? " " + cls : ""));
  const h = el("div", "ph");
  add(h, el("h2", "pt", title), note ? add(el("span", "note"), note) : null);
  box.append(h);
  return box;
}
// The window control: one link per window, the current one lit.
function windowSeg(current, hashOf) {
  const seg = el("div", "seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Window");
  for (const option of WINDOWS) {
    const a = link("segb" + (option === current ? " on" : ""), option, hashOf(option));
    if (option === current) a.setAttribute("aria-current", "true");
    seg.append(a);
  }
  return seg;
}
function verdictBadge(v) {
  if (!v || !VERDICT_WORDS[v]) return null;
  return el("span", "vbadge v-" + v, VERDICT_WORDS[v]);
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
// #overview[?w=7d]  ·  #sessions[?client=pi&problem=any…]  ·  #rules[?w=24h]  ·  #s=<id>  ·  #<slug>

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
  if (name === "rules") {
    const w = params.get("w");
    return {view: "rules", win: WINDOWS.includes(w) ? w : "7d"};
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
function rulesHash(win) { return "#rules" + ((win || S.rwin) === "7d" ? "" : "?w=" + (win || S.rwin)); }

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

// A user action that writes or replays. Same credential and timeout as api().
async function post(path, body) {
  const current = new AbortController();
  const timeout = setTimeout(() => current.abort(), 10000);
  S.controller = current;
  try {
    return await fetch(path, {
      method: "POST",
      headers: Object.assign({"Content-Type": "application/json"}, S.token ? {"X-Router-Admin-Token": S.token} : {}),
      cache: "no-store",
      body: JSON.stringify(body),
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
    } else if (S.view === "rules") {
      const response = await api("/router/config?window=" + encodeURIComponent(S.rwin));
      if (gen !== S.gen) return;
      if (response.status === 401) return unauthorized();
      if (!response.ok) throw new Error("HTTP " + response.status);
      const text = await response.text();
      if (gen !== S.gen) return;
      if (text !== S.rulesJson) {
        S.rulesJson = text;
        S.rules = JSON.parse(text);
        changed = true;
      }
      S.cfgHash = S.rules.config_hash;
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
  const tabs = {overview: $("v-overview"), sessions: $("v-sessions"), rules: $("v-rules")};
  tabs.overview.href = overviewHash();
  tabs.sessions.href = listHash();
  tabs.rules.href = rulesHash();
  const on = S.view === "session" ? "sessions" : S.view;
  for (const [name, a] of Object.entries(tabs)) {
    if (name === on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
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
  } else if (route.view === "rules") {
    if (route.win !== S.rwin) {
      S.rwin = route.win;
      S.rules = null;
      S.rulesJson = "";
    }
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
  } else if (S.view === "rules") {
    renderRules(kind);
  } else {
    main.replaceChildren();
    if (S.view === "overview") {
      if (S.overview) main.append(overviewView(kind));
    } else if (S.missing) {
      main.append(sessionNav(), el("p", "gaprow", S.slug !== null && !S.sid ? "no bookmark named " + S.slug : "session not found"));
    } else if (S.data) {
      const top = kind === "view" ? "" : " still";
      const low = kind === "data" ? " still" : "";
      const ctx = context();
      main.append(sessionNav(), hero(ctx, top), spine(ctx, top), detail(ctx, low));
    }
  }
  if (focused) {
    const again = document.querySelector('[data-fk="' + CSS.escape(focused) + '"]');
    if (again && again !== document.activeElement) again.focus({preventScroll: true});
  }
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
  search.placeholder = "Search";
  search.autocomplete = "off";
  search.spellcheck = false;
  search.maxLength = 80;
  search.setAttribute("aria-label", "Search titles and prompts");
  search.addEventListener("input", () => {
    clearTimeout(S.searchTimer);
    S.searchTimer = setTimeout(() => {
      const v = search.value.trim();
      // Text the router would refuse is never sent; the list stays as it is.
      if (!v || SEARCH_OK.test(v)) setFilter("q", v, true);
    }, 250);
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { search.blur(); event.stopPropagation(); }
    if (event.key === "Enter" || event.key === "ArrowDown") {
      const first = document.querySelector(".srow[href]");
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
    select("f-feedback", "Feedback", (v) => setFilter("feedback", v)),
    select("f-state", "State", (v) => setFilter("state", v)),
    select("f-adaptive", "Adaptive effort", (v) => setFilter("adaptive", v)),
    select("f-problem", "Problems", (v) => setFilter("problem", v)),
    select("f-window", "Last seen", (v) => setFilter("window", v)),
    pinned, clear, count);
  return bar;
}

// A toolbar control, attached to the page or not yet.
function tb(id) { return S.toolbar.querySelector("#" + id); }

const SELECTS = ["f-client", "f-model", "f-rule", "f-feedback", "f-state", "f-adaptive", "f-problem", "f-window"];

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
    // The verdicts the facet saw, in a fixed order, then any the link names.
    const seen = new Set((facets.feedback || []).map((x) => x.value));
    const verdicts = FEEDBACK_OPTIONS.filter(([v]) => seen.has(v) || !facets.feedback);
    fillSelect(tb("f-feedback"), [["", "Any feedback"]].concat(verdicts), f.feedback);
    fillSelect(tb("f-state"), [["", "Any state"]].concat(STATES.map((s) => [s, cap(s)])), f.state);
    fillSelect(tb("f-adaptive"), [["", "Any effort mode"]].concat(MODES.map((m) => [m, m === "off" ? "Adaptive off" : "Adaptive " + m])), f.adaptive);
    fillSelect(tb("f-problem"), [["", "Any outcome"]].concat(Object.entries(PROBLEMS)), f.problem);
    fillSelect(tb("f-window"), [["", "Any time"]].concat(WINDOWS.map((w) => [w, "Last " + w])), f.window);
  }
  for (const id of SELECTS) tb(id).classList.toggle("set", !!tb(id).value);
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

// [label, class]. Numbers right-aligned.
const COLUMNS = [["Last seen", "c0"], ["Session", "c1"], ["Client", "c2"], ["Workspace", "c3"], ["Turns", "c4 num"],
  ["Model · effort", "c5"], ["Rule", "c6"], ["Feedback", "c7"], ["Problems", "c8"]];

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
  for (const [label, cls] of COLUMNS) head.append(el("span", cls, label));
  root.append(head);
  rows.forEach((s, i) => root.append(sessionRow(s, i)));
  return root;
}

// The muted line under a title: what the prompt says beyond it. A title cut
// short with "…" already shows the prompt's start, so nothing repeats it.
function previewLine(title, preview) {
  if (!preview) return null;
  if (!title) return preview;
  const cut = title.endsWith("…");
  const base = cut ? title.slice(0, -1) : title;
  if (!preview.startsWith(base)) return preview;
  if (cut) return null;
  return preview.slice(base.length).trim() || null;
}

function sessionRow(s, i) {
  const row = link("srow", null, sessionHash(s.session_id));
  row.dataset.fk = "row-" + s.session_id;
  row.dataset.sid = s.session_id;
  row.style.animationDelay = Math.min(300, i * 12) + "ms";
  const when = el("span", "c0 mono");
  add(when, absent(s.last_seen) ? (absent(s.created) ? gap() : stamp(s.created)) : stamp(s.last_seen));

  // The session as a person knows it: what they asked, then where.
  const who = el("span", "c1");
  const title = el("span", "stitle");
  if (s.pinned) title.append(el("span", "pin", "◆"));
  title.append(s.title ? el("span", "stt", s.title) : el("span", "stt sid mono", short(s.session_id, s.client)));
  if (s.pinned) title.append(el("span", "plabel", s.pinned.label));
  if (s.tombstone) title.append(el("span", "faint", "pruned"));
  else if (s.state && s.state !== "active") title.append(el("span", "faint", s.state));
  who.append(title);
  const preview = previewLine(s.title, s.prompt_preview);
  if (preview) who.append(el("span", "sprev", preview));

  const client = add(el("span", "c2"), s.client ? s.client : gap("client"));
  const ws = el("span", "c3");
  if (s.workspace_hint) ws.append(el("span", "wchip mono", s.workspace_hint));
  const turns = el("span", "c4 num mono");
  add(turns, absent(s.turn_count) ? gap() : int(s.turn_count));

  // Model · effort. An adaptive session that moved says from where to where.
  const mdl = el("span", "c5 mono");
  const [prov, name] = splitModel(s.model);
  add(mdl, prov ? el("span", "faint", prov) : null, show(name));
  const now = s.effective_effort || s.effort;
  if (s.effort && now && now !== s.effort) {
    const up = LADDER.indexOf(now) > LADDER.indexOf(s.effort);
    add(mdl, el("span", "eff", " · " + s.effort + " → "), el("span", up ? "eff up" : "eff", now));
  } else if (now) add(mdl, el("span", "eff", " · " + now));
  const rl = add(el("span", "c6 mono"), show(s.rule));
  const fb = el("span", "c7");
  const badge = verdictBadge(s.feedback_verdict);
  if (badge) fb.append(badge);

  const counts = s.requests || {};
  const probs = el("span", "c8");
  for (const p of Array.isArray(s.problems) ? s.problems : []) {
    const k = p === "unknown" ? counts.unknown : p === "rejected" ? counts.rejected : p === "stream_failed" ? counts.stream_failed : null;
    probs.append(el("span", "prob", (k ? k + " " : "") + (PROBLEM_SHORT[p] || human(p))));
  }
  add(row, when, who, client, ws, turns, mdl, rl, fb, probs);
  return row;
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
    pager.append(el("span", "note", (at + 1) + " of " + list.length));
    const prev = at > 0 ? list[at - 1] : null;
    const next = at < list.length - 1 ? list[at + 1] : null;
    const p = prev ? link("pg", "Newer", sessionHash(prev.session_id), "Newer session (k)") : el("span", "pg off", "Newer");
    const n = next ? link("pg", "Older", sessionHash(next.session_id), "Older session (j)") : el("span", "pg off", "Older");
    p.dataset.fk = "pg-prev";
    n.dataset.fk = "pg-next";
    add(pager, p, n);
  } else if (S.list) {
    pager.append(el("span", "note", "not in this list"));
  }
  nav.append(pager);
  return nav;
}

function filterSummary() {
  const f = S.filters;
  const bits = [];
  if (f.q) bits.push("“" + f.q + "”");
  for (const k of ["client", "model", "rule", "state"]) if (f[k]) bits.push(f[k]);
  if (f.adaptive) bits.push("adaptive " + f.adaptive);
  if (f.problem) bits.push(PROBLEMS[f.problem] ? PROBLEMS[f.problem].toLowerCase() : f.problem);
  if (f.feedback) bits.push(VERDICT_WORDS[f.feedback] || human(f.feedback));
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

// Everything derived from the session payload, once per render.
function context() {
  const d = S.data;
  const session = d.session || {};
  const adm = d.admission || null;
  const reqs = requests().slice().sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const index = reqs.findIndex((r) => r.seq === S.sel);
  const req = index >= 0 ? reqs[index] : null;
  const trace = d.trace || {available: false, reason: null};
  const planList = plans().slice().sort((a, b) => (a.sequence || 0) - (b.sequence || 0));
  // The plan's own ladder when it recorded one; the generic order otherwise.
  const recorded = planList.map((p) => p.evidence && p.evidence.ladder).find((l) => Array.isArray(l) && l.length);
  const ladder = recorded ? recorded.slice() : LADDER.slice();
  const extend = (e) => { if (e && !ladder.includes(e)) ladder.push(e); };
  for (const p of planList) [p.from_effort, p.to_effort, p.recommendation, p.expected_effort, p.confirmed_effort].forEach(extend);
  [session.base_effort, session.expected_effort, session.confirmed_effort].forEach(extend);
  // Each request's turn. Recorded on its row since the router began logging
  // it; before that, inferred from log order (a plan's row precedes its turn).
  const turnOf = new Map();
  const byTurn = new Map(planList.filter((p) => p.turn_id).map((p) => [p.turn_id, p]));
  const rowOf = (p) => (p.judgment && !absent(p.judgment.id) ? p.judgment.id : null);
  const before = (p, r) => (rowOf(p) !== null && !absent(r.row) ? rowOf(p) < r.row : !absent(p.created) && !absent(r.created) && p.created <= r.created);
  reqs.forEach((r, i) => {
    if (r.turn_id && byTurn.has(r.turn_id)) { turnOf.set(i, {plan: byTurn.get(r.turn_id), inferred: false}); return; }
    const named = r.request_id ? planList.find((p) => p.request_id === r.request_id) : null;
    if (named) { turnOf.set(i, {plan: named, inferred: false}); return; }
    let owner = null;
    for (const p of planList) if (before(p, r)) owner = p;
    if (owner) turnOf.set(i, {plan: owner, inferred: true});
  });
  // The first request of each turn is where its plan sits on the lane.
  const planAt = new Map();
  const planIndex = new Map();
  for (const [i, t] of turnOf) {
    if (planIndex.has(t.plan)) continue;
    planIndex.set(t.plan, i);
    planAt.set(i, t.plan);
  }
  for (const [i, t] of turnOf) t.first = planIndex.get(t.plan) === i;
  const t0 = adm && !absent(adm.ts) ? adm.ts : session.created;
  return {session, adm, reqs, index, req, trace, ladder, planList, planAt, planIndex, turnOf, t0};
}

function turnNumber(ctx, plan) { return ctx.planList.indexOf(plan) + 1; }

function planMark(p) {
  if (p.mode !== "shadow" && p.status !== "rejected") {
    if (p.confirmed_effort) return {kind: "confirmed", effort: p.confirmed_effort};
    // A keep moves nothing, so the ledger fills no confirmed column for it; its
    // status says a provider completion carried the turn at the expected effort.
    if (p.action === "keep" && p.status === "confirmed" && p.expected_effort) return {kind: "confirmed", effort: p.expected_effort};
    if (p.expected_effort) return {kind: "expected", effort: p.expected_effort};
  }
  const r = p.recommendation || p.to_effort;
  return r ? {kind: "recommended", effort: r} : {kind: "norec", effort: null};
}

function glyph(kind) { return el("span", "glyph g-" + kind); }

function requestTokens(r) {
  const f = r.facts || {};
  return !absent(f.input_tokens) ? f.input_tokens : f.est_tokens;
}

function hero(ctx, still) {
  const {session, adm, reqs, req, t0} = ctx;
  const root = el("section", "hero" + still);
  const meta = el("div", "meta");
  const bits = [];
  if (session.client) bits.push(session.client);
  if (!absent(t0)) bits.push(day(t0), clock(t0).slice(0, 5) + " " + zone(t0));
  bits.push("session " + short(session.session_id, session.client));
  if (session.state && session.state !== "active") bits.push(session.state);
  meta.textContent = bits.join(" · ");
  const [prov, model] = splitModel(session.model);
  const h1 = el("h1", "model");
  const name = el("span");
  add(name, prov ? el("span", "prov", prov) : null, show(model));
  // A bound session with no base effort sends none: the model takes no setting.
  const pill = absent(session.base_effort) ? el("span", "pill soft", "no effort") : el("span", "pill", session.base_effort);
  add(h1, name, pill);
  const conf = session.confirmed_effort;
  const exp = session.expected_effort;
  if (conf && conf !== session.base_effort) add(h1, add(el("span", "pill soft"), glyph("confirmed"), conf));
  if (exp && exp !== conf && exp !== session.base_effort) add(h1, add(el("span", "pill soft"), glyph("expected"), exp));
  root.append(add(el("div", "htop"), h1, meta));

  const tl = el("div", "tl");
  if (ctx.planList.length) tl.append(...effortLane(ctx));
  const admb = button("admb" + (req ? "" : " on") + (adm ? "" : " none"), "", () => pick(null), "Admission");
  admb.dataset.fk = "adm";
  admb.append(el("i"));
  const ticks = el("div", "cols ticks");
  const maxIn = Math.max(1, ...reqs.map(requestTokens).filter((v) => !absent(v)));
  reqs.forEach((r, i) => {
    const t = button("tick" + statusClass(r) + (req === r ? " on" : ""), "", () => pick(r.seq), "Request " + (i + 1));
    t.dataset.fk = "tick-" + r.seq;
    const bar = el("i");
    const tokens = requestTokens(r);
    let h;
    if (r.status === "rejected") h = 16;
    else if (absent(tokens)) h = 40;
    else h = Math.round(18 + 78 * tokens / maxIn);
    bar.style.height = h + "%";
    bar.style.animationDelay = (120 + i * 16) + "ms";
    t.append(bar);
    ticks.append(t);
  });
  const caption = el("b");
  if (req) {
    const at = absent(req.created) ? null : req.created;
    caption.textContent = "Request " + (ctx.index + 1) + " of " + reqs.length + (at ? " · " + clock(at) + (absent(t0) ? "" : " · " + rel(at - t0)) : "");
  } else {
    add(caption, "Admission · ", absent(t0) ? gap() : clock(t0) + " " + zone(t0));
  }
  const tlr = el("div", "tlr");
  const count = el("span", null, reqs.length + (reqs.length === 1 ? " request" : " requests") + (reqs.length ? " · height is input size" : ""));
  add(tlr, count, ctx.planList.length ? laneLegend(ctx) : null, caption);
  add(tl, admb, ticks, el("div", "tlx", "Admission"), tlr);
  root.append(tl);
  return root;
}

function statusClass(r) {
  if (isUnknown(r.status) || r.status === "stream_failed") return " unknown";
  if (r.status === "rejected") return " rejected";
  if (r.status === "completed" && (absent(r.response_ms) || absent(requestTokens(r)))) return " norec";
  return "";
}

function effortLane(ctx) {
  const {session, reqs, planAt, ladder, req} = ctx;
  const used = [];
  const base = session.base_effort;
  if (base && ladder.includes(base)) used.push(ladder.indexOf(base));
  for (const p of planAt.values()) { const m = planMark(p); if (m.effort) used.push(ladder.indexOf(m.effort)); }
  let lo = Math.min(...used);
  let hi = Math.max(...used);
  if (!used.length) { lo = 0; hi = 1; }
  if (lo === hi) { if (hi < ladder.length - 1) hi += 1; else lo -= 1; }
  const y = (effort) => ((ladder.indexOf(effort) - lo) / (hi - lo)) * 100;
  const labels = el("div", "lanebox");
  const labelIn = el("div", "lanein rungs");
  const box = el("div", "lanebox");
  const inner = el("div", "lanein");
  for (let k = lo; k <= hi; k++) {
    const name = ladder[k];
    const line = el("i", "rl" + (name === base ? " base" : ""));
    line.style.bottom = y(name) + "%";
    inner.append(line);
    const label = el("span", name === base ? "base" : null, name);
    label.style.bottom = y(name) + "%";
    labelIn.append(label);
  }
  labels.append(labelIn);
  const cols = el("div", "cols lane");
  reqs.forEach((r, i) => {
    const slot = el("div", "slot");
    const p = planAt.get(i);
    if (p) {
      const m = planMark(p);
      const mk = el("i", "mkr g-" + m.kind + (req === r ? " on" : ""));
      mk.style.bottom = (m.effort ? y(m.effort) : 0) + "%";
      mk.style.animationDelay = (300 + i * 16) + "ms";
      mk.setAttribute("aria-hidden", "true");
      slot.append(mk);
    }
    cols.append(slot);
  });
  inner.append(cols);
  box.append(inner);
  return [labels, box];
}

const KIND_WORDS = {confirmed: "confirmed", expected: "sent, not confirmed", recommended: "recommended only"};

function laneLegend(ctx) {
  const kinds = new Set([...ctx.planAt.values()].map((p) => planMark(p).kind));
  const box = el("span", "legend");
  for (const kind of ["confirmed", "expected", "recommended"]) {
    if (kinds.has(kind)) add(box, add(el("span"), glyph(kind), KIND_WORDS[kind]));
  }
  return box;
}

function pick(seq) {
  if (S.sel === seq) return;
  S.sel = seq;
  render("pane");
  reveal();
}
function setStage(k) {
  if (S.stage === k) return;
  S.stage = k;
  render("pane");
  reveal();
}

// After a choice, keep the stage cards and the open pane on screen together.
function reveal() {
  const spineNode = document.querySelector(".spine");
  const detailNode = document.querySelector(".detail");
  if (!spineNode || !detailNode) return;
  const shown = window.innerHeight - detailNode.getBoundingClientRect().top;
  if (shown >= window.innerHeight * 0.4) return;
  const top = spineNode.getBoundingClientRect().top + window.scrollY - 16;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({top, behavior: still ? "auto" : "smooth"});
}

// --- stage cards -----------------------------------------------------------

function answers(adm) { return adm && adm.answers && typeof adm.answers === "object" ? adm.answers : {}; }
function firstOf(ans, type) { const e = Object.entries(ans).find(([, a]) => a && a.type === type); return e || null; }
function answerValue(a) { return a ? a[a.type] : null; }

function cardData(ctx) {
  const {session, adm, req, trace, reqs} = ctx;
  const ans = answers(adm);
  const choice = firstOf(ans, "choice");
  const numeric = Object.entries(ans).filter(([, a]) => a && (a.type === "score" || a.type === "noul") && !absent(a[a.type]));
  const jevSub = numeric.map(([k, a]) => human(k).split(" ")[0] + " " + num2(a[a.type])).join(" · ");
  const jev = !adm ? [gap(), null] : adm.fallback || session.decision_source === "fallback"
    ? ["fallback", "no Jev answers"]
    : choice ? [cap(answerValue(choice[1])), jevSub || null] : [Object.keys(ans).length ? jevSub : gap(), null];
  const rule = adm ? adm.rule : session.decision_rule;
  let ruleSub;
  if (trace.available) {
    const rules = Array.isArray(trace.rules) ? trace.rules : [];
    const hit = rules.find((r) => r.outcome === "hit");
    const gate = trace.gate || {};
    const quota = quotaMoved(adm, session) ? "quota moved it" : "quota no effect";
    ruleSub = (hit ? "rule " + hit.index + " of " + rules.length + " · " : gate.applies && !gate.passed ? "gate fired · " : "") + quota;
  } else ruleSub = gap("not replayed");
  const [, model] = splitModel(session.model);
  const status = {completed: 0, unknown: 0, rejected: 0, stream_failed: 0};
  for (const r of reqs) { const s = isUnknown(r.status) ? "unknown" : r.status; status[s] = (status[s] || 0) + 1; }
  const effortWord = absent(session.adaptation_mode) ? gap("effort mode not recorded")
    : session.adaptation_mode !== "off" ? "adaptive · " + session.adaptation_mode
    : absent(session.base_effort) ? "no effort" : "effort fixed";
  if (!req) {
    const facts = (adm && adm.facts) || {};
    const outSub = el("span");
    add(outSub, "completed · " + status.unknown + " unknown");
    if (status.rejected) add(outSub, " · " + status.rejected + " rejected");
    if (status.stream_failed) add(outSub, " · " + status.stream_failed + " stream failed");
    return [
      jev,
      [eligibility(adm), headroomText(facts)],
      [rule || gap(), ruleSub],
      [(model || "") + (session.base_effort ? " · " + session.base_effort : ""), add(el("span"), session.provider ? session.provider + " · " : null, effortWord)],
      [status.completed + " / " + reqs.length, outSub],
    ];
  }
  const f = req.facts || {};
  const turn = ctx.turnOf.get(ctx.index);
  const bindSub = el("span");
  add(bindSub, absent(session.base_effort) ? "no effort" : "base " + session.base_effort);
  if (turn) { const m = planMark(turn.plan); add(bindSub, " · ", glyph(m.kind), " ", m.effort || gap()); }
  const outV = !absent(req.upstream_status) ? String(req.upstream_status) : human(req.status || "unknown");
  const outS = el("span");
  add(outS, human(req.status || ""), !absent(req.response_ms) ? " · " + secs(req.response_ms) : null);
  let jevCard = [jev[0], "from admission"];
  if (turn) {
    const j = turn.plan.judgment;
    jevCard = ["Turn " + turnNumber(ctx, turn.plan), turn.first ? (j ? numbersOf(answers(j)) || "judged" : gap("answers not recorded")) : "from turn start"];
  }
  return [
    jevCard,
    [absent(f.headroom) ? gap() : f.headroom >= 0 ? "Within budget" : "Over budget", headroomText(f)],
    [rule || gap(), "from admission"],
    [model || gap(), bindSub],
    [outV, outS],
  ];
}

function numbersOf(ans) {
  return Object.entries(ans).filter(([, a]) => a && (a.type === "score" || a.type === "noul") && !absent(a[a.type]))
    .map(([k, a]) => human(k).split(" ")[0] + " " + num2(a[a.type])).join(" · ");
}

// What eligibility removed before any rule ran. NULL on a row with a packet
// version means the list was empty; without one it was never recorded.
function eligibility(adm) {
  if (!adm) return gap();
  const ex = adm.exclusions;
  if (Array.isArray(ex) && ex.length) return ex.length + " excluded";
  if (absent(ex) && absent(adm.packet_version)) return gap("exclusions not recorded");
  return "All eligible";
}

function headroomText(f) {
  if (absent(f.headroom)) return gap("headroom not recorded");
  return int(f.headroom) + " headroom";
}

// The counterfactual is the zero-pressure choice, stored on every row as
// "model(effort or -)". It is a change only when it differs from the choice.
function chosen(adm) { return (adm.model || "?") + "(" + (adm.effort || "-") + ")"; }
function counterfactualDiffers(adm) { return !!adm.counterfactual && adm.counterfactual !== chosen(adm); }
function quotaMoved(adm, session) {
  if (!adm) return false;
  return (Array.isArray(adm.shifted) && adm.shifted.length > 0) || adm.reordered === true || counterfactualDiffers(adm) || session.quota_changed_choice === true;
}

function spine(ctx, still) {
  const root = el("section", "spine" + still);
  root.setAttribute("aria-label", "Decision stages");
  root.append(el("div", "rail"));
  const data = cardData(ctx);
  data.forEach((c, i) => {
    const k = i + 1;
    const turn = ctx.req ? ctx.turnOf.get(ctx.index) : null;
    const inherited = ctx.req && ((k === 1 && !(turn && turn.first)) || k === 3);
    const card = button("card" + (S.stage === k ? " sel" : "") + (inherited ? " inh" : ""), "", () => setStage(k));
    card.dataset.fk = "card-" + k;
    card.setAttribute("aria-pressed", S.stage === k ? "true" : "false");
    card.style.animationDelay = (200 + i * 70) + "ms";
    const v = el("span", "cv");
    add(v, c[0]);
    const s = el("span", "cs");
    add(s, c[1]);
    add(card, el("span", "cn", STAGES[i]), v, s);
    root.append(card);
  });
  return root;
}

// --- panes -----------------------------------------------------------------

function detail(ctx, still) {
  const root = el("section", "detail");
  root.setAttribute("aria-live", "polite");
  const pane = el("div", "pane" + still);
  pane.dataset.stage = String(S.stage);
  const builders = [jevPane, constraintsPane, rulePane, bindingPane, outcomePane];
  builders[S.stage - 1](pane, ctx);
  root.append(pane);
  return root;
}

function head(pane, title, note) {
  const dh = el("div", "dh");
  const n = el("span", "note");
  add(n, note);
  add(dh, el("h2", "dt", title), n);
  pane.append(dh);
}

function kv(a, b, cls) {
  const row = el("div", "kv");
  const right = el("span", "b" + (cls ? " " + cls : ""));
  add(right, b);
  add(row, typeof a === "string" ? el("span", "a", a) : add(el("span", "a"), a), right);
  return row;
}

function inherited(pane, title, line, values, open) {
  const box = el("div", "inherit");
  add(box, el("p", "big2", title), el("p", "note", line));
  const row = el("div", "inhvals");
  for (const [label, value] of values) row.append(add(el("div", "inhv"), el("div", "qn", label), add(el("div", "qv"), value)));
  box.append(row);
  const [text, seq] = open || ["Open admission", null];
  const b = button("link", text, () => pick(seq));
  b.dataset.fk = "open-src";
  box.append(b);
  pane.append(box);
}

const STATUS_WORDS = {planned: "planned", accepted: "accepted, not confirmed", confirmed: "confirmed", rejected: "rejected", outcome_unknown: "outcome unknown"};

// What a plan did, in words that keep a recommendation apart from a change.
function planWords(p) {
  const m = planMark(p);
  const from = p.from_effort || "?";
  const status = STATUS_WORDS[p.status] || human(p.status || "");
  if (p.mode === "shadow") {
    if (m.effort && m.effort !== from) return {effort: "recommended " + m.effort, status: "shadow · not applied"};
    return {effort: "keep " + from, status: "shadow"};
  }
  if (p.action === "change_effort") return {effort: from + " → " + (p.to_effort || "?"), status};
  if (p.action === "blocked") return {effort: "blocked at " + from, status};
  if (m.effort && m.effort !== from) return {effort: "keep " + from, status: "recommended " + m.effort};
  return {effort: "keep " + from, status: p.status === "confirmed" ? "" : status};
}

function turnPane(pane, ctx, turn) {
  const p = turn.plan;
  const n = turnNumber(ctx, p);
  const note = el("span");
  if (turn.inferred) add(note, gap("turn not on the request row · matched by log order"));
  if (!turn.first) {
    const k = ctx.planIndex.get(p);
    const j = p.judgment ? answers(p.judgment) : {};
    const value = (q) => (j[q] ? show(answerValue(j[q]), num2) : gap());
    head(pane, "Jev", note);
    return inherited(pane, "No Jev call on this request", "Judged at the start of turn " + n + ", before request " + (k + 1) + ".", [
      ["Difficulty", value("difficulty")],
      ["Harm if wrong", value("harm_if_wrong")],
    ], ["Open request " + (k + 1), ctx.reqs[k].seq]);
  }
  head(pane, "Jev · turn " + n, note);
  if (!p.judgment) pane.append(el("p", "gaprow", "this turn's answers not recorded"));
  else jevBody(pane, p.judgment, ctx, false);
  const w = planWords(p);
  const line = el("div", "planline");
  add(line, glyph(planMark(p).kind), add(el("span", "pe"), w.effort), w.status ? el("span", "ps", w.status) : null);
  pane.append(line);
  if (p.reason) pane.append(el("p", "reason", p.reason));
  if (p.evidence && p.evidence.error) pane.append(el("p", "gaprow", "Jev failed on this turn · the current effort was kept"));
}

function jevPane(pane, ctx) {
  const {adm} = ctx;
  if (ctx.req) {
    const turn = ctx.turnOf.get(ctx.index);
    if (turn) return turnPane(pane, ctx, turn);
    const ans = answers(adm);
    const choice = firstOf(ans, "choice");
    const score = firstOf(ans, "score");
    return inherited(pane, "No Jev call on this request", "Judged once, at admission.", [
      [choice ? cap(choice[0]) : "Task", choice ? show(answerValue(choice[1]), cap) : gap()],
      [score ? cap(score[0]) : "Difficulty", score ? show(answerValue(score[1]), num2) : gap()],
    ]);
  }
  head(pane, "Jev", null);
  if (!adm) { pane.append(el("p", "gaprow", "admission not recorded")); return; }
  jevBody(pane, adm, ctx, true);
}

// One recorded Jev call: what it was shown, as shape only, and what it said.
function jevBody(pane, row, ctx, admission) {
  const {session} = ctx;
  const cols = el("div", "cols2");
  const left = el("div");
  left.append(el("div", "qn", "Shown to Jev"));
  const shape = row.state_shape;
  if (!shape || typeof shape !== "object") {
    left.append(el("div", "gaprow", "not recorded for this decision"));
    const read = admission ? readByRules(ctx.trace) : [];
    if (read.length) {
      left.append(add(el("div", "qn sub"), "Read by the rules"));
      const kvs = el("div", "kvs");
      for (const [key, value] of read) kvs.append(kv(key, plain(value)));
      left.append(kvs);
    }
  } else {
    left.append(add(el("div", "builder"), show(shape.builder)));
    const parts = el("div", "kvs");
    for (const [name, chars] of Object.entries(shape.parts || {})) {
      parts.append(kv(human(name), absent(chars) ? gap() : int(chars) + " chars · text not stored"));
    }
    left.append(parts);
    const facts = el("div", "kvs sub");
    for (const [name, value] of Object.entries(shape.facts || {})) {
      if (name === "tool_names") facts.append(kv("tool names", absent(value) ? gap() : int(value) + " · names withheld"));
      else facts.append(kv(human(name), show(value, plain)));
    }
    left.append(facts);
  }
  // Right: active answers, shadow answers, call metadata.
  const right = el("div");
  const ans = answers(row);
  if (!Object.keys(ans).length) {
    const fellBack = admission && (row.fallback || session.decision_source === "fallback");
    right.append(el("div", "gaprow", fellBack ? "fallback admission · no answers" : "answers not recorded"));
  } else {
    const grid = el("div", "answers");
    for (const [name, a] of Object.entries(ans)) grid.append(answerBlock(name, a));
    right.append(grid);
  }
  const sh = el("div", "shadowbox");
  const shadow = row.shadow_answers;
  if (!shadow || typeof shadow !== "object" || !Object.keys(shadow).length) {
    add(sh, add(el("div", "sh"), el("span", null, "Shadow"), el("span", null, "none asked")));
  } else {
    sh.append(add(el("div", "sh"), el("span", null, "Shadow"), el("span", "mono", row.shadow_packet_version || "")));
    for (const [name, a] of Object.entries(shadow)) sh.append(kv(human(name), show(answerText(a))));
  }
  right.append(sh);
  const jm = el("div", "jmeta");
  add(jm, show(row.jev_ms, (v) => Math.round(v) + " ms"), " · ", show(row.jev_tokens, (v) => int(v) + " tokens"), " · ", show(session.jev_model));
  right.append(jm);
  add(cols, left, right);
  pane.append(cols);
}

// Feature values the replayed rules compared: recorded on the row, not guessed.
function readByRules(trace) {
  if (!trace || !trace.available) return [];
  const seen = new Map();
  for (const r of Array.isArray(trace.rules) ? trace.rules : []) {
    for (const c of Array.isArray(r.conditions) ? r.conditions : []) {
      if (c.source === "feature" && !absent(c.value) && !seen.has(c.key)) seen.set(c.key, c.value);
    }
  }
  return [...seen.entries()];
}

function answerText(a) {
  if (!a || typeof a !== "object") return null;
  const v = a[a.type];
  if (absent(v)) return null;
  if (a.type === "choice") return human(v) + (absent(a.confidence) ? "" : " · " + num2(a.confidence));
  return num2(v) + (absent(a.confidence) ? "" : " · confidence " + num2(a.confidence));
}

function answerBlock(name, a) {
  const box = el("div");
  box.append(el("div", "qn", cap(name)));
  if (!a || typeof a !== "object") { box.append(add(el("div", "qv"), gap())); return box; }
  const value = a[a.type];
  const probs = a.probabilities && typeof a.probabilities === "object" ? a.probabilities : null;
  if (a.type === "choice") {
    box.append(add(el("div", "qv"), absent(value) ? gap() : cap(value)));
    if (probs) {
      Object.entries(probs).filter(([, p]) => p >= 0.01).sort((x, y) => y[1] - x[1]).slice(0, 4).forEach(([label, p]) => {
        const row = el("div", "row");
        row.append(add(el("div", "rowh"), el("span", null, human(label)), el("span", "mono", Math.round(p * 100) + "%")));
        const fill = el("div", "fill");
        fill.style.width = Math.max(0.6, p * 100) + "%";
        row.append(add(el("div", "trk"), fill));
        box.append(row);
      });
    } else box.append(gap("distribution not recorded"));
    box.append(add(el("div", "qm"), "confidence ", show(a.confidence, num2)));
  } else if (a.type === "score") {
    box.append(add(el("div", "qv"), show(value, num2)));
    const levels = probs ? Object.keys(probs).map(Number).filter(Number.isFinite).sort((x, y) => x - y) : [];
    if (levels.length >= 2) {
      const top = levels[levels.length - 1];
      const scale = el("div", "scale");
      for (const l of levels) {
        const bar = el("div", "lv");
        bar.style.left = (l / top * 100) + "%";
        bar.style.height = Math.max(2, probs[String(l)] * 100) + "%";
        scale.append(bar);
      }
      if (!absent(value)) {
        const mk = el("div", "mk");
        mk.style.left = Math.min(100, value / top * 100) + "%";
        scale.append(mk);
      }
      const ticks = el("div", "ticksx");
      for (const l of levels) {
        const t = el("span", null, l);
        t.style.left = (l / top * 100) + "%";
        ticks.append(t);
      }
      box.append(scale, ticks);
    } else box.append(gap("distribution not recorded"));
    box.append(add(el("div", "qm"), "confidence ", show(a.confidence, num2)));
  } else if (a.type === "noul") {
    box.append(add(el("div", "qv"), show(value, num2)));
    if (!absent(value)) {
      const fill = el("div", "fill");
      fill.style.width = Math.max(0.6, value * 100) + "%";
      box.append(add(el("div", "trk"), fill), add(el("div", "rowh"), el("span", null, "0"), el("span", null, "1")));
    }
  } else {
    box.append(add(el("div", "qv"), show(answerText(a))));
  }
  return box;
}

function constraintsPane(pane, ctx) {
  const {session, adm, req} = ctx;
  const f = req ? (req.facts || {}) : ((adm && adm.facts) || {});
  const window = !absent(f.context_window) ? f.context_window : session.context_window;
  const input = !absent(f.input_tokens) ? f.input_tokens : f.est_tokens;
  const output = !absent(f.max_tokens) ? f.max_tokens : session.max_output_tokens;
  const reserve = f.reserve_tokens;
  const headroom = f.headroom;
  head(pane, req ? "Request " + (ctx.index + 1) : "Constraints", absent(window) ? gap("window not recorded") : el("span", "mono", int(window) + " token window"));
  if (!req && !adm) { pane.append(el("p", "gaprow", "admission not recorded")); return; }
  const bar = el("div", "budget");
  const seg = (cls, v) => {
    if (absent(v) || absent(window) || window <= 0 || v <= 0) return;
    const i = el("i", cls);
    i.style.width = Math.min(100, v / window * 100) + "%";
    bar.append(i);
  };
  seg("c-in", input);
  seg("c-out", output);
  seg("c-res", reserve);
  const legend = el("div", "bl");
  const item = (cls, label, v) => {
    const d = el("div");
    add(d, el("span", "sw " + cls), label, add(el("b"), show(v, int)));
    legend.append(d);
  };
  item("c-in", "Input", input);
  item("c-out", "Output ceiling", output);
  item("c-res", "Reserve", reserve);
  item("c-head", "Headroom", headroom);
  pane.append(bar, legend);
  const checks = el("div", "checks");
  const check = (k, v, small) => checks.append(add(el("div", "ck"), el("div", "k", k), add(el("div", "v" + (small ? " small" : "")), v)));
  const tools = f.has_tools === true
    ? add(el("span"), show(f.tool_count, int), " offered · ", session.supports_tools === false ? "not supported" : absent(session.supports_tools) ? gap() : "supported")
    : f.has_tools === false ? "none" : gap();
  check("Tools", tools);
  const images = f.has_images === true
    ? (session.supports_vision ? "present · vision" : session.supports_vision === false ? "present · no vision" : gap())
    : f.has_images === false ? (session.supports_vision ? "none · vision available" : "none") : gap();
  check("Images", images);
  check("Context", absent(headroom) ? gap() : headroom >= 0 ? "fits" : "over by " + int(-headroom));
  if (!req) {
    const ex = adm.exclusions;
    let exNode;
    if (absent(ex)) exNode = absent(adm.packet_version) ? gap() : "none";
    else if (!Array.isArray(ex) || !ex.length) exNode = "none";
    else {
      exNode = el("div");
      for (const e of ex) exNode.append(add(el("div"), (e && e.model) || "?", e && e.reason ? " · " + e.reason : ""));
    }
    check("Excluded", exNode, Array.isArray(ex) && ex.length > 0);
  } else {
    check("Output asked", show(f.max_tokens, int));
  }
  pane.append(checks);
}

// The lane the matched rule names, and the evidence that the pair is adequate.
function laneRow(adm, session) {
  const lane = session.quality_lane || adm.quality_lane;
  const row = el("div", "lanerow");
  const laneNode = add(el("div"), el("div", "qn", "Quality lane"), add(el("div", "ln"), lane || "none"));
  const evid = adm.evidence;
  const evNode = add(el("div"), el("div", "qn", "Evidence"), add(el("div", "ev " + (evid === "weak" ? "gap" : "ok")), lane ? show(evid) : (evid || "none")));
  const refNode = add(el("div", "ref"), el("div", "qn", "Qualification"), add(el("div", "mono"), lane ? show(adm.qualification_ref) : (adm.qualification_ref || "none")));
  return add(row, laneNode, evNode, refNode);
}

// --- rule and quota ----------------------------------------------------------

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

function rulePane(pane, ctx) {
  const {adm, session, trace} = ctx;
  if (ctx.req) {
    return inherited(pane, "No rule ran on this request", "A strict binding skips the decider and ignores quota.", [
      ["Rule", show((adm && adm.rule) || (trace.stored && trace.stored.rule) || session.decision_rule)],
      ["Route", show((adm && adm.route) || trace.route)],
    ]);
  }
  const note = el("span");
  const stored = trace.stored || {};
  const recordedRule = (adm && adm.rule) || stored.rule || session.decision_rule;
  if (trace.available) {
    add(note, el("span", "ok", "Replayed under " + hash8(trace.config_hash) + (trace.source === "snapshot" ? " snapshot" : "")));
    if (trace.reproduces) add(note, " · reproduces " + (trace.matched || recordedRule));
    else {
      const rep = trace.replayed || {};
      add(note, " · ", el("span", "gap", "differs · stored " + [stored.model, stored.effort, stored.rule].filter(Boolean).join(" ") + " · replayed " + [rep.model, rep.effort, rep.rule].filter(Boolean).join(" ")));
    }
  } else {
    add(note, gap(reasonText(trace.reason)));
  }
  head(pane, "Rule & quota", note);
  if (!adm) { pane.append(el("p", "gaprow", "admission not recorded")); return; }
  const cols = el("div", "cols2 even");
  const left = el("div");
  const list = el("div", "rules");
  let delay = 60;
  const line = (i, name, why, cls) => {
    const row = el("div", "r " + cls);
    row.style.animationDelay = delay + "ms";
    delay += 55;
    add(row, el("span", "i", i), el("span", "nm", name), add(el("span", "why"), why));
    list.append(row);
  };
  if (trace.available) {
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
    left.append(list);
    const res = el("div", "res");
    add(res, el("span", "note", "route "), el("span", "mono", trace.route || stored.route || "direct"));
    left.append(res);
  } else {
    left.append(el("div", "qn", "Recorded"));
    const kvs = el("div", "kvs");
    kvs.append(kv("rule", show(recordedRule)), kv("route", show(adm.route)));
    left.append(kvs);
  }
  left.append(laneRow(adm, session));
  const right = el("div");
  if (trace.available) {
    const thr = thresholdBlock(trace, adm);
    if (thr) right.append(thr);
    right.append(gateBlock(trace.gate || {}));
  }
  right.append(quotaBlock(adm, session));
  add(cols, left, right);
  pane.append(cols);
}

function reasonText(reason) {
  if (!reason) return "not replayed";
  if (reason === "not_on_file") return "not replayed · config not on file";
  if (reason === "no_answers") return "not replayed · no answers";
  if (reason === "fallback_admission") return "not replayed · fallback admission";
  if (reason === "no_admission_row") return "not replayed · no admission row";
  return "not replayed · " + human(reason);
}

// A threshold is shown only where its question decided the rule: the hit, or
// a miss whose first failed condition it is while every other one passed.
function decidingConditions(r) {
  const conds = Array.isArray(r.conditions) ? r.conditions : [];
  if (r.outcome === "hit") return conds;
  if (r.outcome !== "miss") return [];
  const first = conds.find((c) => c.passed === false);
  if (!first || !conds.every((c) => c === first || c.passed === true)) return [];
  return [first];
}

function thresholdBlock(trace, adm) {
  const byKey = new Map();
  for (const r of trace.rules || []) {
    for (const c of decidingConditions(r)) {
      if (c.source !== "answer" || !["gte", "gt", "lte", "lt"].includes(c.op) || typeof c.threshold !== "number") continue;
      if (!byKey.has(c.key)) byKey.set(c.key, []);
      byKey.get(c.key).push({value: c.threshold, rule: r.name, hit: r.outcome === "hit"});
    }
  }
  if (!byKey.size) return null;
  const [key, list] = [...byKey.entries()].sort((a, b) => b[1].length - a[1].length)[0];
  const answer = answers(adm)[key];
  const value = answer ? answer[answer.type] : null;
  const probs = answer && answer.probabilities ? Object.keys(answer.probabilities).map(Number).filter(Number.isFinite) : [];
  const merged = new Map();
  for (const t of list) if (!merged.has(t.value) || t.hit) merged.set(t.value, t.rule);
  const points = [...merged.entries()].sort((a, b) => a[0] - b[0]);
  const top = probs.length ? Math.max(...probs) : Math.ceil(Math.max(...points.map((p) => p[0]), absent(value) ? 0 : value));
  const x = (v) => Math.max(0, Math.min(100, v / (top || 1) * 100));
  const blk = el("div", "blk");
  blk.append(el("div", "qn", cap(key)));
  const scale = el("div", "scale short");
  const ticks = el("div", "ticksx");
  // Stagger: a label drops a row when it would sit too close to the last one there.
  const lastX = [];
  points.forEach(([v, rule]) => {
    const mk = el("div", "mk thr");
    mk.style.left = x(v) + "%";
    scale.append(mk);
    let row = lastX.findIndex((p) => x(v) - p >= 24);
    if (row < 0) row = lastX.length;
    lastX[row] = x(v);
    const t = el("span", x(v) >= 85 ? "hi" : x(v) <= 12 ? "lo" : null, cfg(v) + " " + rule);
    t.style.left = x(v) + "%";
    t.style.top = (0.45 + row * 1.25) + "rem";
    ticks.append(t);
  });
  ticks.style.height = (0.6 + lastX.length * 1.25) + "rem";
  if (!absent(value)) {
    const mk = el("div", "mk val");
    mk.style.left = x(value) + "%";
    scale.append(mk);
  }
  blk.append(scale, ticks);
  const res = el("div", "res");
  if (absent(value)) add(res, gap());
  else {
    const near = points.reduce((best, p) => (Math.abs(p[0] - value) < Math.abs(best[0] - value) ? p : best), points[0]);
    const d = value - near[0];
    add(res, el("span", "ok mono", num2(value)), " ", el("span", "note", (d === 0 ? "at " : num2(Math.abs(d)) + (d < 0 ? " below " : " above ")) + near[1]));
  }
  blk.append(res);
  return blk;
}

function gateBlock(gate) {
  const blk = el("div", "blk");
  if (!gate.applies) {
    blk.append(el("div", "qn", "Confidence gate"), kv("gate", "none"));
    return blk;
  }
  const qn = el("div", "qn");
  add(qn, "Confidence gate · ", show(gate.min_confidence, num2));
  blk.append(qn);
  const kvs = el("div", "kvs");
  for (const [q, c] of Object.entries(gate.questions || {})) {
    const below = !absent(c) && !absent(gate.min_confidence) && c < gate.min_confidence;
    kvs.append(kv(human(q), absent(c) ? gap() : num2(c) + (below ? " < " + num2(gate.min_confidence) : ""), below ? null : "ok"));
  }
  blk.append(kvs);
  return blk;
}

function quotaBlock(adm, session) {
  const blk = el("div", "blk");
  blk.append(el("div", "qn", "Quota at admission"));
  const snap = adm.quota_snapshot;
  const kvs = el("div", "kvs");
  if (!snap || typeof snap !== "object" || !Object.keys(snap).length) {
    kvs.append(kv("snapshot", gap()));
  } else {
    for (const [provider, q] of Object.entries(snap)) {
      const status = q && q.status;
      const label = el("span");
      add(label, provider);
      if (status === "fresh") {
        kvs.append(kv(label, add(el("span"), "pressure ", show(q.pressure, pressureText))));
        // Each window beside its own numbers; never one window's usage with another's reset.
        for (const w of Array.isArray(q.windows) ? q.windows : []) {
          const name = [w.name, absent(w.window_minutes) ? null : windowSpan(w.window_minutes)].filter(Boolean).join(" · ");
          kvs.append(kv(add(el("span", "wn"), name || "window"), absent(w.used_percent) ? gap("usage not reported") : num1(w.used_percent) + "% used"));
        }
      } else {
        kvs.append(kv(label, (status || "unknown") === "unknown" ? "unreadable · counted as 0" : status + " · unreadable · counted as 0", "gap"));
      }
    }
  }
  blk.append(kvs);
  const res = el("div", "res");
  const lines = [];
  if (Array.isArray(adm.shifted)) for (const s of adm.shifted) lines.push("moved " + human(s.rule || "?") + " " + human(s.question || "") + " " + cfg(s.base) + " → " + cfg(s.effective) + (s.provider ? " · " + s.provider : ""));
  if (adm.reordered === true) lines.push("route reordered");
  if (counterfactualDiffers(adm)) lines.push("without quota " + adm.counterfactual + " · chosen " + chosen(adm));
  if (lines.length) {
    for (const l of lines) res.append(add(el("div"), l));
  } else if (session.quota_changed_choice === true) {
    res.append(gap("quota changed the choice · detail not recorded"));
  } else {
    res.classList.add("ok");
    res.textContent = "No threshold moved · same choice without quota";
  }
  blk.append(res);
  return blk;
}

// --- binding -------------------------------------------------------------------

function bindingPane(pane, ctx) {
  const {session, adm, req, trace, planList, planIndex, ladder} = ctx;
  const note = el("span", "mono");
  // The version is the binding's now; a request row does not record the one it ran under.
  add(note, "decision ", show(session.decision_id, (v) => String(v).slice(0, 12)), req ? null : " · binding v", req ? null : show(session.version));
  head(pane, req ? "Request " + (ctx.index + 1) : "Binding", note);
  const cols = el("div", "cols2 even");
  const facts = el("div", "facts");
  const fact = (v, l) => facts.append(add(el("div"), add(el("div", "fv"), v), el("div", "fl", l)));
  fact(show(session.provider), "provider");
  fact(show(session.protocol), "protocol");
  fact(show(session.context_window, int), "context window");
  fact(show(session.max_output_tokens, int), "output ceiling");
  // A rule may name a model directly; a trace that ran says so with a null route.
  const route = (trace && trace.route) || (adm && adm.route);
  fact(route ? show(route) : trace && trace.available ? "direct" : show(null), "route");
  // NULL means the session never compacted (sessions.COMPACTION_COLUMNS).
  fact(absent(session.compaction_epoch) ? "none" : String(session.compaction_epoch), "compaction epoch");
  const right = el("div");
  right.append(el("div", "qn", "Effort"));
  const base = session.base_effort;
  const turn = req ? ctx.turnOf.get(ctx.index) : null;
  // Shadow and fixed sessions send the base effort on every request by construction.
  const sendsBase = absent(session.adaptation_mode) ? false : session.adaptation_mode !== "active";
  const marks = new Map();
  if (!planList.length || (req && sendsBase)) {
    if (base) marks.set(base, "confirmed");
  }
  if (turn) {
    const m = planMark(turn.plan);
    if (m.effort && !(m.kind === "recommended" && marks.has(m.effort))) marks.set(m.effort, m.kind);
  } else if (!req && planList.length) {
    if (session.expected_effort && session.expected_effort !== session.confirmed_effort) marks.set(session.expected_effort, "expected");
    if (session.confirmed_effort) marks.set(session.confirmed_effort, "confirmed");
  }
  if (absent(base)) {
    right.append(add(el("div", "noeffort"), "No effort sent", el("span", "note", "this model takes no effort setting")));
  } else {
    const ladderNode = el("div", "ladder");
    for (const name of ladder) {
      const rung = el("div", "rung" + (name === base ? " base" : "") + (marks.has(name) ? " lit" : ""));
      add(rung, el("i", marks.has(name) ? "g-" + marks.get(name) : null), el("span", null, name));
      ladderNode.append(rung);
    }
    right.append(ladderNode);
  }
  if (!planList.length) {
    const box = el("div", "shadowbox");
    box.append(add(el("div", "sh"), el("span", null, "Plans"), add(el("span"), absent(session.adaptation_mode) ? gap("effort mode not recorded") : session.adaptation_mode !== "off" ? "none yet · adaptive " + session.adaptation_mode : "adaptive effort off")));
    right.append(box);
  } else {
    const list = el("div", "plans");
    const inferred = [...ctx.turnOf.values()].some((t) => t.inferred);
    for (const p of planList) {
      const i = planIndex.has(p) ? planIndex.get(p) : null;
      const w = planWords(p);
      const on = turn && turn.plan === p;
      const row = el("div", "plan" + (on ? " on" : ""));
      const pn = el("span", "pn" + (inferred && i !== null ? " inf" : ""), i === null ? "–" : "#" + (i + 1));
      add(row, pn, glyph(planMark(p).kind), el("span", "pe", w.effort), el("span", "ps", w.status));
      list.append(row);
    }
    right.append(list);
    if (inferred) right.append(add(el("p", "fine"), gap("turns matched to requests by log order")));
  }
  add(cols, add(el("div"), facts), right);
  pane.append(cols);
}

// --- outcome -------------------------------------------------------------------

function outcomePane(pane, ctx) {
  const {reqs, req, t0, session} = ctx;
  const last = reqs.length ? reqs[reqs.length - 1] : null;
  const lastAt = last ? (last.updated || last.created) : null;
  const note = req
    ? (absent(req.created) ? gap() : clock(req.created) + " " + zone(req.created))
    : (absent(t0) ? gap() : clock(t0) + (lastAt ? " to " + clock(lastAt) : "") + " " + zone(t0));
  head(pane, req ? "Request " + (ctx.index + 1) : "Every request on this binding", note);
  const stats = el("div", "stats");
  const stat = (v, l) => stats.append(add(el("div"), add(el("div", "fv"), v), el("div", "fl", l)));
  if (!req) {
    const median = (key) => {
      const v = reqs.map((r) => r[key]).filter((x) => !absent(x)).sort((a, b) => a - b);
      return v.length ? secs(v[Math.ceil(v.length / 2) - 1]) : gap();
    };
    stat(String(reqs.filter((r) => r.status === "completed").length), "completed");
    stat(String(reqs.filter((r) => isUnknown(r.status)).length), "outcome unknown");
    stat(median("first_byte_ms"), "p50 first byte");
    stat(median("response_ms"), "p50 total");
    stat(absent(t0) || absent(lastAt) ? gap() : span(lastAt - t0), "admission to last reply");
  } else {
    stat(show(req.upstream_status), "HTTP status");
    stat(show(req.stream_state || (req.row === null ? null : req.status), human), "stream");
    stat(show(req.first_byte_ms, secs), "first byte");
    stat(show(req.response_ms, secs), "total");
    stat(show(req.response_bytes, size), "streamed");
  }
  pane.append(stats);
  const chart = el("div", "chart");
  const times = reqs.map((r) => r.response_ms).filter((v) => !absent(v));
  const maxRt = Math.max(1, ...times);
  reqs.forEach((r, i) => {
    const b = button("cb" + statusClass(r) + (req === r ? " on" : ""), "", () => pick(r.seq), "Request " + (i + 1) + (absent(r.response_ms) ? "" : ", " + secs(r.response_ms)));
    b.dataset.fk = "bar-" + r.seq;
    const top = el("i");
    const low = el("em");
    const cls = statusClass(r);
    if (cls === " rejected") top.style.height = "10%";
    else if (cls === " unknown") top.style.height = "55%";
    else if (absent(r.response_ms)) top.style.height = "30%";
    else {
      const h = 6 + 93 * r.response_ms / maxRt;
      const f = absent(r.first_byte_ms) ? 0 : Math.max(1.5, h * Math.min(1, r.first_byte_ms / r.response_ms));
      top.style.height = (h - f) + "%";
      low.style.height = f + "%";
    }
    const delay = (80 + i * 14) + "ms";
    top.style.animationDelay = delay;
    low.style.animationDelay = delay;
    add(b, top, low);
    chart.append(b);
  });
  pane.append(chart);
  const chx = el("div", "chx");
  add(chx, el("span", null, "response time · first byte below"), el("span", "mono", times.length ? "tallest " + secs(maxRt) : ""));
  pane.append(chx);
  const pool = req ? [req] : reqs.filter((r) => r.status === "completed");
  const unreported = pool.filter((r) => r.usage_reported === false).length;
  const unknownUsage = pool.filter((r) => absent(r.usage_reported)).length;
  if (unreported) pane.append(el("p", "gaprow", "usage not reported" + (req ? "" : " · " + unreported + " of " + pool.length)));
  else if (unknownUsage && req && req.row === null) pane.append(el("p", "gaprow", "no decision row for this request"));
  void session;
}

// --- input -------------------------------------------------------------------

$("auth").addEventListener("submit", (event) => {
  event.preventDefault();
  S.token = $("token").value;
  $("token").value = "";
  S.listJson = "";
  S.overviewJson = "";
  S.dataJson = "";
  S.rulesJson = "";
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
  S.rules = null;
  S.rulesJson = "";
  S.draft = null;
  S.draftBase = null;
  S.report = null;
  S.edres = null;
  S.confirming = false;
  S.hl = null;
  S.editor = null;
  S.rulesRoot = null;
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
  if (key === "r") { go(rulesHash()); event.preventDefault(); return; }
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
  if (key === "j") { neighbour(1); event.preventDefault(); return; }
  if (key === "k") { neighbour(-1); event.preventDefault(); return; }
  if (!S.data) return;
  const reqs = requests().slice().sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const at = reqs.findIndex((r) => r.seq === S.sel);
  if (key === "ArrowRight") { setStage(Math.min(STAGES.length, S.stage + 1)); event.preventDefault(); }
  else if (key === "ArrowLeft") { setStage(Math.max(1, S.stage - 1)); event.preventDefault(); }
  else if (key === "]") { if (at < reqs.length - 1) pick(reqs[at + 1].seq); event.preventDefault(); }
  else if (key === "[") { if (at > 0) pick(reqs[at - 1].seq); else pick(null); event.preventDefault(); }
  else if (key === "Escape") {
    // First back to the admission, then back to the list.
    if (S.sel !== null) pick(null); else go(listHash());
    event.preventDefault();
  }
});

onHash();
chrome();
refresh("view");
