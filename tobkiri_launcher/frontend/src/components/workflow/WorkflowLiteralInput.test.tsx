import assert from 'node:assert/strict';
import test from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {WorkflowLiteralInput} from './WorkflowLiteralInput';
import type {WorkflowPort} from '@/src/lib/workflowGraph';
const port=(schema:Record<string,unknown>):WorkflowPort=>({path:['value'],label:'value',kind:schema.type as WorkflowPort['kind'],required:true,schema});
test('schema enum renders a finite picker including false and zero literals',()=>{
 const html=renderToStaticMarkup(<WorkflowLiteralInput port={port({type:'integer',enum:[0,1,2]})} value={0} disabled={false} onChange={()=>{}}/>);
 assert.match(html,/<select/);assert.match(html,/<option value="0" selected="">0<\/option>/);assert.doesNotMatch(html,/<input/);
});
test('long text uses a bounded multiline editor with opaque identifiers uncorrected',()=>{
 const html=renderToStaticMarkup(<WorkflowLiteralInput port={port({type:'string',maxLength:4096})} value={'hello\n<script>fixture</script>'} disabled={false} onChange={()=>{}}/>);
 assert.match(html,/<textarea/);assert.match(html,/maxLength="4096"/);assert.match(html,/autoCorrect="off"/);assert.match(html,/&lt;script&gt;/);
});
test('numeric bounds and disabled state remain visible in the editor',()=>{
 const html=renderToStaticMarkup(<WorkflowLiteralInput port={port({type:'integer',minimum:1,maximum:5})} value={2} disabled onChange={()=>{}}/>);
 assert.match(html,/step="1"/);assert.match(html,/min="1"/);assert.match(html,/max="5"/);assert.match(html,/disabled=""/);
});
test('number ports disclose the canonical integer restriction instead of inviting fractions',()=>{
 const html=renderToStaticMarkup(<WorkflowLiteralInput port={port({type:'number'})} value={2} disabled={false} onChange={()=>{}}/>);
 assert.match(html,/step="1"/);assert.doesNotMatch(html,/step="any"/);assert.match(html,/安全な整数のみ対応/);
});
