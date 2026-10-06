import { expect, test, type Locator } from '@playwright/test';

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
  }
  await expect(root.locator('[data-search-token]')).toHaveText(['@model', '@openai']);
  await expect(root.locator('[data-search-token]').first()).toHaveCSS('color', 'rgb(96, 165, 250)');
  await expect(input).toHaveJSProperty('tagName', 'INPUT');
  await input.evaluate(el => (el as HTMLInputElement).setSelectionRange(0, 7));
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

test('Composer model picker keeps hidden preset invisible and colors typed mentions inline', async ({page}) => {
  await page.goto('/e2e/model-unification-fixture/index.html');
  await page.getByRole('button', {name:'Open composer dropdown'}).click();
  await verifyInline(page.getByRole('combobox', {name:'モデルを検索'}));
  await expect(page.getByRole('button', {name:/を解除/})).toHaveCount(0);
  await page.getByRole('combobox', {name:'モデルを検索'}).locator('..').screenshot({path:'e2e/inline-composer-model-search.png'});
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
  expect(await page.evaluate(() => (window as any).fixture.selections)).toEqual([]);
});
