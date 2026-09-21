// The resolve response the router actually sends: `sessions.contract()` in
// src/jev_router/sessions.py, returned by `app.resolve`. Tests feed this
// shape, not a narrowed copy of what the client already persisted.
export function contract(sessionId, overrides = {}) {
  const {execution, ...rest} = overrides;
  const value = {
    schema_version: "1",
    session_id: sessionId,
    state: "prepared",
    binding_revision: "sha256:0d1f2a3b4c5d6e7f",
    decision_id: "d-20260919-0001",
    execution: {
      model_key: "big",
      provider: "upstream",
      wire_model: "big(high)",
      protocol: "openai-chat",
      context_window: 128000,
      max_output_tokens: 8192,
      supports_tools: true,
      supports_vision: false,
      initial_effort: "high",
      effort_mode: "fixed",
      base_effort: "high",
      effective_effort: "high",
      adaptation: "off",
      compaction_epoch: 0,
      ...execution,
    },
    decision: {
      rule: "hard_work",
      source: "jev",
      quality_lane: "tool_coding",
      quota_changed_choice: false,
      quota_status: {upstream: "fresh"},
    },
    limits: {
      estimated_input_tokens: 1204,
      estimate_method: "serialized_v1",
      reserve_tokens: 4096,
    },
    ...rest,
  };
  // `execution: null` drops the block entirely, for the malformed cases.
  if (execution === null) delete value.execution;
  return value;
}

/** A router error body: `SessionError.body()` in src/jev_router/sessions.py. */
export function sessionError(code, status) {
  return Response.json(
    {error: {type: "session_error", code, message: "PRIVATE-ROUTER-DETAIL", session_id: "s-1"}},
    {status},
  );
}
