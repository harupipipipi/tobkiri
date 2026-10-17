import { expect, test, type Locator } from '@playwright/test';
import { MODEL_CATALOG_DEBOUNCE_MS } from '../src/features/search/modelCatalogSearch';

const verifyInline = async (input: Locator) => {
  await expect(input).toHaveValue('');
  const root = input.locator('..');
  await expect(root.locator('[data-search-token]')).toHaveCount(0);
  await input.fill('@model @openai Fixture');
  await expect(root.locator('[data-search-token]')).toHaveCount(0);
  for (const end of [6, 14]) {
    await input.evaluate((el, caret) => {
      (el as HTMLInputElement).setSelectionRange(caret, caret);
      el.dispatchEvent(new Event('select', {bubbles:true}));
    }, end);
    await input.press('Enter');
    // Confirmation restores its caret in requestAnimationFrame. Observe that
    // callback before the next explicit selection can be overwritten by it.
    await input.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
    await expect(input).toHaveJSProperty('selectionStart', end + 1);
    await expect(input).toHaveJSProperty('selectionEnd', end + 1);
  }
  await expect(root.locator('[data-search-token]')).toHaveText(['@model', '@openai']);
  await expect(root.locator('[data-search-token]').first()).toHaveCSS('color', 'rgb(96, 165, 250)');
  await expect(input).toHaveJSProperty('tagName', 'INPUT');
  await input.evaluate(el => (el as HTMLInputElement).setSelectionRange(0, 7));
  await expect(input).toHaveJSProperty('selectionStart', 0);
  await expect(input).toHaveJSProperty('selectionEnd', 7);
  await input.press('Backspace');
  await expect(input).toHaveValue('@openai Fixture');
  await expect(root.locator('[data-search-token]')).toHaveText(['@openai']);
};

test('Settings model picker keeps hidden preset invisible and colors typed mentions inline', async ({page}) => {
  await page.goto('/e2e/model-unification-fixture/index.html?modal');
  await page.locator('[data-model-search-picker]').first().getByRole('button').first().click();
  const popup = page.locator('[data-viewport-popover]');
  await verifyInline(popup.getByRole('combobox', {name:'モデルを検索'}));
  await expect(page.getByRole('button', {name:/を解除/})).toHaveCount(0);
  await popup.screenshot({path:'e2e/inline-settings-model-search.png'});
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('Composer registered-model search filters providers in a native input without mention tokens', async ({page}) => {
  await page.clock.install();
  await page.goto('/e2e/model-unification-fixture/index.html');
  // No API connection is selected, so the separate route setup stays inactive.
  await expect(page.getByLabel('使用するAPI')).toHaveValue('');
  await page.getByRole('button', {name:'Open composer dropdown'}).click();
  const input = page.getByRole('combobox', {name:'モデルを検索'});
  const root = input.locator('..');
  const results = page.getByRole('listbox', {name:'登録済みモデル'});
  await expect(input).toHaveValue('');
  await expect(input).toHaveJSProperty('tagName', 'INPUT');
  await input.fill('@openai Fixture');
  await expect(results.getByRole('option', {name:/Saved Fixture Route/})).toBeVisible();
  await input.fill('@anthropic Fixture');
  await expect(results.getByRole('option')).toHaveCount(0);
  await input.fill('@openai Fixture');
  await input.evaluate(el => (el as HTMLInputElement).setSelectionRange(0, 8));
  await input.press('Backspace');
  await expect(input).toHaveValue('Fixture');
  await expect(results.getByRole('option', {name:/Saved Fixture Route/})).toBeVisible();
  await expect(root.locator('[data-search-input-overlay], [data-search-token]')).toHaveCount(0);
  await expect(page.getByRole('button', {name:/を解除/})).toHaveCount(0);
  // Observe beyond the catalog debounce without a wall-clock sleep.
  await page.clock.runFor(MODEL_CATALOG_DEBOUNCE_MS + 1);
  expect(await page.evaluate(() => (window as any).fixture.searches)).toEqual([]);
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
  expect(await page.evaluate(() => (window as any).fixture.selections)).toEqual([]);
  await root.screenshot({path:'e2e/inline-composer-model-search.png'});
});
