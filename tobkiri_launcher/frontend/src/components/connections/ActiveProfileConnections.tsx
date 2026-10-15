import {useRuntimeSurface} from '@/src/hooks/useRuntimeSurface';
import {useProfileMutationBlocked, useProfileSelection} from '@/src/lib/profileSelection';
import {isGraphRecord} from '@/src/lib/workflowGraph';
import {InspectedProfileNotice} from '@/src/components/advanced/InspectedProfileNotice';
import {Button} from '@/src/components/ui/Button';
import {LauncherConnectionsPanel} from './LauncherConnectionsPanel';
import {isLauncherActiveProfileContext} from '@/src/lib/launcherConnections';

/** Mount only on explicit Settings or Flow expansion; preferences work offline. */
export function ActiveProfileConnections() {
  const surface = useRuntimeSurface<unknown>('profile');
  const {selectedProfileId} = useProfileSelection();
  const disabled = useProfileMutationBlocked(surface.data?.profile_id);
  // The runtime-surface parser has already checked this activation against the
  // enclosing Profile, plan, catalog and authority-snapshot evidence.
  const data = surface.data?.data;
  const activation = isGraphRecord(data) ? data.activation_record : null;
  const profile = surface.status === 'ready' && !surface.stale
    && isLauncherActiveProfileContext(activation) ? activation : null;
  return <div className="space-y-3">
    <InspectedProfileNotice surfaceProfileId={surface.data?.profile_id}/>
    {!profile && <div role="status" className="rounded border border-border p-3 text-sm">
      有効なProfileを確認してから接続とモデルを表示します
      <Button variant="outline" size="sm" disabled={surface.status==='loading'} onClick={()=>void surface.refresh(true)}>Profileを再確認</Button>
    </div>}
    <LauncherConnectionsPanel key={`${selectedProfileId ?? 'active'}:${profile?.activation_id ?? 'unverified'}`}
      profile={profile} disabled={disabled || !profile}
      onRegistryChanged={()=>{
        window.dispatchEvent(new Event('tobkiri-provider-connections-changed'));
        window.dispatchEvent(new Event('tobkiri-model-profiles-changed'));
      }}/>
  </div>;
}
