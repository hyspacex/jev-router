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
  /** Quota pressure moved this binding off what the rules chose. */
  quota_steered?: boolean;
  /** What the rules chose before quota had a say. */
  intended_model?: string;
  intended_effort?: string;
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
  /** The newest execution request id, kept after it completes, for feedback. */
  lastRequestId?: string;
  /** The newest verdict this session sent. */
  lastFeedback?: Verdict;
}

export type Verdict = "too_weak" | "too_strong" | "right" | "too_slow";

/** `/jev <word>` to the verdict it records. */
export declare const FEEDBACK_WORDS: Readonly<Record<string, Verdict>>;
/** The picker's labels and the verdict each records, in display order. */
export declare const FEEDBACK_CHOICES: readonly (readonly [string, Verdict])[];
export declare const NOTE_MAX_CHARS: number;

export declare function parseFeedbackCommand(args: string):
  | {picker: true}
  | {verdict: Verdict; note?: string}
  | null;

export interface FeedbackResult {
  verdict: Verdict;
  effortChanged: boolean;
  modelKey: string;
  previousEffort: string | null;
  currentEffort: string | null;
  /** Why the effort did not move: `at_highest`, `at_lowest`, `session_closed`, ... */
  reasonCode: string | null;
}

export declare function feedbackMessage(result: FeedbackResult): string;
export declare function statusLine(state: ClientState | undefined): string;

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
  feedback(verdict: Verdict | string, options?: {note?: string; adjustEffort?: boolean; signal?: AbortSignal}): Promise<FeedbackResult>;
  close(signal?: AbortSignal): Promise<void>;
}
