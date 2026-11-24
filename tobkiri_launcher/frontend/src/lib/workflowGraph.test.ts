import assert from 'node:assert/strict';
import test from 'node:test';
import { labelGraphNode, connectExecution, connectionError, deleteGraphNode, disconnectExecution, graphWires, schemaPorts, compatiblePorts, setGraphInput } from './workflowGraph';
const document = () => ({ workflow_api_version: 'io.tobkiri.workflow.v4', steps: ['a', 'b', 'c'].map((id) => ({ id, request: { input: {} }, depends_on: [] })) });
test('graph connections preserve document identity fields and reject cycles, duplicates and self edges', () => {
    const original = document();
    const connected = connectExecution(connectExecution(original, 'a', 'b'), 'b', 'c');
    assert.equal(graphWires(connected).length, 2);
    assert.equal(graphWires(original).length, 0);
    assert.match(connectionError(connected, 'c', 'a')!, /循環/);
    assert.ok(connectionError(connected, 'a', 'a'));
    assert.ok(connectionError(connected, 'missing', 'b'));
    assert.throws(() => connectExecution({ ...original, steps: [...original.steps, original.steps[0]] }, 'a', 'b'));
    assert.equal(graphWires(connectExecution(connected, 'a', 'b')).length, 2);
});
test('graph removal and disconnection affect only intended nodes', () => {
    const connected = connectExecution(connectExecution(document(), 'a', 'b'), 'b', 'c');
    assert.deepEqual(graphWires(disconnectExecution(connected, 'a', 'b')), [{ source: 'b', target: 'c', kind: 'execution' }]);
    assert.equal(graphWires(deleteGraphNode(connected, 'b')).length, 0);
});
test('schema ports use declared types only and reject unknown compatibility', () => {
    const ports = schemaPorts({ type: 'object', required: ['text'], properties: { text: { type: 'string' }, count: { type: 'integer' }, unknown: {$ref:'#/$defs/Unknown'}, nested: { type: 'object', properties: { yes: { type: 'boolean' } } } } });
    assert.equal(ports[0].required, true);
    assert.equal(ports.find((p) => p.label === 'nested.yes')?.kind, 'boolean');
    assert.equal(compatiblePorts(ports[0], ports[1]), false);
    assert.equal(compatiblePorts(ports[2], ports[2]), false);
    assert.equal(compatiblePorts(ports[0], ports[0]), true);
});
test('input editing cannot mutate prototypes or neighboring source values', () => {
    const original = document();
    const changed = setGraphInput(original, 'a', ['message'], 'hello');
    assert.deepEqual((changed.steps as any[])[0].request.input, { message: 'hello' });
    assert.deepEqual(original.steps[0].request.input, {});
    assert.throws(() => setGraphInput(original, 'a', ['__proto__', 'bad'], true));
});
test('typed data wire adds order, rejects wrong ports and clears references on removal', async () => {
    const { connectData, dataWires, parseOutputReference } = await import('./workflowGraph');
    const text = schemaPorts({ properties: { text: { type: 'string' } }, required: ['text'] })[0];
    const number = schemaPorts({ properties: { count: { type: 'number' } } })[0];
    const connected = connectData(document(), 'a', text, 'b', text);
    assert.equal(dataWires(connected).length, 1);
    assert.deepEqual((connected.steps as any[])[1].request.input, { text: '${steps.a.output.text}' });
    assert.throws(() => connectData(document(), 'a', number, 'b', text), /型/);
    assert.equal(dataWires(deleteGraphNode(connected, 'a')).length, 0);
    assert.equal(dataWires(disconnectExecution(connected, 'a', 'b')).length, 0);
    assert.equal(parseOutputReference('${steps.a.output.b.output.text}', ['a', 'a.output.b']), null);
    assert.deepEqual(parseOutputReference('${steps.a.b.output.text}', ['a.b']), { step: 'a.b', path: ['text'] });
});

test('palette accepts only the declared reduced-schema protocol, never guessed metadata', async () => {
    const {parseWorkflowPalette} = await import('./workflowAuthoring');
    const operation = {
        contract_id: 'example.echo.v1', contract_revision_digest: `sha256:${'1'.repeat(64)}`,
        operation_id: 'echo', function_principal_id: 'example.echo', provider_id: 'example.echo',
        input_schema_digest: `sha256:${'2'.repeat(64)}`, output_schema_digest: null,
        effect_ceiling: [], projection: 'display-reduced',
        schemas: {input: {type: 'object', properties: {text: {type: 'string'}}}, output: null},
        input_ports: [{name: 'text', required: false, schema: {type: 'string'}}], output_ports: [],
    };
    const palette = {catalog_digest: `sha256:${'3'.repeat(64)}`, security_epoch: 1, operations: [operation], port_binding: {reference_format: '${steps.<step_id>.output.<path>}', requires_depends_on: true}};
    assert.equal(parseWorkflowPalette(palette)?.operations[0].input_schema?.type, 'object');
    assert.equal(parseWorkflowPalette({...palette, operations: [{...operation, projection: 'canonical'}]}), null);
    const withoutProtocol = {...palette}; delete (withoutProtocol as any).port_binding;
    assert.equal(parseWorkflowPalette(withoutProtocol), null);
    assert.equal(parseWorkflowPalette({...palette, operations: [{...operation, script: 'fake'}]}), null);
});

test('undo memory is bounded by count and encoded content size', async () => {
    const {boundedGraphHistory}=await import('./workflowGraph');
    assert.equal(boundedGraphHistory(Array.from({length:60},(_,index)=>({index}))).length,50);
    assert.equal(boundedGraphHistory([{large:'x'.repeat(3*1024*1024)}]).length,0);
    const result=boundedGraphHistory([{older:'x'.repeat(1500000)},{newer:'y'.repeat(1500000)}]);
    assert.equal(result.length,1);
    assert.ok('newer' in result[0]);
});

 test('node display labels never change identity, edges or input bindings', () => {
    const source = {steps: [{id: 'stable', depends_on: ['before'], request: {input: {text: '${inputs.message}'}}}]};
    const renamed = labelGraphNode(source, 'stable', '音声を文字にする');
    assert.deepEqual(renamed.steps, [{...source.steps[0], label: '音声を文字にする'}]);
    assert.deepEqual(labelGraphNode(renamed, 'stable', '').steps, source.steps);
    assert.throws(() => labelGraphNode(source, 'stable', 'x'.repeat(129)));
    assert.deepEqual(source.steps[0].request.input, {text: '${inputs.message}'});
 });

test('generic object ports bind whole values without inventing fields', async()=>{
 const {connectData,dataWires,parseOutputReference}=await import('./workflowGraph');
 const root=schemaPorts({type:'object'})[0];
 assert.deepEqual(root.path,[]);
 const connected=connectData(document(),'a',root,'b',root);
 assert.equal((connected.steps as any[])[1].request.input,'${steps.a.output}');
 assert.deepEqual(dataWires(connected)[0].sourcePath,[]);
 assert.equal((disconnectExecution(connected,'a','b').steps as any[])[1].request.input.constructor,Object);
 assert.deepEqual(parseOutputReference('${steps.a.output}',['a']),{step:'a',path:[]});
 assert.equal(parseOutputReference('${steps.a.output.output}',['a','a.output']),null);
 assert.deepEqual(schemaPorts({type:'object',additionalProperties:false}),[]);
});

test('JSON Pointer binds arbitrary declared property names without collisions',async()=>{
 const {connectData,dataWires,parseOutputReference}=await import('./workflowGraph');
 const schema={type:'object',properties:{'a.b':{type:'string'},a:{type:'object',properties:{b:{type:'string'}}},camelCase:{type:'string'},'x/y':{type:'string'},'t~u':{type:'string'}}};
 const ports=schemaPorts(schema);
 assert.notEqual(ports.find(p=>p.path.length===1&&p.path[0]==='a.b')!.label,ports.find(p=>p.path.length===2)!.label);
 const source=ports.find(p=>p.path[0]==='a.b')!;
 const target=schemaPorts({properties:{text:{type:'string'}}})[0];
 const connected=connectData(document(),'a',source,'b',target);
 assert.equal((connected.steps as any[])[1].request.input.text,'${steps.a.output@/a.b}');
 assert.deepEqual(dataWires(connected)[0].sourcePath,['a.b']);
 assert.throws(()=>connectData(document(),'a',source,'b',target,false));
 assert.deepEqual(parseOutputReference('${steps.a.output@/x~1y/t~0u}',['a']),{step:'a',path:['x/y','t~u']});
 assert.equal(parseOutputReference('${steps.a.output@/bad~2escape}',['a']),null);
});
