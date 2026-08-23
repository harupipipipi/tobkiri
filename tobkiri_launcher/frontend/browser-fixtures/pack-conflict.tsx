import {useState} from 'react';
import {createRoot} from 'react-dom/client';

import {PackConflictCenter} from '@/src/components/packs/PackConflictCenter';
import type {PackConflictReport, RepairPackState} from '@/src/lib/apiTypes';
import type {PackRepairAction} from '@/src/store';
import '@/src/index.css';

const digest = (character: string) => `sha256:${character.repeat(64)}`;

function repair(state: RepairPackState): NonNullable<PackConflictReport['repair']> {
  return {
    repair_id: 'rpr_1234567890abcdef12345678',
    artifact_hash: digest('c'),
    state,
    capability_delta: [],
    validation_passed: ['validated', 'approved', 'installed', 'active'].includes(state),
    dry_run_resolved: ['validated', 'approved', 'installed', 'active'].includes(state),
    warnings: state === 'stale' ? ['Source Pack binding changed'] : [],
    approval_actor_id: ['approved', 'installed', 'active'].includes(state) ? 'reviewer.one' : null,
  };
}

const base: PackConflictReport = {
  conflict_api_version: 'io.tobkiri.pack-conflict-report.v1',
  conflict_id: 'pcf_1234567890abcdef12345678',
  kind: 'ambiguous_one_provider',
  profile_id: 'fixture.profile',
  profile_fingerprint: digest('d'),
  involved_packs: [
    {pack_id: 'fixture.alpha', version: '1.2.0', artifact_hash: digest('a')},
    {pack_id: 'fixture.beta', version: '2.0.0', artifact_hash: digest('b')},
  ],
  affected_contracts: ['rumi.action.fixture.v1'],
  affected_resources: [],
  schemas: [],
  constraints: ['>=1.0.0 <3.0.0'],
  safe_repair_kinds: ['provider_selection'],
  repairable: true,
  diagnostics: ['Two exact providers have equal priority.'],
  validation_requirements: ['artifact_integrity', 'dry_run_resolution'],
};

const manual: PackConflictReport = {
  ...base,
  conflict_id: 'pcf_abcdef1234567890abcdef12',
  kind: 'incompatible_semantic',
  repairable: false,
  safe_repair_kinds: [],
  diagnostics: ['Semantic equivalence cannot be proven; manual resolution is required.'],
};

const nextState: Partial<Record<PackRepairAction, RepairPackState | undefined>> = {
  generate: 'generated',
  review: 'validated',
  approve: 'approved',
  install: 'installed',
  activate: 'active',
  disable: 'removed',
  remove: undefined,
  regenerate: 'generated',
};

function Fixture() {
  const [state, setState] = useState<RepairPackState | undefined>();
  const [lastAction, setLastAction] = useState('none');
  const onAction = async (_conflictId: string, action: PackRepairAction) => {
    setLastAction(action);
    setState(nextState[action]);
  };
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 sm:p-8">
      <p className="text-sm text-text-muted" aria-live="polite">Last action: {lastAction}</p>
      <PackConflictCenter
        conflicts={[{...base, repair: state ? repair(state) : null}, manual]}
        pending={{}}
        onAction={onAction}
      />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<Fixture />);
