import assert from 'node:assert/strict';
import test from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';

import { TobkiriLoader } from './TobkiriLoader';
import { useAppStore } from '@/src/store';

test('panel loader covers the viewer surface while leaving the sidebar visible', () => {
  useAppStore.setState({ isSidebarOpen: true });
  const markup = renderToStaticMarkup(<TobkiriLoader label="Loading profile" />);

  assert.match(markup, /data-loading-scope="panel"/);
  assert.match(markup, /fixed inset-y-0 right-0/);
  assert.match(markup, /md:left-\[240px\]/);
  assert.match(markup, /<img/);
  assert.match(markup, /aspect-\[2\/1\] w-full animate-pulse object-contain/);
  assert.match(markup, /mix-blend-multiply dark:mix-blend-screen dark:invert/);
  assert.match(markup, /max-w-xs/);
  assert.doesNotMatch(markup, /rounded-2xl/);
  assert.doesNotMatch(markup, /dark:bg-black/);
  assert.doesNotMatch(markup, /tobkiri-launcher-icon\.png/);
  assert.doesNotMatch(markup, /<object/);
});

test('screen loader covers setup screens that do not have a sidebar', () => {
  const markup = renderToStaticMarkup(<TobkiriLoader scope="screen" />);

  assert.match(markup, /data-loading-scope="screen"/);
  assert.match(markup, /fixed inset-0/);
});
