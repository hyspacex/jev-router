import { test } from 'node:test';
import assert from 'node:assert/strict';
import { StrictClient } from '../client.mjs';

const fresh = {messages: [{role: 'user', content: 'test'}]};
function fixture(state) {
  const calls = [], saved = [];
  const client = new StrictClient({origin: 'http://127.0.0.1:8318', piSessionId: 'pi-one', adminToken: 'test-only', state,
    save: s => saved.push(s), fetch: async (url, init) => {
      const body = JSON.parse(init.body); calls.push({url, body, headers: init.headers});
      return Response.json({session_id: body.session_id, binding_revision: 'digest', state: 'prepared', execution: {
        protocol: 'openai-chat', effort_mode: 'fixed', wire_model: 'big(high)', model_key: 'big', initial_effort: 'high',
        context_window: 128000, max_output_tokens: 8192, supports_tools: true, supports_vision: false}});
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
test('unsupported protocol and changed binding fail closed', async () => {
  const {client} = fixture(); await client.bind(fresh); client.resumed = false;
  client.fetch = async () => Response.json({...client.state.binding, binding_revision: 'changed'});
  await assert.rejects(client.bind(fresh), /revision changed/);
  client.fetch = async () => Response.json({...client.state.binding, execution: {...client.state.binding.execution, protocol: 'openai-responses'}});
  await assert.rejects(client.bind(fresh), /Unsupported router binding/);
});
test('credentials never follow redirects or unexpected origins', async () => {
  assert.throws(() => new StrictClient({origin: 'http://user:password@example.com', piSessionId: 'x'}), /origin/);
  const {client} = fixture();
  client.fetch = async (_url, init) => { assert.equal(init.redirect, 'error'); return new Response('', {status: 401}); };
  await assert.rejects(client.bind(fresh), /HTTP_401/);
});
