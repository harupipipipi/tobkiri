import assert from 'node:assert/strict';
import {renderToStaticMarkup} from 'react-dom/server';
import test from 'node:test';

import {useAppStore} from '@/src/store';
import {Settings} from './Settings';

test('Settings identifies Devtools as a Launcher-local switch', () => {
  const previousState = useAppStore.getState();
  try {
    useAppStore.setState({devtoolsEnabled: false});
    const html = renderToStaticMarkup(<Settings />);
    assert.match(html, /role="switch"/);
    assert.match(html, /aria-checked="false"/);
    assert.match(html, /source: launcher_local/);
    assert.match(html, /does not grant runtime authority/);
    assert.match(html, /alter Pack closure/);
  } finally {
    useAppStore.setState(previousState, true);
  }
});
