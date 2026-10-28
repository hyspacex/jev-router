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
// The same verdicts as a status dot, for lists and legends: [class, words].
const VERDICT_DOTS = {right: ["v-good", "good"], too_weak: ["v-weak", "too weak"], too_strong: ["v-strong", "too strong"], too_slow: ["v-slow", "too slow"]};
const FEEDBACK_OPTIONS = [["right", VERDICT_WORDS.right], ["too_weak", VERDICT_WORDS.too_weak], ["too_strong", VERDICT_WORDS.too_strong],
  ["too_slow", VERDICT_WORDS.too_slow], ["unrated", "unrated"]];
// Search accepts what the router's `q` filter accepts: up to 80 plain characters.
const SEARCH_OK = /^[\p{L}\p{N}_ .,:;!?'"()[\]/#@+=&*-]{1,80}$/u;

// Where the browser keeps the admin token between visits. Every read and
// write is guarded: a blocked store means the form asks each time, as before.
const TOKEN_KEY = "jev-router.admin-token";

const S = {
  token: "",
  connected: false,
  connecting: false,  // a stored token is being tried; the form stays out of the way
  remember: false,    // the form was just submitted: keep the token on the first 2xx
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
// A value no row recorded: a quiet dash whose title says why. Never blank,
// never zero, never the colour of a problem.
function gap(text) {
  const node = el("span", "gap", "–");
  node.title = text || "not recorded";
  return node;
}
// A value, or a gap. Never a guess.
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
// A band: one surface, a head with its title and a muted note at the right.
// title null builds a band with no head. note is text or a node.
function band(title, note, cls) {
  const box = el("section", "band" + (cls ? " " + cls : ""));
  if (title !== null && title !== undefined) {
    const h = el("div", "bh");
    add(h, el("h2", null, title), note !== null && note !== undefined && note !== "" ? add(el("span", "note"), note) : null);
    box.append(h);
  }
  return box;
}
// The old name. Also carries the old class names (panel, ph, pt) so the
// views not yet rewritten keep finding their own selectors.
function panel(title, note, cls) {
  const box = band(title, note, cls);
  box.classList.add("panel");
  const h = box.querySelector(".bh");
  if (h) { h.classList.add("ph"); h.firstChild.classList.add("pt"); }
  return box;
}
// A band's footnote line: each argument one item, text or a node.
function foot(...children) {
  const line = el("div", "foot");
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    line.append(typeof child === "string" ? el("span", null, child) : child);
  }
  return line;
}
// The "?" beside a label, its definition on hover and to a screen reader.
function helpMark(text) {
  const node = el("span", "help", "?");
  node.title = text;
  node.tabIndex = 0;
  node.setAttribute("role", "img");
  node.setAttribute("aria-label", text);
  return node;
}
// A status badge: kind is warn, bad, good or plain.
function badge(kind, text, title) {
  const node = el("span", "badge" + (kind ? " " + kind : ""), text);
  if (title) node.title = title;
  return node;
}
// A model as the views show it: the provider prefix quiet, the bare name,
// then " · effort" (or "no effort", muted). When the effort now differs from
// the one admitted, an arrow says which way: up mint, down blue.
function modelNode(model, effort, effortNow) {
  const node = el("span", "model");
  if (absent(model) || model === "") { node.append(gap("model not recorded")); return node; }
  const [prov, name] = splitModel(model);
  if (prov) node.append(el("span", "pv", prov));
  node.append(document.createTextNode(name));
  const from = effort || effortNow || null;
  const now = effortNow || null;
  const ef = el("span", "ef", " · ");
  if (!from) ef.append(el("span", "t3", "no effort"));
  else if (now && now !== from) {
    const a = LADDER.indexOf(from);
    const b = LADDER.indexOf(now);
    const dir = a < 0 || b < 0 ? "" : b > a ? " up" : " down";
    add(ef, from + " ", el("span", "arrow" + dir, "→"), " " + now);
  } else ef.append(document.createTextNode(from));
  node.append(ef);
  return node;
}
// A verdict as a status dot with its words. Null for no verdict.
function verdictDot(v) {
  if (!v || !VERDICT_DOTS[v]) return null;
  const [cls, words] = VERDICT_DOTS[v];
  return el("span", "dot " + cls, words);
}
// The old name, kept so the views not yet rewritten still run.
const verdictBadge = verdictDot;
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
// "1 call", "2 calls".
function plural(n, one, many) { return int(n) + " " + (Math.round(Number(n)) === 1 ? one : (many || one + "s")); }

// --- formatting ----------------------------------------------------------

const int = (n) => Math.round(Number(n)).toLocaleString("en-US");
const num2 = (n) => Number(n).toFixed(2);
const secs = (ms) => (Number(ms) / 1000).toFixed(2) + " s";
const size = (b) => (b >= 1e6 ? (b / 1e6).toFixed(2) + " MB" : Math.max(1, Math.round(b / 1000)) + " KB");
const num1 = (n) => { const v = Number(n); return v > 0 && v < 0.1 ? "<0.1" : v.toFixed(v >= 10 ? 0 : 1); };
const pct = (r) => { const v = Number(r) * 100; return (v > 0 && v < 1 ? "<1" : v > 99 && v < 100 ? ">99" : Math.round(v)) + "%"; };
// Milliseconds to two or three significant figures: "613 ms", "3.2 s", "27 s".
function ms1(v) {
  const n = Number(v);
  if (Math.round(n) < 1000) return Math.round(n) + " ms";
  const s = n / 1000;
  return (Math.round(s * 10) < 100 ? (Math.round(s * 10) / 10).toFixed(1) : String(Math.round(s))) + " s";
}
// Milliseconds as the reader wants them: ms under a second, seconds above.
const ms = ms1;
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

// The remembered token. A store that throws (blocked, private window) reads
// as empty and writes nothing: the form asks, as it always did.
function storedToken() {
  try { return window.localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}
function storeToken(token) {
  try { if (token) window.localStorage.setItem(TOKEN_KEY, token); } catch { /* not kept */ }
}
function forgetToken() {
  try { window.localStorage.removeItem(TOKEN_KEY); } catch { /* nothing kept */ }
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
    S.connecting = false;
    // The first 2xx after the form was submitted keeps the token.
    if (S.remember && refused === null) { storeToken(S.token); S.remember = false; }
    fail(refused);
    chrome();
    if (changed || kind !== "data") render(kind);
  } catch (error) {
    if (gen !== S.gen) return;
    if (S.connecting) { S.connecting = false; chrome(); }
    fail((error && error.name === "AbortError" ? "Refresh timed out" : "Router unreachable") + " · showing the last data");
  } finally {
    if (gen === S.gen) schedule();
  }
}

function unauthorized() {
  // A refused token is forgotten, and the form comes back.
  forgetToken();
  S.remember = false;
  S.connecting = false;
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
  $("auth").hidden = S.connected || S.connecting;
  $("pause").hidden = !S.connected;
  $("disconnect").hidden = !S.connected;
  $("views").hidden = !S.connected;
  $("keys").hidden = !S.connected;
  $("pause").textContent = S.paused ? "Resume" : "Pause";
  const cfgNode = $("cfg");
  cfgNode.replaceChildren();
  if (S.connected) {
    const live = el("span", "live" + (S.paused ? " off" : ""));
    live.title = S.paused ? "Paused: not refreshing" : "Refreshing every 5 s";
    add(live, el("i"), S.paused ? "paused" : "live");
    cfgNode.append(live);
    if (S.cfgHash !== null) {
      const hash = add(el("span", "mono"), show(hash8(S.cfgHash)));
      hash.title = "Running config hash";
      cfgNode.append(hash);
    }
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
  // A card whose name was just re-rendered away has nothing to point at.
  if (RCARD.anchor && !document.contains(RCARD.anchor)) closeRuleCard();
  if (focused) {
    const again = document.querySelector('[data-fk="' + CSS.escape(focused) + '"]');
    if (again && again !== document.activeElement) again.focus({preventScroll: true});
  }
}

// --- sessions ----------------------------------------------------------------
//
// The list: a head with the saved views and the window, a toolbar with one
// search box that also takes `rule:hard` style filters, the active filters as
// chips, and a table. The head and toolbar are built once and synced; the
// live refresh replaces only the table, so typing is never interrupted.

// The filters a `key:value` token in the search box may set, the chips show
// and the "+ Filter" popover lists. `q` lives in the box, `window` in the head.
const CHIP_KEYS = ["client", "model", "rule", "state", "adaptive", "problem", "feedback", "pinned"];
const FILTER_TOKEN = /^(client|model|rule|state|adaptive|problem|feedback|pinned):(.*)$/;
// The saved views: a name and the exact filter set it stands for. A view
// keeps the current window; the window is its own control.
const SAVED_VIEWS = [["All", {}], ["Problems", {problem: "any"}], ["Unrated", {feedback: "unrated"}], ["Pinned", {pinned: "1"}]];
const LIST_WINDOWS = ["24h", "7d", "30d"];
let popWired = false;

function renderSessions(kind) {
  const main = $("main");
  if (!S.toolbar) S.toolbar = buildToolbar();
  syncToolbar();
  const table = sessionTable(kind);
  if (main.firstChild === S.toolbar && main.children.length === 2) main.lastChild.replaceWith(table);
  else main.replaceChildren(S.toolbar, table);
}

// Set several filters at once; an empty value removes one.
function setFilters(patch, typing) {
  const next = Object.assign({}, S.filters);
  for (const [key, value] of Object.entries(patch)) {
    if (value) next[key] = value; else delete next[key];
  }
  const hash = listHash(next);
  if (typing) {
    // Typing replaces the entry rather than piling up history.
    history.replaceState(null, "", hash);
    onHash();
  } else go(hash);
}
function setFilter(key, value, typing) { setFilters({[key]: value}, typing); }

// The 400 the shell turns into an empty list carries no config hash.
function listRefused() { return !!S.list && !("config_hash" in S.list); }
// Every session, any time: the feedback facet groups all of them.
function totalSessions() {
  const fb = S.list && S.list.facets && S.list.facets.feedback;
  return Array.isArray(fb) ? fb.reduce((n, x) => n + (Number(x.n) || 0), 0) : null;
}
// Two filter sets with the same values, the window aside.
function sameFilters(a, b) {
  return FILTER_KEYS.every((k) => k === "window" || (a[k] || "") === (b[k] || ""));
}

// What a saved view would list, or null when nothing on hand says so
// honestly. The lit view is the list on screen. With no filter but the
// window and every row listed, the rows themselves answer for each view.
// Otherwise, without a window, the facets and bookmarks (all time) do; the
// Problems count is in neither.
function viewCount(set) {
  if (!S.list || listRefused()) return null;
  const rows = sessions();
  const matched = typeof S.list.matched === "number" ? S.list.matched : null;
  if (matched !== null && sameFilters(S.filters, set)) return matched;
  if (sameFilters(S.filters, {}) && matched !== null && rows.length === matched) {
    if (set.problem) return rows.filter((s) => Array.isArray(s.problems) && s.problems.length).length;
    if (set.feedback) return rows.filter((s) => !s.feedback_verdict).length;
    if (set.pinned) return rows.filter((s) => s.pinned).length;
    return rows.length;
  }
  if (S.filters.window) return null;
  if (set.feedback) {
    const fb = S.list.facets && S.list.facets.feedback;
    if (!Array.isArray(fb)) return null;
    const hit = fb.find((x) => x.value === "unrated");
    return hit ? Number(hit.n) : 0;
  }
  if (set.pinned) return bookmarks().length;
  if (set.problem) return null;
  return totalSessions();
}

// Split the box text into filters, search words and a token still being
// typed. A token becomes a filter once a space follows it, or on Enter
// (final); an unfinished one is neither a filter nor search text.
function parseSearch(text, final) {
  const words = text.split(/\s+/);
  const filters = {};
  const rest = [];
  const held = [];
  words.forEach((w, i) => {
    if (!w) return;
    const m = FILTER_TOKEN.exec(w);
    if (!m) { rest.push(w); return; }
    const done = final || i < words.length - 1;
    if (done && m[2]) filters[m[1]] = m[2];
    else held.push(w);
  });
  return {filters, q: rest.join(" "), held: held.join(" ")};
}

// Apply what the box holds. Returns true when a token became a filter.
function applySearch(search, final) {
  const parsed = parseSearch(search.value, final);
  const patch = Object.assign({}, parsed.filters);
  const took = Object.keys(patch).length > 0;
  if (took) {
    // The token leaves the box; it is a chip now.
    const left = [parsed.q, parsed.held].filter(Boolean).join(" ");
    search.value = left && !final && !parsed.held ? left + " " : left;
  }
  // Text the router would refuse is never sent; the list stays as it is.
  if (!parsed.q || SEARCH_OK.test(parsed.q)) patch.q = parsed.q;
  if (Object.entries(patch).every(([k, v]) => (S.filters[k] || "") === (v || ""))) return took;
  setFilters(patch, !final);
  return took;
}

function buildToolbar() {
  const root = el("div", "slist");

  // The head: title, saved views, the window.
  const head = el("div", "head");
  const views = el("nav", "views");
  views.id = "sviews";
  views.setAttribute("aria-label", "Saved views");
  for (const [name] of SAVED_VIEWS) {
    const a = link(null, name, "#sessions");
    a.dataset.view = name;
    views.append(a);
  }
  const right = el("div", "right");
  const seg = el("div", "seg");
  seg.id = "swin";
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", "Last seen");
  right.append(seg);
  add(head, el("h1", null, "Sessions"), views, right);

  // The toolbar: search, chips, + Filter, the count.
  const bar = el("div", "toolbar");
  const box = el("label", "search");
  const search = el("input");
  search.id = "q";
  search.type = "search";
  search.placeholder = "Search titles and prompts, or filter: rule:hard model:glm-5.3 problem:any";
  search.autocomplete = "off";
  search.spellcheck = false;
  search.maxLength = 200;
  search.setAttribute("aria-label", "Search titles and prompts, or type key:value to filter");
  add(box, el("span", "t3", "⌕"), search);
  search.addEventListener("input", () => {
    clearTimeout(S.searchTimer);
    S.searchTimer = setTimeout(() => applySearch(search, false), 250);
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { search.blur(); event.stopPropagation(); return; }
    if (event.key === "Enter") {
      clearTimeout(S.searchTimer);
      if (applySearch(search, true)) { event.preventDefault(); return; }
    }
    if (event.key === "Enter" || event.key === "ArrowDown") {
      const first = document.querySelector(".srow[href]");
      if (first) { first.focus(); event.preventDefault(); }
    }
  });
  const chips = el("div", "schips");
  chips.id = "schips";
  const addBtn = button("chip add", "+ Filter", () => togglePop());
  addBtn.id = "sadd";
  addBtn.setAttribute("aria-haspopup", "dialog");
  addBtn.setAttribute("aria-expanded", "false");
  const pop = el("div", "fpop");
  pop.id = "spop";
  pop.hidden = true;
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Add a filter");
  pop.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { togglePop(false); addBtn.focus(); event.stopPropagation(); }
  });
  const count = el("span", "count");
  count.id = "count";
  add(bar, box, chips, addBtn, count, pop);
  add(root, head, bar);

  if (!popWired) {
    popWired = true;
    // A click anywhere else closes the popover.
    document.addEventListener("click", (event) => {
      if (!S.toolbar) return;
      const p = tb("spop");
      if (p.hidden || p.contains(event.target) || tb("sadd").contains(event.target)) return;
      togglePop(false);
    });
  }
  return root;
}

// A toolbar control, attached to the page or not yet.
function tb(id) { return S.toolbar.querySelector("#" + id); }

function togglePop(open) {
  const pop = tb("spop");
  const btn = tb("sadd");
  const showIt = open === undefined ? pop.hidden : open;
  pop.hidden = !showIt;
  btn.setAttribute("aria-expanded", showIt ? "true" : "false");
  if (!showIt) return;
  // Under the button, kept inside the toolbar.
  const bar = btn.parentNode;
  const width = Math.min(440, bar.clientWidth);
  pop.style.width = width + "px";
  pop.style.left = Math.max(0, Math.min(btn.offsetLeft, bar.clientWidth - width)) + "px";
  pop.style.top = (btn.offsetTop + btn.offsetHeight + 6) + "px";
  const first = pop.querySelector("button");
  if (first) first.focus({preventScroll: true});
}

// The values the popover offers for a key: [value, words, count or null].
function popValues(key, facets) {
  if (key === "client" || key === "model" || key === "rule") return (facets[key] || []).map((x) => [x.value, x.value, x.n]);
  if (key === "feedback") {
    const seen = new Map((facets.feedback || []).map((x) => [x.value, x.n]));
    return FEEDBACK_OPTIONS.filter(([v]) => seen.has(v)).map(([v]) => [v, v === "unrated" ? "unrated" : VERDICT_DOTS[v][1], seen.get(v)]);
  }
  if (key === "state") return STATES.map((s) => [s, s, null]);
  if (key === "adaptive") return MODES.map((m) => [m, m, null]);
  if (key === "problem") return Object.keys(PROBLEMS).map((p) => [p, p === "any" ? "any" : (PROBLEM_SHORT[p] || human(p)), null]);
  return [["1", "pinned", null], ["0", "not pinned", null]];
}

function fillPop(pop, facets) {
  const f = S.filters;
  pop.replaceChildren();
  for (const key of CHIP_KEYS) {
    const row = el("div", "fprow");
    const vals = el("div", "fpv");
    const options = popValues(key, facets);
    if (!options.length) vals.append(gap("no " + key + " recorded"));
    for (const [value, words, n] of options) {
      const on = f[key] === value;
      const b = button("fopt" + (on ? " on" : ""), null, () => { togglePop(false); setFilter(key, on ? "" : value); });
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.title = key + ":" + value + (key === "problem" ? " · " + PROBLEMS[value] : "");
      add(b, el("span", key === "client" || key === "model" || key === "rule" ? "mono" : null, words),
        absent(n) ? null : el("small", null, int(n)));
      vals.append(b);
    }
    add(row, el("span", "k", key), vals);
    pop.append(row);
  }
}

function syncToolbar() {
  const f = S.filters;
  const facets = (S.list && S.list.facets) || {};
  const json = JSON.stringify(facets) + "|" + JSON.stringify(f);
  if (json !== S.facetsJson) {
    S.facetsJson = json;
    fillPop(tb("spop"), facets);
    // One chip per filter, other than the search text and the window.
    const chips = tb("schips");
    chips.replaceChildren();
    for (const key of CHIP_KEYS) {
      if (!f[key]) continue;
      const x = button("x", "✕", () => setFilter(key, ""), "Remove filter " + key + " " + f[key]);
      x.title = "Remove filter";
      chips.append(add(el("span", "chip"), el("span", "k", key), " " + f[key], x));
    }
  }
  // Saved views: the lit one is the filter set on screen.
  for (const a of tb("sviews").children) {
    const set = SAVED_VIEWS.find(([name]) => name === a.dataset.view)[1];
    a.href = listHash(Object.assign({}, set, f.window ? {window: f.window} : {}));
    const lit = sameFilters(f, set);
    a.classList.toggle("on", lit);
    if (lit) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    const n = viewCount(set);
    a.replaceChildren(document.createTextNode(a.dataset.view));
    if (n !== null) a.append(el("small", null, int(n)));
  }
  // The window: any, then the spans; a hand-edited one shows as itself.
  const cur = f.window || "";
  const wins = [""].concat(LIST_WINDOWS, cur && !LIST_WINDOWS.includes(cur) ? [cur] : []);
  tb("swin").replaceChildren(...wins.map((w) => {
    const a = link(cur === w ? "on" : null, w || "any", listHash(Object.assign({}, f, {window: w})));
    if (cur === w) a.setAttribute("aria-current", "true");
    return a;
  }));
  const search = tb("q");
  if (document.activeElement !== search && search.value !== (f.q || "")) search.value = f.q || "";
  // "N sessions", or "N of M sessions" when a filter narrows the list.
  const countNode = tb("count");
  if (!S.list || listRefused()) { countNode.textContent = ""; return; }
  const shown = sessions().length;
  const matched = typeof S.list.matched === "number" ? S.list.matched : shown;
  const total = totalSessions();
  const narrowed = FILTER_KEYS.some((k) => f[k]) && total !== null && total !== matched;
  let text = narrowed ? int(matched) + " of " + int(total) + " sessions" : plural(matched, "session");
  if (shown < matched) text = "first " + int(shown) + " · " + text;
  countNode.textContent = text;
}

// [label, class]. Numbers right-aligned.
const COLUMNS = [["Session", null], ["Last seen", null], ["Model · effort", null], ["Rule", null], ["Verdict", null], ["Problems", "num"]];

function sessionTable(kind) {
  const root = band(null, null, "slband" + (kind === "data" ? " still" : ""));
  if (!S.list) return root;
  const rows = sessions();
  if (!rows.length) {
    const empty = el("p", "empty");
    if (FILTER_KEYS.some((k) => S.filters[k])) add(empty, "No session matches these filters · ", link("clear", "Clear", "#sessions"));
    else empty.textContent = "No sessions yet";
    root.append(empty);
    return root;
  }
  const tbl = el("div", "tbl sessions");
  const head = el("div", "tr th");
  head.setAttribute("aria-hidden", "true");
  for (const [label, cls] of COLUMNS) head.append(el("span", cls, label));
  tbl.append(head);
  for (const s of rows) tbl.append(sessionRow(s));
  root.append(tbl);
  // Explained once, under the table, only when some row needs it.
  if (rows.some((s) => !s.title && absent(s.turn_count))) {
    root.append(foot("Sessions from before logging was turned on have no stored prompt, title or turn count."));
  }
  return root;
}

// The muted text after a title: what the prompt says beyond it. A title cut
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

// The problems as one count badge: amber, red when a request was refused or
// a stream broke. Its title names each problem with its count.
function problemBadge(s) {
  const list = Array.isArray(s.problems) ? s.problems : [];
  if (!list.length) return null;
  const counts = s.requests || {};
  const words = list.map((p) => {
    const k = p === "unknown" || p === "rejected" || p === "stream_failed" ? counts[p] : null;
    return (k ? int(k) + " " : "") + (PROBLEM_SHORT[p] || human(p));
  });
  const bad = list.includes("rejected") || list.includes("stream_failed");
  const node = badge(bad ? "bad" : "warn", int(list.length), words.join(", "));
  node.setAttribute("aria-label", plural(list.length, "problem") + ": " + words.join(", "));
  return node;
}

function sessionRow(s) {
  // "srow" is the hook j/k and the search box's Enter look for.
  const row = link("tr srow", null, sessionHash(s.session_id));
  row.dataset.fk = "row-" + s.session_id;
  row.dataset.sid = s.session_id;
  const effortFrom = s.admitted_effort || s.effort;
  const effortNow = s.effective_effort || s.effort;

  // The session as a person knows it: what they asked, then where.
  const ti = el("span", "sti");
  const line = el("span", "stl");
  if (s.pinned) {
    const note = "Pinned" + (s.pinned.label ? " · " + s.pinned.label : "");
    const pin = el("span", "pin", "◆");
    pin.title = note;
    pin.setAttribute("aria-label", note);
    line.append(pin);
  }
  // No title: the bookmark's label when someone gave one, else the short id.
  const name = s.title || (s.pinned && s.pinned.label) || null;
  if (name) line.append(el("b", null, name));
  else add(line, el("b", "mono t2 ssid", short(s.session_id, s.client)), gap("prompt not recorded before logging was on"));
  const preview = previewLine(s.title, s.prompt_preview);
  if (preview) line.append(el("span", "sprev", preview));
  ti.append(line);

  const bits = [s.client, s.workspace_hint, absent(s.turn_count) ? null : plural(s.turn_count, "turn"),
    s.tombstone ? "pruned" : s.state === "closed" ? "closed" : null].filter(Boolean);
  const sub = el("small", "smeta");
  if (bits.length) sub.append(el("span", null, bits.join(" · ")));
  // Narrower, the Problems column folds into this line.
  const fold = problemBadge(s);
  if (fold) { fold.classList.add("sfold"); sub.append(fold); }
  if (bits.length || fold) ti.append(sub);
  // On a phone the model and verdict ride under the title too.
  ti.append(add(el("small", "smline"), modelNode(s.model, effortFrom, effortNow), verdictDot(s.feedback_verdict)));

  const seen = absent(s.last_seen) ? s.created : s.last_seen;
  const when = el("span", "sseen");
  if (absent(seen)) when.append(gap("no time recorded"));
  else { when.textContent = stamp(seen); when.title = day(seen) + " " + clock(seen); }

  add(row,
    ti,
    when,
    add(el("span"), modelNode(s.model, effortFrom, effortNow)),
    add(el("span", "srule"), s.rule ? s.rule : gap("no rule recorded")),
    add(el("span"), verdictDot(s.feedback_verdict)),
    add(el("span", "num"), problemBadge(s)));
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

// --- a rule in words, and its card -------------------------------------------------
//
// A rule's name is a reminder, not an explanation. Wherever one is shown it
// can carry a "matches when" line written from its conditions, the first
// sentence of its description, and a card that opens on click.

// A flat feature condition's comparison, from its key's suffix, as the trace reads it.
function featureOp(key) {
  const m = String(key).match(/_(gte|lte|in|include)$/);
  return !m ? "eq" : m[1] === "include" ? "intersects" : m[1];
}

function firstSentence(text) {
  if (!text) return "";
  const m = String(text).match(/^.*?[.!?](\s|$)/);
  return (m ? m[0] : String(text)).trim();
}

// The level a score sits nearest, by name, or null without names.
function levelName(labels, v) {
  if (!Array.isArray(labels) || !labels.length || typeof v !== "number") return null;
  return labels[Math.max(0, Math.min(labels.length - 1, Math.round(v)))];
}

// A config `when` mapping as the condition list a trace carries.
function whenConds(when) {
  const out = [];
  for (const [key, cond] of Object.entries(when || {})) {
    if (cond === null || typeof cond !== "object" || Array.isArray(cond)) {
      out.push({key, source: "feature", op: featureOp(key), threshold: cond});
      continue;
    }
    for (const [op, v] of Object.entries(cond)) {
      if (op === "shift_with" || op === "max_shift") continue;
      out.push({key, source: "answer", op, threshold: v});
    }
  }
  return out;
}

// 1.399 reads as "under 1.4": a cutoff written one step below a round number.
function underText(t) {
  const up = Math.round((t + 0.001) * 100) / 100;
  return Math.abs(up - t - 0.001) < 1e-9 ? "under " + cfg(up) : "≤ " + cfg(t);
}

// One condition in words: "has tools", "difficulty above routine (1.4+)".
function condPhrase(c, labels) {
  const key = c.source === "feature" ? String(c.key || "?").replace(/_(gte|lte|in|include)$/, "") : String(c.key || "?");
  const name = key.replace(/^has_/, "").replaceAll("_", " ");
  const t = c.threshold;
  if (c.source === "feature" && typeof t === "boolean") return (t ? (key.startsWith("has_") ? "has " : "") : "no ") + name;
  const list = (Array.isArray(t) ? t : [t]).map(plain).join(" or ");
  if (c.op === "in" || c.op === "not_in") return name + (c.op === "in" ? " is " : " is not ") + list;
  if (c.op === "intersects") return name + " include " + list;
  if (c.op === "conf_gte") return name + " confidence " + cfg(t) + "+";
  if (c.op === "eq" && typeof t !== "number") return name + " is " + plain(t);
  if (typeof t !== "number") return name + " " + human(c.op || "=") + " " + plain(t);
  const names = labels && labels[key];
  const top = Array.isArray(names) ? names.length - 1 : null;
  if (c.op === "gte" || c.op === "gt") {
    if (top === null) return name + " " + cfg(t) + "+";
    if (t >= top) return name + " " + names[top] + " (" + cfg(t) + ")";
    if (Number.isInteger(t)) return name + " " + names[t] + " or above (" + cfg(t) + "+)";
    return name + " above " + names[Math.floor(t)] + " (" + cfg(t) + "+)";
  }
  if (c.op === "lte" || c.op === "lt") {
    const under = underText(t);
    if (top === null) return name + " " + under;
    return name + " " + names[Math.max(0, Math.floor(t))] + " or below (" + under + ")";
  }
  return name + " " + (OPS[c.op] ? OPS[c.op][0] : human(c.op)) + " " + cfg(t);
}

// What a rule matches, as one sentence. No conditions: it takes what is left.
function matchWords(conds, labels) {
  if (!conds.length) return "Anything left";
  const text = conds.map((c) => condPhrase(c, labels)).join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// "glm-5.3 · high", from a `{route, model, effort}` destination.
function sendsText(to) {
  if (!to || !to.model) return null;
  return splitModel(to.model)[1] + (to.effort ? " · " + to.effort : "");
}

// The card. info: name, index, count, protected, lane, description,
// sentence, sendsTo, before (names), hits, total, words, open (a node or null).
function ruleCard(info) {
  const c = el("div", "rcard");
  c.setAttribute("role", "dialog");
  c.setAttribute("aria-label", "Rule " + info.name);
  const top = el("div", "rtop");
  add(top, el("span", "rnm", info.name), info.protected ? el("span", "pill", "protected") : null,
    info.index ? el("span", "rpos", info.index + " of " + info.count) : null);
  c.append(top);
  add(c, el("div", "rlbl", "Matches when"), el("div", "rtxt", info.sentence || "–"));
  c.append(el("div", "rlbl", "Why it exists"));
  c.append(info.description ? el("div", "rtxt d", info.description) : el("div", "rtxt d none", "No description in router.yaml"));
  const kv = el("div", "kv");
  const to = sendsText(info.sendsTo);
  const route = info.sendsTo && info.sendsTo.route;
  add(kv, el("span", "k", "Sends to"), to ? el("span", "mono", to + (route ? "  (" + route + ")" : "")) : gap("not resolved"));
  if (info.lane) add(kv, el("span", "k", "Lane"), el("span", "mono", info.lane));
  if (info.before) {
    const n = info.before.length;
    const words = !n ? "first in the ladder" : n <= 2 ? "after " + info.before.join(", ") : "after " + n + " rules, last " + info.before[n - 1];
    add(kv, el("span", "k", "Position"), el("span", null, words));
  }
  if (!absent(info.hits)) add(kv, el("span", "k", "Hits"), el("span", null, int(info.hits) + (absent(info.total) ? "" : " of " + int(info.total)) + " admissions " + (info.words || "")));
  c.append(kv);
  if (info.open) c.append(add(el("div", "racts"), info.open));
  return c;
}

// One card open at a time, on <body>, below the name that opened it.
const RCARD = {node: null, anchor: null};
function closeRuleCard(focus) {
  if (!RCARD.node) return;
  RCARD.node.remove();
  const a = RCARD.anchor;
  RCARD.node = null;
  RCARD.anchor = null;
  if (a) {
    a.setAttribute("aria-expanded", "false");
    if (focus && document.contains(a)) a.focus({preventScroll: true});
  }
}
function openRuleCard(anchor, info) {
  const same = RCARD.anchor === anchor;
  closeRuleCard();
  if (same) return;
  const card = ruleCard(info);
  card.classList.add("pop");
  // clientWidth leaves the scrollbar out, which 100vw would not.
  const room = document.documentElement.clientWidth;
  card.style.width = Math.min(380, room - 32) + "px";
  document.body.append(card);
  const box = anchor.getBoundingClientRect();
  const width = card.offsetWidth;
  card.style.left = Math.max(16, Math.min(box.left, room - width - 16)) + window.scrollX + "px";
  card.style.top = box.bottom + window.scrollY + 6 + "px";
  anchor.setAttribute("aria-expanded", "true");
  RCARD.node = card;
  RCARD.anchor = anchor;
}
// A rule's name that opens its card. info may be a function, read on click.
function ruleName(name, info, cls) {
  const b = el("button", "rn" + (cls ? " " + cls : ""), name);
  b.type = "button";
  b.setAttribute("aria-haspopup", "dialog");
  b.setAttribute("aria-expanded", "false");
  b.addEventListener("click", (event) => {
    event.stopPropagation();
    openRuleCard(b, typeof info === "function" ? info() : info);
  });
  b.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") event.stopPropagation(); });
  return b;
}
document.addEventListener("click", (event) => {
  if (RCARD.node && !(event.target instanceof Element && event.target.closest(".rcard"))) closeRuleCard();
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && RCARD.node) closeRuleCard(true); });
window.addEventListener("hashchange", () => closeRuleCard());

// --- input -------------------------------------------------------------------

$("auth").addEventListener("submit", (event) => {
  event.preventDefault();
  // An empty token is never sent: the router could only answer 401.
  if (!$("token").value) { fail("Admin token required"); return; }
  S.token = $("token").value;
  S.remember = true;
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
  forgetToken();
  S.token = "";
  S.remember = false;
  S.connecting = false;
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

// A token kept from an earlier visit connects at once, without the form.
S.token = storedToken();
S.connecting = !!S.token;
onHash();
chrome();
// Nothing is fetched before a token exists: the form asks for one first.
if (S.token) refresh("view");
else fail("Admin token required");
