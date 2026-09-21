import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import test from 'node:test';

import {useAppStore} from '@/src/store';
import {DEVTOOLS_PREFERENCE_STORAGE_KEY} from './devtoolsPreference';

test('Devtools preference persists locally in both selected states', () => {
  const previousLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const previousState = useAppStore.getState();
  const dom = new JSDOM('', {url: 'http://localhost/panel/settings'});
  Object.defineProperty(globalThis, 'localStorage', {
    value: dom.window.localStorage,
    configurable: true,
  });

  try {
    useAppStore.getState().setDevtoolsEnabled(true);
    assert.equal(useAppStore.getState().devtoolsEnabled, true);
    assert.equal(
      dom.window.localStorage.getItem(DEVTOOLS_PREFERENCE_STORAGE_KEY),
      'true',
    );

    useAppStore.getState().setDevtoolsEnabled(false);
    assert.equal(useAppStore.getState().devtoolsEnabled, false);
    assert.equal(
      dom.window.localStorage.getItem(DEVTOOLS_PREFERENCE_STORAGE_KEY),
      'false',
    );
  } finally {
    useAppStore.setState(previousState, true);
    dom.window.close();
    if (previousLocalStorage) {
      Object.defineProperty(globalThis, 'localStorage', previousLocalStorage);
    } else {
      Reflect.deleteProperty(globalThis, 'localStorage');
    }
  }
});
