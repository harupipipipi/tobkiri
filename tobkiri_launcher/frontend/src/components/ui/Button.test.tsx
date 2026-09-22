import assert from 'node:assert/strict';
import test from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';

import {Button} from './Button';

test('loading buttons expose busy state and a standard inline spinner', () => {
  const markup = renderToStaticMarkup(<Button loading>Saving</Button>);

  assert.match(markup, /aria-busy="true"/);
  assert.match(markup, /disabled=""/);
  assert.match(markup, /data-loading-indicator="spinner"/);
  assert.match(markup, /Saving/);
  assert.doesNotMatch(markup, /tobkiri-startup-blade-cut/);
});

test('idle buttons do not render a loading indicator', () => {
  const markup = renderToStaticMarkup(<Button>Save</Button>);

  assert.doesNotMatch(markup, /aria-busy="true"/);
  assert.doesNotMatch(markup, /data-loading-indicator="spinner"/);
});
