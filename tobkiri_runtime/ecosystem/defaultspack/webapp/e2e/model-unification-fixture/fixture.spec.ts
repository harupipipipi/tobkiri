import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

const chatSendRequests = new WeakMap<Page, string[]>();

test.beforeEach(async ({ page }) => {
  const sends: string[] = [];
  chatSendRequests.set(page, sends);
  page.on('request', request => {
    if (request.method() !== 'GET' && /\/(?:chat|conversation)[^?]*(?:send|turn|message)/.test(request.url())) sends.push(request.url());
  });
  await page.goto('/e2e/model-unification-fixture/index.html');
  await expect(page.getByLabel('使用するAPI')).toBeEnabled();
});
test.afterEach(async ({ page }) => expect(chatSendRequests.get(page)).toEqual([]));

test('API identities, shared provider catalog, conflict and explicit save boundary', async ({ page }) => {
  const api = page.getByLabel('使用するAPI');
  await expect(api.locator('option[value="alpha"]')).toHaveText('openai-Shared (alpha)');
  await expect(api.locator('option[value="beta"]')).toHaveText('openai-Shared (beta)');
  await api.selectOption('beta');
  await expect(page.getByText('カスタムモデル・一覧にないモデル')).toHaveCount(0);
  const search = page.getByRole('combobox', { name: 'モデルを検索' });
  await search.fill('@openai Fixture');
  await expect(page.getByRole('option', { name: /Fixture Remote Model/ })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as any).fixture.searches.length)).toBeGreaterThan(0);
  const requests = await page.evaluate(() => (window as any).fixture.searches);
  expect(requests.every((request: Record<string, unknown>) => request.connection_id === 'beta')).toBe(true);
  await search.fill('@anthropic Fixture');
  await expect(page.getByRole('option', { name: /Fixture Remote Model/ })).toHaveCount(0);
  await search.fill('@openai Fixture');
  await page.getByRole('option', { name: /Fixture Remote Model/ }).click();
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
  await page.getByRole('button', { name: 'このモデルを使う' }).click();
  await expect.poll(() => page.evaluate(() => (window as any).fixture.saves.length)).toBe(1);
  const [saved] = await page.evaluate(() => (window as any).fixture.saves);
  expect(saved).toMatchObject({ model_id: 'fixture-model', provider_instance_id: 'beta', provider_registry_revision: 73 });
});

test('Advanced custom draft survives display toggles without implicit save', async ({ page }) => {
  await page.getByLabel('使用するAPI').selectOption('alpha');
  await page.getByRole('button', { name: 'Toggle display' }).click();
  await page.getByText('カスタムモデル・一覧にないモデル', { exact: true }).click();
  await page.getByRole('checkbox', { name: 'モデルIDを指定する' }).check();
  await page.getByRole('textbox', { name: 'モデルID', exact: true }).fill('custom/raw-model');
  await page.getByRole('button', { name: 'Toggle display' }).click();
  await expect(page.getByRole('textbox', { name: 'モデルID', exact: true })).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: 'カスタムモデル:' })).toContainText('custom/raw-model');
  await page.getByRole('button', { name: 'Toggle display' }).click();
  await expect(page.getByRole('textbox', { name: 'モデルID', exact: true })).toHaveValue('custom/raw-model');
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('real provider popover escapes scroll container, follows resize, closes and restores focus', async ({ page }) => {
  await page.setViewportSize({ width: 480, height: 480 });
  const trigger = page.getByTestId('modal-scroll').getByRole('button', { name: /OpenAI/ });
  await trigger.click();
  const input = page.getByPlaceholder('provider を検索');
  await expect(input).toBeVisible();
  await input.press('Shift+Tab');
  await expect.poll(() => input.evaluate(element => element.closest('[data-viewport-popover]')?.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Tab');
  await expect(input).toBeFocused();
  const panel = input.locator('xpath=ancestor::div[contains(@class,"fixed")][1]');
  const checkBounds = async () => {
    const bounds = await panel.boundingBox();
    const viewport = page.viewportSize()!;
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height);
  };
  await checkBounds();
  await page.getByTestId('modal-scroll').evaluate(element => { element.scrollTop = 30; });
  await page.setViewportSize({ width: 360, height: 360 });
  await expect.poll(async () => { const b = await panel.boundingBox(); return b ? b.x + b.width : 9999; }).toBeLessThanOrEqual(360);
  await checkBounds();
  await input.press('Escape');
  await expect(input).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await page.getByRole('button', { name: 'close provider select', exact: true }).click({ position: { x: 2, y: 2 } });
  await expect(input).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test('composer returns exact registered route and generation blocks selection', async ({ page }) => {
  await page.getByRole('button', { name: 'Open composer dropdown' }).click();
  await page.getByRole('option', { name: /Saved Fixture Route/ }).click();
  expect(await page.evaluate(() => (window as any).fixture.selections)).toEqual([{ profile_id: 'saved-route', display_name: 'Saved Fixture Route', model_id: 'saved-model', provider_id: 'openai', route_configured: true, supports_vision: true, defaults: { thinking: 'high' }, metadata: { fixture: 'route metadata' } }]);
  await page.getByRole('button', { name: 'Toggle generation' }).click();
  await page.getByRole('button', { name: 'Open composer dropdown' }).click();
  await page.getByRole('option', { name: /Saved Fixture Route/ }).click();
  expect(await page.evaluate(() => (window as any).fixture.selections.length)).toBe(1);
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('composer catalogue choice remains unregistered without saving or selecting', async ({ page }) => {
  await page.getByRole('button', { name: 'Open composer dropdown' }).click();
  await page.getByRole('combobox', { name: 'モデルを検索' }).fill('Fixture Remote');
  await page.getByRole('option', { name: /Fixture Remote Model/ }).click();
  await expect(page.getByRole('status').filter({ hasText: 'このモデルは未登録です' })).toBeVisible();
  expect(await page.evaluate(() => (window as any).fixture.selections)).toEqual([]);
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('new preferred connection applies once and registry refresh retains deliberate selection', async ({ page }) => {
  const api = page.getByLabel('使用するAPI');
  await api.selectOption('alpha');
  await page.getByRole('button', { name: 'Prefer beta' }).click();
  await expect(api).toHaveValue('beta');
  await api.selectOption('alpha');
  await page.evaluate(() => window.dispatchEvent(new Event('tobkiri-provider-connections-changed')));
  await expect(api).toHaveValue('alpha');
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('requested catalogue draft prefills exact connection without binding to another connection', async ({ page }) => {
  const api = page.getByLabel('使用するAPI');
  await api.selectOption('alpha');
  await page.getByRole('button', { name: 'Request beta model' }).click();
  await expect(api).toHaveValue('beta');
  await expect(page.getByText('選択中:')).toContainText('fixture-model');
  await expect(page.getByRole('button', { name: 'このモデルを使う' })).toBeEnabled();
  await api.selectOption('alpha');
  await expect(page.getByText('選択中:')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'このモデルを使う' })).toBeDisabled();
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('new preferred connection supersedes retained older properties request', async ({ page }) => {
  await page.getByRole('button', { name: 'Request alpha model' }).click();
  await expect(page.getByLabel('使用するAPI')).toHaveValue('alpha');
  await page.getByRole('button', { name: 'Prefer beta' }).click();
  await expect(page.getByLabel('使用するAPI')).toHaveValue('beta');
  await expect(page.getByRole('button', { name: 'このモデルを使う' })).toBeDisabled();
});

test('captured host scope does not consume another profile properties request', async ({ page }) => {
  await page.goto('/e2e/model-unification-fixture/index.html?modal&scoped');
  await page.evaluate(() => (window as any).requestScopedFixtureProperties('scope-B'));
  await expect(page.locator('[data-settings-profile-panel]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).readFixtureProperties('scope-B')?.scopeId)).toBe('scope-B');
  await page.evaluate(() => (window as any).requestScopedFixtureProperties('scope-A'));
  await expect(page.locator('[data-settings-profile-panel]')).toBeVisible();
  expect(await page.evaluate(() => (window as any).readFixtureProperties())).toBeNull();
});

test('full Settings modal standard hides legacy fields and registered navigation clears active search', async ({ page }) => {
  await page.goto('/e2e/model-unification-fixture/index.html?modal');
  await expect(page.getByText('Advanced: route text', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Fixture Placeholder', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('使用するAPI')).toHaveCount(1);
  await page.getByRole('button', { name: '設定の表示モードを変更', exact: true }).click();
  if (!await page.locator('summary').filter({ hasText: '詳細設定' }).evaluate(element => (element.parentElement as HTMLDetailsElement).open)) await page.locator('summary').filter({ hasText: '詳細設定' }).click();
  await expect(page.getByText('Advanced: route text', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '詳細設定', exact: true }).click();
  if (!await page.locator('summary').filter({ hasText: '詳細設定' }).evaluate(element => (element.parentElement as HTMLDetailsElement).open)) await page.locator('summary').filter({ hasText: '詳細設定' }).click();
  await expect(page.locator('input[value="Fixture original placeholder"]')).toBeVisible();
  await page.getByRole('button', { name: '設定の表示モードを変更', exact: true }).click();
  await expect(page.locator('input[value="Fixture original placeholder"]')).toHaveCount(0);
  await page.getByRole('button', { name: '設定の表示モードを変更', exact: true }).click();
  await page.getByRole('button', { name: '詳細設定', exact: true }).click();
  if (!await page.locator('summary').filter({ hasText: '詳細設定' }).evaluate(element => (element.parentElement as HTMLDetailsElement).open)) await page.locator('summary').filter({ hasText: '詳細設定' }).click();
  await expect(page.locator('input[value="Fixture original placeholder"]')).toHaveValue('Fixture original placeholder');
  const search = page.getByPlaceholder(/設定.*検索|Search settings/);
  await search.fill('never matches fixture');
  await page.evaluate(() => (window as any).requestFixtureProperties());
  await expect(search).toHaveValue('');
  await expect(page.locator('[data-settings-profile-panel]')).toBeVisible();
  await expect(page.locator('[data-settings-profile-panel]')).toContainText('saved-route');
  const profileSearch = page.getByPlaceholder('名前、モデル、Providerを検索…');
  await profileSearch.fill('exclude every profile');
  await expect(page.getByRole('listbox', { name: 'プロファイル', exact: true }).getByRole('option')).toHaveCount(0);
  await page.evaluate(() => (window as any).requestFixtureProperties());
  await expect(profileSearch).toHaveValue('');
  await expect(page.getByRole('listbox', { name: 'プロファイル', exact: true }).getByRole('option', { name: /Saved Fixture Route/ })).toHaveAttribute('aria-selected', 'true');
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});

test('full Settings model popup autofocus and filter Tab remain inside captured modal', async ({ page }) => {
  await page.goto('/e2e/model-unification-fixture/index.html?modal');
  await page.locator('[data-model-search-picker]').first().getByRole('button').first().click();
  const popup = page.locator('[data-viewport-popover]');
  const input = popup.getByRole('combobox', { name: 'モデルを検索' });
  await expect(input).toBeFocused();
  await input.fill('@');
  await input.press('Tab');
  await expect(input).toBeFocused();
  await expect(input).not.toHaveValue('@');
  await input.press('Escape');
  await expect(popup).toHaveCount(0);
});

for (const section of ['models', 'connections']) test(`full Settings modal ${section} provider popup keeps keyboard focus and closes into modal`, async ({ page }) => {
  await page.goto('/e2e/model-unification-fixture/index.html?modal');
  if (section === 'connections') await page.getByRole('button', { name: '接続', exact: true }).click();
  const trigger = page.getByRole('button', { name: 'provider を選択', exact: true });
  await trigger.click();
  const popup = page.locator('[data-viewport-popover]');
  const search = popup.getByPlaceholder('provider を検索');
  await expect(search).toBeVisible();
  await search.press('Shift+Tab');
  await expect.poll(() => popup.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Tab');
  await expect.poll(() => popup.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(popup).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(await page.evaluate(() => (window as any).fixture.saves)).toEqual([]);
});
