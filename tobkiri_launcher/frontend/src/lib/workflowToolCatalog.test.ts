import assert from 'node:assert/strict';
import test from 'node:test';
import {parseWorkflowTools} from './workflowToolCatalog';
const tool={tool_id:'calculator',name:'計算',connection_status:'connected',definition_hash:'a'.repeat(64),input_schema:{type:'object',properties:{expression:{type:'string'}}}};
const snapshot=(tools:unknown[])=>({tools,count:tools.length,registry_revision:4});
test('registered tool projection carries only pinned editable fields',()=>{
 const result=parseWorkflowTools(snapshot([{...tool,authority:'fake',execution:{provider:'fake'},api_key:'not-for-ui'}]));
 assert.ok(result);assert.equal(result.tools[0].definitionHash,tool.definition_hash);
 assert.deepEqual(Object.keys(result.tools[0]).sort(),['definitionHash','inputSchema','name','toolId']);
 result.tools[0].inputSchema.properties={};
 assert.ok(tool.input_schema.properties.expression);
});
test('old or disconnected descriptors cannot be offered as executable typed choices',()=>{
 assert.deepEqual(parseWorkflowTools(snapshot([{tool_id:'old',name:'old'}]))?.tools,[]);
 assert.deepEqual(parseWorkflowTools(snapshot([{...tool,connection_status:'unavailable'}]))?.tools,[]);
});
test('malformed identities, duplicate tools and stale descriptor shapes fail closed',()=>{
 for(const item of [{...tool,tool_id:'calculator\n'},{...tool,definition_hash:'a'.repeat(64)+'\n'},{...tool,input_schema:[]},{...tool,definition_hash:undefined}])assert.equal(parseWorkflowTools(snapshot([item])),null);
 assert.equal(parseWorkflowTools(snapshot([tool,tool])),null);
});


test('result schemas remain bound to the same registered definition',()=>{
 const value=parseWorkflowTools(snapshot([{...tool,result_schema:{type:'string'},result_schema_format:'normalized-result.v1'}]));
 assert.deepEqual(value?.tools[0].resultSchema,{type:'string'});
 assert.equal(parseWorkflowTools(snapshot([{...tool,result_schema:[]} ])),null);
});

test('an advisory result schema is not trusted as a typed output declaration',()=>{
 for(const result_schema_format of [undefined,'unknown.v1'])
  assert.equal(parseWorkflowTools(snapshot([{...tool,result_schema:{type:'string'},result_schema_format}])),null);
});
