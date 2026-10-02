import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';

import {copyTextToClipboard} from './clipboard';

test('copies complete diagnostic text with the Clipboard API', async () => {
  let copied = '';
  const success = await copyTextToClipboard('full diagnostic\nsecond line', {
    writeText: async (text: string) => {
      copied = text;
    },
  });

  assert.equal(success, true);
  assert.equal(copied, 'full diagnostic\nsecond line');
});

test('falls back to a selected textarea when Clipboard API is unavailable', async () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  let command = '';
  let selected = '';
  Object.defineProperty(dom.window.document, 'execCommand', {
    configurable: true,
    value: (nextCommand: string) => {
      command = nextCommand;
      selected = dom.window.document.querySelector('textarea')?.value ?? '';
      return true;
    },
  });

  const success = await copyTextToClipboard('recovery diagnostic', undefined, dom.window.document);

  assert.equal(success, true);
  assert.equal(command, 'copy');
  assert.equal(selected, 'recovery diagnostic');
  assert.equal(dom.window.document.querySelector('textarea'), null);
});

test('returns false when neither clipboard route is available', async () => {
  const success = await copyTextToClipboard('diagnostic', undefined, undefined);
  assert.equal(success, false);
});
