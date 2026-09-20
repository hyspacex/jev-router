import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { stream as streamChat } from "@earendil-works/pi-ai/api/openai-completions";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { StrictClient, ContractError } from "./client.mjs";

const PROVIDER = "jev-router";
const ENTRY = "jev-router.binding.v1";
const COST = {input: 0, output: 0, cacheRead: 0, cacheWrite: 0};

/** Thin Pi client. Fixed effort only; experimental Responses adaptation lives elsewhere. */
export default function (pi: ExtensionAPI) {
  let ctx: ExtensionContext | undefined;
  let client: StrictClient | undefined;
  let config: {origin: string; adminTokenFile?: string; adminTokenEnv?: string} | undefined;
  try {
    config = JSON.parse(readFileSync(join(homedir(), ".pi/agent/jev-router.json"), "utf8"));
  } catch { /* Report on use, not during unrelated provider startup. */ }
  const origin = config?.origin || "http://127.0.0.1:8318";
  const base = {
    id: "auto", name: "auto · strict session", reasoning: false,
    input: ["text", "image"] as ("text" | "image")[], cost: COST,
    contextWindow: 32768, maxTokens: 8192,
    compat: {supportsDeveloperRole: false, supportsReasoningEffort: false, supportsStrictMode: false, maxTokensField: "max_tokens" as const},
  };
  function status() {
    if (!ctx) return;
    const binding = client?.state.binding;
    if (binding && ctx.model?.provider === PROVIDER) {
      ctx.model.contextWindow = binding.execution.context_window;
      ctx.model.maxTokens = binding.execution.max_output_tokens;
      ctx.model.input = binding.execution.supports_vision ? ["text", "image"] : ["text"];
    }
    ctx.ui.setStatus("jev-router", ctx.model?.provider === PROVIDER
      ? binding ? `Jev · ${binding.execution.model_key} · ${binding.execution.initial_effort || "none"} · ${client?.state.pending ? "outcome unresolved" : "strict"}` : "Jev · awaiting fresh admission"
      : undefined);
  }
  function initialize(context: ExtensionContext) {
    ctx = context;
    const piSessionId = ctx.sessionManager.getSessionId();
    const entries = ctx.sessionManager.getEntries();
    const saved = [...entries].reverse().find(entry => entry.type === "custom" && entry.customType === ENTRY && (entry.data as any)?.piSessionId === piSessionId);
    let adminToken = process.env[config?.adminTokenEnv || "JEV_ROUTER_ADMIN_TOKEN"] || "";
    if (config?.adminTokenFile) {
      try { adminToken = readFileSync(config.adminTokenFile, "utf8").trim(); }
      catch { throw new ContractError("Cannot read the configured router admin token file."); }
    }
    client = new StrictClient({origin, piSessionId, adminToken, state: saved?.type === "custom" ? saved.data : undefined,
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
      else context.ui.notify(JSON.stringify({session: client!.state.sessionId, binding: client!.state.binding?.execution, pending: client!.state.pending, closed: client!.state.closed}, null, 2), "info");
      status();
    },
  });
  pi.registerProvider(PROVIDER, {
    baseUrl: `${origin}/v1`, api: "openai-completions", models: [base],
    streamSimple(model, transcript, options) {
      const output = createAssistantMessageEventStream();
      const active = client;
      void (async () => {
        let acquired = false;
        try {
          if (!active || !ctx) throw new ContractError("Pi session is not initialized.");
          if (!active.state.binding && (ctx.sessionManager.getHeader()?.parentSession || ctx.sessionManager.getEntries().some(entry => entry.type === "compaction" || entry.type === "branch_summary"))) {
            throw new ContractError("Strict auto cannot admit a fork or compacted import as fresh work. Use /new.");
          }
          active.startRequest(); acquired = true;
          // Use Pi's serializer for admission; the probe is forbidden from doing network I/O.
          let request: any;
          const probe = streamChat({...model, ...base, provider: PROVIDER, api: "openai-completions"}, transcript, {
            ...options, maxRetries: 0, maxTokens: 8192,
            fetch: async () => { throw new ContractError("Admission serializer attempted network I/O."); },
            onPayload: (payload) => { request = payload; throw new ContractError("Serialization complete"); },
          });
          await probe.result();
          if (!request) throw new ContractError("Could not serialize admission request.");
          const binding = await active.bind(request, options?.signal);
          const e = binding.execution;
          const boundModel = {...model, ...base, provider: PROVIDER, api: "openai-completions" as const,
            id: e.wire_model, baseUrl: `${origin}/v1`, contextWindow: e.context_window, maxTokens: e.max_output_tokens,
            input: e.supports_vision ? ["text", "image"] as ("text" | "image")[] : ["text"] as ("text" | "image")[]};
          // Reflect negotiated limits to Pi before inference; transport never uses the placeholder limits.
          model.contextWindow = e.context_window;
          model.maxTokens = e.max_output_tokens;
          model.input = boundModel.input;
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
              return globalThis.fetch(input, {...init, headers, redirect: "error"});
            },
          });
          let completed = false;
          for await (const event of actual) {
            if (event.type === "done") {
              completed = true;
              active.finish(true); acquired = false;
              event.message.responseModel = e.wire_model;
              event.message.model = "auto";
            }
            if (event.type === "error") { active.finish(false); acquired = false; }
            output.push(event);
          }
          if (acquired) { active.finish(completed); acquired = false; }
          output.end();
        } catch (error) {
          if (acquired) { active?.finish(false); acquired = false; }
          const message = {role: "assistant" as const, content: [], api: model.api, provider: PROVIDER, model: "auto",
            usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: {...COST, total: 0}},
            stopReason: "error" as const, timestamp: Date.now(),
            errorMessage: error instanceof ContractError ? error.message : "Strict router integration failed; no automatic model replacement. Inspect /jev status."};
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
