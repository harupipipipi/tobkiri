import assert from 'node:assert/strict';
import test from 'node:test';
import {act, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {WorkflowCanvas} from './WorkflowCanvas';
import type {WorkflowPaletteOperation} from '@/src/lib/workflowAuthoring';
const operation: WorkflowPaletteOperation = {contract_id:'example.echo.v1',contract_revision_digest:`sha256:${'1'.repeat(64)}`,operation_id:'echo',function_principal_id:'example.echo',provider_id:'example.echo',input_schema_digest:`sha256:${'2'.repeat(64)}`,effect_ceiling:['read'],input_schema:{type:'object',properties:{text:{type:'string'},count:{type:'integer'}}},output_schema:{type:'object',properties:{text:{type:'string'}}}};
test('pointer wires use hit testing, preserve keyboard clicks, and reject canceled or incompatible drops',async()=>{
 const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator};
 const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher.test/'});
 Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
 globalThis.IS_REACT_ACT_ENVIRONMENT=true;
 const container=dom.window.document.getElementById('root')!;const root=createRoot(container);
 let latest:any={workflow_api_version:'io.tobkiri.workflow.v4',steps:[]},commits=0,hit:Element|null=null;
 Object.defineProperty(dom.window.document,'elementFromPoint',{value:()=>hit,configurable:true});
 function Harness(){const [value,setValue]=useState(latest);return <WorkflowCanvas document={value} operations={[operation]} onChange={next=>{latest=next;commits++;setValue(JSON.parse(JSON.stringify(next)));}}/>;}
 const button=(label:string)=>{const el=container.querySelector<HTMLButtonElement>(`[aria-label="${label}"]`);assert.ok(el,label);return el;};
 const pointer=async(el:Element,type:string,x=100,y=80,id=1)=>{await act(async()=>el.dispatchEvent(new dom.window.PointerEvent(type,{bubbles:true,button:0,pointerId:id,clientX:x,clientY:y})));};
 const click=async(el:Element,detail=0)=>{await act(async()=>el.dispatchEvent(new dom.window.MouseEvent('click',{bubbles:true,detail})));};
 try{
  await act(async()=>root.render(<Harness/>));
  await click(button('Add node example.echo.v1 / echo'));await click(button('Add node example.echo.v1 / echo'));
  const source=button('Connect data from echo-1.text'),target=button('Connect data to echo-2.text'),count=button('Connect data to echo-2.count');
  let captures=0;source.setPointerCapture=()=>{captures++;};
  await pointer(source,'pointerdown',20,30);await pointer(source,'pointermove');
  assert.equal(captures,1);assert.ok(container.querySelector('[data-flow-preview]'));assert.equal(target.disabled,false);assert.equal(count.disabled,true);assert.match(source.className,/ring-accent/);
  hit=target.querySelector('span');let before=commits;
  await pointer(source,'pointerup');await click(source,1);
  assert.equal(commits,before+1);assert.equal(source.getAttribute('aria-pressed'),'false');assert.equal(container.querySelector('[data-flow-preview]'),null);
  assert.equal(latest.steps[1].request.input.text,'${steps.echo-1.output.text}');assert.deepEqual(latest.steps[1].depends_on,['echo-1']);
  await click(button('Undo graph edit'));assert.deepEqual(latest.steps[1].request.input,{});await click(button('Redo graph edit'));assert.equal(latest.steps[1].request.input.text,'${steps.echo-1.output.text}');
  for(const nextHit of [count,null]){hit=nextHit;before=commits;await pointer(source,'pointerdown',20,30);await pointer(source,'pointermove');await pointer(source,'pointerup');await click(source,1);assert.equal(commits,before);if(nextHit===count)assert.match(container.querySelector('[role="status"]')?.textContent??'',/型が異なる/);}
  hit=target;before=commits;await pointer(source,'pointerdown',20,30);await pointer(source,'pointermove');await pointer(source,'pointerup',100,80,2);assert.ok(container.querySelector('[data-flow-preview]'));await pointer(source,'pointercancel',100,80,2);assert.ok(container.querySelector('[data-flow-preview]'));await pointer(source,'pointercancel');await pointer(source,'pointerup');assert.equal(commits,before);
  await click(source);assert.equal(source.getAttribute('aria-pressed'),'true','keyboard works after pointer cancel');
  await act(async()=>source.dispatchEvent(new dom.window.KeyboardEvent('keydown',{bubbles:true,key:'Escape'})));
  await click(source);assert.equal(source.getAttribute('aria-pressed'),'true','keyboard works after Escape');await click(target);assert.equal(source.getAttribute('aria-pressed'),'false');
  await pointer(source,'pointerdown',20,30);await pointer(source,'pointerup',20,30);await click(source,1);assert.equal(source.getAttribute('aria-pressed'),'true');await click(target);
  // React may batch a continuous move with release: eligibility is schema-based.
  hit=target;before=commits;await pointer(source,'pointerdown',20,30);
  await act(async()=>{source.dispatchEvent(new dom.window.PointerEvent('pointermove',{bubbles:true,button:0,pointerId:1,clientX:100,clientY:80}));source.dispatchEvent(new dom.window.PointerEvent('pointerup',{bubbles:true,button:0,pointerId:1,clientX:100,clientY:80}));});
  await click(source,1);assert.equal(commits,before+1);
  await pointer(source,'pointerdown',20,30);await pointer(source,'pointermove');await pointer(source,'lostpointercapture');assert.equal(container.querySelector('[data-flow-preview]'),null);
  // Preview is expressed in canvas coordinates at zoom, independent of scroll.
  const surface=source.closest('article')!.parentElement!;let left=100;
  Object.defineProperty(surface,'getBoundingClientRect',{value:()=>({left,top:100})});
  await click(button('Zoom out'));await pointer(source,'pointerdown',190,190);await pointer(source,'pointermove',280,280);
  assert.match(container.querySelector('[data-flow-preview]')!.getAttribute('d')!,/^M 100 100 /);
  left=10;await pointer(source,'pointermove',280,280);assert.match(container.querySelector('[data-flow-preview]')!.getAttribute('d')!,/^M 100 100 /);await pointer(source,'pointercancel');
  // A matching-looking control in another canvas is never a valid drop.
  const outside=dom.window.document.createElement('button');outside.dataset.flowInput='data';outside.dataset.flowStep='echo-2';outside.dataset.flowPath='["text"]';dom.window.document.body.append(outside);
  hit=outside;before=commits;await pointer(source,'pointerdown',190,190);await pointer(source,'pointermove',280,280);await pointer(source,'pointerup',280,280);await click(source,1);assert.equal(commits,before);outside.remove();
  const execution=button('Connect execution from echo-1');hit=target;before=commits;await pointer(execution,'pointerdown',20,30);await pointer(execution,'pointermove');await pointer(execution,'pointerup');await click(execution,1);assert.equal(commits,before);assert.match(container.querySelector('[role="status"]')?.textContent??'',/実行順序の端子とデータ/);hit=button('Connect execution to echo-2');await pointer(execution,'pointerdown',20,30);await pointer(execution,'pointermove');await pointer(execution,'pointerup');await click(execution,1);assert.equal(execution.getAttribute('aria-pressed'),'false');
 }finally{await act(async()=>root.unmount());dom.window.close();Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});}
});
