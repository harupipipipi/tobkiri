import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ModelRouteSetup } from '../../src/features/models/ModelRouteSetup';
import { SearchableProviderField } from '../../src/renderers/settings/renderers/providerSelectField';
import { settingsApiResources } from '../../src/features/settings/resources/settingsApiResources';
import { api } from '../../src/lib/api';
import { ModelDropdown } from '../../src/renderers/ComposerRenderer';
import { SettingsModalRenderer } from '../../src/renderers/SettingsModalRenderer';
import { requestModelProperties, getModelPropertiesRequest, type ModelPropertiesRequest } from '../../src/features/search/modelPropertiesNavigation';
import { VerifiedFrontendHostProvider, type VerifiedFrontendHost } from '../../src/host/VerifiedFrontendHostContext';
import { frontendHostFixtureCatalog } from '../../test-support/frontendHostFixture';
import { searchCaptureScope } from '../../src/features/search/searchCaptureScope';
import { createProviderModelAccessResources, type ModelAccessScope } from '../../src/features/apiKeys/resources/providerModelAccessResources';
import { MODEL_ACCESS_VERSION, type ModelAccessPolicy } from '../../src/features/apiKeys/modelAccessPolicy';
import '../../src/index.css';

const params = new URLSearchParams(location.search);
const accessMode = params.get('access') ?? 'valid';
const fixture = {
  saves: [] as unknown[], searches: [] as Record<string, unknown>[], selections: [] as string[],
  accessReads: [] as ModelAccessScope[], accessCatalogs: [] as ModelAccessScope[], accessWrites: [] as unknown[],
};
const fixtureCapture = (profileId = 'scope-A', stale = false) => {
  const catalog = frontendHostFixtureCatalog();
  const activationId = stale ? `${catalog.activation_id}-stale` : catalog.activation_id;
  return {
    ...catalog, profile_id: profileId, activation_id: activationId, security_epoch: 73,
    contributions: catalog.contributions.map((contribution) => ({
      ...contribution, resolved_profile_id: profileId, resolved_activation_id: activationId,
    })),
  };
};
const capture = fixtureCapture();
const fixtureSearchScope = (profileId = capture.profile_id, stale = false) => {
  const current = fixtureCapture(profileId, stale);
  return searchCaptureScope(profileId, current, current.plan_hash);
};
const fallbackScope = searchCaptureScope(`${location.origin}${location.pathname}`);
const savedModel = { profile_id: 'saved-route', display_name: 'Saved Fixture Route', provider_id: 'openai', model_id: 'saved-model' };
const capturedHost: VerifiedFrontendHost = {
  catalog: capture, activePlanHash: capture.plan_hash,
  capabilities: {
    invokeAction: async () => { throw new Error('Unexpected fixture Host action'); },
    readDataSource: async () => { throw new Error('Unexpected fixture Host resource'); },
  },
};
const accessPolicy: ModelAccessPolicy = {
  version: MODEL_ACCESS_VERSION, mode: 'explicit', model_ids: ['fixture-model'],
};
const requireAccessScope = (scope: ModelAccessScope) => {
  if (scope.profile_id !== capture.profile_id || !['alpha', 'beta'].includes(scope.provider_instance_id)) {
    throw new Error('Unexpected fixture model-access scope');
  }
};
const modelAccessResources = createProviderModelAccessResources({
  async getModelAccess(scope) {
    requireAccessScope(scope);
    fixture.accessReads.push({ ...scope });
    return {
      ...scope,
      ...(accessMode === 'wrong-profile' ? { profile_id: 'scope-B' } : {}),
      ...(accessMode === 'wrong-connection' ? { provider_instance_id: scope.provider_instance_id === 'alpha' ? 'beta' : 'alpha' } : {}),
      registry_revision: 73, native_capability: null,
      model_access: accessMode === 'denied' ? { ...accessPolicy, model_ids: ['saved-model'] } : accessPolicy,
    };
  },
  async getModelAccessCatalog(scope) {
    requireAccessScope(scope);
    fixture.accessCatalogs.push({ profile_id: scope.profile_id, provider_instance_id: scope.provider_instance_id });
    return { profile_id: scope.profile_id, provider_instance_id: scope.provider_instance_id,
      models: [{ model_id: 'fixture-model', display_name: 'Fixture Remote Model' }], status: 'live' };
  },
  async setModelAccess(input) {
    fixture.accessWrites.push(input);
    throw new Error('Model-access changes are outside this fixture task');
  },
});
const modelAccessBinding = accessMode === 'missing' ? undefined
  : { profile_id: capture.profile_id, resources: modelAccessResources };
Object.assign(window, {
  fixture, fixtureSearchScope,
  requestFixtureProperties: () => requestModelProperties(savedModel, true, undefined,
    params.has('scoped') ? fixtureSearchScope() : fallbackScope),
  requestScopedFixtureProperties: (profileId: string, stale = false) =>
    requestModelProperties(savedModel, true, undefined, fixtureSearchScope(profileId, stale)),
  readFixtureProperties: getModelPropertiesRequest,
});
settingsApiResources.listProviderConnections = async () => ({ registry_revision: 73, connections: ['alpha', 'beta'].map(id => ({ provider_instance_id: id, display_name: 'Shared', catalog_provider_id: 'openai', credential_status: 'configured' as const, health_status: 'unverified' as const, reachability: 'unknown' as const, observed_at: null })) });
settingsApiResources.createModelProfile = async (payload) => { fixture.saves.push(payload); return {} as never; };
api.searchModels = async filters => {
  const connectionId = filters.connection_id;
  if (connectionId !== undefined && (typeof connectionId !== 'string' || !['alpha', 'beta'].includes(connectionId))) {
    throw new Error('Unexpected fixture model-search connection');
  }
  fixture.searches.push(filters);
  return { models: [{ profile_id: 'openai/fixture-model', display_name: 'Fixture Remote Model', model_id: 'fixture-model', provider_id: 'openai', connection_id: connectionId, provenance: 'provider_public_catalog', reachability: 'unverified' }], filters_applied: filters };
};
function Fixture() {
  const [mode, setMode] = useState<'standard' | 'advanced'>('standard');
  const [provider, setProvider] = useState('openai');
  const [dropdown, setDropdown] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [preferred, setPreferred] = useState('');
  const [requested, setRequested] = useState<ModelPropertiesRequest | null>(null);
  const profiles = [{ profile_id: 'saved-route', display_name: 'Saved Fixture Route', model_id: 'saved-model', provider_id: 'openai', route_configured: true, supports_vision: true, defaults: { thinking: 'high' }, metadata: { fixture: 'route metadata' } }];
  return <main className="bg-zinc-950 text-zinc-100 p-4 min-h-screen">
    <button onClick={() => setMode(mode === 'standard' ? 'advanced' : 'standard')}>Toggle display</button>
    <button onClick={() => setPreferred('beta')}>Prefer beta</button>
    <button onClick={() => setRequested({ requestId: 2, intent: 'properties', registered: false, identity: { connectionId: 'alpha', providerId: 'openai', modelId: 'fixture-model' }, model: { profile_id: 'openai/fixture-model', display_name: 'Fixture Remote Model', provider_id: 'openai', model_id: 'fixture-model', connection_id: 'alpha' } })}>Request alpha model</button>
    <button onClick={() => setRequested({ requestId: 1, intent: 'properties', registered: false, identity: { connectionId: 'beta', providerId: 'openai', modelId: 'fixture-model' }, model: { profile_id: 'openai/fixture-model', display_name: 'Fixture Remote Model', provider_id: 'openai', model_id: 'fixture-model' } })}>Request beta model</button>
    <ModelRouteSetup displayMode={mode} preferredConnectionId={preferred} requestedModel={requested} modelAccessBinding={modelAccessBinding} />
    <button onClick={() => setDropdown(true)}>Open composer dropdown</button>
    <button onClick={() => setGenerating(!generating)}>Toggle generation</button>
    <div style={{ position: 'relative', marginTop: 15 }}>{dropdown && <ModelDropdown profiles={profiles} selectedProfile={null} isGenerating={generating} placement="below" onSelect={profile => fixture.selections.push(profile as never)} onClose={() => setDropdown(false)} />}</div>
    <div data-testid="modal-scroll" style={{ position: 'fixed', right: 5, bottom: 10, width: 220, height: 150, overflow: 'auto', background: '#18181b' }}>
      <div style={{ height: 80 }} />
      <SearchableProviderField value={provider} options={Array.from({ length: 25 }, (_, index) => ({ provider_id: index ? `fixture-${index}` : 'openai', label: index ? `Fixture Provider ${index}` : 'OpenAI', kind: 'llm' as const, builtin: index === 0 }))} onChange={value => { fixture.selections.push(value); setProvider(value); }} />
      <div style={{ height: 250 }} />
    </div>
  </main>;
}
function ModalFixture() {
  const providers = [{ provider_id: 'openai', label: 'OpenAI', kind: 'llm', apis: [{ api_id: 'main', name: 'Fixture AI key', kind: 'llm', configured: true }] }, { provider_id: 'line', label: 'LINE', kind: 'custom', apis: [{ api_id: 'channel', name: 'Fixture external token', kind: 'custom', configured: true }] }];
  const sections = [{ id: 'models', label: 'Models', fields: [{ id: 'main_model', label: 'Fixture Main Model', type: 'model_select', options: [{ value: 'openai/saved-model', label: 'Saved Fixture Route' }] }, { id: 'model_api_routes', label: 'Fixture Legacy Override', type: 'model_api_routes', renderer: 'model_routing', options: [{ value: 'openai/saved-model', label: 'Saved Fixture Route', provider_id: 'openai' }], api_keys: providers }] }, { id: 'apis', label: 'APIs', fields: [{ id: 'api_keys', label: 'API Keys / Tokens', type: 'api_key_setup', renderer: 'api_key_setup', provider_scope: 'non_llm', api_keys: providers }] }, { id: 'general', label: 'Interface', fields: [{ id: 'composer_placeholder', label: 'Fixture Placeholder', type: 'text' }] }];
  const [values, setValues] = useState({ models: { main_model: 'openai/saved-model', model_api_routes: 'openai/saved-model: openai/main' }, apis: { api_keys: providers }, general: { composer_placeholder: 'Fixture original placeholder' } });
  return <SettingsModalRenderer isOpen activeSectionId="models" catalog={{ sidebar: { filters: [], items: [] }, settings: { sections: [], values: {} }, chat_rendering: { renderers: [] }, extension_points: [] }} health={null} previewsCount={0} settingsSections={sections as never} settingsValues={values} modelProfiles={[{ profile_id: 'saved-route', display_name: 'Saved Fixture Route', provider_id: 'openai', model_id: 'saved-model', route_configured: true }]} onClose={() => {}} onSettingChange={(section, field, value) => setValues(current => ({ ...current, [section]: { ...(current as any)[section], [field]: value } }))} />;
}
createRoot(document.getElementById('root')!).render(params.has('scoped') ? <VerifiedFrontendHostProvider value={capturedHost}><ModalFixture /></VerifiedFrontendHostProvider> : params.has('modal') ? <ModalFixture /> : <Fixture />);
