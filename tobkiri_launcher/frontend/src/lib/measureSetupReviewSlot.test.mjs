import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {createContext, Script} from 'node:vm';

const measure = new Script(readFileSync(new URL('./measureSetupReviewSlot.js', import.meta.url), 'utf8'));
const confirmation = `sha256:${'a'.repeat(64)}`;

function setup() {
  const scroller = {
    getBoundingClientRect: () => ({left: 10, top: 20}),
    clientLeft: 2, clientTop: 3, clientWidth: 700, clientHeight: 400,
    style: {overflowX: 'hidden', overflowY: 'auto'},
  };
  const slot = {
    parentElement: scroller,
    getBoundingClientRect: () => ({left: 30, top: -30, right: 730, bottom: 900}),
  };
  const anchor = {parentElement: slot, getAttribute: () => confirmation};
  const window = {
    location: {pathname: '/panel/setup'},
    visualViewport: {scale: 1, offsetLeft: 0, offsetTop: 0},
    innerWidth: 800, innerHeight: 600,
    getComputedStyle: (node) => ({visibility: 'visible', ...node.style}),
  };
  const document = {
    visibilityState: 'visible',
    documentElement: {clientWidth: 800, clientHeight: 600},
    querySelectorAll: () => [anchor],
    querySelector: () => null,
  };
  return {window, document, slot, anchor, crypto: webcrypto};
}

test('preserves the full slot and clips to ancestor content and the viewport', () => {
  const result = JSON.parse(measure.runInNewContext(setup()));
  assert.match(result.anchorId, /^[a-f0-9]{32}$/);
  assert.equal(result.confirmation, confirmation);
  assert.deepEqual(result.geometry, {
    full: {left: 30, top: -30, right: 730, bottom: 900},
    clip: {left: 30, top: 23, right: 712, bottom: 423},
    viewportWidth: 800, viewportHeight: 600,
  });
});

test('keeps the anchor and clears the geometry during temporary obstruction', () => {
  for (const change of [
    ({document}) => { document.querySelector = () => ({}); },
    ({document}) => { document.visibilityState = 'hidden'; },
    ({window}) => { window.visualViewport.scale = 1.5; },
    ({slot}) => { slot.style = {visibility: 'hidden'}; },
  ]) {
    const context = createContext(setup());
    const before = JSON.parse(measure.runInContext(context));
    change(context);
    assert.deepEqual(JSON.parse(measure.runInContext(context)), {...before, geometry: null});
  }
});

test('rejects another route, missing or duplicate anchors and invalid digests', () => {
  for (const change of [
    ({window}) => { window.location.pathname = '/panel/'; },
    ({document}) => { document.querySelectorAll = () => []; },
    ({document, anchor}) => { document.querySelectorAll = () => [anchor, anchor]; },
    ({anchor}) => { anchor.getAttribute = () => 'different'; },
  ]) {
    const context = setup();
    change(context);
    assert.equal(measure.runInNewContext(context), null);
  }
});

test('gives a replacement anchor a new identity even for the same confirmation', () => {
  const context = createContext(setup());
  const first = JSON.parse(measure.runInContext(context));
  const replacement = {...context.anchor};
  context.document.querySelectorAll = () => [replacement];
  const second = JSON.parse(measure.runInContext(context));
  assert.equal(second.confirmation, first.confirmation);
  assert.notEqual(second.anchorId, first.anchorId);
  assert.deepEqual(JSON.parse(measure.runInContext(context)), second);
});

test('samples a fresh identity after leaving and re-entering the Setup route', () => {
  const context = createContext(setup());
  const first = JSON.parse(measure.runInContext(context));
  context.window.location.pathname = '/panel/';
  assert.equal(measure.runInContext(context), null);
  context.window.location.pathname = '/panel/setup';
  const returned = JSON.parse(measure.runInContext(context));
  assert.equal(returned.confirmation, first.confirmation);
  assert.notEqual(returned.anchorId, first.anchorId);
});
