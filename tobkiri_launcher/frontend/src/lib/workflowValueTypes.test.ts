import assert from 'node:assert/strict';
import test from 'node:test';
import {schemaPorts, compatiblePorts, workflowPortTypeLabel} from './workflowGraph';
const sound = {type:'object',title:'sound','x-tobkiri-value-type':'tobkiri.value.sound.v1',properties:{data_base64:{type:'string'}}};
const port = (schema:Record<string,unknown>) => schemaPorts({properties:{value:schema}})[0];
test('Sound is one atomic port, not base64 and MIME fields',()=>{
 const ports=schemaPorts({properties:{sound}});
 assert.equal(ports.length,1);
 assert.equal(workflowPortTypeLabel(ports[0]),'sound');
 assert.equal(compatiblePorts(ports[0],port(sound)),true);
 assert.equal(compatiblePorts(ports[0],port({...sound,'x-tobkiri-value-type':'other.value.image.v1'})),false);
 assert.equal(compatiblePorts(ports[0],port({type:'object'})),false);
 assert.equal(compatiblePorts(port({type:'string'}),ports[0]),false);
});
test('configuration role inherits into settings children without changing binding paths',()=>{
 const ports=schemaPorts({properties:{parameters:{type:'object','x-tobkiri-flow-role':'configuration',properties:{speed:{type:['string','integer']}}}}});
 assert.equal(ports[1].role,'configuration');
 assert.equal(ports[1].label,'parameters.speed');
 assert.equal(ports[1].kind,'union');
});
test('an empty schema type union remains unknown',()=>{
 assert.equal(port({type:[]}).kind,'unknown');
});


test('generic JSON types support null, Any and unions without unsafe narrowing',()=>{
 assert.equal(port({}).kind,'json');
 assert.equal(port({type:'null'}).kind,'null');
 assert.equal(compatiblePorts(port({type:'string'}),port({})),true);
 assert.equal(compatiblePorts(port({}),port({type:'string'})),false);
 assert.equal(compatiblePorts(port({type:'integer'}),port({type:'number'})),true);
 assert.equal(compatiblePorts(port({type:'number'}),port({type:'integer'})),false);
 assert.equal(compatiblePorts(port({type:['string','null']}),port({type:'string'})),false);
 assert.equal(compatiblePorts(port({type:'string'}),port({type:['string','null']})),true);
 assert.equal(port({$ref:'#/$defs/Unknown'}).kind,'unknown');
});
