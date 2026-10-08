import assert from 'node:assert/strict';
import test from 'node:test';
import {assertWorkflowCanonicalInput} from './workflowCanonicalInput';
import {createWorkflowRun,parseWorkflowRun,type WorkflowAuthoringDependencies} from './workflowAuthoring';
test('canonical input rejects floats, unsafe integers and malformed Unicode locally',()=>{
 for(const value of [0.5,NaN,Infinity,Number.MAX_SAFE_INTEGER+1,'\ud800',undefined])assert.throws(()=>assertWorkflowCanonicalInput({value}));
 assert.doesNotThrow(()=>assertWorkflowCanonicalInput({fraction:'0.5',count:2,emoji:'😀',enabled:true}));
 assert.throws(()=>assertWorkflowCanonicalInput({text:'long'},3));
});
test('invalid run input never reads a capability or starts a mutation',async()=>{
 let calls=0;const dependencies={fetchCatalog:async()=>{calls++;throw new Error('unexpected');},invoke:async()=>{calls++;return {};}} as WorkflowAuthoringDependencies;
 await assert.rejects(()=>createWorkflowRun('voice',`sha256:${'6'.repeat(64)}`,'run-invalid',{temperature:0.5},dependencies),/整数/);
 assert.equal(calls,0);
});
test('ambiguous-effect attempts remain observable without becoming success',()=>{
 const run={run_id:'r',definition_id:'d',revision_digest:`sha256:${'1'.repeat(64)}`,state:'needs_reconciliation'};
 const result=parseWorkflowRun({run,attempts:[{run_id:'r',step_id:'send',attempt_number:1,state:'ambiguous_effect'}]});
 assert.equal(result?.state,'needs_reconciliation');assert.equal(result?.attempts[0].state,'ambiguous_effect');
});
