import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/chat/canonical_conversation_client.dart';
import 'package:rumi_remote_app/src/chat/chat_draft_store.dart';
import 'package:rumi_remote_app/src/chat/chat_screen.dart';
import 'package:rumi_remote_app/src/chat/composer_bar.dart';

Widget _app(Widget child) => MaterialApp(
      localizationsDelegates: GlobalMaterialLocalizations.delegates,
      supportedLocales: const [Locale('en'), Locale('ja')],
      home: child,
    );

Finder _composerField() => find.byWidgetPredicate(
      (widget) => widget is TextField && widget.decoration?.labelText == null,
    );

String _composerText(WidgetTester tester) =>
    tester.widget<TextField>(_composerField()).controller!.text;

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('composer clears exactly once only after explicit acceptance', (
    tester,
  ) async {
    final results = <ComposerSendResult>[
      const ComposerSendResult.rejected('設定を確認してください。'),
      const ComposerSendResult.accepted(),
    ];
    final changes = <String>[];
    await tester.pumpWidget(
      _app(
        Scaffold(
          body: ComposerBar(
            busy: false,
            onChanged: changes.add,
            onSend: (_) async => results.removeAt(0),
            onStop: () {},
          ),
        ),
      ),
    );

    await tester.enterText(_composerField(), '消さないで');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();
    expect(_composerText(tester), '消さないで');
    expect(find.byKey(const ValueKey('composer-retry-state')), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('composer-retry')));
    await tester.pumpAndSettle();
    expect(_composerText(tester), isEmpty);
    expect(changes.where((value) => value.isEmpty), hasLength(1));
  });

  testWidgets('local transport acceptance clears the committed draft', (
    tester,
  ) async {
    final drafts = _MemoryDraftStore();
    final transport = _ScriptedTransport([
      _SendAttempt(const [
        CanonicalChatUpdate(CanonicalChatUpdateKind.accepted),
        CanonicalChatUpdate(CanonicalChatUpdateKind.done),
      ]),
    ]);
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: transport,
          draftStore: drafts,
          draftScope: 'local',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), 'ローカル成功');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();

    expect(_composerText(tester), isEmpty);
    expect(find.text('ローカル成功'), findsOneWidget);
    expect(transport.clientMessageIds, hasLength(1));
    expect(drafts.values['local'], isNull);
  });

  testWidgets('canonical PC acceptance clears only after remote commit', (
    tester,
  ) async {
    final transport = _ScriptedTransport([
      _SendAttempt(const [
        CanonicalChatUpdate(CanonicalChatUpdateKind.accepted),
        CanonicalChatUpdate(CanonicalChatUpdateKind.delta, content: 'PC成功'),
        CanonicalChatUpdate(CanonicalChatUpdateKind.done),
      ]),
    ]);
    await tester.pumpWidget(
      _app(
        ChatScreen(
          connectionStore: _ConnectionStore(_connection),
          clientFactory: (_) => transport,
          draftStore: _MemoryDraftStore(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), 'PCへ送信');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();

    expect(_composerText(tester), isEmpty);
    expect(find.text('PCへ送信'), findsOneWidget);
    expect(find.text('PC成功'), findsOneWidget);
  });

  testWidgets('missing pairing opens setup and leaves the draft intact', (
    tester,
  ) async {
    await tester.pumpWidget(
      _app(
        ChatScreen(
          connectionStore: _ConnectionStore(null),
          draftStore: _MemoryDraftStore(),
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), '設定前の下書き');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();

    expect(find.text('承認済みチャット接続'), findsOneWidget);
    expect(_composerText(tester), '設定前の下書き');
  });

  testWidgets('unpaired draft is restored after secure storage loads', (
    tester,
  ) async {
    final drafts = _MemoryDraftStore();
    drafts.values['unpaired'] = '未接続の下書き';
    await tester.pumpWidget(
      _app(
        ChatScreen(
          connectionStore: _ConnectionStore(null),
          draftStore: drafts,
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(_composerText(tester), '未接続の下書き');
  });

  testWidgets('offline PC and conversation creation failures preserve text', (
    tester,
  ) async {
    final transport = _ScriptedTransport(
      const [],
      createError: StateError('offline'),
    );
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: transport,
          draftStore: _MemoryDraftStore(),
          draftScope: 'pc:offline',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), 'オフラインでも保持');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();

    expect(_composerText(tester), 'オフラインでも保持');
    expect(find.byKey(const ValueKey('composer-retry-state')), findsOneWidget);
    expect(find.text('オフラインでも保持'), findsOneWidget);
  });

  testWidgets('ambiguous pre-commit retry reuses one message id', (
    tester,
  ) async {
    final transport = _ScriptedTransport([
      _SendAttempt(
        const [],
        error: const ConversationSendException(
          'connection lost',
          ambiguous: true,
        ),
      ),
      _SendAttempt(const [
        CanonicalChatUpdate(CanonicalChatUpdateKind.accepted),
        CanonicalChatUpdate(CanonicalChatUpdateKind.done),
      ]),
    ]);
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: transport,
          draftStore: _MemoryDraftStore(),
          draftScope: 'pc:ambiguous',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), '一度だけ送る');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();
    expect(_composerText(tester), '一度だけ送る');

    await tester.tap(find.byKey(const ValueKey('composer-retry')));
    await tester.pumpAndSettle();
    expect(_composerText(tester), isEmpty);
    expect(transport.clientMessageIds, hasLength(2));
    expect(transport.clientMessageIds.toSet(), hasLength(1));
    expect(find.text('一度だけ送る'), findsOneWidget);
  });

  testWidgets('rejected request preserves text for an explicit retry', (
    tester,
  ) async {
    final transport = _ScriptedTransport([
      _SendAttempt(
        const [],
        error: const ConversationSendException(
          'request rejected',
          ambiguous: false,
        ),
      ),
    ]);
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: transport,
          draftStore: _MemoryDraftStore(),
          draftScope: 'pc:rejected',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), '拒否後も保持');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();

    expect(_composerText(tester), '拒否後も保持');
    expect(find.byKey(const ValueKey('composer-retry-state')), findsOneWidget);
    expect(transport.clientMessageIds, hasLength(1));
  });

  testWidgets('post-commit retry does not duplicate the user message', (
    tester,
  ) async {
    final transport = _ScriptedTransport([
      _SendAttempt(
        const [CanonicalChatUpdate(CanonicalChatUpdateKind.accepted)],
        error: const ConversationSendException('stream lost', ambiguous: true),
      ),
      _SendAttempt(const [
        CanonicalChatUpdate(CanonicalChatUpdateKind.accepted),
        CanonicalChatUpdate(CanonicalChatUpdateKind.delta, content: '再開済み'),
        CanonicalChatUpdate(CanonicalChatUpdateKind.done),
      ]),
    ]);
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: transport,
          draftStore: _MemoryDraftStore(),
          draftScope: 'pc:committed',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.enterText(_composerField(), '重複しない');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pumpAndSettle();
    expect(_composerText(tester), isEmpty);
    expect(find.byKey(const ValueKey('chat-retry-committed')), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('chat-retry-committed')));
    await tester.pumpAndSettle();
    expect(transport.clientMessageIds.toSet(), hasLength(1));
    expect(find.text('重複しない'), findsOneWidget);
    expect(find.text('再開済み'), findsOneWidget);
  });

  testWidgets('draft survives disposal and restoration for the same scope', (
    tester,
  ) async {
    final drafts = _MemoryDraftStore();
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: _ScriptedTransport(const []),
          draftStore: drafts,
          draftScope: 'pc:lifecycle',
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.enterText(_composerField(), 'ライフサイクル下書き');
    await tester.pump(const Duration(milliseconds: 300));
    expect(drafts.values['pc:lifecycle'], 'ライフサイクル下書き');

    await tester.pumpWidget(const SizedBox());
    await tester.pump();
    await tester.pumpWidget(
      _app(
        ChatScreen(
          transport: _ScriptedTransport(const []),
          draftStore: drafts,
          draftScope: 'pc:lifecycle',
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(_composerText(tester), 'ライフサイクル下書き');
  });
}

const _connection = MobileChatConnection(
  baseUrl: 'https://pc.example.test:8765',
  deviceId: 'device-1',
  token: 'approved-device-token',
  scopes: {'chat.read', 'chat.write'},
);

class _MemoryDraftStore implements ChatDraftStore {
  final values = <String, String>{};

  @override
  Future<String> load(String scope) async => values[scope] ?? '';

  @override
  Future<void> save(String scope, String text) async {
    if (text.isEmpty) {
      values.remove(scope);
    } else {
      values[scope] = text;
    }
  }
}

class _ConnectionStore implements ChatConnectionStore {
  _ConnectionStore(this.connection);

  MobileChatConnection? connection;

  @override
  Future<MobileChatConnection?> load() async => connection;

  @override
  Future<void> saveVerified(MobileChatConnection value) async {
    connection = value;
  }
}

class _SendAttempt {
  const _SendAttempt(this.updates, {this.error});

  final List<CanonicalChatUpdate> updates;
  final Object? error;
}

class _ScriptedTransport implements ConversationTransport {
  _ScriptedTransport(this.attempts, {this.createError});

  final List<_SendAttempt> attempts;
  final Object? createError;
  final clientMessageIds = <String>[];
  int _attempt = 0;

  @override
  Future<String> createConversation() async {
    if (createError != null) throw createError!;
    return 'conversation-1';
  }

  @override
  Future<int> revision(String conversationId) async => 7;

  @override
  Stream<CanonicalChatUpdate> send({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {
    clientMessageIds.add(clientMessageId);
    final attempt = attempts[_attempt];
    _attempt += 1;
    for (final update in attempt.updates) {
      yield update;
    }
    if (attempt.error != null) throw attempt.error!;
  }

  @override
  Future<void> stop(String conversationId) async {}

  @override
  void close() {}
}
