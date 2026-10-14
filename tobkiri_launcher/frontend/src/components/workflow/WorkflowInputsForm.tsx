import {WorkflowNumberInput} from './WorkflowNumberInput';
import {useEffect,useRef,useState} from 'react';
import type {WorkflowPaletteOperation} from '@/src/lib/workflowAuthoring';
import {isGraphRecord} from '@/src/lib/workflowGraph';
import {encodeInlineAudio,isInlineAudioSchema,workflowInputFields,type WorkflowInputField} from '@/src/lib/workflowInputs';
import {RegisteredModelPicker} from './RegisteredModelPicker';
export function WorkflowInputsForm({document,operations,values,disabled,contextKey,onChange,onBusyChange}:{document:Record<string,unknown>;operations:readonly WorkflowPaletteOperation[];values:Record<string,unknown>;disabled:boolean;contextKey:string;onChange:(path:string[],value:unknown)=>void;onBusyChange:(field:string,busy:boolean)=>void}){
    const fields=workflowInputFields(document,operations);
    return <div className="mt-3 space-y-3">{fields.length?fields.map(field=><WorkflowInput key={field.name} field={field} values={values} disabled={disabled} contextKey={contextKey} onChange={onChange} onBusyChange={onBusyChange}/>):<p className="text-xs text-text-muted">このFlowには開始時に指定する入力がありません</p>}</div>;
}
function WorkflowInput({field,values,disabled,contextKey,onChange,onBusyChange}:{field:WorkflowInputField;values:Record<string,unknown>;disabled:boolean;contextKey:string;onChange:(path:string[],value:unknown)=>void;onBusyChange:(field:string,busy:boolean)=>void}){
    let value:unknown=values;for(const part of field.path)value=isGraphRecord(value)?value[part]:undefined;
    const [error,setError]=useState('');
    const [loading,setLoading]=useState(false);
    const generation=useRef(0),active=useRef(false);
    useEffect(()=>{active.current=true;return()=>{active.current=false;++generation.current;onBusyChange(field.name,false);};},[contextKey]);
    const schema=field.schema;
    const common='mt-1 min-h-10 w-full rounded border border-border bg-bg-main px-3 py-2 text-sm';
    const selectFile=async(file:File|undefined)=>{
        if(!file)return;
        const current=++generation.current;setError('');setLoading(true);onBusyChange(field.name,true);onChange(field.path,undefined);
        try{const content=await encodeInlineAudio(file);if(active.current&&current===generation.current)onChange(field.path,content);}
        catch{if(active.current&&current===generation.current)setError('音声を読み込めません。1MiB以下の対応する音声ファイルを選び直してください');}
        finally{if(active.current&&current===generation.current){setLoading(false);onBusyChange(field.name,false);}}
    };
    const model=schema['x-tobkiri-selector']==='model-profile'||(Array.isArray(schema['x-tobkiri-selector'])&&schema['x-tobkiri-selector'].includes('model-profile'));
    return <div><label className="block text-sm font-medium">{typeof schema.title==='string'?schema.title:field.name}
        {isInlineAudioSchema(schema)?<><input type="file" aria-label={`Audio input ${field.name}`} accept="audio/*,.mp3,.wav,.m4a,.flac,.ogg,.webm" disabled={disabled||loading} className={common} onChange={event=>void selectFile(event.target.files?.[0])}/><span className="mt-1 block text-xs text-text-muted">{loading?'音声を読み込み中…':isGraphRecord(value)&&typeof value.byte_size==='number'?`${Math.ceil(value.byte_size/1024)}KiBの音声を準備済み。実行するとFlowの接続先の処理へ渡されます`:'MP3などの音声を選択（上限1MiB）。選択だけでは送信しません'}</span></>:model?<RegisteredModelPicker value={typeof value==='string'?value:''} disabled={disabled} contextKey={contextKey} onChange={id=>onChange(field.path,id)}/>:schema.type==='boolean'?<select className={common} value={value===true?'true':value===false?'false':''} disabled={disabled} onChange={event=>onChange(field.path,event.target.value===''?undefined:event.target.value==='true')}><option value="">選択してください</option><option value="true">true</option><option value="false">false</option></select>:schema.type==='string'?<textarea className={common} value={typeof value==='string'?value:''} disabled={disabled} maxLength={typeof schema.maxLength==='number'?schema.maxLength:262144} onChange={event=>onChange(field.path,event.target.value)}/>:schema.type==='integer'||schema.type==='number'?<WorkflowNumberInput className={common} value={value} disabled={disabled} schema={schema} onChange={next=>onChange(field.path,next)}/>:<textarea aria-label={`JSON input ${field.name}`} className={common} defaultValue={value===undefined?'':JSON.stringify(value,null,2)} disabled={disabled} onBlur={event=>{try{onChange(field.path,JSON.parse(event.target.value));setError('');}catch{onChange(field.path,undefined);setError('有効なJSONを入力してください');}}}/>}</label>
        <p className="mt-1 break-all text-xs text-text-muted">使用先: {field.consumers.join(' / ')}</p>{error&&<p role="alert" className="mt-1 text-xs text-destructive">{error}</p>}
    </div>;
}
