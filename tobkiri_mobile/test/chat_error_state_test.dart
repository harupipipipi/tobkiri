import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/chat/chat_screen.dart';
import 'package:rumi_remote_app/src/chat/chat_store.dart';
import 'package:rumi_remote_app/src/chat/mobile_operation_failure.dart';
import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/data/pc/pc_catalog.dart';
import 'package:rumi_remote_app/src/data/pc/pc_catalog_client.dart';
import 'package:rumi_remote_app/src/data/pc/pc_chat_backend.dart';
import 'package:rumi_remote_app/src/domain/conversation_backend.dart';
import 'package:rumi_remote_app/src/domain/conversation_locator.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _MemorySecureStorage implements SecureKeyValueStorage {
  final Map<String, String> values = {};

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }
}

class _FailingSecureStorage extends _MemorySecureStorage {
  @override
  Future<String?> read(String key) async {
    throw StateError('secure storage token=must-not-render');
  }
}

class _MemoryChatStorage implements ChatKeyValueStorage {
  final Map<String, String> values = {};

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async => values[key] = value;
}

class _FakePcBackend extends PcConversationBackend {
  _FakePcBackend({
    required this.listLoader,
    required this.conversationLoader,
  }) : super(
          connection: const PcConnection(
            baseUrl: 'https://test-pc.local',
            token: 'test-token',
          ),
          deviceId: 'pc-1',
        );

  final Future<List<ConversationSummary>> Function() listLoader;
  final Future<ConversationSnapshot> Function(ConversationLocator locator)
      conversationLoader;

  @override
  Future<List<ConversationSummary>> listConversations() => listLoader();

  @override
  Future<ConversationSnapshot> getConversation(
    ConversationLocator locator,
  ) =>
      conversationLoader(locator);
}

class _FakePcCatalogClient extends PcCatalogClient {
  _FakePcCatalogClient(this.loader);

  final Future<PcCatalog> Function() loader;

  @override
  Future<PcCatalog> fetchCapabilities(
    PcConnection pc, {
    String? providerFilter,
    bool includeTemplates = true,
  }) =>
      loader();
}

const _pairedDevice = PairedDevice(
  deviceId: 'pc-1',
  deviceToken: 'test-token',
  label: 'Phone',
  scopes: ['chat.read', 'chat.write'],
  pcBaseUrl: 'https://test-pc.local',
  pcLabel: 'Test Mac',
  pairingId: 'pair-1',
);

PcCatalog _emptyCatalog() => PcCatalog.fromJson(const {});

Future<void> _pumpPairedChat(
  WidgetTester tester, {
  required _FakePcBackend backend,
  required Future<PcCatalog> Function() catalogLoader,
}) async {
  await tester.binding.setSurfaceSize(const Size(393, 852));
  final secureStorage = _MemorySecureStorage();
  final deviceStore = MobileDeviceStore(storage: secureStorage);
  await deviceStore.savePairedDevice(_pairedDevice);
  await tester.pumpWidget(
    MaterialApp(
      home: ChatScreen(
        store: ChatStore(storage: _MemoryChatStorage()),
        configStore: ApiConfigStore(storage: secureStorage),
        deviceStore: deviceStore,
        pcBackendFactory: (_, __) => backend,
        pcCatalogClientFactory: () => _FakePcCatalogClient(catalogLoader),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _selectPcSpace(WidgetTester tester) async {
  await tester.tap(find.byTooltip('チャット一覧'));
  await tester.pumpAndSettle();
  await tester.tap(find.text('Test Mac'));
  await tester.pumpAndSettle();
}

void main() {
  test('diagnostics classify auth without copying raw exception text', () {
    final failure = MobileOperationFailure.from(
      StateError(
        'PC API エラー (HTTP 401): token=secret-value; token expired',
      ),
      area: 'PC会話一覧',
    );

    expect(failure.kind, MobileFailureKind.authentication);
    expect(failure.safeDetails, contains('http_status: 401'));
    expect(failure.safeDetails, isNot(contains('secret-value')));
    expect(failure.message, isNot(contains('offline')));
  });

  testWidgets('local initialization failure is distinct from empty chat',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    final secureStorage = _FailingSecureStorage();

    await tester.pumpWidget(
      MaterialApp(
        home: ChatScreen(
          store: ChatStore(storage: _MemoryChatStorage()),
          configStore: ApiConfigStore(storage: secureStorage),
          deviceStore: MobileDeviceStore(storage: secureStorage),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Tobkiriの初期化を読み込めません'), findsOneWidget);
    expect(find.text('ようこそ'), findsNothing);
    expect(find.textContaining('must-not-render'), findsNothing);
    expect(find.text('再試行'), findsOneWidget);
    expect(find.text('設定を開く'), findsOneWidget);
  });

  testWidgets('expired pairing remains an authentication error in PC list',
      (tester) async {
    final backend = _FakePcBackend(
      listLoader: () async => throw StateError(
        'PC API エラー (HTTP 401): token expired',
      ),
      conversationLoader: (_) async => throw UnimplementedError(),
    );
    await _pumpPairedChat(
      tester,
      backend: backend,
      catalogLoader: () async => _emptyCatalog(),
    );
    await _selectPcSpace(tester);
    await tester.tap(find.byTooltip('チャット一覧'));
    await tester.pumpAndSettle();

    expect(find.text('PC会話一覧の認証が必要です'), findsOneWidget);
    expect(find.text('PC会話がありません'), findsNothing);
    expect(find.textContaining('オフライン'), findsNothing);
    expect(find.text('ペアリングを修復'), findsOneWidget);
  });

  testWidgets('catalog failure keeps a visible retryable cause',
      (tester) async {
    final backend = _FakePcBackend(
      listLoader: () async => const [],
      conversationLoader: (_) async => throw UnimplementedError(),
    );
    await _pumpPairedChat(
      tester,
      backend: backend,
      catalogLoader: () async => throw const FormatException(
        'incompatible response token=must-not-render',
      ),
    );
    await _selectPcSpace(tester);

    expect(
      find.text('PCモデルカタログの応答を読み取れません'),
      findsOneWidget,
    );
    expect(find.textContaining('must-not-render'), findsNothing);
    expect(find.text('再試行'), findsOneWidget);
  });

  testWidgets('active PC conversation failure is not rendered as empty data',
      (tester) async {
    final backend = _FakePcBackend(
      listLoader: () async => [
        ConversationSummary(
          id: 'remote-1',
          title: 'Remote conversation',
          authority: ConversationAuthorityKind.pc,
          messageCount: 2,
          updatedAt: DateTime(2026, 8, 27),
          pinned: false,
          revision: 2,
        ),
      ],
      conversationLoader: (_) async => throw StateError(
        'PC API エラー (HTTP 500): internal details',
      ),
    );
    await _pumpPairedChat(
      tester,
      backend: backend,
      catalogLoader: () async => _emptyCatalog(),
    );
    await _selectPcSpace(tester);
    await tester.tap(find.byTooltip('チャット一覧'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Remote conversation'));
    await tester.pumpAndSettle();

    expect(find.text('PC会話で問題が発生しました'), findsOneWidget);
    expect(find.text('ようこそ'), findsNothing);
    expect(find.textContaining('internal details'), findsNothing);
  });
}
