import { useEffect, useRef, useState } from 'react';
import {openLauncherApprovalWindow} from '@/src/lib/launcherConnections';
import { Play, RefreshCw, Square } from 'lucide-react';
import { Button } from '@/src/components/ui/Button';
import { createWorkflowRun, controlWorkflowRun, getWorkflowRun, resumeWorkflowStep, hasUnknownWorkflowMutation, type WorkflowDefinition, type WorkflowAuthoringDependencies, type WorkflowRunView } from '@/src/lib/workflowAuthoring';
import { isMutationResultUnknown } from '@/src/lib/mutationJournal';
import { isGraphRecord } from '@/src/lib/workflowGraph';
import {WorkflowAudioPlayer} from './WorkflowAudioPlayer';
import {WorkflowInputsForm} from './WorkflowInputsForm';
import {setWorkflowInputValue,workflowInputFields} from '@/src/lib/workflowInputs';
import type {WorkflowPaletteOperation} from '@/src/lib/workflowAuthoring';
import {readWorkflowRunPointer,saveWorkflowRunPointer} from '@/src/lib/workflowRunPointer';
/** Effects are initiated only by explicit Run/Stop controls and the captured Host. */
export function WorkflowRunPanel({ definition, dirty, disabled, stopDisabled = disabled, dependencies, operations = [], contextKey = "active", openApprovalWindow = openLauncherApprovalWindow }: {
    openApprovalWindow?: (requestId: string) => Promise<boolean>;
    definition: WorkflowDefinition | null;
    dirty: boolean;
    disabled: boolean;
    /** Authoring reads may be busy while the owned Run still needs Stop. */
    stopDisabled?: boolean;
    dependencies?: WorkflowAuthoringDependencies;
    contextKey?: string;
    operations?: readonly WorkflowPaletteOperation[];
}) {
    const [inputText, setInputText] = useState('{}');
    const [inputMode,setInputMode]=useState<'form'|'json'>('form');
    const [inputBusy,setInputBusy]=useState<Record<string,boolean>>({});
    const pendingInput=Object.values(inputBusy).some(Boolean);
    const parsedInputs=(() => {try{const value:unknown=JSON.parse(inputText);return isGraphRecord(value)?value:null;}catch{return null;}})();
    const missingInput=Boolean(definition&&workflowInputFields(definition.document,operations).some(field=>{let value:unknown=parsedInputs;for(const part of field.path)value=isGraphRecord(value)?value[part]:undefined;return value===undefined;}));
    const [run, setRun] = useState<WorkflowRunView | null>(null);
    const [busy, setBusy] = useState(false);
    const [stopping, setStopping] = useState(false);
    const stoppingRef = useRef(false);
    const [error, setError] = useState('');
    const [openingApproval, setOpeningApproval] = useState(false);
    const approvalInFlight = useRef(false);
    const [fresh, setFresh] = useState(false);
    const inFlight = useRef(false);
    const active = useRef(false);
    const requestedRun = useRef<string | null>(null);
    const generation = useRef(0);
    const continueRequested = useRef(false);
    useEffect(() => { active.current = true; return () => { active.current = false; continueRequested.current = false; ++generation.current; }; }, []);
    const accept = (value: WorkflowRunView, epoch: number) => {
        if (!active.current || epoch !== generation.current) return;
        if (definition && value.definitionId !== definition.definition_id) {
            setFresh(false);
            setError('別のFlowの実行結果は表示できません');
            return;
        }
        setRun(value);
        setFresh(true);
    };
    const read = async () => {
        if (!requestedRun.current || inFlight.current)
            return;
        const epoch = ++generation.current;
        inFlight.current = true;
        setBusy(true);
        setError('');
        try {
            accept(await getWorkflowRun(requestedRun.current, dependencies), epoch);
        }
        catch {
            if (active.current && epoch === generation.current) {
                setFresh(false);
                setError('実行状態を確認できません。再実行せず、状態を再取得してください');
            }
        }
        finally {
            inFlight.current = false;
            if (active.current)
                setBusy(false);
        }
    };
    const start = async () => {
        if (inFlight.current || stoppingRef.current || Boolean(requestedRun.current && !fresh) || pendingInput || missingInput || disabled || dirty || definition?.state !== 'published' || hasUnknownWorkflowMutation())
            return;
        let inputs: unknown;
        try {
            inputs = JSON.parse(inputText);
            if (!isGraphRecord(inputs) || new TextEncoder().encode(inputText).byteLength > 1536 * 1024)
                throw new Error();
        }
        catch {
            setError('開始入力は1.5MiB以下のJSONオブジェクトで指定してください');
            return;
        }
        const id = `run-${crypto.randomUUID()}`;
        try {saveWorkflowRunPointer(window.sessionStorage, contextKey, {runId:id,definitionId:definition.definition_id,revisionDigest:definition.revision_digest});}
        catch {setError('実行参照を安全に保存できません。ブラウザの保存領域を確認してください'); return;}
        requestedRun.current = id;
        continueRequested.current = true;
        const epoch = ++generation.current;
        inFlight.current = true;
        setBusy(true);
        setError('');
        setFresh(false);
        try {
            let current = await createWorkflowRun(definition.definition_id, definition.revision_digest, id, inputs as Record<string, unknown>, dependencies);
            accept(current, epoch);
            await pump(current, epoch);

        }
        catch (caught) {
            if (active.current && epoch === generation.current) {
                setFresh(false);
                setError(isMutationResultUnknown(caught) ? '実行結果が未確認です。新しい実行は開始せず、元の実行状態を確認してください' : '実行を進められませんでした。状態を確認してください');
            }
        }
        finally {
            inFlight.current = false;
            continueRequested.current = false;
            if (active.current)
                setBusy(false);
        }
    };
    const pump = async (initial: WorkflowRunView, epoch: number) => {
        let current = initial;
        const count = Array.isArray(definition?.document.steps) ? definition.document.steps.length : 0;
        for (let index = 0; index <= Math.min(count, 1024) && active.current && continueRequested.current && epoch === generation.current && ['queued', 'running'].includes(current.state); index++) {
            const next = await controlWorkflowRun('run.advance', current.runId, dependencies);
            accept(next, epoch);
            if (next.state === current.state && next.attempts.length === 0) break;
            current = next;
        }
        if (active.current && epoch === generation.current) accept(await getWorkflowRun(current.runId, dependencies), epoch);
    };
    const continueRun = async () => {
        if (inFlight.current || stoppingRef.current || !run || !fresh || disabled || dirty || hasUnknownWorkflowMutation() || run.revisionDigest !== definition?.revision_digest) return;
        const epoch = ++generation.current;
        inFlight.current = true; continueRequested.current = true; setBusy(true); setError('');
        try {
            let current = await getWorkflowRun(run.runId, dependencies);
            if (current.state === 'waiting_approval') {
                const pending = current.attempts.find((attempt) => attempt.state === 'waiting_approval');
                if (!pending) throw new Error();
                await resumeWorkflowStep(current.runId, pending.stepId, dependencies);
                current = await getWorkflowRun(current.runId, dependencies);
            } else if (current.state === 'paused') {
                current = await controlWorkflowRun('run.resume', current.runId, dependencies);
            }
            accept(current, epoch);
            await pump(current, epoch);
        } catch (caught) {
            if (active.current && epoch === generation.current) {setFresh(false);setError(isMutationResultUnknown(caught) ? '続行結果が未確認です。再送せず状態を確認してください' : '続行できませんでした。承認状態と実行状態を確認してください');}
        } finally {inFlight.current = false;continueRequested.current = false;if (active.current) setBusy(false);}
    };
    const showApproval = async (requestId: string) => {
        if (disabled || dirty || busy || !fresh || approvalInFlight.current || !run?.attempts.some(attempt => attempt.state === 'waiting_approval' && attempt.approvalRequestId === requestId)) return;
        const epoch = generation.current;
        approvalInFlight.current = true; setOpeningApproval(true);
        try {
            const opened = await openApprovalWindow(requestId);
            if (!opened && active.current && epoch === generation.current) setError('承認画面を開けませんでした。承認状態は変更していません');
        } catch {
            if (active.current && epoch === generation.current) setError('承認画面の表示を確認できませんでした。承認状態は変更していません');
        } finally {
            approvalInFlight.current = false;
            if (active.current) setOpeningApproval(false);
        }
    };
    const stop = async () => {
        continueRequested.current = false;
        // Cancellation has its own journal identity and must remain available
        // while an advance request is awaiting a model or tool.
        if (stopDisabled || stoppingRef.current || !run)
            return;
        const epoch = ++generation.current;
        stoppingRef.current = true;
        setStopping(true);
        setError('');
        try {
            accept(await controlWorkflowRun('run.stop', run.runId, dependencies), epoch);
        }
        catch (caught) {
            if (active.current && epoch === generation.current) {
                setFresh(false);
                setError(isMutationResultUnknown(caught) ? '停止結果が未確認です。状態を再取得してください' : '停止を確認できません。状態を再取得してください');
            }
        }
        finally {
            stoppingRef.current = false;
            if (active.current)
                setStopping(false);
        }
    };
    useEffect(() => {
        if (!definition) return;
        const pointer = readWorkflowRunPointer(window.sessionStorage, contextKey, definition.definition_id);
        let cancelled = false;
        if (pointer) {requestedRun.current = pointer.runId; queueMicrotask(() => {if (!cancelled && active.current) void read();});}
        return () => {cancelled = true;};
    }, [contextKey, definition?.definition_id]);
    const terminal = run && ['succeeded', 'failed', 'cancelled', 'timed_out'].includes(run.state);
    return <section className="rounded-lg border border-border bg-bg-main p-4" aria-label="Workflow run controls">
    <h3 className="text-sm font-medium">Flowを実行</h3>
    <p className="mt-1 text-xs leading-5 text-text-muted">公開済みの保存内容を実行します。各処理の権限はHostが確認し、必要な承認は省略されません</p>
    {run?.detailsOmitted && <p role="status" className="mt-2 text-xs text-text-muted">結果が大きいため一部のプレビューを省略しています。実行結果自体は変更していません</p>}
    <div className="mt-3 flex gap-2"><Button variant="ghost" size="sm" aria-pressed={inputMode==='form'} onClick={()=>setInputMode('form')} disabled={busy||pendingInput}>入力フォーム</Button><Button variant="ghost" size="sm" aria-pressed={inputMode==='json'} onClick={()=>setInputMode('json')} disabled={busy||pendingInput}>詳細JSON</Button></div>
    {inputMode==='form' && definition && parsedInputs ? <WorkflowInputsForm document={definition.document} operations={operations} values={parsedInputs} disabled={disabled || busy || stopping} contextKey={contextKey} onBusyChange={(field,pending)=>setInputBusy(current=>({...current,[field]:pending}))} onChange={(path,value)=>setInputText(current=>{try{const parsed:unknown=JSON.parse(current);return isGraphRecord(parsed)?JSON.stringify(setWorkflowInputValue(parsed,path,value),null,2):current;}catch{return current;}})}/> : <label className="mt-3 block text-xs">開始入力<textarea aria-label="Workflow run inputs" value={inputText} onChange={(event) => setInputText(event.target.value)} disabled={disabled || busy || stopping} className="mt-1 min-h-20 w-full rounded border border-border bg-bg-main p-2 font-mono text-xs" spellCheck={false}/></label>}
    {dirty && <p className="mt-2 text-xs text-warning">未保存の変更があります。先に別の下書きとして保存・公開してください</p>}
    <div className="mt-3 flex flex-wrap gap-2">
      <Button size="sm" disabled={disabled || busy || stopping || pendingInput || missingInput || Boolean(requestedRun.current && !fresh) || dirty || definition?.state !== 'published' || hasUnknownWorkflowMutation() || Boolean(run && !terminal)} onClick={() => void start()}><Play className="h-4 w-4"/>実行</Button>
      <Button size="sm" variant="outline" disabled={disabled || busy || stopping || dirty || !fresh || !run || !['queued', 'running', 'paused', 'waiting_approval'].includes(run.state) || run.revisionDigest !== definition?.revision_digest || hasUnknownWorkflowMutation()} onClick={() => void continueRun()}>続行</Button>
      <Button size="sm" variant="outline" disabled={!requestedRun.current || busy} onClick={() => void read()}><RefreshCw className="h-4 w-4"/>状態を再取得</Button>
      <Button size="sm" variant="outline" disabled={stopDisabled || !run || Boolean(terminal) || stopping} onClick={() => void stop()}><Square className="h-4 w-4"/>停止</Button>
    </div>
    {missingInput && <p className="mt-2 text-xs text-text-muted">開始入力をすべて指定してから実行してください</p>}
    {run?.previews?.map((preview,index)=><div key={`${preview.stepId}:${index}`} className="mt-3 rounded border border-border p-3"><p className="mb-2 text-xs text-text-muted">{preview.stepId}の結果</p>{preview.text!==undefined&&<p className="whitespace-pre-wrap break-words text-sm">{preview.text}</p>}{preview.audio&&<WorkflowAudioPlayer content={preview.audio}/>}</div>)}
    {error && <p role="alert" className="mt-3 text-sm text-destructive">{error}</p>}
    {run && <div className="mt-3 text-xs" role="status"><p className="break-all">{run.runId} · {run.state}</p><ul className="mt-2 space-y-1">{run.attempts.map((attempt) => <li key={`${attempt.stepId}:${attempt.number}`}>{attempt.stepId} · {attempt.state} · 試行{attempt.number}{attempt.approvalRequestId && <Button className="ml-2" size="sm" variant="outline" disabled={disabled || dirty || busy || !fresh || openingApproval} onClick={() => void showApproval(attempt.approvalRequestId!)}>承認画面を開く</Button>}</li>)}</ul>{run.state === 'waiting_approval' && <p className="mt-2">承認待ちです。Hostの承認画面で内容を確認してください</p>}</div>}
  </section>;
}
