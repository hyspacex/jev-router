import {test} from 'node:test';
import assert from 'node:assert/strict';
import extension from '../index.ts';

function harness(entries = [], sessionId = 'pi-test') {
  const hooks = {}, commands = {};
  let provider;
  const pi = {
    on: (event, handler) => { hooks[event] = handler; },
    registerCommand: (name, value) => { commands[name] = value; },
    registerProvider: (_name, value) => { provider = value; },
    appendEntry: (customType, data) => entries.push({type:'custom',customType,data:structuredClone(data)}),
  };
  extension(pi);
  const model = {...provider.models[0], provider:'jev-router',api:'openai-completions',baseUrl:provider.baseUrl};
  const ctx = {model,hasUI:true,isIdle:()=>true,ui:{setStatus(){},notify(){},confirm:async()=>true},
    sessionManager:{getSessionId:()=>sessionId,getEntries:()=>entries,getHeader:()=>({})}};
  hooks.session_start({},ctx);
  return {provider,model,hooks,ctx,commands,entries};
}
function textResponse(tool = false) {
  const chunks = [
    {id:'chat-1',object:'chat.completion.chunk',choices:[{index:0,delta:tool ? {tool_calls:[{index:0,id:'call-one',type:'function',function:{name:'read',arguments:'{"path":"a"}'}}]} : {content:'OK'},finish_reason:null}]},
    {id:'chat-1',object:'chat.completion.chunk',choices:[{index:0,delta:{},finish_reason:tool?'tool_calls':'stop'}]},
  ];
  return new Response(chunks.map(v=>`data: ${JSON.stringify(v)}\n\n`).join('')+'data: [DONE]\n\n',{headers:{'content-type':'text/event-stream'}});
}
const transcript = {messages:[{role:'system',content:'Be helpful',toolsAdded:[{name:'read',description:'Read a file',parameters:{type:'object',properties:{path:{type:'string'}}}}],timestamp:1},{role:'user',content:'Read a',timestamp:2}]};

test('actual Pi serializer and stream: binding, tools, fixed model, resume and failure guard', async () => {
  const original = globalThis.fetch;
  const previousToken = process.env.JEV_ROUTER_ADMIN_TOKEN;
  process.env.JEV_ROUTER_ADMIN_TOKEN='control-test';
  const calls=[];
  let fail=false, tool=true;
  globalThis.fetch = async (input,init) => {
    const body=JSON.parse(init.body); const url=String(input); const headers=new Headers(init.headers);
    calls.push({url,body,headers});
    if(url.endsWith('/router/resolve')) return Response.json({schema_version:'1',session_id:body.session_id,binding_revision:'revision',state:'prepared',execution:{
      model_key:'big',wire_model:'big(high)',protocol:'openai-chat',context_window:128000,max_output_tokens:8192,supports_tools:true,supports_vision:false,initial_effort:'high',effort_mode:'fixed'}});
    assert.ok(url.endsWith('/v1/chat/completions'));
    assert.equal(headers.get('x-router-admin-token'),'control-test');
    assert.equal(headers.get('authorization'),'Bearer upstream-test');
    assert.equal(headers.get('x-router-client'),'pi');
    assert.equal(body.model,'big(high)');
    assert.equal(body.max_tokens,8192);
    assert.equal(body.reasoning_effort,undefined);
    if(fail) return new Response('{"error":{"message":"upstream failed"}}',{status:503});
    return textResponse(tool);
  };
  try {
    const h=harness();
    const first=await h.provider.streamSimple(h.model,transcript,{apiKey:'upstream-test'}).result();
    assert.equal(first.stopReason,'toolUse');
    assert.equal(first.content[0].name,'read');
    assert.equal(first.model,'auto');
    assert.equal(first.responseModel,'big(high)');
    assert.equal(h.model.contextWindow,128000);
    assert.ok(calls[0].body.request.tools.length);
    assert.equal(calls.length,2);
    const follow={messages:[...transcript.messages,first,{role:'toolResult',toolCallId:'call-one',toolName:'read',content:[{type:'text',text:'data'}],isError:false,timestamp:3}]};
    tool=false;
    const second=await h.provider.streamSimple(h.model,follow,{apiKey:'upstream-test'}).result();
    assert.equal(second.stopReason,'stop');
    assert.equal(calls.length,3);
    assert.notEqual(calls[1].headers.get('x-router-request-id'),calls[2].headers.get('x-router-request-id'));
    const resumed=harness(h.entries);
    await resumed.provider.streamSimple(resumed.model,follow,{apiKey:'upstream-test'}).result();
    assert.equal(calls[3].body.intent,'resume');
    fail=true;
    const failed=await resumed.provider.streamSimple(resumed.model,follow,{apiKey:'upstream-test'}).result();
    assert.equal(failed.stopReason,'error');
    const count=calls.length;
    const retry=await resumed.provider.streamSimple(resumed.model,follow,{apiKey:'upstream-test'}).result();
    assert.match(retry.errorMessage,/Automatic retry is blocked/);
    assert.equal(calls.length,count);
    assert.deepEqual(resumed.hooks.session_before_compact({},resumed.ctx),{cancel:true});
    const fork=harness(h.entries,'fork');
    const refused=await fork.provider.streamSimple(fork.model,follow,{apiKey:'upstream-test'}).result();
    assert.match(refused.errorMessage,/fresh work/);
    assert.equal(calls.length,count);
  } finally {
    globalThis.fetch=original;
    if(previousToken===undefined) delete process.env.JEV_ROUTER_ADMIN_TOKEN;
    else process.env.JEV_ROUTER_ADMIN_TOKEN=previousToken;
  }
});
