import { useEffect, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { loadWorkflowModels, type WorkflowModelSnapshot } from '@/src/lib/workflowModelCatalog';
import { Button } from '@/src/components/ui/Button';
import { useT } from '@/src/lib/i18n';
/** A shared read-only view of the active Profile's owner-managed registrations. */
export function RegisteredModelsPanel({ contextKey }: {
    contextKey: string;
}) {
    const t = useT();
    const [snapshot, setSnapshot] = useState<WorkflowModelSnapshot | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(false);
    const generation = useRef(0);
    const active = useRef(false);
    const refresh = async () => { const current = ++generation.current; setLoading(true); setError(false); setSnapshot(null); try {
        const next = await loadWorkflowModels();
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
    } };
    useEffect(() => { active.current = true; void refresh(); return () => { active.current = false; ++generation.current; }; }, [contextKey]);
    return <section className="space-y-3 rounded-lg border border-border p-4" aria-label={t('registered_models.aria_label')}>
    <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-medium">{t('registered_models.title')}</h3><Button variant="outline" size="sm" loading={loading} onClick={() => void refresh()}><RefreshCw className="h-3 w-3"/>{t('registered_models.refresh')}</Button></div>
    <p className="text-xs leading-5 text-text-muted">{t('registered_models.description')}</p>
    {error && <p role="alert" className="text-sm text-destructive">{t('registered_models.error')}</p>}
    {snapshot && <ul className="divide-y divide-border">{snapshot.models.map((model) => <li key={model.profileId} className="py-3"><p className="text-sm font-medium">{model.displayName}</p><p className="mt-1 break-all text-xs text-text-muted">{model.modelId} · {model.providerId ?? t('registered_models.no_provider')}</p><p className="mt-1 break-all font-mono text-xs text-text-muted">{model.profileId}</p></li>)}</ul>}
    {snapshot && !snapshot.models.length && <p className="text-sm text-text-muted">{t('registered_models.empty')}</p>}
  </section>;
}
