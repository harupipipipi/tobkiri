import assert from 'node:assert/strict';
import test from 'node:test';
import {encodeInlineAudio,workflowInputFields,setWorkflowInputValue} from './workflowInputs';
import {parseWorkflowAudio,verifyWorkflowAudio} from './workflowAudio';
test('user-selected inline audio is size-bound, hashed and never contains its source filename',async()=>{
    const bytes=new TextEncoder().encode('ID3 synthetic fixture, not playback proof');
    const content=await encodeInlineAudio({name:'private-name.mp3',type:'audio/mpeg',size:bytes.length,arrayBuffer:async()=>bytes.buffer});
    assert.equal(content.filename,'audio.mp3');
    assert.equal(content.byte_size,bytes.length);
    assert.doesNotMatch(JSON.stringify(content),/private-name/);
    const parsed=parseWorkflowAudio({...content,url:'https://not-fetched.example/private'});
    assert.ok(parsed);
    assert.ok(!('url' in parsed));
    const blob=await verifyWorkflowAudio(parsed);
    assert.equal(blob.size,bytes.length);
    assert.equal(blob.type,'audio/mpeg');
    await assert.rejects(()=>verifyWorkflowAudio({...parsed,content_id:`sha256:${'0'.repeat(64)}`}));
});
test('oversized or unsupported audio is rejected before reading file bytes',async()=>{
    let reads=0;
    const file={name:'audio.mp3',type:'audio/mpeg',size:4*1024*1024+1,arrayBuffer:async()=>{reads++;return new ArrayBuffer(1);}};
    await assert.rejects(()=>encodeInlineAudio(file));assert.equal(reads,0);
    await assert.rejects(()=>encodeInlineAudio({...file,size:1,type:'text/html'}));assert.equal(reads,0);
});
test('initial input editing cannot pollute objects or silently grant authority',()=>{
    const value=setWorkflowInputValue({},['audio','content_id'],'sha256:fixture');
    assert.deepEqual(value,{audio:{content_id:'sha256:fixture'}});
    assert.throws(()=>setWorkflowInputValue({},['__proto__','x'],true));
    const document={steps:[{id:'stt',request:{input:{audio:'${inputs.audio}'}}}]};
    assert.deepEqual(workflowInputFields(document,[]).map(field=>field.name),['audio']);
});
