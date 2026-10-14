import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {JSDOM} from 'jsdom';
import {RegisteredToolPicker} from './RegisteredToolPicker';
import type {WorkflowToolSnapshot} from '@/src/lib/workflowToolCatalog';

test('disabled inspection does not load active Profile tools or accept an older pending read', async () => {
  const previous = {window: globalThis.window, document: globalThis.document, navigator: globalThis.navigator};
  const dom = new JSDOM('<div id="root"></div>', {url: 'https://launcher.test/'});
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root')!;
  const root = createRoot(container);
  let reads = 0;
  let resolve!: (snapshot: WorkflowToolSnapshot) => void;
  const load = () => {reads++; return new Promise<WorkflowToolSnapshot>(done => {resolve = done;});};
  const render = (disabled: boolean) => <RegisteredToolPicker value="" contextKey="profile-a" disabled={disabled} load={load} onSnapshot={() => {}} onChange={() => {throw new Error('No selection was authorized');}}/>;
  try {
    await act(async () => root.render(render(true)));
    assert.equal(reads, 0);
    await act(async () => root.render(render(false)));
    assert.equal(reads, 1);
    await act(async () => root.render(render(true)));
    await act(async () => resolve({revision: 1, tools: [{toolId:'old-tool',name:'Old active tool',definitionHash:'a'.repeat(64),inputSchema:{type:'object'}}]}));
    assert.doesNotMatch(container.textContent ?? '', /Old active tool/);
    assert.equal(container.querySelector('select')?.disabled, true);
    await act(async () => root.render(render(false)));
    assert.equal(reads, 2);
    await act(async () => dom.window.dispatchEvent(new dom.window.Event('tobkiri-tool-definitions-changed')));
    assert.equal(reads, 3, 'an inline tool registration refreshes the current picker');
    await act(async () => root.render(render(true)));
    await act(async () => dom.window.dispatchEvent(new dom.window.Event('tobkiri-tool-definitions-changed')));
    assert.equal(reads, 3, 'inspection must not refresh another active Profile registry');
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    Object.defineProperties(globalThis, {
      window: {value: previous.window, configurable: true},
      document: {value: previous.document, configurable: true},
      navigator: {value: previous.navigator, configurable: true},
    });
  }
});

test('a tool catalog from another Profile or plan is rejected before display',async()=>{
 const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator};
 const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher.test/'});
 Object.defineProperties(globalThis,{window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},navigator:{value:dom.window.navigator,configurable:true}});
 globalThis.IS_REACT_ACT_ENVIRONMENT=true;
 const container=dom.window.document.getElementById('root')!,root=createRoot(container);
 const published:(WorkflowToolSnapshot|null)[]=[];
 const tool={toolId:'private-tool',name:'Wrong Profile tool',definitionHash:'a'.repeat(64),inputSchema:{type:'object'}};
 try{
  for(const mismatch of [{profileId:'profile-b',planDigest:'plan-a'},{profileId:'profile-a',planDigest:'plan-b'}]){
   const load=async()=>({revision:1,tools:[tool],...mismatch});
   await act(async()=>root.render(<RegisteredToolPicker value="" contextKey="profile-a" profileId="profile-a" planDigest="plan-a" load={load} onSnapshot={snapshot=>published.push(snapshot)} onChange={()=>{throw new Error('No selection');}}/>));
   assert.doesNotMatch(container.textContent??'',/Wrong Profile tool/);
   assert.equal(container.querySelector('select')?.disabled,true);
   assert.match(container.textContent??'',/一覧を取得できません/);
  }
  assert.ok(published.every(value=>value===null));
 }finally{await act(async()=>root.unmount());dom.window.close();Object.defineProperties(globalThis,{window:{value:previous.window,configurable:true},document:{value:previous.document,configurable:true},navigator:{value:previous.navigator,configurable:true}});}
});
