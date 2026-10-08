import assert from 'node:assert/strict';
import test from 'node:test';
import {readWorkflowLayout,saveWorkflowLayout} from './workflowLayout';
test('layout is scoped to the selected Profile/definition and contains only coordinates',()=>{
 const records=new Map<string,string>();const storage={getItem:(key:string)=>records.get(key)??null,setItem:(key:string,value:string)=>{records.set(key,value);}};
 assert.equal(saveWorkflowLayout(storage,'profile-a:flow',{one:{x:10,y:20},other:{x:30,y:40}},new Set(['one'])),true);
 assert.deepEqual(readWorkflowLayout(storage,'profile-a:flow',new Set(['one'])),{one:{x:10,y:20}});
 assert.deepEqual(readWorkflowLayout(storage,'profile-b:flow',new Set(['one'])),{});
 assert.equal([...records.values()][0],'{"one":{"x":10,"y":20}}');
});
test('malformed, excessive and stale coordinates are ignored without inherited keys',()=>{
 const storage={getItem:()=>'{"constructor":{"x":12,"y":24},"bad":{"x":1e99,"y":1},"old":{"x":1,"y":2}}'};
 const value=readWorkflowLayout(storage,'flow',new Set(['constructor','bad']));
 assert.deepEqual(Object.keys(value),['constructor']);assert.deepEqual(value.constructor,{x:12,y:24});
 assert.deepEqual(readWorkflowLayout({getItem:()=>'{bad'},'flow',new Set()),{});
 assert.equal(saveWorkflowLayout({setItem:()=>{throw new Error('blocked');}},'flow',{},new Set()),false);
});
