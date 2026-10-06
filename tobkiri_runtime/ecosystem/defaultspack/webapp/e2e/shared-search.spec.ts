import { test, expect, type Page } from '@playwright/test';
const search = (page: Page) => page.getByRole('combobox', { name: '会話履歴を検索' });
const open = async (page: Page) => { await page.getByRole('textbox', { name:'Composer' }).focus(); await page.keyboard.press('Control+k'); await expect(page.getByRole('dialog')).toBeVisible(); };
test.beforeEach(async ({page}) => { await page.goto('/e2e/shared-search.fixture.html'); });
test('real browser Ctrl+K and Cmd+K open from textarea and Escape restores focus', async ({page}) => {
  await open(page); await search(page).press('Escape'); await expect(page.getByRole('textbox', {name:'Composer'})).toBeFocused();
  await page.keyboard.press('Meta+k'); await expect(search(page)).toBeFocused(); await search(page).press('Escape');
  await expect(page.getByRole('textbox', {name:'Composer'})).toBeFocused();
});
test('Escape dismisses filter candidates before closing dialog; Tab stays inside', async ({page}) => {
  await open(page); await search(page).fill('@'); await expect(page.getByRole('listbox',{name:'参照候補'})).toBeVisible();
  await search(page).press('Escape'); await expect(page.getByRole('dialog')).toBeVisible();
  await search(page).fill(''); await search(page).press('Tab'); await expect(page.getByRole('button',{name:'会話検索を閉じる'})).toBeFocused();
  await page.keyboard.press('Tab'); await expect(page.getByRole('combobox',{name:'絞り込み',exact:true})).toBeFocused();
  await page.keyboard.press('Tab'); await expect(search(page)).toBeFocused();
  await search(page).press('Escape'); await expect(page.getByRole('dialog')).toHaveCount(0);
});
test('synthetic IME events do not open or select (separate from real keyboard evidence)', async ({page}) => {
  await page.getByRole('textbox',{name:'Composer'}).evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,isComposing:true,bubbles:true})));
  await expect(page.getByRole('dialog')).toHaveCount(0); await open(page);
  await search(page).evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true})));
  await expect(page.getByRole('dialog')).toBeVisible(); await expect(page.getByTestId('selection')).toBeEmpty();
  await search(page).evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:229,bubbles:true})));
  await expect(page.getByRole('dialog')).toBeVisible();
});
test('other visible dialog blocks shortcut and Settings alternate survives removed toolbar', async ({page}) => {
  await expect(page.getByTestId('header')).toBeEmpty(); await page.getByRole('button',{name:'Settings alternate route'}).click();
  await page.keyboard.press('Control+k'); await expect(page.getByRole('dialog')).toHaveCount(1); await expect(search(page)).toHaveCount(0);
  await page.getByRole('button',{name:'Close Settings'}).click(); await open(page);
});
test('chat widget tool and model filters use actual registered results and status', async ({page}) => {
  await open(page); await search(page).fill('@chat Launch'); await expect(page.getByRole('option',{name:'Launch notes'})).toBeVisible();
  await search(page).fill('@widget'); await expect(page.getByRole('option',{name:'Notes Widget'})).toBeVisible();
  await search(page).fill('@tool'); await expect(page.getByRole('option',{name:/Disabled Tool.*Disabled/})).toBeVisible(); await expect(page.getByRole('option',{name:/Unavailable Tool.*Unavailable/})).toBeVisible();
  await search(page).fill('@widget absent'); await expect(page.getByRole('status').filter({hasText:'一致する登録済み'})).toBeVisible();
  await search(page).fill('@model empty'); await expect(page.getByRole('status').filter({hasText:'一致するモデルはありません'})).toBeVisible();
});
test('multiple model provider filters preserve free text; native deletion and unknown @ remain literal', async ({page}) => {
  await open(page); await search(page).fill('@model @openrouter alpha');
  await expect(page.getByRole('option',{name:/alpha Model 0/})).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).fixtureCalls.at(-1))).toMatchObject({query:'alpha',provider_id:'openrouter',max_results:100});
  await expect(page.getByRole('button',{name:/を解除/})).toHaveCount(0);
  await search(page).evaluate(el => (el as HTMLInputElement).setSelectionRange(0, 7));
  await search(page).press('Backspace'); await expect(search(page)).toHaveValue('@openrouter alpha');
  await search(page).evaluate(el => (el as HTMLInputElement).setSelectionRange(0, 12));
  await search(page).press('Backspace'); await expect(search(page)).toHaveValue('alpha');
  await search(page).fill('@unknown'); await expect(page.getByRole('button',{name:'@unknown を解除'})).toHaveCount(0); await expect(search(page)).toHaveValue('@unknown');
});
test('hidden preset conflicts stay constrained and selection only requests properties', async ({page}) => {
  const preset = page.getByRole('combobox',{name:'Preset search'}); await preset.fill('@tool');
  await expect(page.getByRole('status').filter({hasText:'固定の検索条件'})).toBeVisible(); await expect(page.getByRole('option',{name:'Preset Model'})).toHaveCount(0);
  await preset.fill('@model @openai'); await expect(page.getByRole('status').filter({hasText:'固定の検索条件'})).toBeVisible();
  await open(page); await search(page).fill('@model @openrouter alpha'); await expect(page.getByRole('option',{name:/alpha Model 0/})).toBeVisible(); await search(page).press('Enter');
  await expect(page.getByTestId('properties')).toContainText('"intent":"properties"'); await expect(page.getByTestId('properties')).toContainText('"registered":false');
  await expect(page.getByTestId('active-model')).toHaveText('unchanged-model'); await expect(page.getByTestId('selection')).toBeEmpty();
});
test('actual model hook caps catalogs and retries errors', async ({page}) => {
  await open(page); await search(page).fill('@model many'); await expect(page.getByRole('listbox',{name:'検索結果'}).getByRole('option')).toHaveCount(100); await expect(page.getByTestId('complete')).toHaveText('false');
  await search(page).fill('@model retry'); await expect(page.getByRole('alert')).toBeVisible(); await page.getByRole('button',{name:'再試行'}).click(); await expect(page.getByRole('option',{name:/retry Model 0/})).toBeVisible();
});
test('actual model hook prevents stale async catalog replacing current results', async ({page}) => {
  await open(page); await search(page).fill('@model slow'); await expect.poll(() => page.evaluate(() => (window as any).fixtureCalls.some((call:any) => call.query==='slow'))).toBe(true);
  await search(page).fill('@model fast'); await expect(page.getByRole('option',{name:/fast Model 0/})).toBeVisible();
  await page.waitForTimeout(850); await expect(page.getByRole('option',{name:/fast Model 0/})).toBeVisible(); await expect(page.getByRole('option',{name:/slow Model 0/})).toHaveCount(0);
});
test('actual RightSidebar external tool and widget requests survive default category and prior search without toggling', async ({page}) => {
  await open(page); await search(page).fill('@tool Web'); await search(page).press('Enter');
  const sidebar = page.getByRole('complementary',{name:'Tools and utility panels'});
  await expect(sidebar.getByRole('heading',{name:'Web Search',exact:true})).toBeVisible();
  await sidebar.getByRole('button',{name:'Close panel',exact:true}).click();
  await sidebar.getByTitle('名前検索',{exact:true}).click(); await page.getByPlaceholder('名前で検索').fill('nonmatching');
  await open(page); await search(page).fill('@widget Notes'); await search(page).press('Enter');
  await expect(sidebar.getByRole('heading',{name:'Notes Widget',exact:true})).toBeVisible();
  await expect(page.getByPlaceholder('名前で検索')).toHaveValue('');
  await page.mouse.click(5, 5); // Dismiss the surviving name-search popover backdrop.
  await sidebar.getByRole('button',{name:'Close panel',exact:true}).click();
  await open(page); await search(page).fill('@tool Web'); await search(page).press('Enter');
  await expect(sidebar.getByRole('heading',{name:'Web Search',exact:true})).toBeVisible();
  await expect(page.getByTestId('toggle-count')).toHaveText('0');
});
test('actual shortcut recorder deliberately captures custom canonical binding, cancels and restores default', async ({page}) => {
  const recorder = page.getByTestId('shortcut-recorder');
  const binding = recorder.getByRole('button').first();
  await expect(binding).toHaveText('Ctrl+K / Cmd+K'); await expect(binding).toHaveAttribute('aria-pressed','false');
  await binding.focus(); await page.keyboard.press('Control+Shift+p'); await expect(page.getByTestId('shortcut-change')).toBeEmpty();
  await binding.click(); await expect(binding).toHaveAttribute('aria-pressed','true'); await page.keyboard.press('Control+Shift+p');
  await expect(page.getByTestId('shortcut-change')).toHaveText('{"section":"general","field":"spotlight_shortcut","value":"Ctrl+Shift+P"}');
  await expect(binding).toHaveText('Ctrl+Shift+P');
  await recorder.screenshot({path:'e2e/shortcut-recorder-current-binding.png'});
  await binding.click(); await page.keyboard.press('Escape'); await expect(binding).toHaveAttribute('aria-pressed','false'); await expect(binding).toHaveText('Ctrl+Shift+P');
  await recorder.getByRole('button',{name:'既定に戻す'}).click(); await expect(binding).toHaveText('Ctrl+K / Cmd+K');
  await expect(page.getByTestId('shortcut-change')).toContainText('"value":"Ctrl+K"');
});
test('recorder consumes actual Ctrl+K before global dialog and rejects modifier or synthetic IME without saving', async ({page}) => {
  const binding = page.getByTestId('shortcut-recorder').getByRole('button').first();
  await binding.click(); await page.keyboard.press('Control'); await expect(binding).toHaveAttribute('aria-pressed','true'); await expect(page.getByTestId('shortcut-change')).toBeEmpty();
  await binding.evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,isComposing:true,bubbles:true})));
  await binding.evaluate(el => el.dispatchEvent(new KeyboardEvent('keydown',{key:'k',ctrlKey:true,keyCode:229,bubbles:true})));
  await expect(binding).toHaveAttribute('aria-pressed','true'); await expect(page.getByTestId('shortcut-change')).toBeEmpty();
  await page.keyboard.press('Control+k'); await expect(binding).toHaveAttribute('aria-pressed','false');
  await expect(page.getByTestId('shortcut-change')).toContainText('"value":"Ctrl+K"'); await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByTestId('composer-keys')).toHaveText('0');
});
test('shared model query colors inline tokens with no removal chip row for screenshot evidence', async ({page}) => {
  await open(page); await search(page).fill('@model @openrouter alpha'); await expect(page.getByRole('option',{name:/alpha Model 0/})).toBeVisible();
  await expect(page.getByRole('button',{name:/を解除/})).toHaveCount(0);
  await expect(page.getByRole('dialog').locator('[data-search-token]')).toHaveText(['@model', '@openrouter']);
  await page.screenshot({path:'e2e/shared-search-inline-model-provider.png'});
});

test('only recognized standalone mentions receive inert inline blue styling', async ({page}) => {
  await open(page);
  await search(page).fill('hello @model @openrouter @unknown email@model @modelled');
  const overlay = page.getByRole('dialog').locator('[data-search-input-overlay]');
  await expect(overlay).toHaveAttribute('aria-hidden', 'true');
  await expect(overlay).toHaveCSS('pointer-events', 'none');
  await expect(overlay).toHaveText('hello @model @openrouter @unknown email@model @modelled');
  const tokens = overlay.locator('[data-search-token]');
  await expect(tokens).toHaveText(['@model', '@openrouter']);
  for (const token of await tokens.all()) {
    const color = await token.evaluate(el => getComputedStyle(el).color);
    const channels = color.match(/\d+/g)!.map(Number);
    expect(channels[2]).toBeGreaterThan(channels[0]);
  }
  await expect(search(page)).toHaveJSProperty('tagName', 'INPUT');
  await expect(search(page)).not.toHaveAttribute('contenteditable', 'true');
  await expect(page.getByRole('button', {name:/を解除/})).toHaveCount(0);
});

test('native selection replacement and caret editing preserve inline token text', async ({page}) => {
  await open(page); await search(page).fill('@model alpha @unknown');
  await search(page).evaluate(el => (el as HTMLInputElement).setSelectionRange(7, 12));
  await search(page).press('x');
  await expect(search(page)).toHaveValue('@model x @unknown');
  await search(page).press('Home'); await search(page).press('ArrowRight'); await search(page).press('Delete');
  await expect(search(page)).toHaveValue('@odel x @unknown');
  await expect(page.getByRole('dialog').locator('[data-search-token]')).toHaveCount(0);
});

test('long native input scrolls its inline overlay and preserves composing edits', async ({page}) => {
  await open(page);
  const value = `@model ${'very-long-search '.repeat(20)}@openrouter`;
  await search(page).fill(value); await search(page).press('End');
  await expect.poll(() => search(page).evaluate(el => (el as HTMLInputElement).scrollLeft)).toBeGreaterThan(0);
  await expect.poll(() => page.getByRole('dialog').locator('[data-search-input-scroll]').evaluate(el => {
    const input = el.parentElement?.parentElement?.querySelector('input');
    const transform = getComputedStyle(el).transform;
    return input ? Math.abs(new DOMMatrixReadOnly(transform).m41 + input.scrollLeft) : Infinity;
  })).toBeLessThan(1);
  await search(page).fill('@model ');
  await search(page).dispatchEvent('compositionstart', {data:''});
  await search(page).evaluate(el => {
    const input = el as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    setter.call(input, '@model 日本');
    input.dispatchEvent(new InputEvent('input', {bubbles:true, data:'日本', inputType:'insertCompositionText', isComposing:true}));
    input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, isComposing:true}));
  });
  await expect(search(page)).toHaveValue('@model 日本');
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByTestId('properties')).toBeEmpty();
  await search(page).dispatchEvent('compositionend', {data:'日本'});
  await expect(page.getByRole('dialog').locator('[data-search-token]')).toHaveText(['@model']);
});

test('preset constraints stay invisible while typed recognized mentions remain inline', async ({page}) => {
  const preset = page.getByRole('combobox',{name:'Preset search'});
  await expect(preset).toHaveValue('');
  const container = preset.locator('..');
  await expect(container.locator('[data-search-token]')).toHaveCount(0);
  await expect(page.getByRole('button',{name:/を解除/})).toHaveCount(0);
  await preset.fill('@model @openrouter alpha');
  await expect(container.locator('[data-search-token]')).toHaveText(['@model', '@openrouter']);
  await expect(preset).toHaveValue('@model @openrouter alpha');
  await expect(page.getByRole('option',{name:'Preset Model'})).toBeVisible();
});

test('ordinary search shows named results and ordinary period and star choices', async ({page}) => {
  await open(page);
  await expect(search(page)).toHaveAttribute('placeholder', '検索');
  await search(page).fill('Launch');
  const results = page.getByRole('dialog').getByRole('listbox', {name:'検索結果'});
  await expect(results.getByRole('option', {name:/Launch notes/})).toBeVisible();
  await expect(results.getByRole('option').first()).toContainText('会話');
  await expect(results).not.toContainText('@');
  await expect(page.getByRole('listbox', {name:'参照候補'})).toHaveCount(0);
  const filters = page.getByRole('combobox', {name:'絞り込み', exact:true});
  await expect(filters).toBeEnabled();
  await expect(filters.locator('option')).toHaveText(['すべて', '今日', '7日以内', '30日以内', 'スター付き']);
  await filters.selectOption('today'); await expect(search(page)).toHaveValue('Launch');
  await filters.selectOption('starred'); await expect(search(page)).toHaveValue('Launch');
  await expect(page.getByRole('dialog').locator('[data-search-token]')).toHaveCount(0);
  await page.getByRole('dialog').screenshot({path:'e2e/inline-ordinary-search.png'});
});

test('at completion offers only references and retains inline model provider filtering', async ({page}) => {
  await open(page); await search(page).fill('@');
  const references = page.getByRole('listbox', {name:'参照候補'});
  await expect(references).toBeVisible();
  const names = await references.getByRole('option').allTextContents();
  expect(names.length).toBeGreaterThan(4);
  expect(names.every(name => /@[a-z0-9-]+$/.test(name.trim()))).toBe(true);
  expect(names.join(' ')).not.toMatch(/期間|今日|7日|30日|スター|実行|削除/);
  await references.getByRole('option', {name:/@model$/}).click();
  await expect(search(page)).toHaveValue('@model ');
  await search(page).press('End'); await search(page).press('@');
  await references.getByRole('option', {name:/@openrouter$/}).click();
  await expect(search(page)).toHaveValue('@model @openrouter ');
  await search(page).press('End'); await search(page).press('a');
  await expect(page.getByRole('option', {name:/a Model 0/})).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).fixtureCalls.at(-1))).toMatchObject({query:'a', provider_id:'openrouter'});
  const tokens = page.getByRole('dialog').locator('[data-search-token]');
  await expect(tokens).toHaveText(['@model', '@openrouter']);
  await expect(tokens.first()).toHaveCSS('color', 'rgb(96, 165, 250)');
  const result = page.getByRole('listbox', {name:'検索結果'}).getByRole('option').first();
  await expect(result).toContainText('モデル'); await expect(result).not.toContainText('@model');
  await expect(page.getByRole('combobox', {name:'絞り込み', exact:true})).toBeDisabled();
});

test('hidden mandatory preset scopes reference candidates without rendering preset tokens', async ({page}) => {
  const preset = page.getByRole('combobox', {name:'Preset search'});
  await preset.fill('@');
  const references = page.getByRole('listbox', {name:'参照候補'});
  await expect(references.getByRole('option')).toHaveCount(2);
  await expect(references.getByRole('option', {name:/@model$/})).toBeVisible();
  await expect(references.getByRole('option', {name:/@openrouter$/})).toBeVisible();
  await expect(preset.locator('..').locator('[data-search-token]')).toHaveCount(0);
  await references.getByRole('option', {name:/@openrouter$/}).click();
  await expect(preset).toHaveValue('@openrouter ');
  await expect(preset.locator('..').locator('[data-search-token]')).toHaveText(['@openrouter']);
  await expect(page.getByRole('option', {name:'Preset Model'})).toBeVisible();
});


test('repeated shortcut stays inside open Spotlight and consumes held chords without reopening', async ({page}) => {
  await open(page);
  await search(page).fill('@model alpha');
  await search(page).press('Control+k');
  await expect(search(page)).toBeFocused();
  await expect(search(page)).toHaveValue('@model alpha');
  await page.keyboard.down('Meta');
  await page.keyboard.down('k');
  await page.keyboard.down('k');
  await page.keyboard.up('k');
  await page.keyboard.up('Meta');
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await expect(search(page)).toBeFocused();
  await expect(search(page)).toHaveValue('@model alpha');
  const consumed = await search(page).evaluate(el => !el.dispatchEvent(new KeyboardEvent('keydown', {
    key:'k',ctrlKey:true,repeat:true,bubbles:true,cancelable:true,
  })));
  expect(consumed).toBe(true);
  await expect(page.getByRole('button', {name:/を解除/})).toHaveCount(0);
  await expect(page.getByRole('dialog').locator('[data-search-token]')).toHaveText(['@model']);
});


test('input-disabled shortcuts remain inactive outside search but are consumed inside open Spotlight', async ({page}) => {
  await page.goto('/e2e/shared-search.fixture.html?allowTextInput=false');
  await page.getByRole('textbox', {name:'Composer'}).focus();
  const outsideConsumed = await page.getByRole('textbox', {name:'Composer'}).evaluate(el => !el.dispatchEvent(new KeyboardEvent('keydown', {
    key:'k',ctrlKey:true,bubbles:true,cancelable:true,
  })));
  expect(outsideConsumed).toBe(false);
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', {name:'Open search', exact:true}).click();
  await search(page).fill('@model alpha');
  for (const repeat of [false, true]) {
    const consumed = await search(page).evaluate((el, repeat) => !el.dispatchEvent(new KeyboardEvent('keydown', {
      key:'k',ctrlKey:true,repeat,bubbles:true,cancelable:true,
    })), repeat);
    expect(consumed).toBe(true);
  }
  await expect(page.getByRole('dialog')).toHaveCount(1);
  await expect(search(page)).toHaveValue('@model alpha');
});
