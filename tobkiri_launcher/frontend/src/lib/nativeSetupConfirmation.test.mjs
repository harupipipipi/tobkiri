import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const script = readFileSync(new URL('./nativeSetupConfirmation.js', import.meta.url), 'utf8');

function fixture() {
  class Element {
    disabled = false;
    closest() { return null; }
  }
  let checkbox = new Element();
  checkbox.checked = false;
  const button = new Element();
  button.closest = () => button;
  const messages = [];
  let click;
  runInNewContext(script, {
    Element,
    document: {
      querySelector: () => checkbox,
      addEventListener: (_name, callback) => { click = callback; },
    },
    window: {webkit: {messageHandlers: {tobkiriSetupConfirmation: {
      postMessage: (value) => messages.push(value),
    }}}},
  });
  return {
    checkbox, button, messages,
    click: (target, isTrusted = true) => click({target, isTrusted}),
    replaceCheckbox: () => { checkbox = Object.assign(new Element(), {checked: true}); },
  };
}

test('requires trusted review and confirmation gestures, then consumes them once', () => {
  const review = fixture();
  const {checkbox, button, messages, click} = review;
  checkbox.checked = true;
  click(checkbox, false);
  click(button);
  assert.deepEqual(messages, []);
  click(checkbox);
  click(button, false);
  assert.deepEqual(messages, []);
  click(button);
  click(button);
  assert.deepEqual(messages, ['confirm']);
  assert.ok(button.disabled && checkbox.disabled);
});

test('refuses an unchecked, disabled or replaced review control', () => {
  for (const change of [
    ({checkbox}) => { checkbox.checked = false; },
    ({checkbox}) => { checkbox.disabled = true; },
    ({button}) => { button.disabled = true; },
    ({replaceCheckbox}) => replaceCheckbox(),
  ]) {
    const review = fixture();
    review.checkbox.checked = true;
    review.click(review.checkbox);
    change(review);
    review.click(review.button);
    assert.deepEqual(review.messages, []);
  }
});
