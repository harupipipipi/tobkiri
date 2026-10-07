import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {WorkflowRunPanel} from './WorkflowRunPanel';
import type {WorkflowAuthoringDependencies,WorkflowDefinition} from '@/src/lib/workflowAuthoring';
const digest=(x:string)=>`sha256:${x.repeat(64)}`;
const definition:WorkflowDefinition={definition_id:'flow.voice',revision:1,revision_digest:digest('6'),etag:'"v1"',state:'published',document:{workflow_api_version:'io.tobkiri.workflow.v4',steps:[{id:'echo'}]}};
const catalog={version:'rumi.ui.contribution.v1',profile_id:'defaults',profile_revision:digest('1'),activation_id:'activation:test',plan_hash:digest('2'),catalog_hash:digest('3'),diagnostics:[],quarantined_pack_ids:[],contributions:['run.create','run.advance','run.stop','run.observe'].map((id)=>({contribution_id:`pack.tobkiri_workflow_pack.${id}`,owner_pack_id:'tobkiri_workflow_pack',owner_pack_hash:digest('4'),label:id,action_contract: id === 'run.stop' ? 'tobkiri.workflow.stop.v4' : 'tobkiri.workflow.v4',operation_id:id,provider_id: id === 'run.stop' ? 'tobkiri.workflow.stop.provider' : 'tobkiri.workflow.provider',function_id: id === 'run.stop' ? 'tobkiri.workflow.stop.provider' : 'tobkiri.workflow.provider',build_identity: id === 'run.stop' ? 'tobkiri.workflow.stop.provider' : 'tobkiri.workflow.provider',descriptor_hash:digest('5'),kind:'action' as const,mode:'declarative' as const,resolved_profile_id:'defaults',resolved_profile_revision:digest('1'),resolved_activation_id:'activation:test',resolved_plan_hash:digest('2')}))};

test('Run double-click is single-flight and Stop remains usable while an advance is pending',async()=>{
  const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator};
  const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher-run.test/'});
  Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
  globalThis.IS_REACT_ACT_ENVIRONMENT=true;
  const container=dom.window.document.getElementById('root')!;
  const root=createRoot(container);
  let id='',creates=0,cancels=0;
  let resolveAdvance:(value:unknown)=>void=()=>{};
  const pending=new Promise((resolve)=>{resolveAdvance=resolve;});
  const result=(state:string)=>({run_id:id,definition_id:'flow.voice',revision_digest:digest('6'),state});
  const dependencies:WorkflowAuthoringDependencies={fetchCatalog:async()=>catalog,invoke:async(request)=>{
    id=String(request.payload.run_id);
    if(request.contributionId.endsWith('run.create')){assert.equal(request.payload.revision_digest, definition.revision_digest);creates++;return result('queued');}
    if(request.contributionId.endsWith('run.advance'))return pending;
    if(request.contributionId.endsWith('run.stop')){cancels++;return result('cancelled');}
    return {run:result('cancelled'),attempts:[]};
  }};
  const button=(text:string)=>{const found=Array.from(container.querySelectorAll('button')).find((item)=>item.textContent===text);assert.ok(found,text);return found;};
  try{
    await act(async()=>root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={false} dependencies={dependencies}/>));
    await act(async()=>{button('実行').click();button('実行').click();await new Promise((resolve)=>setTimeout(resolve,5));});
    assert.equal(creates,1);
    assert.equal(button('停止').disabled,false);
    await act(async()=>root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={true} dependencies={dependencies}/>));
    assert.equal(button('停止').disabled,true);
    await act(async()=>button('停止').click());
    assert.equal(cancels,0,'inactive inspection must not dispatch a Stop mutation');
    await act(async()=>root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={true} stopDisabled={false} dependencies={dependencies}/>));
    assert.equal(button('停止').disabled,false,'a pending authoring read must not hide owned Run Stop');
    await act(async()=>root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={false} dependencies={dependencies}/>));
    assert.equal(button('停止').disabled,false);
    await act(async()=>{button('停止').click();button('停止').click();await new Promise((resolve)=>setTimeout(resolve,5));});
    assert.equal(cancels,1);
    assert.match(container.textContent??'',/cancelled/);
    await act(async()=>{resolveAdvance({run:result('running'),attempts:[]});await new Promise((resolve)=>setTimeout(resolve,5));});
    assert.match(container.textContent??'',/cancelled/);
    assert.doesNotMatch(container.textContent??'',/· running/);
  }finally{
    await act(async()=>root.unmount());dom.window.close();
    Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});
  }
});

test('a missed approval window can be reopened explicitly without replaying the workflow', async () => {
  const previous = {window: globalThis.window, document: globalThis.document, navigator: globalThis.navigator};
  const dom = new JSDOM('<div id="root"></div>', {url: 'https://launcher-approval.test/'});
  Object.defineProperties(globalThis, {window: {value: dom.window, configurable: true}, document: {value: dom.window.document, configurable: true}, navigator: {value: dom.window.navigator, configurable: true}});
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root')!;
  const root = createRoot(container);
  const approvalId = `workflow-approval-sha256:${'a'.repeat(64)}`;
  let id = '', opens = 0;
  const mutations: string[] = [];
  let finishOpen: (opened: boolean) => void = () => {};
  const opening = new Promise<boolean>(resolve => {finishOpen = resolve;});
  const openApprovalWindow = async (requestId: string) => {assert.equal(requestId, approvalId); opens++; return opening;};
  const dependencies: WorkflowAuthoringDependencies = {fetchCatalog: async () => catalog, invoke: async request => {
    id = String(request.payload.run_id);
    if (!request.contributionId.endsWith('run.observe')) mutations.push(request.contributionId);
    return {run: {run_id: id, definition_id: 'flow.voice', revision_digest: digest('6'), state: 'waiting_approval'}, attempts: [{run_id: id, step_id: 'voice', attempt_number: 1, state: 'waiting_approval', approval_request_id: approvalId}]};
  }};
  const button = (text: string) => {const found = Array.from(container.querySelectorAll('button')).find(item => item.textContent === text); assert.ok(found, text); return found;};
  try {
    await act(async () => root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={false} dependencies={dependencies} openApprovalWindow={openApprovalWindow}/>));
    await act(async () => {button('実行').click(); await new Promise(resolve => setTimeout(resolve, 5));});
    assert.equal(opens, 0, 'status reads never reopen a window automatically');
    await act(async () => {button('承認画面を開く').click(); button('承認画面を開く').click();});
    assert.equal(opens, 1);
    assert.equal(button('停止').disabled, false);
    await act(async () => {finishOpen(false); await opening;});
    assert.match(container.textContent ?? '', /承認状態は変更していません/);
    assert.equal(mutations.length, 1, 'opening a window does not resume or approve the run');
    await act(async () => root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={true} dependencies={dependencies} openApprovalWindow={openApprovalWindow}/>));
    assert.equal(button('承認画面を開く').disabled, true);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    Object.defineProperties(globalThis, {window: {value: previous.window, configurable: true}, document: {value: previous.document, configurable: true}, navigator: {value: previous.navigator, configurable: true}});
  }
});

test('unmounting run controls stops local pumping after the in-flight request returns', async () => {
  const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator};
  const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher-run-hide.test/'});
  Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
  globalThis.IS_REACT_ACT_ENVIRONMENT=true;
  const container=dom.window.document.getElementById('root')!;const root=createRoot(container);
  let runId='', advances=0, reads=0;
  let finish:(value:unknown)=>void=()=>{};
  const pending=new Promise(resolve=>{finish=resolve;});
  const result=(state:string)=>({run_id:runId,definition_id:definition.definition_id,revision_digest:definition.revision_digest,state});
  const dependencies:WorkflowAuthoringDependencies={fetchCatalog:async()=>catalog,invoke:async request=>{
    runId=String(request.payload.run_id);
    if(request.contributionId.endsWith('run.create'))return result('queued');
    if(request.contributionId.endsWith('run.advance')){advances++;return pending;}
    reads++;return {run:result('running'),attempts:[]};
  }};
  try{
    await act(async()=>root.render(<WorkflowRunPanel definition={definition} dirty={false} disabled={false} dependencies={dependencies}/>));
    const start=Array.from(container.querySelectorAll('button')).find(button=>button.textContent==='実行');assert.ok(start);
    await act(async()=>{start.click();await new Promise(resolve=>setTimeout(resolve,5));});
    assert.equal(advances,1);
    await act(async()=>root.render(null));
    await act(async()=>{finish({run:result('running'),attempts:[{run_id:runId,step_id:'echo',attempt_number:1,state:'succeeded'}]});await new Promise(resolve=>setTimeout(resolve,5));});
    assert.equal(advances,1);assert.equal(reads,0);
  }finally{
    await act(async()=>root.unmount());dom.window.close();
    Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});
  }
});
