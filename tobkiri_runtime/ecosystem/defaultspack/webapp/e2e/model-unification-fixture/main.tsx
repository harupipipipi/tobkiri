import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ModelRouteSetup } from '../../src/features/models/ModelRouteSetup';
import { SearchableProviderField } from '../../src/renderers/settings/renderers/providerSelectField';
import { settingsApiResources } from '../../src/features/settings/resources/settingsApiResources';
import { api } from '../../src/lib/api';
import { ModelDropdown } from '../../src/renderers/ComposerRenderer';
import { SettingsModalRenderer } from '../../src/renderers/SettingsModalRenderer';
import { requestModelProperties, getModelPropertiesRequest, type ModelPropertiesRequest } from '../../src/features/search/modelPropertiesNavigation';
import { VerifiedFrontendHostProvider } from '../../src/host/VerifiedFrontendHostContext';
import '../../src/index.css';

const fixture = { saves: [] as unknown[], searches: [] as Record<string, unknown>[], selections: [] as string[] };
Object.assign(window, { fixture });
Object.assign(window, { requestFixtureProperties: () => requestModelProperties({ profile_id: 'saved-route', display_name: 'Saved Fixture Route', provider_id: 'openai', model_id: 'saved-model' }, true) });
Object.assign(window, { requestScopedFixtureProperties: (scope: string) => requestModelProperties({ profile_id: 'saved-route', display_name: 'Saved Fixture Route', provider_id: 'openai', model_id: 'saved-model' }, true, undefined, scope), readFixtureProperties: getModelPropertiesRequest });
settingsApiResources.listProviderConnections = async () => ({ registry_revision: 73, connections: ['alpha', 'beta'].map(id => ({ provider_instance_id: id, display_name: 'Shared', catalog_provider_id: 'openai', credential_status: 'configured' as const, health_status: 'unverified' as const, reachability: 'unknown' as const, observed_at: null })) });
settingsApiResources.createModelProfile = async (payload) => { fixture.saves.push(payload); return {} as never; };
api.searchModels = async filters => { fixture.searches.push(filters); return { models: [{ profile_id: 'openai/fixture-model', display_name: 'Fixture Remote Model', model_id: 'fixture-model', provider_id: 'openai', connection_id: filters.connection_id, provenance: 'provider_public_catalog', reachability: 'unverified' }], filters_applied: filters }; };
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
    <ModelRouteSetup displayMode={mode} preferredConnectionId={preferred} requestedModel={requested} />
    <button onClick={() => setDropdown(true)}>Open composer dropdown</button>
    <button onClick={() => setGenerating(!generating)}>Toggle generation</button>
    <div style={{ position: 'relative', marginTop: 15 }}>{dropdown && <ModelDropdown profiles={profiles} selectedProfile={null} isGenerating={generating} placement="below" onSelect={profile => fixture.selections.push(profile as never)} onClose={() => setDropdown(false)} />}</div>
    <div data-testid="modal-scroll" style={{ position: 'fixed', right: 5, bottom: 10, width: 220, height: 150, overflow: 'auto', background: '#18181b' }}>
      <div style={{ height: 80 }} />
      <SearchableProviderField value={provider} options={Array.from({ length: 25 }, (_, index) => ({ provider_id: index ? `fixture-${index}` : 'openai', label: index ? `Fixture Provider ${index}` : 'OpenAI', kind: 'llm' as const }))} onChange={value => { fixture.selections.push(value); setProvider(value); }} />
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
createRoot(document.getElementById('root')!).render(location.search.includes('scoped') ? <VerifiedFrontendHostProvider value={{ catalog: { profile_id: 'scope-A' } as never, activePlanHash: 'fixture-plan', capabilities: {} as never }}><ModalFixture /></VerifiedFrontendHostProvider> : location.search.includes('modal') ? <ModalFixture /> : <Fixture />);
