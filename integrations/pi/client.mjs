import { randomUUID } from "node:crypto";

export class ContractError extends Error {}

export function validateBinding(value, sessionId) {
  const e = value?.execution;
  if (value?.session_id !== sessionId || typeof value.binding_revision !== "string" ||
      !e || e.protocol !== "openai-chat" || e.effort_mode !== "fixed" ||
      typeof e.wire_model !== "string" || !e.wire_model ||
      !Number.isSafeInteger(e.context_window) || e.context_window <= 0 ||
      !Number.isSafeInteger(e.max_output_tokens) || e.max_output_tokens <= 0 ||
      typeof e.supports_tools !== "boolean" || typeof e.supports_vision !== "boolean") {
    throw new ContractError("Unsupported router binding; this integration requires fixed-effort openai-chat.");
  }
  if (!["prepared", "active"].includes(value.state)) throw new ContractError(`Binding is ${value.state}; it cannot execute.`);
  // Persist only the contract, never arbitrary fields from a control response.
  return {session_id: value.session_id, binding_revision: value.binding_revision, state: value.state,
    execution: Object.fromEntries(["model_key", "wire_model", "protocol", "context_window", "max_output_tokens", "supports_tools", "supports_vision", "initial_effort", "effort_mode"].map(key => [key, e[key]]))};
}

/** Owns identity only. No policy, prompt persistence, model fallback or automatic retry. */
export class StrictClient {
  constructor({ origin, piSessionId, adminToken, fetch: transport = globalThis.fetch, save, state }) {
    let url;
    try { url = new URL(origin); }
    catch { throw new ContractError("Router URL is invalid."); }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
      throw new ContractError("Router URL must be an HTTP(S) origin without credentials, path or query.");
    }
    this.origin = url.origin;
    this.adminToken = adminToken;
    this.fetch = transport;
    this.save = save;
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
      let code = `HTTP_${response.status}`;
      try { const body = await response.json(); code = body?.error?.code || code; } catch { /* no provider error text retained */ }
      throw new ContractError(`Router refused control request: ${code}. No new binding was created by the client.`);
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
    if (request.messages.some(message => !["system", "developer", "user"].includes(message.role))) {
      throw new ContractError("Strict auto requires fresh work. Imported/forked or previously started conversations cannot be admitted as new; use /new.");
    }
    // Persist the session ID before admission so an interrupted resolve cannot create a second binding.
    this.persist();
    const value = await this.control("/router/resolve", {
      schema_version: "1", intent: "new", session_id: this.state.sessionId, request_id: "admission", alias: "auto", client: "pi",
      client_contract: {dynamic_metadata: true, protocols: ["openai-chat"], fresh_execution_context: true, turn_boundary_reporting: false},
      request, limits: {max_output_tokens: 8192},
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
