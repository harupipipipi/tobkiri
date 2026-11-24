/** Finite JSON value kinds, not coercions or permission decisions. */
export type JsonValueKind='string'|'number'|'integer'|'boolean'|'object'|'array'|'null';
const all:JsonValueKind[]=['string','number','integer','boolean','object','array','null'];
const record=(value:unknown):value is Record<string,unknown>=>Boolean(value)&&typeof value==='object'&&!Array.isArray(value);
const literal=(value:unknown):JsonValueKind=>value===null?'null':Array.isArray(value)?'array':typeof value==='number'?(Number.isInteger(value)?'integer':'number'):typeof value as JsonValueKind;
export function schemaValueKinds(schema:Record<string,unknown>,depth=0):JsonValueKind[]|null{
 if(depth>8||'$ref' in schema)return null;
 if('const' in schema)return [literal(schema.const)];
 if(Array.isArray(schema.enum)&&schema.enum.length&&schema.enum.length<=128)return [...new Set(schema.enum.map(literal))];
 const types=typeof schema.type==='string'?[schema.type]:schema.type;
 if(Array.isArray(types)){
  if(!types.length||types.some(type=>!all.includes(type as JsonValueKind)))return null;
  return [...new Set((types.includes('number')?[...types,'integer']:types) as JsonValueKind[])];
 }
 for(const key of ['anyOf','oneOf']){
  const branches=schema[key];
  if(Array.isArray(branches)){
   if(!branches.length||branches.length>32)return null;
   const sets=branches.map(branch=>record(branch)?schemaValueKinds(branch,depth+1):null);
   if(sets.some(set=>set===null))return null;
   return [...new Set(sets.flat() as JsonValueKind[])];
  }
 }
 // An explicitly unconstrained JSON Schema is Any. Complex unsupported
 // constraints are Unknown rather than guessed as a specific type.
 if(Object.keys(schema).every(key=>['title','description','default','examples','x-tobkiri-flow-role','x-tobkiri-value-type'].includes(key)))return [...all];
 return null;
}
export function kindsCompatible(source:Record<string,unknown>,target:Record<string,unknown>):boolean{
 const from=schemaValueKinds(source),to=schemaValueKinds(target);
 return Boolean(from&&to&&from.every(kind=>to.includes(kind)));
}
