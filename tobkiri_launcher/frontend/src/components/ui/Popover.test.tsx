import assert from 'node:assert/strict';
import test from 'node:test';
import {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {JSDOM} from 'jsdom';

import {
  Popover,
  PopoverContent,
  PopoverMenuItem,
  PopoverTrigger,
  computePopoverPosition,
} from './Popover';

const nextTick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

type Surface = {
  container: HTMLElement;
  dom: JSDOM;
  root: Root;
  restore: () => void;
};

const GLOBAL_KEYS = [
  'window',
  'document',
  'navigator',
  'MutationObserver',
  'ResizeObserver',
  'IS_REACT_ACT_ENVIRONMENT',
] as const;

function createSurface(resizeObserver?: typeof ResizeObserver): Surface {
  const snapshots = new Map<PropertyKey, PropertyDescriptor | undefined>();
  for (const key of GLOBAL_KEYS) {
    snapshots.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  }
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="root"></div></body></html>',
    {url: 'http://localhost/panel/'},
  );
  Object.defineProperties(globalThis, {
    window: {value: dom.window, configurable: true},
    document: {value: dom.window.document, configurable: true},
    navigator: {value: dom.window.navigator, configurable: true},
    MutationObserver: {value: dom.window.MutationObserver, configurable: true},
    ResizeObserver: {value: resizeObserver, configurable: true},
    IS_REACT_ACT_ENVIRONMENT: {value: true, configurable: true},
  });
  const container = dom.window.document.querySelector<HTMLElement>('#root');
  assert.ok(container);
  return {
    container,
    dom,
    root: createRoot(container),
    restore: () => {
      dom.window.close();
      for (const key of GLOBAL_KEYS) {
        const descriptor = snapshots.get(key);
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else Reflect.deleteProperty(globalThis, key);
      }
    },
  };
}

async function cleanup(surface: Surface): Promise<void> {
  await act(async () => { surface.root.unmount(); });
  surface.restore();
}

function key(target: EventTarget, dom: JSDOM, value: string): void {
  target.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
    bubbles: true,
    cancelable: true,
    key: value,
  }));
}

test('generic popover uses disclosure semantics and only closes intentionally', async () => {
  const surface = createSurface();
  try {
    await act(async () => {
      surface.root.render(
        <Popover mode="popover">
          <PopoverTrigger>Details</PopoverTrigger>
          <PopoverContent>
            <button type="button" onClick={() => undefined}>Async action</button>
            <button type="button" data-popover-close onClick={() => undefined}>Done</button>
          </PopoverContent>
        </Popover>,
      );
    });
    const trigger = surface.container.querySelector<HTMLButtonElement>('button');
    assert.ok(trigger);
    const controlledId = trigger.getAttribute('aria-controls');
    assert.ok(controlledId);
    assert.equal(trigger.getAttribute('aria-haspopup'), null);
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    trigger.focus();

    await act(async () => { trigger.click(); await nextTick(); });
    const content = surface.dom.window.document.getElementById(controlledId);
    assert.ok(content);
    assert.equal(content.getAttribute('role'), null);
    assert.equal(content.getAttribute('aria-labelledby'), trigger.id);
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    assert.equal(surface.dom.window.document.activeElement, trigger);

    const asyncAction = Array.from(content.querySelectorAll('button')).find(
      (button) => button.textContent === 'Async action',
    );
    assert.ok(asyncAction);
    await act(async () => { asyncAction.click(); await nextTick(); });
    assert.ok(surface.dom.window.document.getElementById(controlledId));

    const done = content.querySelector<HTMLButtonElement>('[data-popover-close]');
    assert.ok(done);
    await act(async () => { done.click(); await nextTick(); });
    assert.equal(surface.dom.window.document.getElementById(controlledId), null);
    assert.equal(surface.dom.window.document.activeElement, trigger);
  } finally {
    await cleanup(surface);
  }
});

test('dialog mode enters focus and Tab may leave without returning to the trigger', async () => {
  const surface = createSurface();
  try {
    await act(async () => {
      surface.root.render(
        <>
          <Popover mode="dialog">
            <PopoverTrigger>Open panel</PopoverTrigger>
            <PopoverContent aria-label="Tools">
              <button type="button" onClick={() => undefined}>First tool</button>
            </PopoverContent>
          </Popover>
          <button type="button" onClick={() => undefined}>After panel</button>
        </>,
      );
    });
    const trigger = Array.from(surface.container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Open panel',
    );
    const after = Array.from(surface.container.querySelectorAll('button')).find(
      (button) => button.textContent === 'After panel',
    );
    assert.ok(trigger);
    assert.ok(after);
    assert.equal(trigger.getAttribute('aria-haspopup'), 'dialog');

    await act(async () => { trigger.click(); await nextTick(); });
    const dialog = surface.dom.window.document.querySelector<HTMLElement>('[role="dialog"]');
    const first = dialog?.querySelector<HTMLButtonElement>('button');
    assert.ok(dialog);
    assert.ok(first);
    assert.equal(surface.dom.window.document.activeElement, first);

    await act(async () => { after.focus(); await nextTick(); });
    assert.equal(surface.dom.window.document.querySelector('[role="dialog"]'), null);
    assert.equal(surface.dom.window.document.activeElement, after);
  } finally {
    await cleanup(surface);
  }
});

test('sibling dialog popovers keep independent state', async () => {
  const surface = createSurface();
  try {
    await act(async () => {
      surface.root.render(
        <>
          <Popover mode="dialog">
            <PopoverTrigger>First trigger</PopoverTrigger>
            <PopoverContent aria-label="First content">
              <a href="/first">First link</a>
            </PopoverContent>
          </Popover>
          <Popover mode="dialog">
            <PopoverTrigger>Second trigger</PopoverTrigger>
            <PopoverContent aria-label="Second content">
              <a href="/second">Second link</a>
            </PopoverContent>
          </Popover>
        </>,
      );
    });
    const triggers = surface.container.querySelectorAll<HTMLButtonElement>('button');
    assert.equal(triggers.length, 2);
    await act(async () => { triggers[1]?.click(); await nextTick(); });
    assert.ok(surface.dom.window.document.querySelector('[aria-label="Second content"]'));
    assert.equal(surface.dom.window.document.querySelector('[aria-label="First content"]'), null);
  } finally {
    await cleanup(surface);
  }
});

test('menu mode implements APG movement, disabled handling, typeahead, Tab, and Escape', async () => {
  const surface = createSurface();
  let disabledActivations = 0;
  try {
    await act(async () => {
      surface.root.render(
        <Popover mode="menu">
          <PopoverTrigger>Commands</PopoverTrigger>
          <PopoverContent aria-label="Commands">
            <PopoverMenuItem>Alpha</PopoverMenuItem>
            <PopoverMenuItem disabled onClick={() => { disabledActivations += 1; }}>
              Beta disabled
            </PopoverMenuItem>
            <PopoverMenuItem>Charlie</PopoverMenuItem>
          </PopoverContent>
        </Popover>,
      );
    });
    const trigger = surface.container.querySelector<HTMLButtonElement>('button');
    assert.ok(trigger);
    assert.equal(trigger.getAttribute('aria-haspopup'), 'menu');

    await act(async () => { key(trigger, surface.dom, 'ArrowDown'); await nextTick(); });
    let menu = surface.dom.window.document.querySelector<HTMLElement>('[role="menu"]');
    assert.ok(menu);
    const items = Array.from(menu.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'));
    assert.equal(items.length, 3);
    assert.equal(surface.dom.window.document.activeElement, items[0]);
    assert.equal(items[1]?.getAttribute('aria-disabled'), 'true');
    assert.equal(items[1]?.hasAttribute('disabled'), false);

    await act(async () => { key(items[0]!, surface.dom, 'ArrowDown'); });
    assert.equal(surface.dom.window.document.activeElement, items[1]);
    await act(async () => { items[1]?.click(); await nextTick(); });
    assert.equal(disabledActivations, 0);
    assert.ok(surface.dom.window.document.querySelector('[role="menu"]'));

    await act(async () => { key(items[1]!, surface.dom, 'End'); });
    assert.equal(surface.dom.window.document.activeElement, items[2]);
    await act(async () => { key(items[2]!, surface.dom, 'Home'); });
    assert.equal(surface.dom.window.document.activeElement, items[0]);
    await act(async () => { key(items[0]!, surface.dom, 'c'); });
    assert.equal(surface.dom.window.document.activeElement, items[2]);
    await act(async () => { key(items[2]!, surface.dom, 'ArrowDown'); });
    assert.equal(surface.dom.window.document.activeElement, items[0]);

    await act(async () => { key(items[0]!, surface.dom, 'Escape'); await nextTick(); });
    assert.equal(surface.dom.window.document.querySelector('[role="menu"]'), null);
    assert.equal(surface.dom.window.document.activeElement, trigger);

    await act(async () => { key(trigger, surface.dom, 'ArrowUp'); await nextTick(); });
    menu = surface.dom.window.document.querySelector<HTMLElement>('[role="menu"]');
    const reopenedItems = Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    assert.equal(surface.dom.window.document.activeElement, reopenedItems[2]);
    await act(async () => { key(reopenedItems[2]!, surface.dom, 'Tab'); await nextTick(); });
    assert.equal(surface.dom.window.document.querySelector('[role="menu"]'), null);
    assert.notEqual(surface.dom.window.document.activeElement, trigger);
  } finally {
    await cleanup(surface);
  }
});

test('Escape closes only the topmost nested layer', async () => {
  const surface = createSurface();
  try {
    await act(async () => {
      surface.root.render(
        <Popover mode="dialog">
          <PopoverTrigger>Outer</PopoverTrigger>
          <PopoverContent aria-label="Outer layer">
            <Popover mode="dialog">
              <PopoverTrigger>Inner</PopoverTrigger>
              <PopoverContent aria-label="Inner layer">
                <button type="button" onClick={() => undefined}>Inner action</button>
              </PopoverContent>
            </Popover>
          </PopoverContent>
        </Popover>,
      );
    });
    const outerTrigger = surface.container.querySelector<HTMLButtonElement>('button');
    assert.ok(outerTrigger);
    await act(async () => { outerTrigger.click(); await nextTick(); });
    const innerTrigger = surface.dom.window.document.querySelector<HTMLButtonElement>(
      '[role="dialog"][aria-label="Outer layer"] button',
    );
    assert.ok(innerTrigger);
    await act(async () => { innerTrigger.click(); await nextTick(); });
    const inner = surface.dom.window.document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="Inner layer"]',
    );
    assert.ok(inner);

    await act(async () => { key(inner, surface.dom, 'Escape'); await nextTick(); });
    assert.equal(
      surface.dom.window.document.querySelector('[role="dialog"][aria-label="Inner layer"]'),
      null,
    );
    assert.ok(
      surface.dom.window.document.querySelector('[role="dialog"][aria-label="Outer layer"]'),
    );

    const outer = surface.dom.window.document.querySelector<HTMLElement>(
      '[role="dialog"][aria-label="Outer layer"]',
    );
    assert.ok(outer);
    await act(async () => { key(outer, surface.dom, 'Escape'); await nextTick(); });
    assert.equal(surface.dom.window.document.querySelector('[role="dialog"]'), null);
  } finally {
    await cleanup(surface);
  }
});

test('trigger removal closes the layer and restores the nearest surviving focus target', async () => {
  const surface = createSurface();
  const renderFixture = (showTrigger: boolean) => (
    <>
      <button type="button" onClick={() => undefined}>Before</button>
      <Popover mode="dialog">
        {showTrigger ? <PopoverTrigger>Removable trigger</PopoverTrigger> : null}
        <PopoverContent aria-label="Temporary panel">
          <button type="button" onClick={() => undefined}>Panel action</button>
        </PopoverContent>
      </Popover>
      <button type="button" onClick={() => undefined}>After</button>
    </>
  );
  try {
    await act(async () => { surface.root.render(renderFixture(true)); });
    const trigger = Array.from(surface.container.querySelectorAll('button')).find(
      (button) => button.textContent === 'Removable trigger',
    );
    assert.ok(trigger);
    await act(async () => { trigger.click(); await nextTick(); });
    assert.ok(surface.dom.window.document.querySelector('[aria-label="Temporary panel"]'));

    await act(async () => { surface.root.render(renderFixture(false)); await nextTick(); });
    await act(async () => { await nextTick(); });
    const after = Array.from(surface.container.querySelectorAll('button')).find(
      (button) => button.textContent === 'After',
    );
    assert.ok(after);
    assert.equal(surface.dom.window.document.querySelector('[aria-label="Temporary panel"]'), null);
    assert.equal(surface.dom.window.document.activeElement, after);
  } finally {
    await cleanup(surface);
  }
});

test('positioning flips and shifts inside bottom, right, oversized, and zoomed viewports', () => {
  assert.deepEqual(
    computePopoverPosition(
      {bottom: 780, height: 40, left: 760, right: 800, top: 740, width: 40},
      {height: 120, width: 240},
      {height: 800, width: 800},
      'right',
    ),
    {left: 552, placement: 'top', top: 612},
  );
  assert.deepEqual(
    computePopoverPosition(
      {bottom: 460, height: 40, left: 650, right: 690, top: 420, width: 40},
      {height: 160, width: 220},
      {height: 300, left: 300, top: 200, width: 400},
      'left',
    ),
    {left: 472, placement: 'top', top: 252},
  );
  assert.deepEqual(
    computePopoverPosition(
      {bottom: 40, height: 20, left: 20, right: 40, top: 20, width: 20},
      {height: 500, width: 500},
      {height: 180, width: 180},
      'center',
    ),
    {left: 8, placement: 'bottom', top: 8},
  );
});

test('content and visual viewport changes recompute measured placement', async () => {
  const resizeCallbacks: ResizeObserverCallback[] = [];
  class TestResizeObserver {
    constructor(callback: ResizeObserverCallback) { resizeCallbacks.push(callback); }
    disconnect(): void {}
    observe(): void {}
    unobserve(): void {}
  }
  const surface = createSurface(TestResizeObserver as unknown as typeof ResizeObserver);
  const visualViewport = new surface.dom.window.EventTarget() as VisualViewport;
  Object.assign(visualViewport, {
    height: 300,
    offsetLeft: 100,
    offsetTop: 50,
    width: 400,
  });
  Object.defineProperty(surface.dom.window, 'visualViewport', {
    configurable: true,
    value: visualViewport,
  });
  let contentSize = {height: 80, width: 160};
  const originalRect = surface.dom.window.HTMLElement.prototype.getBoundingClientRect;
  surface.dom.window.HTMLElement.prototype.getBoundingClientRect = function getRect() {
    if (this.textContent === 'Position trigger') {
      return {bottom: 330, height: 30, left: 430, right: 470, top: 300, width: 40} as DOMRect;
    }
    if (this.getAttribute('aria-label') === 'Position content') {
      return {
        bottom: contentSize.height,
        height: contentSize.height,
        left: 0,
        right: contentSize.width,
        top: 0,
        width: contentSize.width,
      } as DOMRect;
    }
    return originalRect.call(this);
  };
  try {
    await act(async () => {
      surface.root.render(
        <Popover mode="popover">
          <PopoverTrigger>Position trigger</PopoverTrigger>
          <PopoverContent aria-label="Position content">Localized content</PopoverContent>
        </Popover>,
      );
    });
    const trigger = surface.container.querySelector<HTMLButtonElement>('button');
    assert.ok(trigger);
    await act(async () => { trigger.click(); await nextTick(); });
    const content = surface.dom.window.document.querySelector<HTMLElement>(
      '[aria-label="Position content"]',
    );
    assert.ok(content);
    assert.equal(content.style.left, '310px');
    assert.equal(content.style.top, '212px');
    assert.equal(content.style.maxWidth, '384px');

    contentSize = {height: 220, width: 380};
    await act(async () => {
      for (const callback of resizeCallbacks) callback([], {} as ResizeObserver);
      await nextTick();
    });
    assert.equal(content.style.left, '108px');
    assert.equal(content.style.top, '72px');

    Object.assign(visualViewport, {height: 240, offsetLeft: 180, width: 260});
    await act(async () => {
      visualViewport.dispatchEvent(new surface.dom.window.Event('resize'));
      await nextTick();
    });
    assert.equal(content.style.left, '188px');
    assert.equal(content.style.maxWidth, '244px');
    assert.equal(content.style.minWidth, '192px');
  } finally {
    surface.dom.window.HTMLElement.prototype.getBoundingClientRect = originalRect;
    await cleanup(surface);
  }
});
