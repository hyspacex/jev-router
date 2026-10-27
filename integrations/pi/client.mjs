import { randomUUID } from "node:crypto";

export class ContractError extends Error {}

/** A refusal the router named with one of its own machine codes. */
export class RouterError extends ContractError {
  constructor(code, note) {
    super(`Router refused: ${code}. ${note}`);
    this.code = code;
  }
}

export const DEFAULT_MAX_OUTPUT_TOKENS = 8192;

/**
 * The strict aliases the picker offers. The router owns what each means, the
 * client only names it.
 */
export const ALIASES = ["auto"];

/**
 * Aliases the router still serves for sessions bound under them, but no
 * longer offers. `auto-conserve` now routes exactly as `auto` does, so a
 * session bound under it carries on from the `auto` entry.
 */
export const RETIRED_ALIASES = ["auto-conserve"];

/**
 * Router codes that mean the request was definitively refused before the
 * provider saw it (`docs/SESSION_ROUTING.md`: "Definitively rejected before
 * acceptance: retried normally"). Everything else keeps the block: a
 * transport failure, an upstream status the router passed through, a stream
 * that broke after acceptance, EXECUTION_OUTCOME_UNKNOWN, SESSION_CONFLICT
 * and REQUEST_ALREADY_COMPLETED all leave an outcome only a person can state.
 */
export const PRE_ACCEPTANCE_CODES = new Set([
  "INVALID_ROUTER_INPUT",
  "ROUTER_UNAUTHORIZED",
  "SESSION_UNKNOWN",
  "SESSION_CLOSED",
  "PROFILE_CHANGED",
  "NO_SAFE_ADMISSION",
  "CONTEXT_BUDGET_EXCEEDED",
  "UNSUPPORTED_PROFILE",
  "PROVIDER_UNAVAILABLE",
]);

/**
 * The machine code of a refused response and, when the router gave one, the
 * whole seconds until the provider can serve again. Nothing else is read from
 * the body: provider and router message text never reach a transcript, a log
 * line or an error message.
 */
export async function readRefusal(response) {
  let code = `HTTP_${response.status}`, retryAfterSeconds;
  try {
    const error = (await response.json())?.error;
    if (typeof error?.code === "string" && /^[A-Z][A-Z_]{2,63}$/.test(error.code)) code = error.code;
    const wait = error?.retry_after_seconds;
    if (Number.isSafeInteger(wait) && wait >= 0) retryAfterSeconds = wait;
  } catch { /* not a structured router error */ }
  return {code, retryAfterSeconds};
}

/** The machine code of a refused response, and nothing else from its body. */
export async function readErrorCode(response) {
  return (await readRefusal(response)).code;
}

function waitText(seconds) {
  return seconds < 3600 ? `${Math.max(1, Math.ceil(seconds / 60))} min` : `${Math.ceil(seconds / 3600)} h`;
}

const CONTRACT_FIELDS = ["model_key", "wire_model", "protocol", "context_window",
  "max_output_tokens", "supports_tools", "supports_vision", "initial_effort", "effort_mode"];

/**
 * Fields a newer router adds to the contract. Each is kept only when it has
 * the type it should; a missing or malformed one is dropped, never refused,
 * so this client works against a router that predates them.
 */
const OPTIONAL_FIELDS = {quota_steered: "boolean", intended_model: "string", intended_effort: "string"};

export function validateBinding(value, sessionId) {
  const e = value?.execution;
  // The first thing that is wrong, named. The router's own message text is
  // never quoted back, and no value from the response is echoed.
  let problem = "";
  if (value?.session_id !== sessionId) problem = "session_id is not this session";
  else if (typeof value.binding_revision !== "string" || !value.binding_revision) problem = "binding_revision is missing";
  else if (!e || typeof e !== "object") problem = "execution is missing";
  else if (e.protocol !== "openai-chat") problem = 'execution.protocol is not "openai-chat"';
  else if (e.effort_mode !== "fixed") problem = 'execution.effort_mode is not "fixed"';
  else if (typeof e.wire_model !== "string" || !e.wire_model) problem = "execution.wire_model is missing";
  else if (typeof e.model_key !== "string" || !e.model_key) problem = "execution.model_key is missing";
  else if (!Number.isSafeInteger(e.context_window) || e.context_window <= 0) problem = "execution.context_window is not a positive integer";
  else if (!Number.isSafeInteger(e.max_output_tokens) || e.max_output_tokens <= 0) problem = "execution.max_output_tokens is not a positive integer";
  else if (typeof e.supports_tools !== "boolean") problem = "execution.supports_tools is not a boolean";
  else if (typeof e.supports_vision !== "boolean") problem = "execution.supports_vision is not a boolean";
  if (problem) {
    throw new ContractError(
      `Unsupported router binding: ${problem}. This integration executes a fixed-effort openai-chat binding only.`);
  }
  if (!["prepared", "active"].includes(value.state)) throw new ContractError(`Binding is ${value.state}; it cannot execute.`);
  // Persist only the contract, never arbitrary fields from a control response.
  const execution = Object.fromEntries(CONTRACT_FIELDS.map(key => [key, e[key]]));
  for (const [key, type] of Object.entries(OPTIONAL_FIELDS)) {
    if (typeof e[key] === type && (type !== "string" || /^[A-Za-z0-9_.:/()+-]{1,64}$/.test(e[key]))) execution[key] = e[key];
  }
  return {session_id: value.session_id, binding_revision: value.binding_revision, state: value.state, execution};
}

/** `/jev <word>` to the verdict it records. */
export const FEEDBACK_WORDS = {
  more: "too_weak", "+": "too_weak", up: "too_weak",
  less: "too_strong", "-": "too_strong", down: "too_strong",
  good: "right", ok: "right",
};

/** The picker's choices, in the order shown, and the verdict each records. */
export const FEEDBACK_CHOICES = [
  ["↑ Needed more intelligence (too_weak)", "too_weak"],
  ["✓ Just right (right)", "right"],
  ["↓ Needed less intelligence / overkill (too_strong)", "too_strong"],
];

export const NOTE_MAX_CHARS = 2000;

/**
 * `/jev` arguments read as feedback: `{verdict, note}`, `{picker: true}` for
 * `/jev feedback` or `/jev fb`, or null when the arguments are not feedback.
 */
export function parseFeedbackCommand(args) {
  const text = String(args ?? "").trim();
  const [word] = text.split(/\s+/);
  const lowered = (word ?? "").toLowerCase();
  if (lowered === "feedback" || lowered === "fb") return {picker: true};
  const verdict = FEEDBACK_WORDS[lowered];
  if (!verdict) return null;
  const note = text.slice(word.length).trim();
  return {verdict, note: note ? note.slice(0, NOTE_MAX_CHARS) : undefined};
}

/** What Pi tells the person after feedback. Built from fields, never router text. */
export function feedbackMessage(result) {
  const said = {too_weak: "more intelligence", too_strong: "less intelligence", right: "right intelligence"}[result.verdict] ?? result.verdict;
  if (result.effortChanged) return `Recorded '${said}': ${result.modelKey} effort ${result.previousEffort} → ${result.currentEffort} for the rest of this session.`;
  if (result.reasonCode === "at_highest") return `Recorded '${said}' (already at max effort for ${result.modelKey}). Run /new to start a fresh session on a higher model tier.`;
  if (result.reasonCode === "at_lowest") return `Recorded '${said}' (already at the lowest effort ${result.modelKey} allows here).`;
  if (result.reasonCode && result.reasonCode !== "verdict_keeps_effort") return `Recorded '${said}'. Effort unchanged (${result.reasonCode}).`;
  return `Recorded '${said}'.`;
}

/** The status bar text for a bound session. */
export function statusLine(state) {
  const binding = state?.binding;
  if (!binding) return "Jev · awaiting fresh admission";
  const e = binding.execution;
  const steered = e.quota_steered === true && e.intended_model && e.intended_model !== e.model_key
    ? ` (steered from ${e.intended_model}: quota)` : "";
  const mark = state.lastFeedback === "too_weak" ? " · [↑ more]" : state.lastFeedback === "too_strong" ? " · [↓ less]" : "";
  return `Jev ${state.alias ?? "auto"} · ${e.model_key} · ${e.initial_effort || "none"}${steered} · ${state.pending ? "outcome unresolved" : "strict"}${mark}`;
}

/** Owns identity only. No policy, prompt persistence, model fallback or automatic retry. */
export class StrictClient {
  constructor({ origin, piSessionId, adminToken, fetch: transport = globalThis.fetch, save, state,
                maxOutputTokens = DEFAULT_MAX_OUTPUT_TOKENS }) {
    let url;
    try { url = new URL(origin); }
    catch { throw new ContractError("Router URL is invalid."); }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new ContractError("Router URL must be an HTTP(S) origin without credentials, path or query.");
    }
    if (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens <= 0) {
      throw new ContractError("maxOutputTokens must be a positive whole number of tokens.");
    }
    this.origin = url.origin;
    this.adminToken = adminToken;
    this.fetch = transport;
    this.save = save;
    this.maxOutputTokens = maxOutputTokens;
    this.busy = false;
    this.resumed = false;
    this.state = state?.piSessionId === piSessionId && state?.origin === this.origin ? structuredClone(state) : {
      version: 1, piSessionId, origin: this.origin, sessionId: `pi-${randomUUID()}`, binding: null, pending: null, closed: false,
    };
  }
  persist() { this.save(structuredClone(this.state)); }
  async control(path, payload, signal) {
    if (!this.adminToken) throw new ContractError("Configure the router admin credential for the Pi integration.");
    const response = await this.fetch(this.origin + path, {
      method: "POST", redirect: "error", headers: {"Content-Type": "application/json", "X-Router-Admin-Token": this.adminToken},
      body: JSON.stringify(payload), signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000),
    });
    if (!response.ok) {
      const {code, retryAfterSeconds} = await readRefusal(response);
      throw new RouterError(code, retryAfterSeconds === undefined
        ? "No new binding was created by the client."
        : `No binding was created. The chosen model's quota resets in about ${waitText(retryAfterSeconds)}.`);
    }
    return response.json();
  }
  async bind(request, signal, alias = "auto") {
    if (this.state.closed) throw new ContractError("This binding is closed. Start a new Pi session.");
    if (!ALIASES.includes(alias)) throw new ContractError(`Unknown router alias; expected one of ${ALIASES.join(", ")}.`);
    if (this.state.binding) {
      // A binding made before aliases were recorded was made under `auto`.
      const bound = this.state.alias ?? "auto";
      const retired = RETIRED_ALIASES.includes(bound) && alias === "auto";
      if (bound !== alias && !retired) throw new ContractError(`This session is bound under ${bound}. Use /new to work under ${alias}.`);
      if (!this.resumed) {
        const value = await this.control("/router/resolve", {schema_version: "1", intent: "resume", session_id: this.state.sessionId, request_id: randomUUID(), client: "pi"}, signal);
        const binding = validateBinding(value, this.state.sessionId);
        if (binding.binding_revision !== this.state.binding.binding_revision) throw new ContractError("Resumed binding revision changed; refusing execution.");
        this.state.binding = binding;
        this.persist();
        this.resumed = true;
      }
      return this.state.binding;
    }
    // The transport-level half of the fresh-work rule: anything the model or
    // a tool produced means this conversation already ran somewhere.
    if (request.messages.some(message => !["system", "developer", "user"].includes(message.role))) {
      throw new ContractError("Strict auto requires fresh work. Imported/forked or previously started conversations cannot be admitted as new; use /new.");
    }
    // Persist the session ID before admission so an interrupted resolve cannot create a second binding.
    this.state.alias = alias;
    this.persist();
    const value = await this.control("/router/resolve", {
      schema_version: "1", intent: "new", session_id: this.state.sessionId, request_id: "admission", alias, client: "pi",
      client_contract: {dynamic_metadata: true, protocols: ["openai-chat"], fresh_execution_context: true, turn_boundary_reporting: false},
      request, limits: {max_output_tokens: this.maxOutputTokens},
    }, signal);
    this.state.binding = validateBinding(value, this.state.sessionId);
    this.persist();
    this.resumed = true;
    return this.state.binding;
  }
  startRequest() {
    if (this.busy) throw new ContractError("One execution at a time per Pi session.");
    if (this.state.pending) throw new ContractError(`Prior execution ${this.state.pending} has no client-confirmed completion. Automatic retry is blocked. Inspect /jev status; /jev retry explicitly permits a new attempt.`);
    if (this.state.closed) throw new ContractError("This binding is closed. Use /new.");
    this.busy = true;
  }
  executionHeaders() {
    if (!this.state.binding) throw new ContractError("No binding available.");
    if (!this.state.pending) {
      this.state.pending = randomUUID();
      // Kept after completion, so feedback can name the request it is about.
      this.state.lastRequestId = this.state.pending;
      this.persist(); // Must succeed before any inference is sent.
    }
    return {"X-Router-Client": "pi", "X-Router-Admin-Token": this.adminToken,
      "X-Router-Session": this.state.sessionId, "X-Router-Binding": this.state.binding.binding_revision,
      "X-Router-Request-Id": this.state.pending};
  }
  /**
   * A refused execution response. A code the router only raises before it
   * forwards clears the pending execution, because the provider never ran
   * that request id and the router will let it be retried. Any other code,
   * and any refusal that carries no code, leaves the block in place.
   * Returns whether the pending execution was cleared.
   */
  noteExecutionRefusal(code) {
    if (!PRE_ACCEPTANCE_CODES.has(code)) return false;
    if (this.state.pending) {
      this.state.pending = null;
      this.persist();
    }
    return true;
  }
  finish(completed) {
    try {
      if (completed) {
        const pending = this.state.pending;
        this.state.pending = null;
        try { this.persist(); } catch (error) { this.state.pending = pending; throw error; }
      }
    } finally { this.busy = false; }
  }
  acknowledgeNewAttempt() {
    if (this.busy) throw new ContractError("Wait for the current request to settle.");
    this.state.pending = null;
    this.persist();
  }
  /**
   * Tell the router what this session's routing felt like. `too_weak` and
   * `too_strong` also ask for one effort rung up or down on the same model
   * (`adjustEffort`, on by default for those two). The router decides; when
   * it moved the effort, the local contract takes the new effort and wire
   * model so the next request is sent at it. The model never changes.
   */
  async feedback(verdict, {note, adjustEffort, signal} = {}) {
    if (!["too_weak", "too_strong", "right", "too_slow"].includes(verdict)) throw new ContractError("Unknown feedback verdict.");
    const binding = this.state.binding;
    if (!binding) throw new ContractError("No binding yet. Feedback is about a bound session.");
    const payload = {verdict, adjust_effort: adjustEffort ?? (verdict === "too_weak" || verdict === "too_strong"), source: "pi"};
    if (this.state.lastRequestId) payload.request_id = this.state.lastRequestId;
    if (note) payload.note = String(note).slice(0, NOTE_MAX_CHARS);
    const value = await this.control(`/router/sessions/${encodeURIComponent(this.state.sessionId)}/feedback`, payload, signal);
    const e = binding.execution;
    const changed = value?.effort_changed === true && value.model_key === e.model_key
      && typeof value.current_effort === "string" && typeof value.wire_model === "string" && value.wire_model.length > 0;
    if (changed) {
      // A new object, so a request already holding the old contract keeps it.
      this.state.binding = {...binding, execution: {...e, initial_effort: value.current_effort, wire_model: value.wire_model}};
    }
    this.state.lastFeedback = verdict;
    this.persist();
    return {
      verdict, effortChanged: changed, modelKey: e.model_key,
      previousEffort: typeof value?.previous_effort === "string" ? value.previous_effort : e.initial_effort,
      currentEffort: changed ? value.current_effort : e.initial_effort,
      reasonCode: typeof value?.reason_code === "string" && /^[a-z_]{1,40}$/.test(value.reason_code) ? value.reason_code : null,
    };
  }
  async close(signal) {
    if (this.busy) throw new ContractError("Wait for the current request to settle.");
    if (this.state.binding) await this.control(`/router/sessions/${encodeURIComponent(this.state.sessionId)}/close`, {}, signal);
    this.state.closed = true;
    this.persist();
  }
}
