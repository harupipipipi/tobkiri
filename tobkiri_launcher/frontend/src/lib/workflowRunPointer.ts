/** Non-secret navigation pointers, never execution or approval authority. */
export interface WorkflowRunPointer {runId: string; definitionId: string; revisionDigest: string}
const key = (context: string, definition: string) => `tobkiri-workflow-run-v1:${encodeURIComponent(context)}:${encodeURIComponent(definition)}`;
const valid = (value: unknown): value is WorkflowRunPointer => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return Object.keys(row).sort().join(',') === 'definitionId,revisionDigest,runId'
    && typeof row.runId === 'string' && /^run-[a-zA-Z0-9-]{1,100}$/.test(row.runId)
    && typeof row.definitionId === 'string' && row.definitionId.length > 0 && row.definitionId.length <= 256
    && typeof row.revisionDigest === 'string' && /^sha256:[a-f0-9]{64}$/.test(row.revisionDigest);
};
export function saveWorkflowRunPointer(storage: Pick<Storage,'setItem'>, context: string, value: WorkflowRunPointer): void {
  if (!context || context.length > 1024 || !valid(value)) throw new Error('実行参照を保存できません');
  storage.setItem(key(context,value.definitionId),JSON.stringify(value));
}
export function readWorkflowRunPointer(storage: Pick<Storage,'getItem'>, context: string, definition: string): WorkflowRunPointer | null {
  try {
    const raw=storage.getItem(key(context,definition));
    if (!raw || raw.length > 2048) return null;
    const value: unknown=JSON.parse(raw);
    return valid(value)&&value.definitionId===definition ? value : null;
  } catch {return null;}
}
