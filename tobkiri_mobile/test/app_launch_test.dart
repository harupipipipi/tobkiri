import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/app_theme.dart' as theme;
import 'package:rumi_remote_app/src/chat/chat_screen.dart';
import 'package:rumi_remote_app/src/chat/chat_store.dart';
import 'package:rumi_remote_app/src/chat/composer_bar.dart';
import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakeSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      _values.remove(key);
    } else {
      _values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async => _values.remove(key);
}

class _FakeChatStorage implements ChatKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String value) async {
    _values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

ChatStore _testStore() => ChatStore(storage: _FakeChatStorage());

void main() {
  testWidgets('app launches and shows chat empty state', (tester) async {
    final storage = _FakeSecureStorage();
    final store = _testStore();
    final configStore = ApiConfigStore(storage: storage);
    final deviceStore = MobileDeviceStore(storage: storage);

    await tester.pumpWidget(MaterialApp(
      theme: theme.buildRumiTheme(),
      home: ChatScreen(
        store: store,
        configStore: configStore,
        deviceStore: deviceStore,
      ),
    ));
    await tester.pumpAndSettle(const Duration(seconds: 3));

    expect(find.text('ようこそ'), findsOneWidget);
    expect(find.text('Rumiへようこそ'), findsNothing);
    expect(find.byType(ComposerBar), findsOneWidget);
  });

  testWidgets('app still launches with stale or malformed persisted state',
      (tester) async {
    final storage = _FakeSecureStorage()
      .._values['rumi.api_config.v1'] = '{"temperature":"bad"}'
      .._values['rumi.mobile_provider_configs.v1'] =
          '[{"providerId":123,"openaiCompatible":"yes"}]'
      .._values['rumi.mobile_model_favorites.v1'] =
          '[{"source":"pc","profileId":123}]'
      .._values['rumi.paired_device.v1'] = '{"scopes":"chat.read"}'
      .._values['rumi.paired_devices.v1'] =
          '[null, {"deviceId":"mobile-old","deviceToken":"dtk-old","scopes":["chat.read"],"pcBaseUrl":"http://192.168.11.25:8765","pcLabel":"Old Mac","pairingId":"old-pair"}]';
    final chatStorage = _FakeChatStorage()
      .._values['rumi_chat.conversations.v1'] = jsonEncode([
        {'id': 1, 'messages': 'bad'},
        {
          'id': 'valid',
          'title': 'Valid',
          'messages': const [],
          'createdAt': DateTime.now().toIso8601String(),
          'updatedAt': DateTime.now().toIso8601String(),
        },
      ])
      .._values['rumi_chat.active_id.v1'] = 'missing-active';
    final store = ChatStore(storage: chatStorage);
    final configStore = ApiConfigStore(storage: storage);
    final deviceStore = MobileDeviceStore(storage: storage);

    await tester.pumpWidget(MaterialApp(
      theme: theme.buildRumiTheme(),
      home: ChatScreen(
        store: store,
        configStore: configStore,
        deviceStore: deviceStore,
      ),
    ));
    await tester.pumpAndSettle(const Duration(seconds: 3));

    expect(find.text('ようこそ'), findsOneWidget);
    expect(find.text('このスマホ'), findsWidgets);
    expect(find.byType(ComposerBar), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('settings screen opens and shows sections', (tester) async {
    final storage = _FakeSecureStorage();
    final store = _testStore();
    final configStore = ApiConfigStore(storage: storage);
    final deviceStore = MobileDeviceStore(storage: storage);

    await tester.pumpWidget(MaterialApp(
      theme: theme.buildRumiTheme(),
      home: ChatScreen(
        store: store,
        configStore: configStore,
        deviceStore: deviceStore,
      ),
    ));
    await tester.pumpAndSettle(const Duration(seconds: 3));

    await tester.tap(find.byTooltip('チャット一覧'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('設定'));
    await tester.pumpAndSettle();

    expect(find.text('このスマホのAI API'), findsOneWidget);
    expect(find.text('API / プロバイダー設定'), findsOneWidget);
    expect(find.text('モデル設定'), findsOneWidget);
    expect(find.text('Anthropic'), findsNothing);
    await tester.tap(find.text('モデル設定'));
    await tester.pumpAndSettle();
    expect(find.text('Star付きモデル'), findsOneWidget);
    expect(find.text('PCからモデルを取り込む'), findsOneWidget);
    await tester.pageBack();
    await tester.pumpAndSettle();
    await tester.tap(find.text('API / プロバイダー設定'));
    await tester.pumpAndSettle();
    expect(find.text('API設定'), findsOneWidget);
    expect(find.text('Anthropic'), findsWidgets);
    expect(find.text('Cerebras'), findsWidgets);
    expect(
      tester.getTopLeft(find.text('Anthropic').first).dy,
      lessThan(tester.getTopLeft(find.text('Cerebras').first).dy),
    );
    await tester.enterText(find.byType(TextField).first, 'cerebras');
    await tester.pumpAndSettle();
    expect(find.text('Cerebras'), findsWidgets);
    expect(find.text('Anthropic'), findsNothing);
    final focusedNode = FocusManager.instance.primaryFocus;
    expect(focusedNode, isNotNull);
    await tester.tap(find.text('API設定'));
    await tester.pumpAndSettle();
    expect(FocusManager.instance.primaryFocus, isNot(focusedNode));
    await tester.pageBack();
    await tester.pumpAndSettle();
    await tester.dragUntilVisible(
      find.text('PC接続'),
      find.byType(ListView).last,
      const Offset(0, -500),
      maxIteration: 20,
    );
    expect(find.text('PC接続'), findsWidgets);
  });

  testWidgets('drawer shows space selector with local space', (tester) async {
    final storage = _FakeSecureStorage();
    final store = _testStore();
    final configStore = ApiConfigStore(storage: storage);
    final deviceStore = MobileDeviceStore(storage: storage);

    await tester.pumpWidget(MaterialApp(
      theme: theme.buildRumiTheme(),
      home: ChatScreen(
        store: store,
        configStore: configStore,
        deviceStore: deviceStore,
      ),
    ));
    await tester.pumpAndSettle(const Duration(seconds: 3));

    await tester.tap(find.byTooltip('チャット一覧'));
    await tester.pumpAndSettle();

    expect(find.text('このスマホ'), findsWidgets);
    expect(find.text('新規チャット'), findsWidgets);
  });
}
