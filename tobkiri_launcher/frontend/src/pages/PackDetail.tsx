import { useEffect } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useAppStore } from '@/src/store';
import { useT } from '@/src/lib/i18n';
import { Button } from '@/src/components/ui/Button';
import { Badge } from '@/src/components/ui/Badge';
import { Switch } from '@/src/components/ui/Switch';
import { Card, CardHeader, CardTitle, CardContent } from '@/src/components/ui/Card';
import { panelRoutes } from '@/src/lib/routes';
import { ArrowLeft, Play } from 'lucide-react';
import { InlineLoadError } from '@/src/components/ui/InlineLoadError';

export function PackDetail() {
  const t = useT();
  const { id } = useParams();
  const navigate = useNavigate();
  const packs = useAppStore(state => state.packs);
  const packsLoading = useAppStore(state => state.packsLoading);
  const packsError = useAppStore(state => state.packsError);
  const packTogglePending = useAppStore(state => state.packTogglePending);
  const loadPacks = useAppStore(state => state.loadPacks);
  const togglePack = useAppStore(state => state.togglePack);
  const addToast = useAppStore(state => state.addToast);

  const pack = packs.find(p => p.id === id);

  useEffect(() => {
    if (packs.length === 0) void loadPacks();
  }, [packs.length, loadPacks]);

  if (packsLoading && packs.length === 0) {
    return (
      <div className="flex flex-1 flex-col gap-5 p-6" role="status" aria-label={t('pack.loading')}>
        <div className="h-8 w-64 animate-pulse rounded bg-bg-hover" />
        <div className="grid gap-6 lg:grid-cols-2">
          {[0, 1, 2].map((item) => <div key={item} className="h-48 animate-pulse rounded-xl border border-border bg-bg-card" />)}
        </div>
      </div>
    );
  }

  if (packsError && !pack) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <div className="w-full max-w-xl">
          <InlineLoadError
            title="Pack details could not be loaded"
            message={packsError}
            onRetry={() => void loadPacks()}
            retrying={packsLoading}
          />
        </div>
      </div>
    );
  }

  if (!pack) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <p className="text-sm font-medium text-text-main">Pack not found</p>
          <Button className="mt-3" variant="outline" onClick={() => navigate(panelRoutes.packs)}>Back to packs</Button>
        </div>
      </div>
    );
  }

  const handleToggle = async () => {
    const key = pack.enabled ? 'packs.toggle_off' : 'packs.toggle_on';
    if (await togglePack(pack.id)) addToast(t(key, { name: pack.name }), 'success');
  };

  return (
    <div className="flex-1 overflow-y-auto page-enter">
      <div className="mx-auto max-w-4xl px-6 py-8 flex flex-col gap-6">
        {/* Header */}
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0">
            <Button variant="ghost" size="icon" onClick={() => navigate(panelRoutes.packs)} aria-label="Back to packs">
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h1 className="text-xl font-semibold tracking-tight text-text-main">{pack.name}</h1>
                <Badge variant="outline">{pack.version}</Badge>
                <Badge variant={pack.type === 'core' ? 'default' : 'secondary'}>{pack.type}</Badge>
              </div>
              <p className="mt-0.5 text-sm text-text-muted">{pack.description}</p>
            </div>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            <span className="text-sm text-text-muted">{pack.enabled ? t('packs.enabled') : t('packs.disabled')}</span>
            <Switch
              checked={pack.enabled}
              disabled={Boolean(packTogglePending[pack.id])}
              onCheckedChange={() => { void handleToggle(); }}
              aria-label={`Toggle ${pack.name}`}
            />
          </div>
        </div>

        {/* Content grid */}
        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>{t('pack.capabilities')}</CardTitle>
            </CardHeader>
            <CardContent>
              {pack.capabilities.length === 0 ? (
                <p className="text-sm text-text-muted">No capabilities registered.</p>
              ) : (
                <ul className="space-y-3">
                  {pack.capabilities.map((cap, i) => (
                    <li key={i} className="flex flex-col gap-0.5">
                      <span className="text-sm font-medium text-text-main">{cap.name}</span>
                      <span className="text-xs text-text-muted">{cap.description}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('pack.flows')}</CardTitle>
            </CardHeader>
            <CardContent>
              {pack.flows.length === 0 ? (
                <p className="text-sm text-text-muted">No flows available.</p>
              ) : (
                <ul className="space-y-2">
                  {pack.flows.map((flow, i) => (
                    <li key={i} className="flex items-center justify-between rounded-lg border border-border p-3">
                      <span className="text-sm font-medium text-text-main">{flow}</span>
                      <Button size="sm" variant="outline" onClick={() => navigate(panelRoutes.flows)}>
                        <Play className="h-3 w-3" />
                        {t('pack.run')}
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('pack.dependencies')}</CardTitle>
            </CardHeader>
            <CardContent>
              {pack.dependencies.length === 0 ? (
                <p className="text-sm text-text-muted">No dependencies.</p>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {pack.dependencies.map((dep, i) => (
                    <Badge key={i} variant="secondary">{dep}</Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
