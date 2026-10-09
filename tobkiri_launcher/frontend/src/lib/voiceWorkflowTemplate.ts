/** Optional voice recipe composed only from exact admitted public contracts. */
import type {WorkflowPaletteOperation} from './workflowAuthoring';
import {isGraphRecord} from './workflowGraph';
const contracts={stt:'tobkiri.service.ai.audio.transcribe.v1',reply:'tobkiri.service.ai.text.generate.v1',tts:'tobkiri.service.ai.audio.speech.v1'} as const;
function operationFor(operations:readonly WorkflowPaletteOperation[],contract:string,fields:string[]):WorkflowPaletteOperation|null {
    const matches=operations.filter((operation)=>operation.contract_id===contract);
    if(matches.length!==1)return null;
    const operation=matches[0];
    const properties=operation.input_schema?.properties;
    return isGraphRecord(properties)&&fields.every((field)=>Object.hasOwn(properties,field))?operation:null;
}
export function voiceWorkflowOperations(operations:readonly WorkflowPaletteOperation[]):Record<keyof typeof contracts,WorkflowPaletteOperation>|null {
    const stt=operationFor(operations,contracts.stt,['audio','model_profile_id']);
    const reply=operationFor(operations,contracts.reply,['text','model_profile_id']);
    const tts=operationFor(operations,contracts.tts,['input','voice','model_profile_id']);
    return stt&&reply&&tts?{stt,reply,tts}:null;
}
export function createVoiceWorkflow(operations:readonly WorkflowPaletteOperation[]):Record<string,unknown> {
    const selected=voiceWorkflowOperations(operations);
    if(!selected)throw new Error('音声会話に必要な操作と型情報が揃っていません');
    const step=(id:keyof typeof contracts,input:Record<string,unknown>,depends_on:string[])=>{
        const operation=selected[id];
        return {id,label:({stt:'音声を文字にする',reply:'AIの返答を作る',tts:'返答を読み上げる'})[id],depends_on,request:{contract_id:operation.contract_id,contract_revision_digest:operation.contract_revision_digest,operation_id:operation.operation_id,function_principal_id:operation.function_principal_id,input},retry:{max_attempts:1,backoff_ms:0}};
    };
    return {workflow_api_version:'io.tobkiri.workflow.v4',name:'音声で会話',max_concurrency:1,steps:[
        step('stt',{audio:'${inputs.audio}',model_profile_id:''},[]),
        step('reply',{text:'${steps.stt.output.text}',model_profile_id:''},['stt']),
        step('tts',{input:'${steps.reply.output.text}',voice:'',model_profile_id:''},['reply']),
    ]};
}
