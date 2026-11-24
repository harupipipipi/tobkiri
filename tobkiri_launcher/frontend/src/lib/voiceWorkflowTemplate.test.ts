import assert from 'node:assert/strict';
import test from 'node:test';
import {createVoiceWorkflow,voiceWorkflowOperations} from './voiceWorkflowTemplate';
import type {WorkflowPaletteOperation} from './workflowAuthoring';
const operation=(contract_id:string,fields:string[]):WorkflowPaletteOperation=>({contract_id,contract_revision_digest:`sha256:${'a'.repeat(64)}`,operation_id:'exact.operation',function_principal_id:'exact.function',provider_id:'exact.function',input_schema_digest:`sha256:${'b'.repeat(64)}`,effect_ceiling:[],input_schema:{type:'object',properties:Object.fromEntries(fields.map(field=>[field,{type:'string'}]))}});
const operations=[operation('tobkiri.service.ai.audio.transcribe.v1',['audio','model_profile_id']),operation('tobkiri.service.ai.text.generate.v1',['text','model_profile_id']),operation('tobkiri.service.ai.audio.speech.v1',['input','voice','model_profile_id'])];
test('voice recipe pins only actual admitted operations and does not invent model registrations',()=>{
    const document=createVoiceWorkflow(operations);
    const steps=document.steps as any[];
    assert.equal(steps.length,3);
    assert.equal(steps[1].request.input.text,'${steps.stt.output.text}');
    assert.equal(steps[2].request.input.model_profile_id,'');
    assert.equal(steps[2].request.function_principal_id,'exact.function');
    assert.deepEqual(steps[2].depends_on,['reply']);
});
test('missing or ambiguous operations keep the voice recipe unavailable',()=>{
    assert.equal(voiceWorkflowOperations(operations.slice(1)),null);
    assert.equal(voiceWorkflowOperations([...operations,operations[0]]),null);
    assert.throws(()=>createVoiceWorkflow([]));
});
