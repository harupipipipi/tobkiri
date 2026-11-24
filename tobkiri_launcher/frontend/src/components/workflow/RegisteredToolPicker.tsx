import { useEffect, useRef, useState } from 'react';
import { loadWorkflowTools, type WorkflowToolSnapshot, type WorkflowTool } from '@/src/lib/workflowToolCatalog';
export interface RegisteredToolPickerProps {
    value: string;
    onChange: (tool: WorkflowTool | null) => void;
    onSnapshot: (snapshot: WorkflowToolSnapshot | null) => void;
    expectedHash?: string;
    profileId?: string;
    planDigest?: string;
    disabled?: boolean;
    /** Identity of the editor context; a changed Profile cannot inherit an old read. */
    contextKey: string;
    load?: () => Promise<WorkflowToolSnapshot>;
}
export function RegisteredToolPicker({ value, onChange, onSnapshot, expectedHash, profileId, planDigest, disabled = false, contextKey, load = loadWorkflowTools }: RegisteredToolPickerProps) {
    const [snapshot, setSnapshot] = useState<WorkflowToolSnapshot | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(false);
    const generation = useRef(0);
    const report = useRef(onSnapshot);
    report.current = onSnapshot;
    const active = useRef(false);
    const refresh = async () => {
        if (disabled) return;
        const current = ++generation.current;
        setSnapshot(null);
        report.current(null);
        setLoading(true);
        setError(false);
        try {
            const next = await load();
            if ((profileId && next.profileId !== profileId) || (planDigest && next.planDigest !== planDigest)) throw new Error("Tool registry context changed");
            if (active.current && current === generation.current) {
                setSnapshot(next); report.current(next);
            }
        }
        catch {
            if (active.current && current === generation.current)
                setError(true);
        }
        finally {
            if (active.current && current === generation.current)
                setLoading(false);
        }
    };
    useEffect(() => {
        active.current = true;
        if (disabled) { setSnapshot(null); report.current(null); setLoading(false); }
        else void refresh();
        const changed = () => { if (!disabled) void refresh(); };
        window.addEventListener('tobkiri-tool-definitions-changed', changed);
        window.addEventListener('tobkiri-provider-connections-changed', changed);
        return () => {
            active.current = false;
            ++generation.current;
            window.removeEventListener('tobkiri-tool-definitions-changed', changed);
            window.removeEventListener('tobkiri-provider-connections-changed', changed);
        };
    }, [contextKey, load, disabled, profileId, planDigest]);
    const missing = Boolean(value && snapshot && !snapshot.tools.some((tool) => tool.toolId === value && tool.definitionHash === expectedHash));
    return <div className="mt-1 space-y-2">
    <select aria-label="Registered tool" className="min-h-10 w-full rounded border border-border bg-bg-main px-2 text-sm" value={value ? `${value}@${expectedHash ?? ''}` : ''} disabled={disabled || loading || error || !snapshot} onChange={(event) => onChange(snapshot?.tools.find(tool => `${tool.toolId}@${tool.definitionHash}` === event.target.value) ?? null)}>
      <option value="">{loading ? 'ツールを取得中…' : '登録済みツールを選択'}</option>
      {missing && <option value={`${value}@${expectedHash ?? ''}`} disabled>利用できないツール: {value}</option>}
      {snapshot?.tools.map((tool) => <option key={tool.toolId} value={`${tool.toolId}@${tool.definitionHash}`}>{tool.name} · {tool.toolId}</option>)}
    </select>
    <p className="text-xs text-text-muted">登録済みの定義と引数を使います。実行時にも定義の一致と権限を確認します</p>
    {missing && <p role="alert" className="text-xs text-destructive">このツールは変更・削除されたか、このProfileでは利用できません。再選択して引数を確認してください</p>}
    {error && <p role="alert" className="text-xs text-destructive">ツール一覧を取得できませんでした</p>}
    <button type="button" className="min-h-9 text-xs text-accent disabled:opacity-50" disabled={disabled || loading} onClick={() => void refresh()}>登録情報を更新</button>
  </div>;
}
