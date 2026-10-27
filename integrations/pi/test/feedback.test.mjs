import './env.mjs';
import {contract} from './fixtures/contract.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  StrictClient, validateBinding, ContractError, RouterError,
  parseFeedbackCommand, feedbackMessage, statusLine, FEEDBACK_CHOICES,
} from '../client.mjs';

const fresh = {messages: [{role: 'user', content: 'test'}]};

/** The body `POST /router/sessions/{id}/feedback` answers with. */
function feedbackReply(body, overrides = {}) {
  const up = body.verdict === 'too_weak', down = body.verdict === 'too_strong';
  const moved = body.adjust_effort && (up || down);
  const current = moved ? (up ? 'xhigh' : 'medium') : 'high';
  return {
    ok: true, feedback_id: 7, session_id: 's', decision_id: 'd-20260919-0001', verdict: body.verdict,
    turn_index: 2, model_key: 'big', previous_effort: 'high', current_effort: current,
    wire_model: `big(${current})`, effort_changed: Boolean(moved), reason_code: null, reason: null,
    ...overrides,
  };
}

function fixture({reply = feedbackReply, bindingOverrides} = {}) {
  const calls = [], saved = [];
  const client = new StrictClient({origin: 'http://127.0.0.1:8318', piSessionId: 'pi-one', adminToken: 'test-only',
    save: s => saved.push(s), fetch: async (url, init) => {
      const body = JSON.parse(init.body); calls.push({url, body, headers: init.headers});
      if (url.endsWith('/feedback')) {
        const value = reply(body);
        return value instanceof Response ? value : Response.json(value);
      }
      return Response.json(contract(body.session_id, bindingOverrides));
    }});
  return {client, calls, saved};
}

test('feedback words map to verdicts and keep the note', () => {
  for (const word of ['more', '+', 'up', 'MORE']) assert.deepEqual(parseFeedbackCommand(word), {verdict: 'too_weak', note: undefined});
  for (const word of ['less', '-', 'down']) assert.equal(parseFeedbackCommand(word).verdict, 'too_strong');
  for (const word of ['good', 'ok']) assert.equal(parseFeedbackCommand(word).verdict, 'right');
  assert.deepEqual(parseFeedbackCommand('  more   missed the race in quota.py '), {verdict: 'too_weak', note: 'missed the race in quota.py'});
  assert.deepEqual(parseFeedbackCommand('feedback'), {picker: true});
  assert.deepEqual(parseFeedbackCommand('fb'), {picker: true});
  for (const other of ['status', 'retry', 'close', 'audit', '', 'moreover']) assert.equal(parseFeedbackCommand(other), null, other);
  assert.equal(parseFeedbackCommand('less ' + 'x'.repeat(5000)).note.length, 2000);
  assert.deepEqual(FEEDBACK_CHOICES.map(([, verdict]) => verdict), ['too_weak', 'right', 'too_strong']);
});

test('more posts to the session feedback endpoint and steps the local contract', async () => {
  const {client, calls, saved} = fixture();
  client.startRequest(); await client.bind(fresh); const requestId = client.executionHeaders()['X-Router-Request-Id']; client.finish(true);
  const before = client.state.binding;
  const result = await client.feedback('too_weak', {note: 'missed a constraint'});

  const call = calls.at(-1);
  assert.equal(call.url, `http://127.0.0.1:8318/router/sessions/${client.state.sessionId}/feedback`);
  assert.deepEqual(call.body, {verdict: 'too_weak', adjust_effort: true, source: 'pi', request_id: requestId, note: 'missed a constraint'});
  assert.equal(call.headers['X-Router-Admin-Token'], 'test-only');
  assert.deepEqual(result, {verdict: 'too_weak', effortChanged: true, modelKey: 'big', previousEffort: 'high', currentEffort: 'xhigh', reasonCode: null});

  const e = client.state.binding.execution;
  assert.equal(e.initial_effort, 'xhigh');
  assert.equal(e.wire_model, 'big(xhigh)');
  assert.equal(e.model_key, 'big', 'the model never changes');
  assert.equal(client.state.binding.binding_revision, before.binding_revision);
  assert.notEqual(client.state.binding, before, 'a request holding the old contract keeps it');
  assert.equal(before.execution.wire_model, 'big(high)');
  assert.equal(saved.at(-1).lastFeedback, 'too_weak');
  assert.equal(saved.at(-1).binding.execution.wire_model, 'big(xhigh)');
  assert.equal(JSON.stringify(saved).includes('test-only'), false);
  assert.match(statusLine(client.state), /Jev auto · big · xhigh · strict · \[↑ more\]$/);
});

test('good records without asking for a step; less steps down', async () => {
  const {client, calls} = fixture();
  await client.bind(fresh);
  const good = await client.feedback('right');
  assert.equal(calls.at(-1).body.adjust_effort, false);
  assert.equal(good.effortChanged, false);
  assert.equal(client.state.binding.execution.initial_effort, 'high');
  const less = await client.feedback('too_strong');
  assert.equal(less.currentEffort, 'medium');
  assert.match(statusLine(client.state), /· medium · strict · \[↓ less\]$/);
});

test('a step the router refused leaves the contract alone and says why from the code', async () => {
  const {client} = fixture({reply: body => feedbackReply(body, {
    effort_changed: false, current_effort: 'high', wire_model: 'big(high)',
    reason_code: 'at_highest', reason: 'PRIVATE-ROUTER-DETAIL'})});
  await client.bind(fresh);
  const result = await client.feedback('too_weak');
  assert.equal(result.effortChanged, false);
  assert.equal(client.state.binding.execution.wire_model, 'big(high)');
  const message = feedbackMessage(result);
  assert.match(message, /already at max effort for big/);
  assert.match(message, /\/new/);
  assert.doesNotMatch(message, /PRIVATE-ROUTER-DETAIL/);
});

test('a reply naming another model is never applied', async () => {
  const {client} = fixture({reply: body => feedbackReply(body, {model_key: 'other', wire_model: 'other(xhigh)'})});
  await client.bind(fresh);
  const result = await client.feedback('too_weak');
  assert.equal(result.effortChanged, false);
  assert.equal(client.state.binding.execution.wire_model, 'big(high)');
});

test('feedback needs a binding, and a router refusal is a named error', async () => {
  const unbound = fixture();
  await assert.rejects(unbound.client.feedback('too_weak'), ContractError);
  assert.equal(unbound.calls.length, 0);
  await assert.rejects(unbound.client.feedback('maybe'), /Unknown feedback verdict/);
  const refused = fixture({reply: () => Response.json(
    {error: {code: 'SESSION_UNKNOWN', message: 'PRIVATE-ROUTER-DETAIL'}}, {status: 409})});
  await refused.client.bind(fresh);
  await assert.rejects(refused.client.feedback('right'), error => {
    assert.ok(error instanceof RouterError);
    assert.equal(error.code, 'SESSION_UNKNOWN');
    assert.doesNotMatch(error.message, /PRIVATE-ROUTER-DETAIL/);
    return true;
  });
});

test('the status bar says when quota steered the binding', async () => {
  const steered = fixture({bindingOverrides: {execution: {
    model_key: 'gpt-6-sol', wire_model: 'gpt-6-sol(high)', quota_steered: true,
    intended_model: 'gpt-6-astra', intended_effort: 'medium'}}});
  await steered.client.bind(fresh);
  assert.equal(statusLine(steered.client.state), 'Jev auto · gpt-6-sol · high (steered from gpt-6-astra: quota) · strict');
  const plain = fixture(); await plain.client.bind(fresh);
  assert.equal(statusLine(plain.client.state), 'Jev auto · big · high · strict');
  assert.equal(statusLine({binding: null}), 'Jev · awaiting fresh admission');
});

test('validateBinding tolerates the new fields missing or malformed', () => {
  const old = contract('pi-session');
  for (const key of ['quota_steered', 'intended_model', 'intended_effort']) delete old.execution[key];
  const binding = validateBinding(old, 'pi-session');
  assert.equal('quota_steered' in binding.execution, false);
  const odd = validateBinding(contract('pi-session', {execution: {
    quota_steered: 'yes', intended_model: 'has spaces <b>', intended_effort: 3}}), 'pi-session');
  assert.equal('quota_steered' in odd.execution, false);
  assert.equal('intended_model' in odd.execution, false);
  assert.equal('intended_effort' in odd.execution, false);
  const good = validateBinding(contract('pi-session', {execution: {quota_steered: true, intended_model: 'gpt-6-astra'}}), 'pi-session');
  assert.equal(good.execution.quota_steered, true);
  assert.equal(good.execution.intended_model, 'gpt-6-astra');
});
