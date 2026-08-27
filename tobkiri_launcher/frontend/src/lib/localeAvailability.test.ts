import assert from 'node:assert/strict';
import test from 'node:test';

import {
  GENERAL_LOCALE_COVERAGE_THRESHOLD,
  UI_LOCALE_OPTIONS,
  applyLocaleToRoot,
  resolveUiLocale,
} from './localeAvailability';
import {
  localeIsGenerallyAvailable,
  resolveLocale,
  setMissingTranslationReporterForTests,
  translate,
  translationCoverage,
} from './i18n';
import {useAppStore} from '../store';

test('only audited locales are selectable and incomplete locales are marked preview', () => {
  assert.deepEqual(UI_LOCALE_OPTIONS.map(({id, availability}) => ({id, availability})), [
    {id: 'en', availability: 'general'},
    {id: 'ja', availability: 'preview'},
  ]);
  assert.equal(localeIsGenerallyAvailable('en'), true);
  assert.equal(localeIsGenerallyAvailable('ja'), false);
  assert.ok(translationCoverage('en').ratio >= GENERAL_LOCALE_COVERAGE_THRESHOLD);
  assert.match(
    translate('settings.language_preview_help', undefined, 'ja'),
    /Launcher の一部画面はまだ英語で表示されます/,
  );
});

test('retired partial locale values fail closed to the generally available locale', () => {
  for (const language of ['zh', 'ko', 'es', 'fr', 'de', 'pt', 'ru', 'ar', 'unknown']) {
    assert.equal(resolveUiLocale(language), 'en');
    assert.equal(resolveLocale(language), 'en');
  }
});

test('profile updates cannot persist an unadvertised locale', () => {
  const previousState = useAppStore.getState();
  try {
    useAppStore.setState({profile: {...previousState.profile, language: 'ja'}});
    useAppStore.getState().updateLocalProfile({language: 'ar'});
    assert.equal(useAppStore.getState().profile.language, 'en');
  } finally {
    useAppStore.setState(previousState, true);
  }
});

test('preview fallback reports missing keys instead of passing silently', () => {
  const reports: Array<{locale: string; key: string}> = [];
  const restore = setMissingTranslationReporterForTests((locale, key) => {
    reports.push({locale, key});
  });
  try {
    assert.equal(translate('test.missing.translation', undefined, 'ja'), 'test.missing.translation');
    assert.deepEqual(reports, [{locale: 'ja', key: 'test.missing.translation'}]);
  } finally {
    restore();
  }
});

test('locale root attributes update without requiring a restart', () => {
  const root = {lang: '', dir: ''} as HTMLElement;
  applyLocaleToRoot(root, 'ja');
  assert.equal(root.lang, 'ja');
  assert.equal(root.dir, 'ltr');
  applyLocaleToRoot(root, 'ar');
  assert.equal(root.lang, 'en');
  assert.equal(root.dir, 'ltr');
});
