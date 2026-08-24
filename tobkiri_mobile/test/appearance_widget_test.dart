import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/appearance_settings.dart';
import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/rumi_remote_app.dart';
import 'package:rumi_remote_app/src/rumi_remote_home.dart';

import 'appearance_test_support.dart';

Future<void> _pumpHome(
  WidgetTester tester, {
  required AppearanceMode mode,
  required Future<void> Function(AppearanceMode mode) onAppearanceChanged,
  Size surfaceSize = const Size(393, 852),
}) async {
  await tester.binding.setSurfaceSize(surfaceSize);
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final storage = SecureStorageHarness(values: {...fixtureSettings});
  storage.install();
  addTearDown(storage.restore);
  await tester.pumpWidget(
    MaterialApp(
      theme: buildRumiLightTheme(),
      home: RumiRemoteHome(
        appearanceMode: mode,
        onAppearanceChanged: onAppearanceChanged,
        settingsStore: storage.createSettingsStore(),
        clientFactory: (settings) =>
            createMockApiClient(settings, healthyHomeResponse),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(seconds: 1));
}

void main() {
  testWidgets('RumiRemoteApp resolves system, light, and dark modes',
      (tester) async {
    final storage = SecureStorageHarness();
    storage.install();
    addTearDown(storage.restore);
    final controller = AppearanceSettingsController(
      store: InMemoryAppearanceSettingsStore(),
      initialMode: AppearanceMode.system,
    );
    addTearDown(controller.dispose);

    await tester.pumpWidget(
      MediaQuery(
        data: const MediaQueryData(platformBrightness: Brightness.dark),
        child: RumiRemoteApp(appearanceController: controller),
      ),
    );
    await tester.pump();

    MaterialApp app() => tester.widget<MaterialApp>(find.byType(MaterialApp));

    expect(app().themeMode, ThemeMode.system);
    expect(
      Theme.of(tester.element(find.byType(RumiRemoteHome))).brightness,
      Brightness.dark,
    );
    expect(
      Theme.of(tester.element(find.byType(RumiRemoteHome)))
          .extension<RumiColors>(),
      same(RumiColors.dark),
    );

    await controller.select(AppearanceMode.light);
    await tester.pump(const Duration(milliseconds: 300));
    expect(app().themeMode, ThemeMode.light);
    expect(
      app().theme!.brightness,
      Brightness.light,
    );
    expect(app().theme!.extension<RumiColors>(), same(RumiColors.light));

    await controller.select(AppearanceMode.dark);
    await tester.pump(const Duration(milliseconds: 300));
    expect(app().themeMode, ThemeMode.dark);
    expect(
      app().darkTheme!.brightness,
      Brightness.dark,
    );
    expect(app().darkTheme!.extension<RumiColors>(), same(RumiColors.dark));

    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('settings selector persists System, Light, and Dark choices',
      (tester) async {
    final store = InMemoryAppearanceSettingsStore();
    final controller = AppearanceSettingsController(store: store);
    addTearDown(controller.dispose);
    await _pumpHome(
      tester,
      mode: controller.mode,
      onAppearanceChanged: controller.select,
    );

    await tester.tap(find.byTooltip('Settings'));
    await tester.pump(const Duration(milliseconds: 500));
    expect(find.byType(SegmentedButton<AppearanceMode>), findsOneWidget);

    tester
        .widget<SegmentedButton<AppearanceMode>>(
          find.byType(SegmentedButton<AppearanceMode>),
        )
        .onSelectionChanged!
        .call({AppearanceMode.light});
    await tester.pump(const Duration(milliseconds: 300));
    expect(controller.mode, AppearanceMode.light);
    expect(store.value, AppearanceMode.light);

    tester
        .widget<SegmentedButton<AppearanceMode>>(
          find.byType(SegmentedButton<AppearanceMode>),
        )
        .onSelectionChanged!
        .call({AppearanceMode.dark});
    await tester.pump(const Duration(milliseconds: 300));
    expect(controller.mode, AppearanceMode.dark);
    expect(store.value, AppearanceMode.dark);

    tester
        .widget<SegmentedButton<AppearanceMode>>(
          find.byType(SegmentedButton<AppearanceMode>),
        )
        .onSelectionChanged!
        .call({AppearanceMode.system});
    await tester.pump(const Duration(milliseconds: 300));
    expect(controller.mode, AppearanceMode.system);
    expect(store.value, AppearanceMode.system);
    expect(tester.takeException(), isNull);
  });

  testWidgets('settings sheet remains usable with a keyboard-sized viewport',
      (tester) async {
    final controller = AppearanceSettingsController(
      store: InMemoryAppearanceSettingsStore(),
    );
    addTearDown(controller.dispose);
    await _pumpHome(
      tester,
      mode: controller.mode,
      onAppearanceChanged: controller.select,
      surfaceSize: const Size(375, 667),
    );

    await tester.tap(find.byTooltip('Settings'));
    await tester.pump(const Duration(milliseconds: 500));
    tester.view.viewInsets = const FakeViewPadding(bottom: 340);
    addTearDown(tester.view.resetViewInsets);
    await tester.pump(const Duration(milliseconds: 500));

    expect(find.text('Appearance'), findsOneWidget);
    expect(find.text('Save'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
