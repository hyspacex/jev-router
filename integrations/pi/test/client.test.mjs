import './env.mjs';
import {contract} from './fixtures/contract.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StrictClient, validateBinding, ContractError } from '../client.mjs';

const fresh = {messages: [{role: 'user', content: 'test'}]};

function fixture(state, {respond} = {}) {
  const calls = [], saved = [];
  const client = new StrictClient({origin: 'http://127.0.0.1:8318', piSessionId: 'pi-one', adminToken: 'test-only', state,
    save: s => saved.push(s), fetch: async (url, init) => {
      const body = JSON.parse(init.body); calls.push({url, body, headers: init.headers});
      return respond ? respond(body) : Response.json(contract(body.session_id));
    }});
  return {client, calls, saved};
}

test('resolve once; execution IDs stable until completion; no text persisted', async () => {
  const {client, calls, saved} = fixture();
  client.startRequest(); await client.bind(fresh);
  const h = client.executionHeaders();
  assert.deepEqual(client.executionHeaders(), h);
  assert.equal(h['X-Router-Client'], 'pi');
  client.finish(true);
  client.startRequest(); await client.bind(fresh);
  assert.notEqual(client.executionHeaders()['X-Router-Request-Id'], h['X-Router-Request-Id']);
  client.finish(true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.alias, 'auto');
  assert.equal(JSON.stringify(saved).includes('"content"'), false);
  assert.equal(JSON.stringify(saved).includes('test-only'), false);
});

test('reload resumes, never admits again', async () => {
  const first = fixture(); await first.client.bind(fresh);
  const second = fixture(first.client.state); await second.client.bind(fresh);
  assert.equal(second.calls[0].body.intent, 'resume');
  assert.equal(second.calls[0].body.request, undefined);
});

test('fork does not inherit binding and imported history is refused', async () => {
  const first = fixture(); await first.client.bind(fresh);
  const fork = fixture({...first.client.state, piSessionId: 'parent'});
  assert.notEqual(fork.client.state.sessionId, first.client.state.sessionId);
  await assert.rejects(fork.client.bind({messages: [{role: 'assistant', content: 'history'}]}), /fresh work/);
  assert.equal(fork.calls.length, 0);
});

test('unknown completion blocks both same-process and restarted retries', async () => {
  const f = fixture(); f.client.startRequest(); await f.client.bind(fresh); f.client.executionHeaders(); f.client.finish(false);
  assert.throws(() => f.client.startRequest(), /Automatic retry is blocked/);
  const resumed = fixture(f.client.state);
  assert.throws(() => resumed.client.startRequest(), /Automatic retry is blocked/);
  resumed.client.acknowledgeNewAttempt(); resumed.client.startRequest(); resumed.client.finish(false);
});

test('concurrent executions are refused', () => {
  const {client} = fixture(); client.startRequest();
  assert.throws(() => client.startRequest(), /One execution/); client.finish(false);
});

test('persistence failure prevents inference headers', async () => {
  const {client} = fixture(); await client.bind(fresh);
  client.save = () => { throw Error('disk full'); };
  assert.throws(() => client.executionHeaders(), /disk full/);
});

test('the configured output ceiling is what admission asks for', async () => {
  const {calls, saved} = fixture();
  assert.equal(calls.length, 0);
  const bounded = new StrictClient({origin: 'http://127.0.0.1:8318', piSessionId: 'pi-two', adminToken: 'test-only',
    maxOutputTokens: 2048, save: () => {}, fetch: async (_url, init) => {
      calls.push(JSON.parse(init.body)); return Response.json(contract(JSON.parse(init.body).session_id));
    }});
  await bounded.bind(fresh);
  assert.equal(calls.at(-1).limits.max_output_tokens, 2048);
  const plain = fixture(); await plain.client.bind(fresh);
  assert.equal(plain.calls[0].body.limits.max_output_tokens, 8192, 'the default');
  assert.throws(() => new StrictClient({origin: 'http://127.0.0.1:8318', piSessionId: 'x', maxOutputTokens: 0}), /positive whole number/);
  assert.equal(saved.length, 0);
});

test('a real resolve response is accepted and narrowed to the contract', () => {
  const value = contract('pi-session');
  const binding = validateBinding(value, 'pi-session');
  assert.deepEqual(Object.keys(binding).sort(), ['binding_revision', 'execution', 'session_id', 'state']);
  assert.deepEqual(Object.keys(binding.execution).sort(), [
    'context_window', 'effort_mode', 'initial_effort', 'max_output_tokens',
    'model_key', 'protocol', 'supports_tools', 'supports_vision', 'wire_model'].sort());
  assert.equal(binding.execution.wire_model, 'big(high)');
  // Everything else the router discloses stays on the wire, not in Pi's
  // session file: the decision, the estimate and the effort experiment's
  // fields have no business being replayed as a contract.
  const stored = JSON.stringify(binding);
  for (const field of ['decision_id', 'quality_lane', 'estimate_method', 'adaptation', 'compaction_epoch', 'provider', 'base_effort']) {
    assert.equal(stored.includes(field), false, `${field} is not persisted`);
  }
});

test('a malformed resolve response is refused', () => {
  const cases = [
    ['another session', contract('somebody-else'), /session_id is not this session/],
    ['no binding revision', contract('pi-session', {binding_revision: null}), /binding_revision is missing/],
    ['no execution block', contract('pi-session', {execution: null}), /execution is missing/],
    ['responses protocol', contract('pi-session', {execution: {protocol: 'openai-responses'}}), /execution\.protocol/],
    ['adaptive effort', contract('pi-session', {execution: {effort_mode: 'adaptive'}}), /execution\.effort_mode/],
    ['no wire model', contract('pi-session', {execution: {wire_model: ''}}), /execution\.wire_model/],
    ['fractional window', contract('pi-session', {execution: {context_window: 1.5}}), /execution\.context_window/],
    ['zero output', contract('pi-session', {execution: {max_output_tokens: 0}}), /execution\.max_output_tokens/],
    ['sqlite integer flag', contract('pi-session', {execution: {supports_tools: 1}}), /execution\.supports_tools/],
    ['closed session', contract('pi-session', {state: 'closed'}), /cannot execute/],
  ];
  for (const [name, value, expected] of cases) {
    assert.throws(() => validateBinding(value, 'pi-session'), expected, name);
  }
  // A whole response that is not a contract at all.
  assert.throws(() => validateBinding({error: {code: 'NO_SAFE_ADMISSION'}}, 'pi-session'), ContractError);
  assert.throws(() => validateBinding(null, 'pi-session'), ContractError);
});

test('a resumed binding whose revision moved is refused', async () => {
  const {client} = fixture(); await client.bind(fresh); client.resumed = false;
  client.fetch = async (_url, init) =>
    Response.json(contract(JSON.parse(init.body).session_id, {binding_revision: 'sha256:moved'}));
  await assert.rejects(client.bind(fresh), /revision changed/);
});

test('a pre-acceptance code releases the pending execution, anything else keeps it', async () => {
  for (const code of ['CONTEXT_BUDGET_EXCEEDED', 'INVALID_ROUTER_INPUT', 'PROFILE_CHANGED', 'SESSION_CLOSED']) {
    const {client} = fixture(); client.startRequest(); await client.bind(fresh); client.executionHeaders();
    assert.equal(client.noteExecutionRefusal(code), true, code);
    assert.equal(client.state.pending, null, code);
    client.finish(false);
    client.startRequest(); // not blocked
    client.finish(false);
  }
  for (const code of ['EXECUTION_OUTCOME_UNKNOWN', 'SESSION_CONFLICT', 'REQUEST_ALREADY_COMPLETED', 'HTTP_503', 'HTTP_429']) {
    const {client} = fixture(); client.startRequest(); await client.bind(fresh); client.executionHeaders();
    assert.equal(client.noteExecutionRefusal(code), false, code);
    assert.ok(client.state.pending, code);
    client.finish(false);
    assert.throws(() => client.startRequest(), /Automatic retry is blocked/, code);
  }
});

test('credentials never follow redirects or unexpected origins', async () => {
  assert.throws(() => new StrictClient({origin: 'http://user:password@example.com', piSessionId: 'x'}), /origin/);
  const {client} = fixture();
  client.fetch = async (_url, init) => { assert.equal(init.redirect, 'error'); return new Response('', {status: 401}); };
  await assert.rejects(client.bind(fresh), /HTTP_401/);
});

test('a router refusal never repeats the router message text', async () => {
  const {client} = fixture({}, {});
  client.fetch = async () => Response.json(
    {error: {type: 'session_error', code: 'NO_SAFE_ADMISSION', message: 'PRIVATE-ROUTER-DETAIL'}}, {status: 422});
  await assert.rejects(client.bind(fresh), error => {
    assert.match(error.message, /NO_SAFE_ADMISSION/);
    assert.equal(error.code, 'NO_SAFE_ADMISSION');
    assert.doesNotMatch(error.message, /PRIVATE-ROUTER-DETAIL/);
    assert.doesNotMatch(error.message, /test-only/);
    return true;
  });
});

test('the picked alias is admitted, kept, and a spent provider says when it resets', async () => {
  const conserve = fixture(); await conserve.client.bind(fresh, undefined, 'auto-conserve');
  assert.equal(conserve.calls[0].body.alias, 'auto-conserve');
  // A binding is only ever used under the alias it was admitted under.
  const reload = fixture(conserve.client.state);
  await assert.rejects(reload.client.bind(fresh, undefined, 'auto'), /bound under auto-conserve.*\/new/);
  assert.equal(reload.calls.length, 0);
  // A binding saved before aliases were recorded was made under `auto`.
  const legacy = fixture({...conserve.client.state, alias: undefined});
  await assert.rejects(legacy.client.bind(fresh, undefined, 'auto-conserve'), /bound under auto\./);
  await assert.rejects(fixture().client.bind(fresh, undefined, 'auto-fast'), /Unknown router alias/);

  const spent = fixture(undefined, {respond: () => Response.json(
    {error: {code: 'PROVIDER_UNAVAILABLE', message: 'secret provider text', retry_after_seconds: 13 * 3600 + 5}}, {status: 503})});
  const refused = await spent.client.bind(fresh).catch(error => error);
  assert.equal(refused.code, 'PROVIDER_UNAVAILABLE');
  assert.match(refused.message, /resets in about 14 h/);
  assert.equal(refused.message.includes('secret'), false);
  assert.equal(spent.client.state.binding, null);
});
