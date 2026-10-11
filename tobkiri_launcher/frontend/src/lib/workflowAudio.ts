/** Bounded inline audio values. A content hash checks bytes, not ownership. */
export interface WorkflowAudioContent {content_id:string;media_type:string;data_base64:string;byte_size:number}
const MAX_AUDIO=1024*1024;
const MEDIA=new Set(['audio/mpeg','audio/mp3','audio/wav','audio/x-wav','audio/wave','audio/mp4','audio/m4a','audio/aac','audio/flac','audio/ogg','audio/webm','audio/l16']);
export function parseWorkflowAudio(value:unknown):WorkflowAudioContent|null{
    if(!value||typeof value!=='object'||Array.isArray(value))return null;
    const row=value as Record<string,unknown>;
    if(typeof row.content_id!=='string'||!/^sha256:[a-f0-9]{64}$/.test(row.content_id)||typeof row.media_type!=='string'||!MEDIA.has(row.media_type)||typeof row.data_base64!=='string'||row.data_base64.length>Math.ceil(MAX_AUDIO/3)*4||row.data_base64.length%4!==0||!row.data_base64.length||!/^[A-Za-z0-9+/]+={0,2}$/.test(row.data_base64)||!Number.isSafeInteger(row.byte_size)||Number(row.byte_size)<1||Number(row.byte_size)>MAX_AUDIO)return null;
    return {content_id:row.content_id,media_type:row.media_type,data_base64:row.data_base64,byte_size:Number(row.byte_size)};
}
export async function verifyWorkflowAudio(content:WorkflowAudioContent):Promise<Blob>{
    const checked=parseWorkflowAudio(content);if(!checked)throw new Error('音声データが不正です');
    const binary=atob(checked.data_base64);
    if(binary.length!==checked.byte_size)throw new Error('音声サイズが一致しません');
    const bytes=Uint8Array.from(binary,char=>char.charCodeAt(0));
    const hash=new Uint8Array(await crypto.subtle.digest('SHA-256',bytes));
    const digest=`sha256:${Array.from(hash,b=>b.toString(16).padStart(2,'0')).join('')}`;
    if(digest!==checked.content_id)throw new Error('音声の内容を確認できません');
    return new Blob([bytes],{type:checked.media_type});
}
