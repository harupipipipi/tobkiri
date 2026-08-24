import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/appearance_settings.dart';
import 'package:rumi_remote_app/src/app_theme.dart';

class _RecordingAppearanceStore implements AppearanceSettingsStore {
  _RecordingAppearanceStore({
    this.value = AppearanceMode.system,
    this.failLoad = false,
    this.failSave = false,
  });

  AppearanceMode value;
  bool failLoad;
  bool failSave;
  final saved = <AppearanceMode>[];

  @override
  Future<AppearanceMode> load() async {
    if (failLoad) {
      throw StateError('load failed');
    }
    return value;
  }

  @override
  Future<void> save(AppearanceMode mode) async {
    if (failSave) {
      throw StateError('save failed');
    }
    value = mode;
    saved.add(mode);
  }
}

void main() {
  group('AppearanceMode', () {
    test('round-trips known values and defaults unknown values to system', () {
      expect(AppearanceMode.fromStorage('system'), AppearanceMode.system);
      expect(AppearanceMode.fromStorage('light'), AppearanceMode.light);
      expect(AppearanceMode.fromStorage('dark'), AppearanceMode.dark);
      expect(AppearanceMode.fromStorage(null), AppearanceMode.system);
      expect(AppearanceMode.fromStorage('sepia'), AppearanceMode.system);
    });

    test('exposes Material modes and stable user-facing labels', () {
      expect(AppearanceMode.system.themeMode, ThemeMode.system);
      expect(AppearanceMode.light.themeMode, ThemeMode.light);
      expect(AppearanceMode.dark.themeMode, ThemeMode.dark);
      expect(
        AppearanceMode.values.map((mode) => mode.label),
        ['System', 'Light', 'Dark'],
      );
    });
  });

  group('AppearanceSettingsController', () {
    test('preloads the persisted mode and saves subsequent selections',
        () async {
      final store = _RecordingAppearanceStore(value: AppearanceMode.dark);
      final controller = await AppearanceSettingsController.load(store: store);
      addTearDown(controller.dispose);

      expect(controller.mode, AppearanceMode.dark);
      await controller.select(AppearanceMode.light);

      expect(controller.mode, AppearanceMode.light);
      expect(store.value, AppearanceMode.light);
      expect(store.saved, [AppearanceMode.light]);
    });

    test('falls back to system when loading a preference fails', () async {
      final controller = await AppearanceSettingsController.load(
        store: _RecordingAppearanceStore(
          value: AppearanceMode.dark,
          failLoad: true,
        ),
      );
      addTearDown(controller.dispose);

      expect(controller.mode, AppearanceMode.system);
    });

    test('rolls back the visible mode when persistence fails', () async {
      final store = _RecordingAppearanceStore(
        value: AppearanceMode.light,
        failSave: true,
      );
      final controller = AppearanceSettingsController(
        store: store,
        initialMode: AppearanceMode.light,
      );
      addTearDown(controller.dispose);
      var notifications = 0;
      controller.addListener(() => notifications++);

      await expectLater(
        controller.select(AppearanceMode.dark),
        throwsA(isA<StateError>()),
      );

      expect(controller.mode, AppearanceMode.light);
      expect(notifications, 2);
    });
  });

  group('Rumi theme extensions', () {
    test('light and dark themes attach matching semantic color extensions', () {
      final light = buildRumiLightTheme();
      final dark = buildRumiDarkTheme();

      expect(light.brightness, Brightness.light);
      expect(light.extension<RumiColors>(), same(RumiColors.light));
      expect(light.extension<RumiColors>()!.bubbleUserText,
          isNot(RumiColors.dark.bubbleUserText));
      expect(dark.brightness, Brightness.dark);
      expect(dark.extension<RumiColors>(), same(RumiColors.dark));
      expect(dark.extension<RumiColors>()!.codeBackground,
          RumiColors.dark.codeBackground);
    });

    test('high-contrast themes pair brightness and semantic colors', () {
      final light = buildRumiHighContrastLightTheme();
      final dark = buildRumiHighContrastDarkTheme();

      expect(light.brightness, Brightness.light);
      expect(light.extension<RumiColors>(), same(RumiColors.highContrastLight));
      expect(dark.brightness, Brightness.dark);
      expect(dark.extension<RumiColors>(), same(RumiColors.highContrastDark));
      expect(light.dividerTheme.thickness, 2);
      expect(dark.dividerTheme.thickness, 2);
    });
  });
}
