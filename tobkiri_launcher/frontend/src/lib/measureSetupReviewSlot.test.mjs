import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {Script} from 'node:vm';

const measure = new Script(readFileSync(new URL('./measureSetupReviewSlot.js', import.meta.url), 'utf8'));

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
  const window = {
    location: {pathname: '/panel/setup'},
    visualViewport: {scale: 1, offsetLeft: 0, offsetTop: 0},
    innerWidth: 800, innerHeight: 600,
    getComputedStyle: (node) => ({visibility: 'visible', ...node.style}),
  };
  const document = {
    visibilityState: 'visible',
    documentElement: {clientWidth: 800, clientHeight: 600},
    querySelectorAll: () => [slot],
    querySelector: () => null,
  };
  return {window, document, slot};
}

test('preserves the full slot and clips to ancestor content and the viewport', () => {
  const result = JSON.parse(measure.runInNewContext(setup()));
  assert.deepEqual(result, {
    full: {left: 30, top: -30, right: 730, bottom: 900},
    clip: {left: 30, top: 23, right: 712, bottom: 423},
    viewportWidth: 800, viewportHeight: 600,
  });
});

test('removes the geometry for modal, hidden, zoomed or different pages', () => {
  for (const change of [
    ({document}) => { document.querySelector = () => ({}); },
    ({document}) => { document.visibilityState = 'hidden'; },
    ({window}) => { window.visualViewport.scale = 1.5; },
    ({window}) => { window.location.pathname = '/panel/'; },
    ({slot}) => { slot.style = {visibility: 'hidden'}; },
    ({document, slot}) => { document.querySelectorAll = () => [slot, slot]; },
  ]) {
    const context = setup();
    change(context);
    assert.equal(measure.runInNewContext(context), null);
  }
});
