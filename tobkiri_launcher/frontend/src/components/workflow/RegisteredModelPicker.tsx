import { useEffect, useRef, useState } from 'react';
import { loadWorkflowModels, type WorkflowModelSnapshot } from '@/src/lib/workflowModelCatalog';
export interface RegisteredModelPickerProps {
    value: string;
    onChange: (profileId: string) => void;
    disabled?: boolean;
    /** Identity of the editor context; a changed Profile cannot inherit an old read. */
    contextKey: string;
    load?: () => Promise<WorkflowModelSnapshot>;
}
export function RegisteredModelPicker({ value, onChange, disabled = false, contextKey, load = loadWorkflowModels }: RegisteredModelPickerProps) {
    const [snapshot, setSnapshot] = useState<WorkflowModelSnapshot | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(false);
    const generation = useRef(0);
    const active = useRef(false);
    const refresh = async () => {
        if (disabled) return;
        const current = ++generation.current;
        setSnapshot(null);
        setLoading(true);
        setError(false);
        try {
            const next = await load();
            if (active.current && current === generation.current)
                setSnapshot(next);
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
        if (disabled) { setSnapshot(null); setLoading(false); }
        else void refresh();
        const changed = () => { if (!disabled) void refresh(); };
        window.addEventListener('tobkiri-model-profiles-changed', changed);
        window.addEventListener('tobkiri-provider-connections-changed', changed);
        return () => {
            active.current = false;
            ++generation.current;
            window.removeEventListener('tobkiri-model-profiles-changed', changed);
            window.removeEventListener('tobkiri-provider-connections-changed', changed);
        };
    }, [contextKey, load, disabled]);
    const missing = Boolean(value && snapshot && !snapshot.models.some((model) => model.profileId === value));
    return <div className="mt-1 space-y-2">
    <select aria-label="Registered model" className="min-h-10 w-full rounded border border-border bg-bg-main px-2 text-sm" value={value} disabled={disabled || loading || error || !snapshot} onChange={(event) => onChange(event.target.value)}>
      <option value="">{loading ? 'モデルを取得中…' : '登録済みモデルを選択'}</option>
      {missing && <option value={value} disabled>利用できないモデル: {value}</option>}
      {snapshot?.models.map((model) => <option key={model.profileId} value={model.profileId}>{model.displayName} · {model.modelId}</option>)}
    </select>
    <p className="text-xs text-text-muted">Settingsと同じ登録情報を使用します。APIキーはFlowに保存されません。選んだモデルがこの処理に対応していることも確認してください</p>
    {missing && <p role="alert" className="text-xs text-destructive">このモデルは削除・無効化されたか、このProfileでは利用できません。選び直してください</p>}
    {error && <p role="alert" className="text-xs text-destructive">モデル一覧を取得できませんでした</p>}
    <button type="button" className="min-h-9 text-xs text-accent disabled:opacity-50" disabled={disabled || loading} onClick={() => void refresh()}>登録情報を更新</button>
  </div>;
}
