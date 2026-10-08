import {WorkflowNumberInput} from './WorkflowNumberInput';
import {schemaValueKinds} from '@/src/lib/workflowValueKinds';
import {useEffect,useState} from 'react';
import {assertWorkflowCanonicalInput} from '@/src/lib/workflowCanonicalInput';
import type {WorkflowPort} from '@/src/lib/workflowGraph';

/** Render literals from the captured schema; never infer an executable action. */
export function WorkflowLiteralInput({port,value,disabled,onChange}:{port:WorkflowPort;value:unknown;disabled:boolean;onChange:(value:unknown)=>void}) {
  const field='mt-1 min-h-10 w-full rounded border border-border bg-bg-main px-2 py-2 text-sm';
  if(port.kind==='object'||port.kind==='array'||port.kind==='json'||port.kind==='union')return <StructuredLiteral port={port} value={value} disabled={disabled} onChange={onChange}/>;
  if(port.kind==='null')return <select className={field} disabled={disabled} value={value===null?'null':''} onChange={event=>onChange(event.target.value==='null'?null:undefined)}><option value="">未設定</option><option value="null">null</option></select>;
  const choices=Array.isArray(port.schema.enum)&&port.schema.enum.length<=128
    &&port.schema.enum.every(item=>item===null||['string','number','boolean'].includes(typeof item))?port.schema.enum:null;
  if(choices)return <select className={field} disabled={disabled} value={choices.findIndex(item=>item===value)} onChange={event=>{const index=Number(event.target.value);onChange(index<0?undefined:choices[index]);}}>
    <option value={-1}>未設定</option>{choices.map((item,index)=><option key={index} value={index}>{String(item)}</option>)}
  </select>;
  if(port.kind==='boolean')return <select className={field} disabled={disabled} value={value===true?'true':value===false?'false':''} onChange={event=>onChange(event.target.value===''?undefined:event.target.value==='true')}><option value="">未設定</option><option value="true">true</option><option value="false">false</option></select>;
  if(port.kind==='string')return <textarea className={`${field} min-h-24 resize-y`} disabled={disabled} value={typeof value==='string'||typeof value==='number'?String(value):''} maxLength={typeof port.schema.maxLength==='number'?Math.min(port.schema.maxLength,262144):262144} spellCheck={false} autoCorrect="off" autoCapitalize="none" onChange={event=>onChange(event.target.value)}/>;
  return <WorkflowNumberInput className={field} disabled={disabled} value={value} schema={port.schema} onChange={onChange}/>;
}

/** Generic structured values are explicit JSON, never guessed field schemas. */
function StructuredLiteral({port,value,disabled,onChange}:{port:WorkflowPort;value:unknown;disabled:boolean;onChange:(value:unknown)=>void}) {
 const encoded=value===undefined?'':JSON.stringify(value,null,2);
 const [draft,setDraft]=useState(encoded),[error,setError]=useState('');
 useEffect(()=>{setDraft(encoded);setError('');},[encoded]);
 return <><textarea className="mt-1 min-h-24 w-full rounded border border-border bg-bg-main p-2 font-mono text-xs" aria-label={`JSON value ${port.label}`} disabled={disabled} value={draft} spellCheck={false} maxLength={262144} onChange={event=>{setDraft(event.target.value);setError('');}} onBlur={()=>{
  try {
   if(!draft.trim()){onChange(undefined);return;}
   const parsed:unknown=JSON.parse(draft);
   const kind=parsed===null?'null':Array.isArray(parsed)?'array':typeof parsed==='number'?(Number.isInteger(parsed)?'integer':'number'):typeof parsed;
   if(!schemaValueKinds(port.schema)?.includes(kind as never))throw new Error('declared type');
   assertWorkflowCanonicalInput(parsed);
   onChange(parsed);setError('');
  }catch{setError('宣言された型のJSONを入力してください。未保存です');}
 }}/>{error&&<span role="alert" className="block text-destructive">{error}</span>}</>;
}
