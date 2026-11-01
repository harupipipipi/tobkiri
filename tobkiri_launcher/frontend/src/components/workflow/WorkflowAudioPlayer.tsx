import {useEffect,useState} from 'react';
import {verifyWorkflowAudio,type WorkflowAudioContent} from '@/src/lib/workflowAudio';
/** Blob URL only, verified before rendering; never load a Provider-supplied URL. */
export function WorkflowAudioPlayer({content}:{content:WorkflowAudioContent}){
    const [url,setUrl]=useState<string|null>(null),[failed,setFailed]=useState(false);
    useEffect(()=>{
        let active=true,owned:string|null=null;
        setUrl(null);setFailed(false);
        void verifyWorkflowAudio(content).then(blob=>{
            if(!active)return;
            owned=URL.createObjectURL(blob);setUrl(owned);
        }).catch(()=>{if(active)setFailed(true);});
        return()=>{active=false;if(owned)URL.revokeObjectURL(owned);};
    },[content.content_id,content.data_base64,content.media_type,content.byte_size]);
    if(failed)return <p role="alert" className="text-xs text-destructive">音声の整合性を確認できないため再生できません</p>;
    if(!url)return <p className="text-xs text-text-muted">音声を確認中…</p>;
    const raw=content.media_type==='audio/l16';
    return <div className="space-y-2">{!raw&&<audio controls preload="none" src={url} className="w-full" aria-label="Flow audio result"/>}{raw&&<p className="text-xs text-text-muted">PCM音声はダウンロードして再生してください</p>}<a href={url} download={raw?'flow-audio.pcm':'flow-audio'} className="inline-flex min-h-9 items-center text-xs text-accent">音声をダウンロード</a></div>;
}
