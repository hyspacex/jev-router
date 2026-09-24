// Types for client.mjs. The runtime file stays plain JavaScript so the tests
// run under `node --test` without the Pi packages being loadable.

export declare class ContractError extends Error {}

/** A refusal the router named with one of its own machine codes. */
export declare class RouterError extends ContractError {
  readonly code: string;
  constructor(code: string, note: string);
}

export declare const DEFAULT_MAX_OUTPUT_TOKENS: number;

/** The strict aliases the picker offers. */
export declare const ALIASES: readonly string[];
/** Aliases still served for sessions bound under them, no longer offered. */
export declare const RETIRED_ALIASES: readonly string[];

/** Codes that mean the provider never saw the request. */
export declare const PRE_ACCEPTANCE_CODES: ReadonlySet<string>;

/** The machine code of a refused response and the router's retry wait, if any. */
export declare function readRefusal(response: Response): Promise<{code: string; retryAfterSeconds?: number}>;

/** The machine code of a refused response, and nothing else from its body. */
export declare function readErrorCode(response: Response): Promise<string>;

export interface BindingExecution {
  model_key: string;
  wire_model: string;
  protocol: "openai-chat";
  context_window: number;
  max_output_tokens: number;
  supports_tools: boolean;
  supports_vision: boolean;
  initial_effort: string | null;
  effort_mode: "fixed";
}

export interface Binding {
  session_id: string;
  binding_revision: string;
  state: "prepared" | "active";
  execution: BindingExecution;
}

export interface ClientState {
  version: number;
  piSessionId: string;
  origin: string;
  sessionId: string;
  /** The alias the binding was admitted under; absent means `auto`. */
  alias?: string;
  binding: Binding | null;
  pending: string | null;
  closed: boolean;
}

export declare function validateBinding(value: unknown, sessionId: string): Binding;

export declare class StrictClient {
  constructor(options: {
    origin: string;
    piSessionId: string;
    adminToken?: string;
    fetch?: typeof globalThis.fetch;
    save?: (state: ClientState) => void;
    state?: ClientState;
    maxOutputTokens?: number;
  });
  readonly origin: string;
  readonly state: ClientState;
  maxOutputTokens: number;
  busy: boolean;
  resumed: boolean;
  persist(): void;
  control(path: string, payload: unknown, signal?: AbortSignal): Promise<any>;
  bind(request: {messages: {role: string}[]}, signal?: AbortSignal, alias?: string): Promise<Binding>;
  startRequest(): void;
  executionHeaders(): Record<string, string>;
  /** True when the pending execution was released. */
  noteExecutionRefusal(code: string): boolean;
  finish(completed: boolean): void;
  acknowledgeNewAttempt(): void;
  close(signal?: AbortSignal): Promise<void>;
}
