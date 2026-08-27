import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/chat/chat_drawer.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/chat_screen.dart';
import 'package:rumi_remote_app/src/chat/chat_store.dart';
import 'package:rumi_remote_app/src/chat/composer_bar.dart';
import 'package:rumi_remote_app/src/chat/message_view.dart';
import 'package:rumi_remote_app/src/data/local/local_chat_backend.dart';
import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/domain/chat_event.dart';
import 'package:rumi_remote_app/src/domain/connection_state.dart';
import 'package:rumi_remote_app/src/domain/conversation_locator.dart';
import 'package:rumi_remote_app/src/domain/space.dart';
import 'package:rumi_remote_app/src/features/chat/connection_chip.dart';
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
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

class _FakeChatStorage implements ChatKeyValueStorage {
  final Map<String, String> _values = {};
  bool failReads = false;
  bool failWrites = false;
  int writes = 0;

  @override
  Future<String?> read(String key) async {
    if (failReads) throw StateError('private read failure');
    return _values[key];
  }

  @override
  Future<void> write(String key, String value) async {
    writes += 1;
    if (failWrites) throw StateError('private write failure');
    _values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

class _FakeActivityBackend extends LocalConversationBackend {
  _FakeActivityBackend({required super.store, required super.configStore})
    : _store = store;

  final ChatStore _store;
  final started = Completer<void>();
  final releaseTool = Completer<void>();
  final releaseFinal = Completer<void>();

  @override
  Stream<ChatEvent> sendMessage({
    required ConversationLocator locator,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
    String? model,
    String? profileId,
    Map<String, dynamic>? params,
  }) async* {
    const runId = 'run-activity-test';
    const assistantId = 'assistant-activity-test';
    await _store.addMessage(
      locator.conversationId,
      ChatMessage(
        id: clientMessageId,
        role: ChatRole.user,
        content: text,
        createdAt: DateTime.now(),
      ),
    );
    await _store.addMessage(
      locator.conversationId,
      ChatMessage(
        id: assistantId,
        role: ChatRole.assistant,
        content: '',
        createdAt: DateTime.now(),
        pending: true,
      ),
    );

    if (!started.isCompleted) started.complete();
    yield ChatRunStarted(
      locator: locator,
      runId: runId,
      assistantMessageId: assistantId,
    );
    yield ChatStatusEvent(
      locator: locator,
      runId: runId,
      message: '考えています',
      phase: 'thinking',
    );

    await releaseTool.future.timeout(const Duration(seconds: 2));
    yield ToolCallEvent(
      locator: locator,
      runId: runId,
      toolId: 'tool-todo',
      toolName: 'todo',
      status: 'completed',
      arguments: {'action': 'add', 'title': 'Write UI activity test'},
      summary: 'Write UI activity test',
    );

    await releaseFinal.future.timeout(const Duration(seconds: 2));
    await _store.updateMessage(
      locator.conversationId,
      assistantId,
      'できました',
      pending: false,
    );
    yield ChatMessageCommitted(
      locator: locator,
      runId: runId,
      messageId: assistantId,
      content: 'できました',
      error: false,
    );
    yield ChatRunCompleted(locator: locator, runId: runId);
  }
}

void main() {
  Widget wrap(Widget child) =>
      MaterialApp(theme: buildRumiTheme(dark: true), home: child);

  testWidgets('chat screen renders simple empty state with composer', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    final store = ChatStore(storage: _FakeChatStorage());
    final fakeStorage = _FakeSecureStorage();
    final configStore = ApiConfigStore(storage: fakeStorage);
    final deviceStore = MobileDeviceStore(storage: fakeStorage);
    await tester.pumpWidget(
      wrap(
        ChatScreen(
          store: store,
          configStore: configStore,
          deviceStore: deviceStore,
        ),
      ),
    );
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 30)),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 30));

    expect(find.text('ようこそ'), findsOneWidget);
    expect(find.text('Rumiへようこそ'), findsNothing);
    expect(find.byType(ActionChip), findsNothing);
    expect(find.byType(ComposerBar), findsOneWidget);
    expect(find.byTooltip('新規チャット'), findsOneWidget);
    expect(find.byIcon(Icons.settings_outlined), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('failed local save rolls back and presents a live error banner', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(320, 700));
    final storage = _FakeChatStorage();
    final store = ChatStore(storage: storage);
    final secureStorage = _FakeSecureStorage();
    final configStore = ApiConfigStore(storage: secureStorage);
    final deviceStore = MobileDeviceStore(storage: secureStorage);
    await tester.pumpWidget(
      wrap(
        MediaQuery(
          data: const MediaQueryData(textScaler: TextScaler.linear(1.6)),
          child: ChatScreen(
            store: store,
            configStore: configStore,
            deviceStore: deviceStore,
          ),
        ),
      ),
    );
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 30)),
    );
    await tester.pumpAndSettle();
    final durableConversationId = store.active!.id;
    storage.failWrites = true;

    await tester.tap(find.byTooltip('新規チャット'));
    await tester.pumpAndSettle();

    expect(find.textContaining('変更を元に戻しました'), findsOneWidget);
    expect(find.text('再読み込み'), findsOneWidget);
    expect(store.conversations, hasLength(1));
    expect(store.active?.id, durableConversationId);
    expect(find.bySemanticsLabel(RegExp('チャット保存エラー')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('chat drawer shows new chat button and sections', (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    final store = ChatStore(storage: _FakeChatStorage());
    await store.load();
    final convo = await store.createAndPersist();
    await store.addMessage(
      convo.id,
      ChatMessage(
        id: 'm1',
        role: ChatRole.user,
        content: 'こんにちは',
        createdAt: DateTime.now(),
      ),
    );

    await tester.pumpWidget(
      wrap(
        Scaffold(
          body: ChatDrawer(
            spaces: const [Space.local],
            activeSpaceId: Space.local.id,
            conversations: store.conversations,
            activeId: store.active?.id,
            onNewChat: () {},
            onSelectSpace: (_) {},
            onSelect: (_) {},
            onDelete: (_) {},
            onRename: (_) {},
            onPin: (_) {},
            onReconnectSpace: () {},
            onContinueOffline: () {},
            onOpenSettings: () {},
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Rumi'), findsOneWidget);
    expect(find.text('新規チャット'), findsWidgets);
    expect(find.text('チャット'), findsOneWidget);
    expect(find.textContaining('こんにちは'), findsNWidgets(2));
    expect(tester.takeException(), isNull);
  });

  testWidgets('connection chip hides raw pc urls', (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    const pairedDevice = PairedDevice(
      deviceId: 'mobile-1',
      deviceToken: 'dtk-test',
      label: 'iPhone',
      scopes: ['chat.read', 'chat.write'],
      pcBaseUrl: 'http://192.168.11.25:8765',
      pcLabel: 'http://192.168.11.25:8765',
      pairingId: 'pair-1',
    );

    await tester.pumpWidget(
      wrap(
        const Scaffold(
          body: Center(
            child: ConnectionChip(
              connectionView: DeviceConnectionView(
                pairingState: PairingState.paired,
                pcConnectionState: PcConnectionState.online,
              ),
              pairedDevice: pairedDevice,
            ),
          ),
        ),
      ),
    );

    expect(find.text('PC'), findsOneWidget);
    expect(find.textContaining('http://'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('user and assistant messages render without overflow', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    final longText = List<String>.generate(60, (i) => 'メッセージ$i').join(' ');
    await tester.pumpWidget(
      wrap(
        Scaffold(
          body: ListView(
            children: [
              MessageView(
                message: ChatMessage(
                  id: 'u',
                  role: ChatRole.user,
                  content: longText,
                  createdAt: DateTime.now(),
                ),
              ),
              MessageView(
                message: ChatMessage(
                  id: 'a',
                  role: ChatRole.assistant,
                  content: '# 見出し\n\n本文です。\n\n```dart\nvoid main() {}\n```',
                  createdAt: DateTime.now(),
                ),
              ),
            ],
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.textContaining('メッセージ0'), findsOneWidget);
    expect(find.text('見出し'), findsOneWidget);
    expect(find.text('void main() {}'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('pending assistant message shows typing indicator', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    await tester.pumpWidget(
      wrap(
        Scaffold(
          body: MessageView(
            message: ChatMessage(
              id: 'p',
              role: ChatRole.assistant,
              content: '',
              createdAt: DateTime.now(),
              pending: true,
            ),
          ),
        ),
      ),
    );
    await tester.pump();
    expect(find.text('処理中...'), findsOneWidget);
    expect(find.byIcon(Icons.auto_awesome), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'chat screen renders thinking and tool activity while streaming',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(393, 852));
      final store = ChatStore(storage: _FakeChatStorage());
      final fakeStorage = _FakeSecureStorage();
      final configStore = ApiConfigStore(storage: fakeStorage);
      await configStore.saveApi(
        const ApiConfig(
          baseUrl: 'http://127.0.0.1:8765/v1',
          apiKey: 'sk-test',
          model: 'gpt-test',
        ),
      );
      final backend = _FakeActivityBackend(
        store: store,
        configStore: configStore,
      );
      final deviceStore = MobileDeviceStore(storage: fakeStorage);
      await tester.pumpWidget(
        wrap(
          ChatScreen(
            store: store,
            configStore: configStore,
            deviceStore: deviceStore,
            localBackend: backend,
          ),
        ),
      );
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 30)),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'todoを作って');
      await tester.pump();
      final sendBtn = find.ancestor(
        of: find.byIcon(Icons.arrow_upward_rounded),
        matching: find.bySubtype<IconButton>(),
      );
      await tester.tap(sendBtn);
      await tester.runAsync(
        () => backend.started.future.timeout(const Duration(seconds: 2)),
      );
      await tester.pump();

      expect(find.text('考えています'), findsOneWidget);

      backend.releaseTool.complete();
      await tester.pump();
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 30)),
      );
      await tester.pump();

      expect(find.text('todo'), findsOneWidget);
      expect(find.textContaining('Write UI activity test'), findsOneWidget);

      backend.releaseFinal.complete();
      await tester.pumpAndSettle();

      expect(find.textContaining('できました'), findsOneWidget);
      expect(find.text('todo'), findsNothing);
      expect(find.textContaining('Write UI activity test'), findsNothing);
      expect(find.text('考えています'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('user and assistant messages can be copied', (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    String? clipboardText;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
          switch (call.method) {
            case 'Clipboard.setData':
              final data = call.arguments as Map;
              clipboardText = data['text'] as String?;
              return null;
            case 'Clipboard.getData':
              return {'text': clipboardText};
          }
          return null;
        });
    addTearDown(() {
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(SystemChannels.platform, null);
    });
    await tester.pumpWidget(
      wrap(
        Scaffold(
          body: ListView(
            children: [
              MessageView(
                message: ChatMessage(
                  id: 'u-copy',
                  role: ChatRole.user,
                  content: 'ユーザーの本文',
                  createdAt: DateTime.now(),
                ),
              ),
              MessageView(
                message: ChatMessage(
                  id: 'a-copy',
                  role: ChatRole.assistant,
                  content: 'AIの本文',
                  createdAt: DateTime.now(),
                ),
              ),
            ],
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final copyButtons = find.byTooltip('コピー');
    expect(copyButtons, findsNWidgets(2));

    await tester.tap(copyButtons.first);
    await tester.pump();
    var copied = await Clipboard.getData(Clipboard.kTextPlain);
    expect(copied?.text, 'ユーザーの本文');

    await tester.tap(copyButtons.last);
    await tester.pump();
    copied = await Clipboard.getData(Clipboard.kTextPlain);
    expect(copied?.text, 'AIの本文');
    expect(tester.takeException(), isNull);
  });

  testWidgets('composer bar enables send only with text', (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    var sent = '';
    await tester.pumpWidget(
      wrap(
        Scaffold(
          body: ComposerBar(
            onSend: (t) => sent = t,
            onStop: () {},
            busy: false,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final sendIcon = find.byIcon(Icons.arrow_upward_rounded);
    final sendBtn = find.ancestor(
      of: sendIcon,
      matching: find.bySubtype<IconButton>(),
    );
    final textField = find.byType(TextField);

    expect(tester.widget<IconButton>(sendBtn).onPressed, isNull);

    await tester.enterText(textField, 'テスト入力');
    await tester.pump();
    expect(tester.widget<IconButton>(sendBtn).onPressed, isNotNull);

    await tester.tap(sendBtn);
    await tester.pump();
    expect(sent, 'テスト入力');
    expect(tester.takeException(), isNull);
  });

  testWidgets('composer shows stop button when busy', (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    var stopped = false;
    await tester.pumpWidget(
      wrap(
        Scaffold(
          body: ComposerBar(
            onSend: (_) {},
            onStop: () => stopped = true,
            busy: true,
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();

    final stop = find.byIcon(Icons.stop_rounded);
    expect(stop, findsOneWidget);
    await tester.tap(stop);
    await tester.pump();
    expect(stopped, isTrue);
    expect(tester.takeException(), isNull);
  });
}
