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
// Feedback verdicts, one vocabulary for every view: list badges, the
// inspector's badge and buttons, the rules legend and the preview marks.
// The database values (right, too_weak, too_strong, too_slow) never change.
const VERDICT_WORDS = {right: "✓ good", too_weak: "↑ too weak", too_strong: "↓ too strong", too_slow: "too slow"};
const FEEDBACK_OPTIONS = [["right", VERDICT_WORDS.right], ["too_weak", VERDICT_WORDS.too_weak], ["too_strong", VERDICT_WORDS.too_strong],
  ["too_slow", VERDICT_WORDS.too_slow], ["unrated", "unrated"]];
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
  passed: null,       // the draft text the last Check or Preview accepted; Apply needs it to match
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
// The one badge for a verdict, in its colour: mint good, amber too weak,
// blue too strong. Null for no verdict.
function verdictBadge(v) {
  if (!v || !VERDICT_WORDS[v]) return null;
  return el("span", "vbadge v-" + v, VERDICT_WORDS[v]);
}
// "1 call", "2 calls".
function plural(n, one, many) { return int(n) + " " + (Math.round(Number(n)) === 1 ? one : (many || one + "s")); }

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
    } else {
      main.append(inspectorView(kind));
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
  if (s.client) title.append(el("span", "scli", s.client + " ·")); // shown on narrow screens only
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

  // Model · effort. A session whose effort moved, by a plan or by feedback,
  // says from where to where: mint for up, blue for down.
  const mdl = el("span", "c5 mono");
  const [prov, name] = splitModel(s.model);
  add(mdl, prov ? el("span", "faint", prov) : null, show(name));
  const from = s.admitted_effort || s.effort;
  const now = s.effective_effort || s.effort;
  if (from && now && now !== from) {
    const a = LADDER.indexOf(from);
    const b = LADDER.indexOf(now);
    const dir = a < 0 || b < 0 ? "" : b > a ? " up" : " down";
    add(mdl, el("span", "eff", " · " + from + " "), el("span", "earr" + dir, "→"), el("span", "eff" + dir, " " + now));
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
  // An empty token is never sent: the router could only answer 401.
  if (!$("token").value) { fail("Admin token required"); return; }
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
  S.passed = null;
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
  inspectorKey(event);
});

onHash();
chrome();
// Nothing is fetched before a token exists: the form asks for one first.
if (S.token) refresh("view");
else fail("Admin token required");
