/** Read-only registered tool choices; a descriptor is never execution authority. */
import {fetchFrontendContractOperation} from './defaultspackClient';
export interface WorkflowTool {
  toolId: string;
  name: string;
  definitionHash: string;
  inputSchema: Record<string, unknown>;
  resultSchema?: Record<string, unknown>;
}
export interface WorkflowToolSnapshot {profileId?: string; planDigest?: string; revision: number; tools: WorkflowTool[]}
const record=(v:unknown):v is Record<string,unknown>=>typeof v==='object'&&v!==null&&!Array.isArray(v);
export function parseWorkflowTools(value:unknown):WorkflowToolSnapshot|null {
  if(!record(value)||!Array.isArray(value.tools)||value.tools.length>4096||value.count!==value.tools.length||!Number.isSafeInteger(value.registry_revision)||Number(value.registry_revision)<0)return null;
  const tools:WorkflowTool[]=[],seen=new Set<string>();
  for(const item of value.tools){
    if(!record(item)||typeof item.tool_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$(?![\s\S])/.test(item.tool_id)||seen.has(item.tool_id))return null;
    seen.add(item.tool_id);
    // Older server snapshots may list tools without the new safe projection.
    if(item.definition_hash===undefined&&item.input_schema===undefined)continue;
    if(typeof item.definition_hash!=='string'||!/^[0-9a-f]{64}$/.test(item.definition_hash)||item.definition_hash.length!==64||!record(item.input_schema)||JSON.stringify(item.input_schema).length>32768)return null;
    if(item.result_schema!==undefined&&(item.result_schema_format!=='normalized-result.v1'||!record(item.result_schema)||JSON.stringify(item.result_schema).length>32768))return null;
    if(item.connection_status!=='connected')continue;
    tools.push({toolId:item.tool_id,name:typeof item.name==='string'?item.name.slice(0,512):item.tool_id,definitionHash:item.definition_hash,inputSchema:JSON.parse(JSON.stringify(item.input_schema)),...(record(item.result_schema)?{resultSchema:JSON.parse(JSON.stringify(item.result_schema))}:{})});
  }
  return {revision:Number(value.registry_revision),tools,profileId:typeof value.profile_id==='string'?value.profile_id:undefined,planDigest:typeof value.plan_digest==='string'?value.plan_digest:undefined};
}
export async function loadWorkflowTools(read:()=>Promise<unknown>=()=>fetchFrontendContractOperation('GET','/api/tools/catalog')):Promise<WorkflowToolSnapshot>{
  const parsed=parseWorkflowTools(await read());
  if(!parsed)throw new Error('登録済みツール一覧を確認できません');
  return parsed;
}
