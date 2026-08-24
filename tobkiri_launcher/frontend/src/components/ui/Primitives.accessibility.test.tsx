import assert from 'node:assert/strict';
import test from 'node:test';
import type {ReactElement} from 'react';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {renderToStaticMarkup} from 'react-dom/server';
import {JSDOM} from 'jsdom';

import {useAppStore} from '@/src/store';
import {Button} from './Button';
import {Input} from './Input';
import {Switch} from './Switch';

interface RenderedDom {
  container: HTMLElement;
  dom: JSDOM;
  root: Root;
}

async function renderInDom(element: ReactElement): Promise<RenderedDom> {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="root"></div></body></html>',
    {url: 'http://localhost/'},
  );
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
  });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  return {container, dom, root};
}

async function closeRenderedDom(surface: RenderedDom): Promise<void> {
  await act(async () => surface.root.unmount());
  surface.dom.window.close();
}

test('Switch composes caller activation and cancellation for pointer and keyboard clicks', async () => {
  const changes: boolean[] = [];
  const callerClicks: number[] = [];
  const surface = await renderInDom(
    <Switch
      aria-label="Toggle Research Pack"
      checked={false}
      onClick={(event) => {
        callerClicks.push(event.detail);
        if (event.detail === 2) event.preventDefault();
      }}
      onCheckedChange={(checked) => changes.push(checked)}
    />,
  );

  try {
    const control = surface.container.querySelector<HTMLButtonElement>('[role="switch"]');
    assert.ok(control);
    assert.match(control.className, /min-h-11/);
    assert.match(control.className, /min-w-11/);

    await act(async () => {
      control.dispatchEvent(new window.MouseEvent('click', {bubbles: true, detail: 1}));
      control.dispatchEvent(new window.MouseEvent('click', {bubbles: true, detail: 0}));
      control.dispatchEvent(new window.MouseEvent('click', {bubbles: true, detail: 2}));
    });

    assert.deepEqual(callerClicks, [1, 0, 2]);
    assert.deepEqual(changes, [true, true]);
  } finally {
    await closeRenderedDom(surface);
  }
});

test('Switch preserves caller keyboard handlers while native activation owns toggling', async () => {
  let keyDownCount = 0;
  let checkedChangeCount = 0;
  const surface = await renderInDom(
    <Switch
      aria-label="Toggle Research Pack"
      checked={false}
      onKeyDown={() => { keyDownCount += 1; }}
      onCheckedChange={() => { checkedChangeCount += 1; }}
    />,
  );

  try {
    const control = surface.container.querySelector<HTMLButtonElement>('[role="switch"]');
    assert.ok(control);
    control.dispatchEvent(new window.KeyboardEvent('keydown', {key: ' ', bubbles: true}));
    assert.equal(keyDownCount, 1);
    assert.equal(checkedChangeCount, 0);
    await act(async () => {
      control.dispatchEvent(new window.MouseEvent('click', {bubbles: true, detail: 0}));
    });
    assert.equal(checkedChangeCount, 1);
  } finally {
    await closeRenderedDom(surface);
  }
});

test('Switch fails development and test renders without a naming relationship', () => {
  assert.throws(
    () => renderToStaticMarkup(
      // @ts-expect-error The runtime assertion protects untyped consumers too.
      <Switch checked={false} />,
    ),
    /requires aria-label or aria-labelledby/,
  );
});

test('disabled Switch ignores activation', async () => {
  let checkedChangeCount = 0;
  const surface = await renderInDom(
    <Switch
      aria-label="Toggle Research Pack"
      checked={false}
      disabled
      onCheckedChange={() => { checkedChangeCount += 1; }}
    />,
  );
  try {
    const control = surface.container.querySelector<HTMLButtonElement>('[role="switch"]');
    assert.ok(control);
    await act(async () => control.click());
    assert.equal(checkedChangeCount, 0);
  } finally {
    await closeRenderedDom(surface);
  }
});

test('Input creates stable unique IDs independent of duplicate or localized labels', async () => {
  const surface = await renderInDom(
    <><Input label="Name" /><Input label="Name" /></>,
  );

  try {
    const inputs = [...surface.container.querySelectorAll<HTMLInputElement>('input')];
    assert.equal(inputs.length, 2);
    assert.ok(inputs[0].id);
    assert.notEqual(inputs[0].id, inputs[1].id);
    assert.equal(surface.container.querySelectorAll(`label[for="${inputs[0].id}"]`).length, 1);
    const stableId = inputs[0].id;

    await act(async () => surface.root.render(<Input label={'\u540d\u524d'} />));
    assert.equal(surface.container.querySelector<HTMLInputElement>('input')?.id, stableId);
    assert.equal(surface.container.querySelector('label')?.textContent, '\u540d\u524d');
  } finally {
    await closeRenderedDom(surface);
  }
});

test('Input composes unlabeled helper and error relationships with semantic state', async () => {
  const surface = await renderInDom(
    <Input
      helperText="Use a workspace-relative path."
      aria-describedby="external-description"
      required
    />,
  );

  try {
    const input = surface.container.querySelector<HTMLInputElement>('input');
    assert.ok(input);
    const stableId = input.id;
    assert.equal(surface.container.querySelectorAll('[role="alert"]').length, 0);

    await act(async () => surface.root.render(
      <Input
        helperText="Use a workspace-relative path."
        error="Path is required."
        aria-describedby="external-description"
        required
      />,
    ));

    const invalidInput = surface.container.querySelector<HTMLInputElement>('input');
    assert.ok(invalidInput);
    assert.equal(invalidInput.id, stableId);
    const helper = document.getElementById(`${invalidInput.id}-helper`);
    const error = document.getElementById(`${invalidInput.id}-error`);
    assert.ok(helper);
    assert.ok(error);
    assert.equal(error.getAttribute('role'), 'alert');
    assert.equal(surface.container.querySelectorAll('[role="alert"]').length, 1);
    assert.equal(invalidInput.required, true);
    assert.equal(invalidInput.getAttribute('aria-invalid'), 'true');
    assert.deepEqual(
      invalidInput.getAttribute('aria-describedby')?.split(' '),
      ['external-description', helper.id, error.id],
    );
    assert.doesNotMatch(invalidInput.getAttribute('aria-describedby') ?? '', /undefined/);
  } finally {
    await closeRenderedDom(surface);
  }
});

test('Input hides the visual required marker from the accessible label', () => {
  const markup = renderToStaticMarkup(<Input label="Workspace" required />);
  assert.match(markup, /required=""/);
  assert.match(markup, /aria-hidden="true"[^>]*>\*<\/span>/);
});

test('icon-only Button fails development and test renders without an accessible name', () => {
  assert.throws(
    () => renderToStaticMarkup(<Button size="icon"><svg /></Button>),
    /requires aria-label or aria-labelledby/,
  );
  assert.doesNotThrow(() => renderToStaticMarkup(
    <Button size="icon" aria-label="Refresh"><svg /></Button>,
  ));
  const namedMarkup = renderToStaticMarkup(
    <Button size="icon" aria-label="Refresh"><svg /></Button>,
  );
  assert.match(namedMarkup, /min-h-11/);
  assert.match(namedMarkup, /min-w-11/);
  assert.throws(
    () => renderToStaticMarkup(<Button size="icon" aria-label=" "><svg /></Button>),
    /requires aria-label or aria-labelledby/,
  );
});

test('loading Button is busy, localized, disabled, and cannot submit twice', async () => {
  const previousState = useAppStore.getState();
  useAppStore.setState({profile: {...previousState.profile, language: 'ja'}});
  let submissions = 0;
  const surface = await renderInDom(
    <Button loading onClick={() => { submissions += 1; }}>Save</Button>,
  );

  try {
    const button = surface.container.querySelector<HTMLButtonElement>('button');
    assert.ok(button);
    assert.equal(button.disabled, true);
    assert.equal(button.getAttribute('aria-busy'), 'true');
    assert.equal(button.getAttribute('aria-label'), '\u51e6\u7406\u4e2d...');
    assert.equal(button.textContent, '\u51e6\u7406\u4e2d...');
    assert.equal(button.querySelector('[aria-hidden="true"]')?.getAttribute('class')?.includes('animate-spin'), true);
    await act(async () => {
      button.click();
      button.click();
    });
    assert.equal(submissions, 0);
  } finally {
    await closeRenderedDom(surface);
    useAppStore.setState(previousState, true);
  }
});

test('Button preserves caller busy state and supports a specific pending label', () => {
  const idleMarkup = renderToStaticMarkup(<Button aria-busy="true">Refresh</Button>);
  assert.match(idleMarkup, /aria-busy="true"/);
  const pendingMarkup = renderToStaticMarkup(
    <Button loading loadingLabel="Saving profile">Save</Button>,
  );
  assert.match(pendingMarkup, /aria-label="Saving profile"/);
  assert.match(pendingMarkup, />Saving profile<\/span>/);
  assert.doesNotMatch(pendingMarkup, />Save<\/button>/);
});
