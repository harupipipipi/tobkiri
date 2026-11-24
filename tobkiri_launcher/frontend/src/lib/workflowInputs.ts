import type {WorkflowPaletteOperation} from './workflowAuthoring';
import {exactGraphOperation,graphSteps,isGraphRecord} from './workflowGraph';
export interface WorkflowInputField {name:string;path:string[];schema:Record<string,unknown>;consumers:string[]}
const safePath=(parts:string[])=>parts.length>0&&parts.length<=16&&parts.every(part=>/^[a-z][a-z0-9_-]*$/.test(part)&&!['__proto__','prototype','constructor'].includes(part));
export function workflowInputFields(document:Record<string,unknown>,operations:readonly WorkflowPaletteOperation[]):WorkflowInputField[]{
    const fields=new Map<string,WorkflowInputField>();
    const walk=(value:unknown,schema:unknown,consumer:string,depth:number)=>{
        if(depth>16)return;
        if(typeof value==='string'){
            const match=/^\$\{inputs\.([a-z][a-z0-9_.-]*)\}$/.exec(value);
            if(!match)return;
            const path=match[1].split('.');if(!safePath(path))return;
            const current=fields.get(match[1]);
            const normalized=isGraphRecord(schema)?schema:{};
            if(current){current.consumers.push(consumer);if(JSON.stringify(current.schema)!==JSON.stringify(normalized))current.schema={};}
            else fields.set(match[1],{name:match[1],path,schema:normalized,consumers:[consumer]});
        }else if(isGraphRecord(value)){
            const properties=isGraphRecord(schema)&&isGraphRecord(schema.properties)?schema.properties:{};
            for(const [key,item]of Object.entries(value))walk(item,properties[key],`${consumer}.${key}`,depth+1);
        }else if(Array.isArray(value)){
            for(const [index,item]of value.entries())walk(item,isGraphRecord(schema)?schema.items:null,`${consumer}[${index}]`,depth+1);
        }
    };
    for(const step of graphSteps(document))if(isGraphRecord(step.request))walk(step.request.input,exactGraphOperation(step,operations)?.input_schema,String(step.id),0);
    return [...fields.values()].slice(0,128);
}
export function setWorkflowInputValue(current:Record<string,unknown>,path:string[],value:unknown):Record<string,unknown>{
    if(!safePath(path))throw new Error('開始入力の名前が不正です');
    const next=JSON.parse(JSON.stringify(current)) as Record<string,unknown>;
    let target=next;
    for(const part of path.slice(0,-1)){if(!isGraphRecord(target[part]))target[part]={};target=target[part] as Record<string,unknown>;}
    if(value===undefined)delete target[path.at(-1)!];else target[path.at(-1)!]=value;
    return next;
}
export function isInlineAudioSchema(schema:Record<string,unknown>):boolean{
    const props=schema.properties;
    return schema.type==='object'&&isGraphRecord(props)&&['content_id','media_type','data_base64'].every(key=>isGraphRecord(props[key])&&props[key].type==='string');
}
export const MAX_INLINE_AUDIO_BYTES=1024*1024;
const mediaByExtension:Record<string,string>={mp3:'audio/mpeg',wav:'audio/wav',m4a:'audio/mp4',mp4:'audio/mp4',aac:'audio/aac',flac:'audio/flac',ogg:'audio/ogg',opus:'audio/ogg',webm:'audio/webm'};
const allowedMedia=new Set([...Object.values(mediaByExtension),'audio/mp3','audio/x-wav','audio/wave','audio/m4a']);
export interface InlineAudioContent {content_id:string;media_type:string;data_base64:string;byte_size:number;filename:string}
/** User-selected bytes only: never a filesystem path, URL or authority handle. */
export async function encodeInlineAudio(file:Pick<File,'size'|'type'|'name'|'arrayBuffer'>):Promise<InlineAudioContent>{
    if(!Number.isSafeInteger(file.size)||file.size<1||file.size>MAX_INLINE_AUDIO_BYTES)throw new Error('音声は1MiB以下のファイルを選択してください');
    const extension=file.name.split('.').at(-1)?.toLowerCase()??'';
    const media=file.type||mediaByExtension[extension];
    if(!allowedMedia.has(media))throw new Error('対応する音声ファイルを選択してください');
    const buffer=await file.arrayBuffer();
    if(buffer.byteLength!==file.size)throw new Error('音声ファイルのサイズを確認できません');
    const bytes=new Uint8Array(buffer);
    let binary='';
    for(let index=0;index<bytes.length;index+=32768)binary+=String.fromCharCode(...bytes.subarray(index,index+32768));
    const hash=new Uint8Array(await crypto.subtle.digest('SHA-256',buffer));
    return {content_id:`sha256:${Array.from(hash,b=>b.toString(16).padStart(2,'0')).join('')}`,media_type:media,data_base64:btoa(binary),byte_size:bytes.length,filename:`audio.${Object.entries(mediaByExtension).find(([,type])=>type===media)?.[0] ?? (media.includes('wav')||media==='audio/wave'?'wav':'mp3')}`};
}
