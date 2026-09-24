import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { stream as streamChat } from "@earendil-works/pi-ai/api/openai-completions";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { StrictClient, ContractError, RouterError, readErrorCode, ALIASES, DEFAULT_MAX_OUTPUT_TOKENS } from "./client.mjs";

const PROVIDER = "jev-router";
const ENTRY = "jev-router.binding.v1";
const COST = {input: 0, output: 0, cacheRead: 0, cacheWrite: 0};

type PiRouterConfig = {
  origin?: string;
  adminTokenFile?: string;
  adminTokenEnv?: string;
  maxOutputTokens?: number;
};

/** Where the extension config lives. `JEV_ROUTER_PI_CONFIG` overrides it. */
export function configPath(): string {
  return process.env.JEV_ROUTER_PI_CONFIG || join(homedir(), ".pi/agent/jev-router.json");
}

function readConfig(): PiRouterConfig | undefined {
  try { return JSON.parse(readFileSync(configPath(), "utf8")); }
  catch { return undefined; } // Report on use, not during unrelated provider startup.
}

/** The negotiated profile, reflected onto whatever model object Pi holds. */
function applyBinding(model: any, execution: any) {
  model.contextWindow = execution.context_window;
  model.maxTokens = execution.max_output_tokens;
  model.input = execution.supports_vision ? ["text", "image"] : ["text"];
}

/** Thin Pi client. Fixed effort only; experimental Responses adaptation lives elsewhere. */
export default function (pi: ExtensionAPI) {
  let ctx: ExtensionContext | undefined;
  let client: StrictClient | undefined;
  const config = readConfig();
  const origin = config?.origin || "http://127.0.0.1:8318";
  const configuredOutput = config?.maxOutputTokens;
  // The placeholder the picker shows before admission. A bad value is
  // refused by the client when the session is initialized, not here, so one
  // typo cannot stop unrelated providers loading.
  const placeholderOutput = Number.isSafeInteger(configuredOutput) && (configuredOutput as number) > 0
    ? configuredOutput as number : DEFAULT_MAX_OUTPUT_TOKENS;
  // The limits the picker shows before admission.
  const placeholder = {input: ["text", "image"] as ("text" | "image")[], contextWindow: 32768, maxTokens: placeholderOutput};
  const NAMES: Record<string, string> = {"auto": "auto · strict session"};
  // One picker entry per strict alias; the entry chosen is the alias admitted.
  const models = ALIASES.map(id => ({
    id, name: NAMES[id] ?? id, reasoning: false, cost: COST, ...placeholder,
    compat: {supportsDeveloperRole: false, supportsReasoningEffort: false, supportsStrictMode: false, maxTokensField: "max_tokens" as const},
  }));
  function status() {
    if (!ctx) return;
    const binding = client?.state.binding;
    if (binding && ctx.model?.provider === PROVIDER) applyBinding(ctx.model, binding.execution);
    ctx.ui.setStatus("jev-router", ctx.model?.provider === PROVIDER
      ? binding ? `Jev ${client?.state.alias ?? "auto"} · ${binding.execution.model_key} · ${binding.execution.initial_effort || "none"} · ${client?.state.pending ? "outcome unresolved" : "strict"}` : "Jev · awaiting fresh admission"
      : undefined);
  }
  function initialize(context: ExtensionContext) {
    ctx = context;
    const piSessionId = ctx.sessionManager.getSessionId();
    const entries = ctx.sessionManager.getEntries();
    const saved = [...entries].reverse().find(entry => entry.type === "custom" && entry.customType === ENTRY && (entry.data as any)?.piSessionId === piSessionId);
    // The environment variable wins. A configured token file is the fallback,
    // so a test run or a one-off can hold the deployment's file at arm's
    // length instead of reading the real credential.
    let adminToken = process.env[config?.adminTokenEnv || "JEV_ROUTER_ADMIN_TOKEN"] || "";
    if (!adminToken && config?.adminTokenFile) {
      try { adminToken = readFileSync(config.adminTokenFile, "utf8").trim(); }
      catch { throw new ContractError("Cannot read the configured router admin token file."); }
    }
    client = new StrictClient({origin, piSessionId, adminToken, state: saved?.type === "custom" ? saved.data : undefined,
      maxOutputTokens: configuredOutput ?? DEFAULT_MAX_OUTPUT_TOKENS,
      save: (state: unknown) => pi.appendEntry(ENTRY, state)});
    status();
  }
  pi.on("session_start", (_event, context) => initialize(context));
  pi.on("model_select", (_event, context) => { ctx = context; status(); });
  pi.on("cache_warming_decision", (_event, context) => context.model?.provider === PROVIDER ? {action: "stop"} : undefined);
  pi.on("session_before_compact", (_event, context) => {
    if (context.model?.provider !== PROVIDER) return;
    context.ui.notify("Strict auto: client compaction is not qualified in this version. Start new work explicitly; the binding is unchanged.", "warning");
    return {cancel: true};
  });
  pi.on("session_before_tree", (_event, context) => {
    if (context.model?.provider !== PROVIDER) return;
    context.ui.notify("Strict auto does not yet support tree rewinds. Use a new session for independent work.", "warning");
    return {cancel: true};
  });
  pi.registerCommand("jev", {
    description: "Strict router: status, audit, retry (explicit new attempt), close",
    handler: async (args, context) => {
      if (!client) initialize(context);
      if (args.trim() === "retry") {
        if (!context.isIdle()) throw new ContractError("Wait for the agent to settle.");
        if (!context.hasUI || !await context.ui.confirm("Permit a new execution attempt?", "The previous request may have run. This does not reconcile its outcome and can duplicate work.")) return;
        client!.acknowledgeNewAttempt();
      } else if (args.trim() === "close") {
        if (!context.isIdle()) throw new ContractError("Wait for the agent to settle.");
        if (!context.hasUI || !await context.ui.confirm("Close strict binding?", "Future execution requires a new Pi session.")) return;
        await client!.close();
      } else if (args.trim() === "audit") context.ui.notify(`${origin}/dashboard`, "info");
      else context.ui.notify(JSON.stringify({session: client!.state.sessionId, alias: client!.state.alias ?? "auto", binding: client!.state.binding?.execution, pending: client!.state.pending, closed: client!.state.closed}, null, 2), "info");
      status();
    },
  });
  pi.registerProvider(PROVIDER, {
    baseUrl: `${origin}/v1`, api: "openai-completions", models,
    streamSimple(model, transcript, options) {
      const output = createAssistantMessageEventStream();
      const active = client;
      // A refusal the router named. Kept here because the stream may report
      // it as an event rather than raise it out of the fetch call.
      let refusal: RouterError | undefined;
      void (async () => {
        let acquired = false;
        try {
          if (!active || !ctx) throw new ContractError("Pi session is not initialized.");
          // The Pi-side half of the fresh-work rule. The client cannot see
          // this lineage; it only sees the messages.
          if (!active.state.binding && (ctx.sessionManager.getHeader()?.parentSession || ctx.sessionManager.getEntries().some(entry => entry.type === "compaction" || entry.type === "branch_summary"))) {
            throw new ContractError("Strict auto cannot admit a fork or compacted import as fresh work. Use /new.");
          }
          active.startRequest(); acquired = true;
          // Use Pi's serializer for admission; the probe is forbidden from doing network I/O.
          // `placeholder` restores the unbound limits, so admission is
          // serialized against them rather than a previous binding's.
          let request: any;
          const probe = streamChat({...model, ...placeholder, provider: PROVIDER, api: "openai-completions"}, transcript, {
            ...options, maxRetries: 0, maxTokens: placeholder.maxTokens,
            fetch: async () => { throw new ContractError("Admission serializer attempted network I/O."); },
            onPayload: (payload) => { request = payload; throw new ContractError("Serialization complete"); },
          });
          await probe.result();
          if (!request) throw new ContractError("Could not serialize admission request.");
          const binding = await active.bind(request, options?.signal, model.id);
          const e = binding.execution;
          const boundModel = {...model, provider: PROVIDER, api: "openai-completions" as const,
            id: e.wire_model, baseUrl: `${origin}/v1`};
          applyBinding(boundModel, e);
          // Reflect negotiated limits to Pi before inference; transport never uses the placeholder limits.
          applyBinding(model, e);
          status();
          const actual = streamChat(boundModel, transcript, {
            ...options, maxRetries: 0, maxTokens: e.max_output_tokens,
            onPayload: async (payload, selectedModel) => {
              const changed: any = await options?.onPayload?.(payload, selectedModel) ?? payload;
              if (changed.model !== e.wire_model) throw new ContractError("Another extension changed the bound model.");
              return changed;
            },
            fetch: async (input, init) => {
              const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
              if (url !== `${origin}/v1/chat/completions`) throw new ContractError("Refusing unexpected execution destination.");
              const headers = new Headers(init?.headers);
              for (const [key, value] of Object.entries(active.executionHeaders())) headers.set(key, String(value));
              const response = await globalThis.fetch(input, {...init, headers, redirect: "error"});
              if (!response.ok) {
                // Only the machine code is read. A code the router raises
                // before it forwards frees the request id; anything else
                // leaves an outcome nobody here can state.
                const code = await readErrorCode(response);
                refusal = new RouterError(code, active.noteExecutionRefusal(code)
                  ? "The provider never ran this request, so it was released and the next one may go ahead."
                  : "The outcome of this request is unresolved, so automatic retry is blocked. Inspect /jev status.");
                throw refusal;
              }
              return response;
            },
          });
          let completed = false;
          for await (const event of actual) {
            if (event.type === "done") {
              completed = true;
              active.finish(true); acquired = false;
              event.message.responseModel = e.wire_model;
              event.message.model = model.id;
            }
            if (event.type === "error") {
              active.finish(false); acquired = false;
              // The stream reports a refused fetch as a generic connection
              // error. Put the router's own code back in its place.
              if (refusal && event.error) event.error.errorMessage = refusal.message;
            }
            output.push(event);
          }
          if (acquired) { active.finish(completed); acquired = false; }
          output.end();
        } catch (error) {
          if (acquired) { active?.finish(false); acquired = false; }
          const named = refusal ?? (error instanceof ContractError ? error : undefined);
          const message = {role: "assistant" as const, content: [], api: model.api, provider: PROVIDER, model: model.id,
            usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: {...COST, total: 0}},
            stopReason: "error" as const, timestamp: Date.now(),
            errorMessage: named ? named.message : "Strict router integration failed; no automatic model replacement. Inspect /jev status."};
          output.push({type: "error", reason: "error", error: message});
          output.end();
        } finally {
          if (acquired) active?.finish(false);
          status();
        }
      })();
      return output;
    },
  });
}
