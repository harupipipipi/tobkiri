import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';
import 'package:rumi_remote_app/src/settings/settings_screen.dart';

class _ControllableSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> values = {};
  int mutationCount = 0;
  bool failNextMutation = false;

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String? value) async {
    _beginMutation();
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    _beginMutation();
    values.remove(key);
  }

  void _beginMutation() {
    mutationCount += 1;
    if (failNextMutation) {
      failNextMutation = false;
      throw StateError('injected persistence failure');
    }
  }
}

Future<void> _pumpSettings(
  WidgetTester tester,
  _ControllableSecureStorage storage,
) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = const Size(800, 1200);
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      home: SettingsScreen(
        configStore: ApiConfigStore(storage: storage),
        deviceStore: MobileDeviceStore(storage: storage),
        onApiChanged: (_) {},
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _scrollTo(WidgetTester tester, Finder finder) async {
  for (var attempt = 0;
      attempt < 30 && finder.evaluate().isEmpty;
      attempt += 1) {
    await tester.drag(
      find.byType(Scrollable).last,
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
  }
  expect(finder, findsWidgets);
  await tester.scrollUntilVisible(
    finder.first,
    400,
    scrollable: find.byType(Scrollable).last,
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('validation failure performs no settings persistence mutation',
      (tester) async {
    final storage = _ControllableSecureStorage();
    await _pumpSettings(tester, storage);

    final pcUrl = find.widgetWithText(TextField, 'Kernel API URL');
    await _scrollTo(tester, pcUrl);
    await tester.enterText(pcUrl, 'ftp://unsafe.example');
    await tester.enterText(
      find.widgetWithText(TextField, 'Bearer token'),
      'token-value',
    );
    final mutationCount = storage.mutationCount;
    await _scrollTo(tester, find.text('保存').last);
    await tester.tap(find.text('保存').last);
    await tester.pumpAndSettle();

    expect(
      find.text(
        'release版ではPC接続にHTTPS URLが必要です。保存は行われていません。',
      ),
      findsOneWidget,
    );
    expect(storage.mutationCount, mutationCount);
    expect(find.text('保存'), findsWidgets);
  });

  testWidgets('notification failure reverts and inline retry persists',
      (tester) async {
    final storage = _ControllableSecureStorage();
    await _pumpSettings(tester, storage);

    final notification = find.widgetWithText(
      SwitchListTile,
      'PCタスク完了通知',
    );
    storage.failNextMutation = true;
    await tester.tap(notification);
    await tester.pumpAndSettle();

    expect(tester.widget<SwitchListTile>(notification).value, isTrue);
    expect(
      find.text('通知とtool委譲の設定を保存できなかったため、表示を元に戻しました。'),
      findsOneWidget,
    );
    await tester.tap(find.text('再試行'));
    await tester.pumpAndSettle();

    expect(tester.widget<SwitchListTile>(notification).value, isFalse);
    expect(find.text('PCタスク完了通知をOFFで保存しました'), findsOneWidget);
  });

  testWidgets('provider failure is reconciled and exposes retry',
      (tester) async {
    final storage = _ControllableSecureStorage();
    await _pumpSettings(tester, storage);

    await tester.tap(find.text('API / プロバイダー設定'));
    await tester.pumpAndSettle();
    expect(find.text('API設定'), findsOneWidget);
    await _scrollTo(tester, find.text('API Key').first);
    await tester.tap(find.text('API Key').first);
    await tester.pumpAndSettle();
    await tester.enterText(
      find.widgetWithText(TextField, 'API Key').last,
      'test-provider-key',
    );
    storage.failNextMutation = true;
    await tester.tap(find.text('保存').last);
    await tester.pumpAndSettle();
    await tester.fling(
      find.byType(Scrollable).last,
      const Offset(0, 2000),
      2000,
    );
    await tester.pumpAndSettle();

    expect(
      find.text('provider設定を保存できませんでした。以前の表示へ戻しました。'),
      findsOneWidget,
    );
    expect(
        await ApiConfigStore(storage: storage).loadProviderConfigs(), isEmpty);
    await tester.tap(find.text('再試行'));
    await tester.pumpAndSettle();
    expect(find.textContaining('provider設定を保存し'), findsOneWidget);
    expect(
      await ApiConfigStore(storage: storage).loadProviderConfigs(),
      isNotEmpty,
    );
  });

  testWidgets('dirty API form requires save discard or cancel on exit',
      (tester) async {
    final storage = _ControllableSecureStorage();
    await _pumpSettings(tester, storage);

    await tester.tap(find.text('API / プロバイダー設定'));
    await tester.pumpAndSettle();
    expect(find.text('API設定'), findsOneWidget);
    await _scrollTo(tester, find.text('高度な設定'));
    await tester.tap(find.text('高度な設定'));
    await tester.pumpAndSettle();
    final baseUrl = find.widgetWithText(TextField, 'API Base URL');
    await _scrollTo(tester, baseUrl);
    await tester.enterText(baseUrl, 'https://changed.example/v1');
    await tester.pumpAndSettle();
    expect(find.textContaining('未保存の変更があります。戻る前に'), findsOneWidget);

    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.text('未保存の変更があります'), findsOneWidget);
    expect(find.text('保存'), findsWidgets);
    expect(find.text('破棄'), findsOneWidget);
    expect(find.text('キャンセル'), findsOneWidget);
    await tester.tap(find.widgetWithText(TextButton, 'キャンセル'));
    await tester.pumpAndSettle();
    expect(find.text('API設定'), findsOneWidget);

    await tester.pageBack();
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, '破棄'));
    await tester.pumpAndSettle();
    expect(find.text('設定'), findsOneWidget);
  });
}
