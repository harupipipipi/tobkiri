import assert from 'node:assert/strict';
import test from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {MemoryRouter} from 'react-router';

import {AiInput} from './AiInput';
import {ApiMap} from './ApiMap';
import {Flow} from './Flow';
import {Graph} from './Graph';
import {NodeManager} from './NodeManager';
import {Profile} from './Profile';
import {ProfileFiles} from './ProfileFiles';
import {ProfileWiring} from './ProfileWiring';
import {Settings} from './Settings';
import {LAUNCHER_ADVANCED_VIEWS} from '@/src/lib/advancedSurfaces';
import {useAppStore} from '@/src/store';

const routePages = [
  ['profile', Profile],
  ['settings', Settings],
  ['profileWiring', ProfileWiring],
  ['profileFiles', ProfileFiles],
  ['flow', Flow],
  ['graph', Graph],
  ['aiInput', AiInput],
  ['apiMap', ApiMap],
  ['nodeManager', NodeManager],
] as const;

function assertRenderedLabel(html: string, label: string): void {
  // renderToStaticMarkup escapes text nodes; assert the serialized DOM value
  // without changing the product copy or treating `&` as a regexp token.
  const serializedLabel = label.replaceAll('&', '&amp;');
  assert.ok(html.includes(serializedLabel), `missing surface label: ${label}`);
}

test('every restored Advanced route renders its named v4 surface and explicit support status', () => {
  const previousState = useAppStore.getState();
  useAppStore.setState({packs: [], packsLoading: false});
  try {
    for (const [id, Page] of routePages) {
      const html = renderToStaticMarkup(
        <MemoryRouter initialEntries={['/']}>
          <Page />
        </MemoryRouter>,
      );
      assertRenderedLabel(html, LAUNCHER_ADVANCED_VIEWS[id].label);
      assert.match(html, new RegExp(`data-advanced-action="${LAUNCHER_ADVANCED_VIEWS[id].actions}"`));
      assert.match(html, /Partial|Mapped|Rebuilt|Launcher local/);
      assert.doesNotMatch(html, /runtime-recovery|Recovery|GraphEditor|aiInputGraph/);
    }
  } finally {
    useAppStore.setState(previousState, true);
  }
});

test('Graph and Profile Wiring wait for evidence before declaring bindings unavailable', () => {
  const graph = renderToStaticMarkup(<MemoryRouter><Graph /></MemoryRouter>);
  const wiring = renderToStaticMarkup(<MemoryRouter><ProfileWiring /></MemoryRouter>);
  for (const html of [graph, wiring]) {
    assert.match(html, /Loading the canonical v4 projection/);
    assert.doesNotMatch(html, /v4 operation is not provided/);
  }
});

test('Profile Advanced route presents the authoritative catalog source with Tobkiri naming', () => {
  assert.deepEqual(LAUNCHER_ADVANCED_VIEWS.profile.sources, ['profile', 'profiles']);
  const html = renderToStaticMarkup(<MemoryRouter><Profile /></MemoryRouter>);
  assert.match(html, /Advanced Profile catalog/);
  assert.match(html, /Broker-backed Protocol v4 catalog/);
  assert.doesNotMatch(html, /Rumi AI|Rumi Viewer|Tokbiri|Tobikiri/);
});

test('Advanced action metadata is visible and aligned with desktop/mobile-safe controls', () => {
  const flow = renderToStaticMarkup(<MemoryRouter><Flow /></MemoryRouter>);
  const aiInput = renderToStaticMarkup(<MemoryRouter><AiInput /></MemoryRouter>);
  const graph = renderToStaticMarkup(<MemoryRouter><Graph /></MemoryRouter>);
  const apiMap = renderToStaticMarkup(<MemoryRouter><ApiMap /></MemoryRouter>);

  assert.match(flow, /data-advanced-action="contract_invoke"/);
  assert.match(aiInput, /Action: contract_invoke/);
  assert.match(flow + aiInput, /provider side effects|Provider may perform side effects/i);
  assert.match(flow + aiInput, /Host approval is required/);
  assert.doesNotMatch(flow + aiInput, /Partial \/ read-only/);
  assert.match(graph + apiMap, /data-advanced-action="read_only"/);
  assert.match(graph + apiMap, /No runtime invocation controls are available/);
  assert.doesNotMatch(graph + apiMap, /Invoke declared operation/);
});
