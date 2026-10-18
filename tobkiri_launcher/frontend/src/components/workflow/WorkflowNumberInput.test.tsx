import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {JSDOM} from 'jsdom';
import {WorkflowNumberInput} from './WorkflowNumberInput';

test('fractional or unsafe edits clear the executable value and disclose rejection',async()=>{
  const dom=new JSDOM('<div id="root"></div>',{url:'https://launcher.test/'});
  const previous={window:globalThis.window,document:globalThis.document,navigator:globalThis.navigator};
  Object.defineProperties(globalThis,{
    window:{value:dom.window,configurable:true},document:{value:dom.window.document,configurable:true},
    navigator:{value:dom.window.navigator,configurable:true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT=true;
  const {createRoot}=await import('react-dom/client');
  const container=dom.window.document.getElementById('root')!;
  const root=createRoot(container);
  let saved:number|undefined=2;
  const render=()=>root.render(<WorkflowNumberInput value={saved} schema={{type:'number'}} disabled={false} className="" onChange={value=>{saved=value;render();}}/>);
  const setValue=Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value')!.set!;
  try{
    await act(async()=>render());
    for(const invalid of ['0.5','9007199254740992']){
      await act(async()=>{
        const input=container.querySelector('input')!;
        setValue.call(input,invalid);
        input.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
      });
      assert.equal(saved,undefined,'an old accepted value cannot silently execute');
      assert.match(container.querySelector('[role="alert"]')?.textContent??'',/安全な整数/);
    }
    await act(async()=>{
      const input=container.querySelector('input')!;
      setValue.call(input,'3');
      input.dispatchEvent(new dom.window.Event('input',{bubbles:true}));
    });
    assert.equal(saved,3);
    assert.equal(container.querySelector('[role="alert"]'),null);
  }finally{
    await act(async()=>root.unmount());dom.window.close();
    Object.defineProperties(globalThis,Object.fromEntries(Object.entries(previous).map(([key,value])=>[key,{value,configurable:true}])));
  }
});
