// Transport facts only: the real Pi serializer, the real stream parser and
// the wire the extension puts between them. Client-state rules (resume,
// message-role refusal, the retry block, request-id rotation) are tested
// directly in client.test.mjs and are not repeated here.
import './env.mjs';
import {FIXTURE_ORIGIN, FIXTURE_MAX_OUTPUT_TOKENS, withRouter} from './env.mjs';
import {contract, sessionError} from './fixtures/contract.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import extension, {configPath} from '../index.ts';

function harness({entries = [], sessionId = 'pi-test', header = {}} = {}) {
  const hooks = {}, commands = {};
  let provider;
  const pi = {
    on: (event, handler) => { hooks[event] = handler; },
    registerCommand: (name, value) => { commands[name] = value; },
    registerProvider: (_name, value) => { provider = value; },
    appendEntry: (customType, data) => entries.push({type: 'custom', customType, data: structuredClone(data)}),
  };
  extension(pi);
  const model = {...provider.models[0], provider: 'jev-router', api: 'openai-completions', baseUrl: provider.baseUrl};
  const ctx = {model, hasUI: true, isIdle: () => true, ui: {setStatus(){}, notify(){}, confirm: async () => true},
    sessionManager: {getSessionId: () => sessionId, getEntries: () => entries, getHeader: () => header}};
  hooks.session_start({}, ctx);
  const saved = () => [...entries].reverse().find(e => e.type === 'custom')?.data;
  return {provider, model, hooks, ctx, commands, entries, saved};
}

function textResponse(tool = false) {
  const chunks = [
    {id: 'chat-1', object: 'chat.completion.chunk', choices: [{index: 0, delta: tool ? {tool_calls: [{index: 0, id: 'call-one', type: 'function', function: {name: 'read', arguments: '{"path":"a"}'}}]} : {content: 'OK'}, finish_reason: null}]},
    {id: 'chat-1', object: 'chat.completion.chunk', choices: [{index: 0, delta: {}, finish_reason: tool ? 'tool_calls' : 'stop'}]},
  ];
  return new Response(chunks.map(v => `data: ${JSON.stringify(v)}\n\n`).join('') + 'data: [DONE]\n\n',
    {headers: {'content-type': 'text/event-stream'}});
}

/** A router that always admits, and answers execution with `execute`. */
function routerStub(execute = () => textResponse()) {
  const calls = [];
  const stub = async (input, init) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(init.body) : {};
    const headers = new Headers(init?.headers);
    calls.push({url, body, headers});
    if (url.endsWith('/router/resolve')) return Response.json(contract(body.session_id));
    return execute({url, body, headers, index: calls.length});
  };
  return {calls, stub};
}

const transcript = {messages: [
  {role: 'system', content: 'Be helpful', toolsAdded: [{name: 'read', description: 'Read a file', parameters: {type: 'object', properties: {path: {type: 'string'}}}}], timestamp: 1},
  {role: 'user', content: 'Read a', timestamp: 2},
]};

test('the Pi serializer produces the admission request and the bound execution', async () => {
  const {calls, stub} = routerStub(() => textResponse(true));
  await withRouter(stub, async () => {
    const h = harness();
    const reply = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(calls.length, 2);

    const admission = calls[0];
    assert.equal(admission.url, `${FIXTURE_ORIGIN}/router/resolve`);
    assert.equal(admission.body.intent, 'new');
    assert.equal(admission.body.alias, 'auto');
    assert.equal(admission.body.client, 'pi');
    assert.ok(admission.body.request.tools.length, 'tool schemas reach admission');
    assert.equal(admission.body.client_contract.turn_boundary_reporting, false);

    const execution = calls[1];
    assert.equal(execution.url, `${FIXTURE_ORIGIN}/v1/chat/completions`);
    assert.equal(execution.headers.get('x-router-admin-token'), 'control-test');
    assert.equal(execution.headers.get('authorization'), 'Bearer upstream-test');
    assert.equal(execution.headers.get('x-router-client'), 'pi');
    assert.equal(execution.headers.get('x-router-binding'), 'sha256:0d1f2a3b4c5d6e7f');
    assert.equal(execution.body.model, 'big(high)');
    assert.equal(execution.body.max_tokens, 8192, 'the negotiated ceiling, not the placeholder');
    assert.equal(execution.body.reasoning_effort, undefined, 'effort is the router\'s, not the client\'s');

    assert.equal(reply.stopReason, 'toolUse');
    assert.equal(reply.content[0].name, 'read');
    assert.equal(reply.model, 'auto');
    assert.equal(reply.responseModel, 'big(high)');
  });
});

test('the configured output ceiling is what admission asks for', async () => {
  const {calls, stub} = routerStub();
  await withRouter(stub, async () => {
    const h = harness();
    assert.equal(h.provider.models[0].maxTokens, FIXTURE_MAX_OUTPUT_TOKENS);
    await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(calls[0].body.limits.max_output_tokens, FIXTURE_MAX_OUTPUT_TOKENS);
  });
});

test('negotiated limits replace the picker placeholders', async () => {
  const {stub} = routerStub();
  await withRouter(stub, async () => {
    const h = harness();
    assert.equal(h.model.contextWindow, 32768);
    assert.deepEqual(h.model.input, ['text', 'image']);
    await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(h.model.contextWindow, 128000);
    assert.equal(h.model.maxTokens, 8192);
    assert.deepEqual(h.model.input, ['text'], 'the bound profile has no vision');
  });
});

test('the config override decides the origin, so no request can reach HOME', async () => {
  const {calls, stub} = routerStub();
  await withRouter(stub, async () => {
    const h = harness();
    assert.equal(h.provider.baseUrl, `${FIXTURE_ORIGIN}/v1`);
    await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.ok(calls.length);
    for (const call of calls) assert.ok(call.url.startsWith(`${FIXTURE_ORIGIN}/`), call.url);
  });
});

test('JEV_ROUTER_PI_CONFIG replaces the path under HOME', () => {
  assert.equal(configPath(), process.env.JEV_ROUTER_PI_CONFIG);
  const previous = process.env.JEV_ROUTER_PI_CONFIG;
  delete process.env.JEV_ROUTER_PI_CONFIG;
  try { assert.match(configPath(), /\.pi[/\\]agent[/\\]jev-router\.json$/); }
  finally { process.env.JEV_ROUTER_PI_CONFIG = previous; }
});

test('the admin credential comes from the environment first, the token file second', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'jev-pi-'));
  const tokenFile = join(dir, 'router-token');
  const configFile = join(dir, 'jev-router.json');
  writeFileSync(tokenFile, 'FILE-TOKEN\n');
  writeFileSync(configFile, JSON.stringify({origin: FIXTURE_ORIGIN, adminTokenFile: tokenFile}));
  const previousConfig = process.env.JEV_ROUTER_PI_CONFIG;
  const previousToken = process.env.JEV_ROUTER_ADMIN_TOKEN;
  const previousFetch = globalThis.fetch;
  process.env.JEV_ROUTER_PI_CONFIG = configFile;
  try {
    const env = routerStub();
    await withRouter(env.stub, async () => {
      const h = harness();
      await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
      assert.equal(env.calls[1].headers.get('x-router-admin-token'), 'control-test');
      const sent = JSON.stringify(env.calls.map(call => [...call.headers]));
      assert.equal(sent.includes('FILE-TOKEN'), false, 'the token file never overrides the environment');
    });
    // With no environment credential, the configured file is the fallback.
    const file = routerStub();
    delete process.env.JEV_ROUTER_ADMIN_TOKEN;
    globalThis.fetch = file.stub;
    const h = harness();
    await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(file.calls[1].headers.get('x-router-admin-token'), 'FILE-TOKEN');
  } finally {
    globalThis.fetch = previousFetch;
    process.env.JEV_ROUTER_PI_CONFIG = previousConfig;
    if (previousToken === undefined) delete process.env.JEV_ROUTER_ADMIN_TOKEN;
    else process.env.JEV_ROUTER_ADMIN_TOKEN = previousToken;
    rmSync(dir, {recursive: true, force: true});
  }
});

test('a forked Pi session is refused before admission', async () => {
  const {calls, stub} = routerStub();
  await withRouter(stub, async () => {
    const h = harness({header: {parentSession: 'parent-session'}});
    const reply = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(reply.stopReason, 'error');
    assert.match(reply.errorMessage, /fork or compacted import/);
    assert.equal(calls.length, 0, 'nothing is sent for a fork');
  });
});

test('a pre-acceptance refusal names the code and releases the request id', async () => {
  const {calls, stub} = routerStub(({index}) =>
    index === 2 ? sessionError('CONTEXT_BUDGET_EXCEEDED', 422) : textResponse());
  await withRouter(stub, async () => {
    const h = harness();
    const refused = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(refused.stopReason, 'error');
    assert.match(refused.errorMessage, /CONTEXT_BUDGET_EXCEEDED/);
    assert.doesNotMatch(refused.errorMessage, /PRIVATE-ROUTER-DETAIL/, 'router message text is never repeated');
    assert.doesNotMatch(refused.errorMessage, /control-test/, 'credentials never reach an error');
    assert.equal(h.saved().pending, null, 'the provider never ran it, so the id is free');

    const after = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(after.stopReason, 'stop');
    assert.equal(calls.length, 3, 'the next request goes out without a manual /jev retry');
  });
});

test('an upstream failure the router passed through keeps the block', async () => {
  const {calls, stub} = routerStub(() => new Response('{"error":{"message":"PRIVATE-ROUTER-DETAIL"}}', {status: 503}));
  await withRouter(stub, async () => {
    const h = harness();
    const failed = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.equal(failed.stopReason, 'error');
    assert.match(failed.errorMessage, /HTTP_503/);
    assert.doesNotMatch(failed.errorMessage, /PRIVATE-ROUTER-DETAIL/);
    assert.ok(h.saved().pending, 'an unresolved outcome keeps its request id');

    const blocked = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.match(blocked.errorMessage, /Automatic retry is blocked/);
    assert.equal(calls.length, 2, 'nothing is resubmitted');
  });
});

test('EXECUTION_OUTCOME_UNKNOWN keeps the block', async () => {
  const {calls, stub} = routerStub(() => sessionError('EXECUTION_OUTCOME_UNKNOWN', 409));
  await withRouter(stub, async () => {
    const h = harness();
    const unknown = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.match(unknown.errorMessage, /EXECUTION_OUTCOME_UNKNOWN/);
    assert.match(unknown.errorMessage, /unresolved/);
    assert.ok(h.saved().pending);
    const blocked = await h.provider.streamSimple(h.model, transcript, {apiKey: 'upstream-test'}).result();
    assert.match(blocked.errorMessage, /Automatic retry is blocked/);
    assert.equal(calls.length, 2);
  });
});

test('compaction is cancelled for this provider', async () => {
  const {stub} = routerStub();
  await withRouter(stub, async () => {
    const h = harness();
    assert.deepEqual(h.hooks.session_before_compact({}, h.ctx), {cancel: true});
    assert.deepEqual(h.hooks.session_before_tree({}, h.ctx), {cancel: true});
  });
});
