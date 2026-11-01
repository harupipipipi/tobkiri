import {schemaValueKinds,kindsCompatible} from './workflowValueKinds';
/** Local graph editing. This module never grants authority or invokes a Pack. */
import type { WorkflowPaletteOperation } from './workflowAuthoring';
export type PortKind = 'execution' | 'string' | 'number' | 'integer' | 'boolean' | 'object' | 'array' | 'null' | 'json' | 'union' | 'unknown';
export interface WorkflowPort {
    path: string[];
    label: string;
    kind: PortKind;
    required: boolean;
    schema: Record<string, unknown>;
    valueType?: string;
    role?: 'data' | 'configuration' | 'metadata';
}
export interface WorkflowNodeSchema {
    input: Record<string, unknown>;
    output: Record<string, unknown>;
}
export interface WorkflowWire {
    source: string;
    target: string;
    kind: 'execution' | 'data';
    sourcePath?: string[];
    targetPath?: string[];
}
export const isGraphRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
export const workflowPathLabel = (path: readonly string[]): string => !path.length ? '$' : path.every(part => /^[a-z][a-z0-9_-]*$/.test(part)) ? path.join('.') : '/' + path.map(part=>part.replace(/~/g,'~0').replace(/\//g,'~1')).join('/');
const clone = (value: Record<string, unknown>) => JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
export const graphSteps = (document: Record<string, unknown>): Record<string, unknown>[] => Array.isArray(document.steps) ? document.steps.filter(isGraphRecord) : [];
export function exactGraphOperation(step: Record<string, unknown>, operations: readonly WorkflowPaletteOperation[]): WorkflowPaletteOperation | undefined {
    const request = isGraphRecord(step.request) ? step.request : {};
    return operations.find((operation) => ['contract_id', 'contract_revision_digest', 'operation_id', 'function_principal_id'].every((key) => operation[key] === request[key]));
}
/** Only schema-declared ports are offered. Unknown schemas are never guessed. */
export function workflowValueType(schema: Record<string, unknown>): string | undefined {
    const value = schema['x-tobkiri-value-type'];
    return typeof value === 'string' && /^[a-z][a-z0-9._-]{0,119}\.v[1-9][0-9]{0,3}$/.test(value) ? value : undefined;
}
export function workflowPortTypeLabel(port: WorkflowPort): string {
    return port.valueType ? port.valueType.split('.').at(-2) ?? port.valueType : port.kind==='union' ? (schemaValueKinds(port.schema)??[]).join(' | ') : port.kind==='json' ? 'JSON' : (port.kind === 'string' ? 'text' : port.kind);
}
export function schemaPorts(schema: Record<string, unknown>, prefix: string[] = [], depth = 0, inheritedRole: WorkflowPort['role'] = 'data'): WorkflowPort[] {
    if (depth > 4) return [];
    if (!prefix.length && (workflowValueType(schema) || !isGraphRecord(schema.properties) || !Object.keys(schema.properties).length)) {
        if (schema.type !== 'object') return [];
        if (schema.type === 'object' && schema.additionalProperties === false && (!isGraphRecord(schema.properties) || !Object.keys(schema.properties).length)) return [];
        return [{path:[],label:'$',kind:schema.type,required:true,schema,valueType:workflowValueType(schema),role:inheritedRole}];
    }
    if (!isGraphRecord(schema.properties)) return [];
    const required = Array.isArray(schema.required) ? schema.required : [];
    return Object.entries(schema.properties).slice(0, 64).flatMap(([name, raw]) => {
        if (!isGraphRecord(raw) || ['__proto__', 'prototype', 'constructor'].includes(name))
            return [];
        const kinds = schemaValueKinds(raw);
        const kind:PortKind = !kinds ? 'unknown' : kinds.length===1 ? kinds[0] : kinds.length===7 ? 'json' : kinds.length===2&&kinds.includes('number')&&kinds.includes('integer') ? 'number' : 'union';
        const valueType = workflowValueType(raw);
        const hint = raw['x-tobkiri-flow-role'];
        const role = hint === 'configuration' || hint === 'metadata' || hint === 'data' ? hint : inheritedRole;
        const path = [...prefix, name];
        return [{ path, label: workflowPathLabel(path), kind, required: required.includes(name), schema: raw, role, valueType }, ...(valueType ? [] : schemaPorts(raw, path, depth + 1, role))];
    }).slice(0, 128);
}
/** Human labels never replace the canonical path used by bindings. */
export function workflowPortTitle(port: WorkflowPort): string {
    const title = port.schema.title;
    return typeof title === 'string' && title.trim() && title.length <= 128 ? title : port.path.length ? port.label : 'データ全体';
}
/** A conservative editor hint; the Host validates the actual resolved value. */
export function compatiblePorts(source: WorkflowPort, target: WorkflowPort, depth = 0): boolean {
    if (depth > 8)
        return false;
    const sourceType = workflowValueType(source.schema), targetType = workflowValueType(target.schema);
    if ((sourceType || targetType) && sourceType !== targetType) return false;
    if (source.kind === 'unknown' || target.kind === 'unknown')
        return false;
    if (!kindsCompatible(source.schema,target.schema)) return false;
    if (Object.hasOwn(target.schema,'const')) return Object.hasOwn(source.schema,'const') ? JSON.stringify(source.schema.const)===JSON.stringify(target.schema.const) : Array.isArray(source.schema.enum)&&source.schema.enum.length>0&&source.schema.enum.every(value=>JSON.stringify(value)===JSON.stringify(target.schema.const));
    const allowed = target.schema.enum;
    if (Array.isArray(allowed))
        return Object.hasOwn(source.schema,'const') ? allowed.some(value=>JSON.stringify(value)===JSON.stringify(source.schema.const)) : Array.isArray(source.schema.enum) && source.schema.enum.length>0 && source.schema.enum.every(value=>allowed.some(item=>JSON.stringify(item)===JSON.stringify(value)));
    if (source.kind === 'array' && isGraphRecord(target.schema.items)) {
        if (!isGraphRecord(source.schema.items))
            return false;
        const item = (schema: Record<string, unknown>): WorkflowPort => ({ path: [], label: '', kind: typeof schema.type === 'string' ? schema.type as PortKind : 'unknown', required: true, schema });
        if (!compatiblePorts(item(source.schema.items), item(target.schema.items), depth + 1))
            return false;
    }
    if (source.kind === 'object' && Array.isArray(target.schema.required)) {
        const outputs = schemaPorts(source.schema), inputs = schemaPorts(target.schema);
        for (const field of inputs.filter((port) => port.required && port.path.length === 1)) {
            const provided = outputs.find((port) => port.label === field.label && port.required);
            if (!provided || !compatiblePorts(provided, field, depth + 1))
                return false;
        }
    }
    return true;
}
export function connectionError(document: Record<string, unknown>, source: string, target: string): string | null {
    const steps = graphSteps(document);
    if (!source || !target || !steps.some((step) => step.id === source) || !steps.some((step) => step.id === target))
        return '接続先のノードが見つかりません';
    if (steps.filter((step) => step.id === source || step.id === target).length !== 2)
        return 'ノードIDが重複しています。先に修正してください';
    if (source === target)
        return '同じノードには接続できません';
    const dependencies = new Map(steps.map((step) => [String(step.id), Array.isArray(step.depends_on) ? step.depends_on.filter((id): id is string => typeof id === 'string') : []]));
    const seen = new Set<string>();
    const visits = (id: string): boolean => {
        if (id === target)
            return true;
        if (seen.has(id))
            return false;
        seen.add(id);
        return (dependencies.get(id) ?? []).some(visits);
    };
    return visits(source) ? '循環する接続は作れません' : null;
}
export function connectExecution(document: Record<string, unknown>, source: string, target: string): Record<string, unknown> {
    const error = connectionError(document, source, target);
    if (error)
        throw new Error(error);
    const next = clone(document);
    const step = graphSteps(next).find((item) => item.id === target)!;
    step.depends_on = [...new Set([...(Array.isArray(step.depends_on) ? step.depends_on : []), source])];
    return next;
}
export function graphWires(document: Record<string, unknown>): WorkflowWire[] {
    return graphSteps(document).flatMap((step) => (Array.isArray(step.depends_on) ? step.depends_on : []).filter((id): id is string => typeof id === 'string').map((source) => ({ source, target: String(step.id), kind: 'execution' as const })));
}
export function disconnectExecution(document: Record<string, unknown>, source: string, target: string): Record<string, unknown> {
    let next = clone(document);
    for (const wire of dataWires(document).filter((wire) => wire.source === source && wire.target === target)) {
        next = setGraphInput(next, target, wire.targetPath!, undefined);
    }
    const step = graphSteps(next).find((item) => item.id === target);
    if (step && Array.isArray(step.depends_on))
        step.depends_on = step.depends_on.filter((id) => id !== source);
    return next;
}
/** A node label is presentation only; stable IDs and bindings never change. */
export function labelGraphNode(document: Record<string, unknown>, id: string, label: string): Record<string, unknown> {
    if (label.length > 128) throw new Error('ノード名は128文字以内にしてください');
    const next = clone(document);
    const step = graphSteps(next).find((item) => item.id === id);
    if (!step) throw new Error('ノードが見つかりません');
    if (label.trim()) step.label = label;
    else delete step.label;
    return next;
}

/** Delete a node and its explicit outgoing data/order connections together. */
export function deleteGraphNode(document: Record<string, unknown>, id: string): Record<string, unknown> {
    let next = clone(document);
    for (const wire of dataWires(document).filter((wire) => wire.source === id && wire.target !== id)) {
        next = setGraphInput(next, wire.target, wire.targetPath!, undefined);
    }
    next.steps = graphSteps(next).filter((step) => step.id !== id).map((step) => ({ ...step, depends_on: Array.isArray(step.depends_on) ? step.depends_on.filter((source) => source !== id) : [] }));
    return next;
}
export function setGraphInput(document: Record<string, unknown>, id: string, path: string[], value: unknown): Record<string, unknown> {
    if (path.length > 8 || path.some((part) => !part || ['__proto__', 'prototype', 'constructor'].includes(part)))
        throw new Error('入力フィールドが不正です');
    const next = clone(document);
    const step = graphSteps(next).find((item) => item.id === id);
    if (!step || !isGraphRecord(step.request))
        throw new Error('ノードが見つかりません');
    if (!path.length) { step.request.input = value === undefined ? {} : value; return next; }
    if (!isGraphRecord(step.request.input))
        step.request.input = {};
    let current = step.request.input as Record<string, unknown>;
    for (const part of path.slice(0, -1)) {
        if (!isGraphRecord(current[part]))
            current[part] = {};
        current = current[part] as Record<string, unknown>;
    }
    if (value === undefined) delete current[path.at(-1)!];
    else current[path.at(-1)!] = value;
    return next;
}
/** Resolve syntax against declared IDs, rejecting legal but ambiguous dotted IDs. */
export function parseOutputReference(value: unknown, ids: readonly string[]): {
    step: string;
    path: string[];
} | null {
    if (typeof value !== 'string' || !value.startsWith('${steps.') || !value.endsWith('}'))
        return null;
    const matches = ids.flatMap((id) => {
        const pointerPrefix='${steps.'+id+'.output@';
        if(value.startsWith(pointerPrefix)){
            const pointer=value.slice(pointerPrefix.length,-1);
            if(pointer==='') return [{step:id,path:[]}];
            if(!pointer.startsWith('/')||pointer.length>4096||/~(?![01])/.test(pointer))return [];
            const path=pointer.slice(1).split('/').map(part=>part.replace(/~1/g,'/').replace(/~0/g,'~'));
            if(path.length>32||path.some(part=>/[\u0000-\u001f]/.test(part)))return [];
            return [{step:id,path}];
        }
        if (value === '${steps.' + id + '.output}') return [{step:id,path:[]}];
        const prefix = '${steps.' + id + '.output.';
        if (!value.startsWith(prefix))
            return [];
        const path = value.slice(prefix.length, -1).split('.');
        return path.every((part) => /^[a-z][a-z0-9_-]*$/.test(part) && !['__proto__', 'prototype', 'constructor'].includes(part)) ? [{ step: id, path }] : [];
    });
    return matches.length === 1 ? matches[0] : null;
}
export function connectData(document: Record<string, unknown>, source: string, output: WorkflowPort, target: string, input: WorkflowPort, allowPointer = true): Record<string, unknown> {
    if (!compatiblePorts(output, input))
        throw new Error('端子の型が一致しません');
    const next = connectExecution(document, source, target);
    let reference = '${steps.' + source + '.output' + (output.path.length ? '.' + output.path.join('.') : '') + '}';
    const ids=graphSteps(document).map(step=>String(step.id));
    const parsed=parseOutputReference(reference,ids);
    if(!parsed || JSON.stringify(parsed.path)!==JSON.stringify(output.path)){
        if(!allowPointer)throw new Error('このHostはJSON Pointer接続に対応していません');
        reference='${steps.'+source+'.output@'+(output.path.length?'/'+output.path.map(part=>part.replace(/~/g,'~0').replace(/\//g,'~1')).join('/'):'')+'}';
    }
    if (!parseOutputReference(reference, graphSteps(document).map((step) => String(step.id))))
        throw new Error('この端子名は参照が曖昧です');
    return setGraphInput(next, target, input.path, reference);
}
export function dataWires(document: Record<string, unknown>): WorkflowWire[] {
    const steps = graphSteps(document), ids = steps.map((step) => String(step.id));
    const result: WorkflowWire[] = [];
    const visit = (value: unknown, target: string, path: string[], depth: number) => {
        if (depth > 16)
            return;
        const reference = parseOutputReference(value, ids);
        if (reference)
            result.push({ source: reference.step, target, kind: 'data', sourcePath: reference.path, targetPath: path });
        else if (isGraphRecord(value))
            for (const [key, item] of Object.entries(value))
                visit(item, target, [...path, key], depth + 1);
    };
    for (const step of steps)
        if (isGraphRecord(step.request))
            visit(step.request.input, String(step.id), [], 0);
    return result;
}

/** Keep undo bounded even when a definition contains large literal inputs. */
export function boundedGraphHistory(items: readonly Record<string, unknown>[]): Record<string, unknown>[] {
    let bytes = 0;
    const retained: Record<string, unknown>[] = [];
    for (let index = items.length - 1; index >= 0 && retained.length < 50; index--) {
        const size = new TextEncoder().encode(JSON.stringify(items[index])).byteLength;
        if (bytes + size > 2 * 1024 * 1024) break;
        retained.unshift(items[index]);
        bytes += size;
    }
    return retained;
}
