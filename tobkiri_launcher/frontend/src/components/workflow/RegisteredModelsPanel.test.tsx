import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {RegisteredModelsPanel} from './RegisteredModelsPanel';
import {translate} from '@/src/lib/i18n';
import {useAppStore} from '@/src/store';

test('registered model panel renders client-owned labels in the selected locale', async () => {
  const dom = new JSDOM('<div id="root"></div>', {url: 'http://localhost/panel/'});
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.querySelector('#root');
  assert.ok(container);
  const root = createRoot(container);
  const initial = useAppStore.getState().profile;
  try {
    await act(async () => {
      useAppStore.setState({profile: {...initial, language: 'en'}});
      root.render(<RegisteredModelsPanel contextKey="profile-a"/>);
    });
    const english = container.innerHTML;
    assert.match(english, /aria-label="Shared registered models"/);
    assert.match(english, /Registered models/);
    assert.match(english, /Refresh/);
    assert.match(english, /API keys are not returned/);

    await act(async () => useAppStore.setState({profile: {...initial, language: 'ja'}}));
    const japanese = container.innerHTML;
    assert.match(japanese, /aria-label="共有の登録済みモデル"/);
    assert.match(japanese, /登録済みモデル/);
    assert.match(japanese, /更新/);
    assert.match(japanese, /APIキーは返しません/);
  } finally {
    await act(async () => root.unmount());
    useAppStore.setState({profile: initial});
    dom.window.close();
  }
});

test('registered model error, empty and missing-provider text has English and Japanese translations', () => {
  const expected = {
    error: ['Could not load registrations.', '登録情報を取得できません。'],
    empty: ['No registered models are available for selection.', '選択可能な登録済みモデルがありません'],
    no_provider: ['No connection specified', '接続未指定'],
  };
  for (const [key, [english, japanese]] of Object.entries(expected)) {
    assert.ok(translate(`registered_models.${key}`, undefined, 'en').startsWith(english));
    assert.ok(translate(`registered_models.${key}`, undefined, 'ja').startsWith(japanese));
    assert.equal(translate(`registered_models.${key}`, undefined, 'fr'), translate(`registered_models.${key}`, undefined, 'en'));
  }
});
