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
]);

/**
 * The machine code of a refused response, and nothing else from its body.
 * Provider and router message text is never read out, so it cannot reach a
 * transcript, a log line or an error message.
 */
export async function readErrorCode(response) {
  try {
    const body = await response.json();
    const code = body?.error?.code;
    if (typeof code === "string" && /^[A-Z][A-Z_]{2,63}$/.test(code)) return code;
  } catch { /* not a structured router error */ }
  return `HTTP_${response.status}`;
}

const CONTRACT_FIELDS = ["model_key", "wire_model", "protocol", "context_window",
  "max_output_tokens", "supports_tools", "supports_vision", "initial_effort", "effort_mode"];

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
  return {session_id: value.session_id, binding_revision: value.binding_revision, state: value.state,
    execution: Object.fromEntries(CONTRACT_FIELDS.map(key => [key, e[key]]))};
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
      throw new RouterError(await readErrorCode(response), "No new binding was created by the client.");
    }
    return response.json();
  }
  async bind(request, signal) {
    if (this.state.closed) throw new ContractError("This binding is closed. Start a new Pi session.");
    if (this.state.binding) {
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
    this.persist();
    const value = await this.control("/router/resolve", {
      schema_version: "1", intent: "new", session_id: this.state.sessionId, request_id: "admission", alias: "auto", client: "pi",
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
  async close(signal) {
    if (this.busy) throw new ContractError("Wait for the current request to settle.");
    if (this.state.binding) await this.control(`/router/sessions/${encodeURIComponent(this.state.sessionId)}/close`, {}, signal);
    this.state.closed = true;
    this.persist();
  }
}
