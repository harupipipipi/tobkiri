import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';
import 'package:rumi_remote_app/src/settings/settings_screen.dart';

class _FaultingSecureStorage implements SecureKeyValueStorage {
  final values = <String, String>{};
  int writes = 0;
  String? throwBeforeWriteKey;
  String? blockWriteKey;
  Completer<void>? writeGate;

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String? value) async {
    writes += 1;
    if (throwBeforeWriteKey == key) {
      throw StateError('secure storage unavailable');
    }
    if (blockWriteKey == key && writeGate != null) {
      await writeGate!.future;
    }
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }
}

Future<void> _pumpSettings(
  WidgetTester tester,
  _FaultingSecureStorage storage,
) async {
  await tester.binding.setSurfaceSize(const Size(393, 852));
  addTearDown(() => tester.binding.setSurfaceSize(null));
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

Future<void> _enterPcDraft(
  WidgetTester tester, {
  required String url,
  String token = 'secret-token',
}) async {
  final urlField = find.byKey(const ValueKey('settings-pc-url'));
  await tester.ensureVisible(urlField);
  await tester.enterText(urlField, url);
  await tester.enterText(
    find.byKey(const ValueKey('settings-pc-token')),
    token,
  );
}

Future<void> _tapSave(WidgetTester tester) async {
  final save = find.byKey(const ValueKey('settings-save'));
  await tester.ensureVisible(save);
  await tester.tap(save);
  await tester.pump();
}

Future<void> _openAdvancedApiSettings(WidgetTester tester) async {
  final apiSettings = find.text('API / プロバイダー設定');
  for (var attempt = 0;
      apiSettings.evaluate().isEmpty && attempt < 12;
      attempt += 1) {
    await tester.drag(find.byType(ListView).last, const Offset(0, 700));
    await tester.pump();
  }
  expect(apiSettings, findsOneWidget);
  await tester.tap(
    find.ancestor(
      of: apiSettings,
      matching: find.byType(InkWell),
    ),
  );
  await tester.pumpAndSettle();
  expect(find.text('API設定'), findsOneWidget);
  final advanced = find.text('高度な設定');
  for (var attempt = 0;
      advanced.evaluate().isEmpty && attempt < 24;
      attempt += 1) {
    await tester.drag(find.byType(ListView).last, const Offset(0, -700));
    await tester.pump();
  }
  expect(advanced, findsOneWidget);
  await tester.tap(advanced);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('invalid PC URL performs no write and preserves every edit',
      (tester) async {
    final storage = _FaultingSecureStorage();
    await _pumpSettings(tester, storage);
    await _openAdvancedApiSettings(tester);
    final apiUrl = find.byKey(const ValueKey('settings-api-base-url'));
    await tester.ensureVisible(apiUrl);
    await tester.enterText(apiUrl, 'https://changed.example.test/v1');
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    await _enterPcDraft(tester, url: 'pc.example.test');
    final writesBeforeSave = storage.writes;

    await _tapSave(tester);
    await tester.pumpAndSettle();

    expect(storage.writes, writesBeforeSave);
    expect(find.textContaining('完全なHTTPまたはHTTPS'), findsOneWidget);
    expect(find.textContaining('設定は保存されていません'), findsOneWidget);
    final token = tester.widget<TextField>(
      find.byKey(const ValueKey('settings-pc-token')),
    );
    expect(token.controller?.text, 'secret-token');

    await _openAdvancedApiSettings(tester);
    final retainedApi = tester.widget<TextField>(
      find.byKey(const ValueKey('settings-api-base-url')),
    );
    expect(retainedApi.controller?.text, 'https://changed.example.test/v1');
  });

  testWidgets('storage failure keeps edits and a retry commits one revision',
      (tester) async {
    final storage = _FaultingSecureStorage();
    await _pumpSettings(tester, storage);
    await _enterPcDraft(tester, url: 'https://pc.example.test');
    storage.throwBeforeWriteKey = ApiConfigStore.settingsRecordKey;

    await _tapSave(tester);
    await tester.pumpAndSettle();

    expect(find.textContaining('以前の設定を維持'), findsOneWidget);
    expect(find.text('設定を保存しました'), findsNothing);
    final failedDraft = tester.widget<TextField>(
      find.byKey(const ValueKey('settings-pc-url')),
    );
    expect(failedDraft.controller?.text, 'https://pc.example.test');

    storage.throwBeforeWriteKey = null;
    await _tapSave(tester);
    await tester.pumpAndSettle();

    expect(find.text('設定を保存しました'), findsOneWidget);
    final saved = await ApiConfigStore(storage: storage).loadSettingsRevision();
    expect(saved.revision, 1);
    expect(saved.pc?.token, 'secret-token');
  });

  testWidgets('duplicate submit and navigation are blocked during commit',
      (tester) async {
    final storage = _FaultingSecureStorage();
    await _pumpSettings(tester, storage);
    await _enterPcDraft(tester, url: 'https://pc.example.test');
    storage
      ..blockWriteKey = ApiConfigStore.settingsRecordKey
      ..writeGate = Completer<void>();

    await _tapSave(tester);
    final writesWhileBlocked = storage.writes;
    await _tapSave(tester);
    await tester.binding.handlePopRoute();
    await tester.pump();

    expect(storage.writes, writesWhileBlocked);
    expect(find.text('保存中...'), findsOneWidget);
    expect(find.byType(SettingsScreen), findsOneWidget);

    storage.writeGate!.complete();
    await tester.pumpAndSettle();
  });

  testWidgets('dirty navigation offers save discard and cancel',
      (tester) async {
    final storage = _FaultingSecureStorage();
    await _pumpSettings(tester, storage);
    await _enterPcDraft(tester, url: 'https://draft.example.test');

    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();

    expect(find.text('未保存の設定'), findsOneWidget);
    expect(find.text('編集に戻る'), findsOneWidget);
    expect(find.text('破棄'), findsOneWidget);
    expect(
      find.descendant(
        of: find.byType(AlertDialog),
        matching: find.widgetWithText(FilledButton, '保存'),
      ),
      findsOneWidget,
    );

    await tester.tap(find.text('編集に戻る'));
    await tester.pumpAndSettle();
    final draft = tester.widget<TextField>(
      find.byKey(const ValueKey('settings-pc-url')),
    );
    expect(draft.controller?.text, 'https://draft.example.test');
  });
}
