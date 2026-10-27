import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { WorkflowCanvas } from './WorkflowCanvas';
import type { WorkflowPaletteOperation } from '@/src/lib/workflowAuthoring';
const operation: WorkflowPaletteOperation = { contract_id: 'example.echo.v1', contract_revision_digest: `sha256:${'1'.repeat(64)}`, operation_id: 'echo', function_principal_id: 'example.echo', provider_id: 'example.echo', input_schema_digest: `sha256:${'2'.repeat(64)}`, effect_ceiling: ['read'], input_schema: {type: 'object', properties: {text: {type: 'string'}}}, output_schema: {type: 'object', properties: {text: {type: 'string'}}} };
const empty = { workflow_api_version: 'io.tobkiri.workflow.v4', steps: [] };
test('canvas adds canonical nodes, connects with keyboard-accessible controls, undoes, and removes edges', async () => {
    const previous = { window: globalThis.window, document: globalThis.document, navigator: globalThis.navigator };
    const dom = new JSDOM('<div id="root"></div>', { url: 'https://launcher.test/' });
    Object.defineProperties(globalThis, { window: { value: dom.window, configurable: true }, document: { value: dom.window.document, configurable: true }, navigator: { value: dom.window.navigator, configurable: true } });
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const container = dom.window.document.getElementById('root')!;
    const root = createRoot(container);
    let latest: Record<string, unknown> = empty;
    function Harness() { const [value, setValue] = useState<Record<string, unknown>>(empty); return <WorkflowCanvas document={value} operations={[operation]} onChange={(next) => { latest = next; setValue(next); }}/>; }
    const button = (label: string) => { const found = container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`); assert.ok(found, label); return found; };
    try {
        await act(async () => root.render(<Harness />));
        await act(async () => button('Add node example.echo.v1 / echo').click());
        await act(async () => button('Add node example.echo.v1 / echo').click());
        assert.equal((latest.steps as unknown[]).length, 2);
        await act(async () => button('Connect execution from echo-1').click());
        assert.equal(button('Connect execution to echo-1').disabled, true);
        await act(async () => button('Connect execution to echo-2').click());
        assert.deepEqual((latest.steps as any[])[1].depends_on, ['echo-1']);
        await act(async () => button('Undo graph edit').click());
        assert.deepEqual((latest.steps as any[])[1].depends_on, []);
        await act(async () => button('Redo graph edit').click());
        await act(async () => button('Connect execution from echo-2').click());
        assert.equal(button('Connect execution to echo-1').disabled, true, 'cycle is unavailable');
        await act(async () => button('Select node echo-1').click());
        await act(async () => button('Disconnect echo-1 to echo-2').click());
        assert.deepEqual((latest.steps as any[])[1].depends_on, []);
        assert.equal(container.querySelectorAll('svg path[stroke="var(--accent)"]').length, 0);
        await act(async () => button('Connect data from echo-1.text').click());
        await act(async () => button('Connect data to echo-2.text').click());
        assert.deepEqual((latest.steps as any[])[1].request.input, {text: '${steps.echo-1.output.text}'});
        assert.deepEqual((latest.steps as any[])[1].depends_on, ['echo-1']);
        await act(async () => button('Select node echo-2').click());
        assert.match(container.textContent ?? '', /steps.echo-1.output.text/);
        assert.ok(container.querySelector('[aria-label="Node inspector"]'));
        await act(async () => button('Close node inspector').click());
        assert.equal(container.querySelector('[aria-label="Node inspector"]'), null);
        const canvas = container.querySelector('[aria-label="Node canvas"]')!;
        Object.defineProperty(canvas, 'clientWidth', { value: 600 });
        await act(async () => button('Fit canvas to view').click());
        assert.equal(container.querySelector('[aria-label="Canvas zoom"]')?.textContent, '60%');
        await act(async () => button('Reset canvas layout').click());
        assert.equal(container.querySelector('[aria-label="Canvas zoom"]')?.textContent, '100%');

    }
    finally {
        await act(async () => root.unmount());
        dom.window.close();
        Object.defineProperties(globalThis, { window: { value: previous.window, configurable: true }, document: { value: previous.document, configurable: true }, navigator: { value: previous.navigator, configurable: true } });
    }
});

test('legal prototype-like node IDs do not inherit canvas coordinates', async () => {
    const {renderToStaticMarkup} = await import('react-dom/server');
    const html = renderToStaticMarkup(<WorkflowCanvas document={{workflow_api_version:'io.tobkiri.workflow.v4',steps:[{id:'constructor',depends_on:[],request:{contract_id:operation.contract_id,contract_revision_digest:operation.contract_revision_digest,operation_id:operation.operation_id,function_principal_id:operation.function_principal_id,input:{}}}]}} operations={[operation]} onChange={()=>{throw new Error('render must not mutate');}}/>);
    assert.match(html,/left:40px;top:40px/);
    assert.doesNotMatch(html,/NaN|undefinedpx/);
});

test('run-input model references are shown as bindings without looking up a literal model ID',async()=>{
 const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator,fetch:globalThis.fetch};
 const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher.test/'});
 Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
 globalThis.IS_REACT_ACT_ENVIRONMENT=true;
 let reads=0;globalThis.fetch=(async()=>{reads++;throw new Error('A bound input must not query a literal model');}) as typeof fetch;
 const container=dom.window.document.getElementById('root')!;const root=createRoot(container);
 const op={...operation,input_schema:{type:'object',properties:{model_profile_id:{type:'string','x-tobkiri-selector':'model-profile'}}}};
 const document={workflow_api_version:'io.tobkiri.workflow.v4',steps:[{id:'model',request:{contract_id:op.contract_id,contract_revision_digest:op.contract_revision_digest,operation_id:op.operation_id,function_principal_id:op.function_principal_id,input:{model_profile_id:'${inputs.model}'}}}]};
 try{
  await act(async()=>root.render(<WorkflowCanvas document={document} operations={[op]} onChange={()=>{}}/>));
  await act(async()=>container.querySelector<HTMLButtonElement>('[aria-label="Select node model"]')!.click());
  assert.match(container.textContent??'',/開始時の入力から設定/);
  assert.doesNotMatch(container.textContent??'',/利用できないモデル/);assert.equal(reads,0);
 }finally{await act(async()=>root.unmount());dom.window.close();Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});globalThis.fetch=previous.fetch;}
});


test('object content is a single normal port instead of exposing base64 implementation fields', async () => {
    const {renderToStaticMarkup} = await import('react-dom/server');
    const op = {...operation, output_schema: {type: 'object', properties: {content: {type: 'object', properties: {data_base64: {type: 'string'}}}}}};
    const html = renderToStaticMarkup(<WorkflowCanvas document={{workflow_api_version: 'io.tobkiri.workflow.v4', steps: [{id: 'audio', request: {contract_id: op.contract_id, contract_revision_digest: op.contract_revision_digest, operation_id: op.operation_id, function_principal_id: op.function_principal_id, input: {}}}]}} operations={[op]} onChange={() => {}}/>);
    assert.match(html, /Connect data from audio.content/);
    assert.doesNotMatch(html, /Connect data from audio.content.data_base64/);
    assert.match(html, /Show detailed ports/);
});


test('an existing connection beyond the ordinary port limit remains visible', async () => {
    const {renderToStaticMarkup} = await import('react-dom/server');
    const properties = Object.fromEntries(Array.from({length: 14}, (_, index) => [`field${index}`, {type: 'string'}]));
    const op = {...operation, input_schema: {type: 'object', properties}, output_schema: {type: 'object', properties}};
    const request = {contract_id: op.contract_id, contract_revision_digest: op.contract_revision_digest, operation_id: op.operation_id, function_principal_id: op.function_principal_id};
    const html = renderToStaticMarkup(<WorkflowCanvas document={{workflow_api_version: 'io.tobkiri.workflow.v4', steps: [
        {id: 'source', request: {...request, input: {}}},
        {id: 'target', depends_on: ['source'], request: {...request, input: {field13: '${steps.source.output.field13}'}}},
    ]}} operations={[op]} onChange={() => {}}/>);
    assert.match(html, /Connect data from source.field13/);
    assert.match(html, /Connect data to target.field13/);
    assert.doesNotMatch(html, /Connect data from source.field12/);
});


test('human schema labels do not alter canonical port identities', async () => {
    const {renderToStaticMarkup} = await import('react-dom/server');
    const op = {...operation, output_schema: {type: 'object', properties: {text: {type: 'string', title: '認識した文字'}}}};
    const document = {workflow_api_version: 'io.tobkiri.workflow.v4', steps: [{id: 'audio', request: {contract_id: op.contract_id, contract_revision_digest: op.contract_revision_digest, operation_id: op.operation_id, function_principal_id: op.function_principal_id, input: {}}}]};
    const before = JSON.stringify(document);
    const html = renderToStaticMarkup(<WorkflowCanvas document={document} operations={[op]} onChange={() => {}}/>);
    assert.match(html, /認識した文字/);
    assert.match(html, /aria-label="Connect data from audio.text"/);
    assert.equal(JSON.stringify(document), before);
});

test('typed audio node exposes text and sound pins but keeps model and voice in configuration', async () => {
 const {renderToStaticMarkup}=await import('react-dom/server');
 const typed={...operation,input_schema:{type:'object',properties:{input:{type:'string',title:'text'},model_profile_id:{type:'string','x-tobkiri-flow-role':'configuration'},voice:{type:'string','x-tobkiri-flow-role':'configuration'}}},output_schema:{type:'object',properties:{content:{type:'object',title:'sound','x-tobkiri-value-type':'tobkiri.value.sound.v1',properties:{data_base64:{type:'string'}}},status:{type:'string','x-tobkiri-flow-role':'metadata'}}}};
 const document={workflow_api_version:'io.tobkiri.workflow.v4',steps:[{id:'tts',depends_on:[],request:{contract_id:typed.contract_id,contract_revision_digest:typed.contract_revision_digest,operation_id:typed.operation_id,function_principal_id:typed.function_principal_id,input:{}}}]};
 const html=renderToStaticMarkup(<WorkflowCanvas document={document} operations={[typed]} onChange={()=>{throw new Error('render cannot write')}}/>);
 assert.match(html,/Connect data to tts.input/);
 assert.match(html,/Connect data from tts.content/);
 assert.doesNotMatch(html,/Connect data to tts.model_profile_id|Connect data to tts.voice|Connect data from tts.status|content.data_base64/);
 assert.match(html,/sound/);
});

test('real inspector shows voice and numeric speed as settings without exposing their pins',async()=>{
 const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator,fetch:globalThis.fetch};
 const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher.test/'});
 Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
 globalThis.IS_REACT_ACT_ENVIRONMENT=true;
 globalThis.fetch=async()=>new Response('{}',{status:503});
 const root=createRoot(dom.window.document.getElementById('root')!);
 const catalog=JSON.parse(readFileSync(new URL('../../../../../tobkiri_runtime/schemas/pack_v4_catalog.v1.json',import.meta.url),'utf8'));
 const speech=catalog.packs.flatMap((pack:any)=>pack.provided_contracts??[]).find((contract:any)=>contract.contract_id==='tobkiri.service.ai.audio.speech.v1');
 const typed:WorkflowPaletteOperation={...operation,contract_id:speech.contract_id,input_schema:speech.schemas.input,output_schema:speech.schemas.output};
 const operations=[typed];
 const document={workflow_api_version:'io.tobkiri.workflow.v4',steps:[{id:'tts',depends_on:[],request:{contract_id:typed.contract_id,contract_revision_digest:typed.contract_revision_digest,operation_id:typed.operation_id,function_principal_id:typed.function_principal_id,input:{input:'hello',model_profile_id:'voice-model',voice:'voice-a',parameters:{speed:1}}}}]};
 try{
  await act(async()=>root.render(<WorkflowCanvas document={document} operations={operations} onChange={()=>{throw new Error('opening inspector must not write')}}/>));
  await act(async()=>dom.window.document.querySelector<HTMLButtonElement>('[aria-label="Select node tts"]')!.click());
  const inspector=dom.window.document.querySelector('[aria-label="Node inspector"]')!;
  assert.ok(inspector);
  assert.match(inspector.textContent??'',/声.*ノード設定/s);
  assert.match(inspector.textContent??'',/読み上げ速度.*ノード設定/s);
  const values=Array.from(inspector.querySelectorAll('textarea')).map(x=>x.value);
  assert.ok(values.includes('voice-a'));
  assert.ok(values.includes('1'));
  assert.equal(dom.window.document.querySelector('[aria-label="Connect data to tts.parameters.speed"]'),null);
  assert.equal(dom.window.document.querySelector('[aria-label="Connect data to tts.voice"]'),null);
 }finally{await act(async()=>root.unmount());dom.window.close();Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});globalThis.fetch=previous.fetch;}
});

test('registered tool selection pins its definition and reveals typed argument ports',async()=>{
 const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator};
 const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher.test/'});
 Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
 globalThis.IS_REACT_ACT_ENVIRONMENT=true;
 const container=dom.window.document.getElementById('root')!,root=createRoot(container);
 const op={...operation,output_schema:{type:'object',properties:{result:{}}},input_schema:{type:'object',properties:{tool_id:{type:'string','x-tobkiri-selector':'tool-definition','x-tobkiri-flow-role':'configuration'},tool_call_id:{type:'string','x-tobkiri-flow-role':'configuration'},expected_definition_hash:{type:'string','x-tobkiri-flow-role':'configuration'},arguments:{type:'object'}}}};
 let latest:any={workflow_api_version:'io.tobkiri.workflow.v4',steps:[{id:'tool',request:{contract_id:op.contract_id,contract_revision_digest:op.contract_revision_digest,operation_id:op.operation_id,function_principal_id:op.function_principal_id,input:{arguments:{expression:'2+3'}}}}]};
 const consumer={...operation,operation_id:'consume'};
 latest.steps.push({id:'consumer',request:{contract_id:consumer.contract_id,contract_revision_digest:consumer.contract_revision_digest,operation_id:consumer.operation_id,function_principal_id:consumer.function_principal_id,input:{}}});
 const load=async()=>({revision:1,tools:[{toolId:'calculator',name:'計算',definitionHash:'a'.repeat(64),inputSchema:{type:'object',properties:{expression:{type:'string'},related_tool:{type:'string','x-tobkiri-selector':'tool-definition'}},required:['expression']},resultSchema:{type:'string'}}]});
 function Harness(){const [value,setValue]=useState(latest);return <WorkflowCanvas document={value} operations={[op,consumer]} loadTools={load} layoutKey="profile-a" onChange={next=>{latest=next;setValue(next);}}/>;}
 try{
  await act(async()=>root.render(<Harness/>));
  await act(async()=>container.querySelector<HTMLButtonElement>('[aria-label="Select node tool"]')!.click());
  const select=container.querySelector<HTMLSelectElement>('[aria-label="Registered tool"]')!;
  assert.ok(select);assert.equal(select.disabled,false);
  await act(async()=>{select.value=`calculator@${'a'.repeat(64)}`;select.dispatchEvent(new dom.window.Event('change',{bubbles:true}));});
  assert.equal(latest.steps[0].request.input.tool_id,'calculator');
  assert.equal(container.querySelectorAll('[aria-label="Registered tool"]').length,1,'nested hints must not replace the outer pinned tool definition');
  assert.equal(latest.steps[0].request.input.expected_definition_hash,'a'.repeat(64));
  assert.equal(latest.steps[0].request.input.tool_call_id,'flow.tool');
  assert.equal(latest.steps[0].request.input.arguments.expression,'2+3');
  assert.ok(container.querySelector('[aria-label="Connect data to tool.arguments.expression"]'));
  assert.equal('definition' in latest.steps[0].request.input,false);
  assert.equal('executor' in latest.steps[0].request.input,false);
  await act(async()=>container.querySelector<HTMLButtonElement>('[aria-label="Connect data from tool.result"]')!.click());
  const target=container.querySelector<HTMLButtonElement>('[aria-label="Connect data to consumer.text"]')!;
  assert.equal(target.disabled,false);
  await act(async()=>target.click());
  assert.equal(latest.steps[1].request.input.text,'${steps.tool.output.result}');
 }finally{await act(async()=>root.unmount());dom.window.close();Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});}
});
