import React, { useState } from 'react';
import '../src/index.css';
import { createRoot } from 'react-dom/client';
import { ConversationSpotlight } from '../src/components/ConversationSpotlight';
import { SharedSearchTemplate } from '../src/features/search/SharedSearchTemplate';
import { useSpotlightShortcut } from '../src/features/search/useSpotlightShortcut';
import { useModelCatalogSearch } from '../src/features/search/useModelCatalogSearch';
import { parseSearchQuery } from '../src/features/search/searchQuery';
import { requestModelProperties } from '../src/features/search/modelPropertiesNavigation';
import { SpotlightShortcutRecorder } from '../src/renderers/SpotlightShortcutRecorder';
import { RightSidebar } from '../src/components/RightSidebar';
import { ChatHeaderRenderer } from '../src/renderers/ChatHeaderRenderer';
import { spotlightSidebarResults, type SpotlightResult } from '../src/lib/spotlightNavigation';
import type { ModelSearchItem, ModelSearchResponse, SidebarItem } from '../src/lib/api';

const calls: Record<string, unknown>[] = [];
const attempts = new Map<string, number>();
const fakeSearch = async (filters: Record<string, unknown>): Promise<ModelSearchResponse> => {
  calls.push(filters);
  const query = String(filters.query ?? '');
  attempts.set(query, (attempts.get(query) ?? 0) + 1);
  await new Promise(resolve => setTimeout(resolve, query === 'slow' ? 700 : 70));
  if (query === 'retry' && attempts.get(query) === 1) throw new Error('fixture failure');
  const provider = String(filters.provider_id ?? 'openrouter');
  const count = query === 'many' ? 130 : query === 'empty' ? 0 : 1;
  const models: ModelSearchItem[] = Array.from({ length: count }, (_, i) => ({
    profile_id: `${provider}/${query || 'alpha'}-${i}`, provider_id: provider,
    provider_display_name: provider === 'openrouter' ? 'OpenRouter' : provider,
    model_id: `${query || 'alpha'}-${i}`, display_name: `${query || 'Alpha'} Model ${i}`, configured: false,
  }));
  return { models, total: count, has_more: false } as ModelSearchResponse;
};
const catalog: SidebarItem[] = [
  { id: 'tool-search', label: 'Web Search', category: 'tool' },
  { id: 'disabled', label: 'Disabled Tool', category: 'tool' },
  { id: 'missing', label: 'Unavailable Tool', category: 'tool', tool_info: { setup_state: { status: 'missing' } } },
  { id: 'notes', label: 'Notes Widget', category: 'widget' },
];
function Fixture() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<'all'>('all');
  const [other, setOther] = useState(false);
  const [selection, setSelection] = useState('');
  const [properties, setProperties] = useState('');
  const [presetQuery, setPresetQuery] = useState('');
  const [sidebarTarget, setSidebarTarget] = useState('');
  const [targetTick, setTargetTick] = useState(0);
  const [toggleCount, setToggleCount] = useState(0);
  const [shortcut, setShortcut] = useState('Ctrl+K');
  const [changes, setChanges] = useState('');
  const [composerKeys, setComposerKeys] = useState(0);
  const parsed = parseSearchQuery(query);
  const models = useModelCatalogSearch({ text: parsed.text, providerIds: parsed.providerIds,
    enabled: open && !parsed.conflict && parsed.kinds.includes('model'), searchModels: fakeSearch });
  const allowTextInput = new URLSearchParams(window.location.search).get('allowTextInput') !== 'false';
  useSpotlightShortcut({ shortcut, enabled: true, allowTextInput, isMac: true,
    blocked: other || ((open || other) && !open), onOpen: () => setOpen(true) });
  const results: SpotlightResult[] = [];
  if (parsed.kinds.includes('chat') && (!parsed.text || 'Launch notes'.toLowerCase().includes(parsed.text.toLowerCase()))) {
    results.push({ kind: 'chat', id: 'chat-1', title: 'Launch notes', conversation: {
      conversation_id: 'chat-1', title: 'Launch notes', updated_at: Date.now(), matches: [], match_count: 0,
    } as any });
  }
  for (const kind of ['widget', 'tool'] as const) if (parsed.kinds.includes(kind)) {
    results.push(...spotlightSidebarResults(catalog, kind, parsed.text, { enabled: true, disabledToolIds: new Set(['disabled']) }));
  }
  if (parsed.kinds.includes('model')) results.push(...models.models.map(model => ({
    kind: 'model' as const, id: model.profile_id, title: model.display_name, model,
  })));
  return <><header className="border-b border-zinc-700 bg-zinc-900 px-5 py-3 text-sm text-zinc-300">Fixture · model API fake</header>
    <textarea aria-label="Composer" defaultValue="draft" onKeyDown={() => setComposerKeys(count => count + 1)} />
    <section data-testid="shortcut-recorder" className="m-5 max-w-xl rounded-xl border border-zinc-700 bg-zinc-900 p-5"><p className="mb-3 text-xs text-zinc-400">Fixture · model API fake</p><SpotlightShortcutRecorder sectionId="general" field={{id: "spotlight_shortcut", label: "会話検索のキー", type: "text", default: "Ctrl+K"} as any} value={shortcut} isMac onChange={(section, field, value) => {setChanges(JSON.stringify({section,field,value}));setShortcut(String(value));}} /></section>
    <output data-testid="shortcut-change">{changes}</output><output data-testid="composer-keys">{composerKeys}</output>
    <button onClick={() => setOther(true)}>Settings alternate route</button>
    <button onClick={() => setOpen(true)}>Open search</button>
    <div data-testid="header"><ChatHeaderRenderer title="Chat" showPreview={false} canShowPreview canOpenSettings onTogglePreview={() => {}} onOpenSettings={() => setOther(true)} /></div>
    {other && <section role="dialog" aria-label="Settings"><button onClick={() => setOther(false)}>Close Settings</button></section>}
    <RightSidebar items={catalog} activeItemId={sidebarTarget} settingsValues={{}} settingsSections={[]} workspaceTabsEnabled={false} onSettingChange={() => {}} onOpenSettings={() => setOther(true)} onToolToggle={() => setToggleCount(count => count + 1)} />
    <output data-testid="toggle-count">{toggleCount}</output>
    <output data-testid="selection">{selection}</output><output data-testid="properties">{properties}</output>
    <output data-testid="active-model">unchanged-model</output>
    <output data-testid="model-count">{models.models.length}</output><output data-testid="complete">{String(models.complete)}</output>
    <SharedSearchTemplate query={presetQuery} onQueryChange={setPresetQuery} preset={{ kinds: ['model'], providerIds: ['openrouter'] }} inputLabel="Preset search"
      items={[{ key:'preset', kind:'model', title:'Preset Model', value:'preset' }]} onSelect={() => setSelection('preset')} />
    <ConversationSpotlight isOpen={open} query={query} filter={filter} results={results} locale="ja" loading={models.loading} error={models.error} onRetry={models.retry}
      shortcutLabel="Ctrl+K / Cmd+K" onQueryChange={setQuery} onFilterChange={() => setFilter('all')} onClose={() => {setOpen(false);setQuery('');}}
      onOpenResult={result => { if (!result) return; if(result.kind === 'model') {
        setProperties(JSON.stringify(requestModelProperties(result.model, false)));
      } else { setSelection(`${result.kind}:${result.id}`); if (result.kind === 'tool' || result.kind === 'widget') { setSidebarTarget(`${result.id}:${targetTick + 1}`); setTargetTick(targetTick + 1); } } setOpen(false); setQuery(''); }} />
  </>;
}
(window as any).fixtureCalls = calls;
createRoot(document.getElementById('root')!).render(<Fixture />);
