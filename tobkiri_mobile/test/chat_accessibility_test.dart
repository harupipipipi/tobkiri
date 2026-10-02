import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/chat/canonical_conversation_client.dart';
import 'package:rumi_remote_app/src/chat/chat_draft_store.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/chat_screen.dart';
import 'package:rumi_remote_app/src/chat/composer_bar.dart';
import 'package:rumi_remote_app/src/chat/message_view.dart';

Widget _app(Widget child, {Locale locale = const Locale('ja')}) {
  return MaterialApp(
    locale: locale,
    supportedLocales: const [Locale('en'), Locale('ja')],
    localizationsDelegates: GlobalMaterialLocalizations.delegates,
    home: Scaffold(body: child),
  );
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('messages announce author state and content exactly once', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    await tester.binding.setSurfaceSize(const Size(900, 1800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    const messages = [
      ChatMessage(id: 'user', role: ChatRole.user, content: 'こんにちは'),
      ChatMessage(id: 'assistant', role: ChatRole.assistant, content: 'ようこそ'),
      ChatMessage(
        id: 'pending-empty',
        role: ChatRole.assistant,
        content: '',
        pending: true,
      ),
      ChatMessage(
        id: 'pending-content',
        role: ChatRole.assistant,
        content: '途中',
        pending: true,
      ),
      ChatMessage(
        id: 'error-content',
        role: ChatRole.assistant,
        content: '再試行してください',
        error: true,
      ),
      ChatMessage(
        id: 'error-empty',
        role: ChatRole.assistant,
        content: '',
        error: true,
      ),
      ChatMessage(id: 'empty', role: ChatRole.assistant, content: ''),
    ];
    await tester.pumpWidget(
      _app(
        SingleChildScrollView(
          child: Column(
            children: [
              for (final message in messages) MessageView(message: message),
            ],
          ),
        ),
      ),
    );

    expect(
      tester
          .getSemantics(find.byKey(const ValueKey('message-semantics:user')))
          .label,
      'あなたのメッセージ, 内容: こんにちは',
    );
    expect(
      tester
          .getSemantics(
            find.byKey(const ValueKey('message-semantics:assistant')),
          )
          .label,
      'Tobkiriの応答, 内容: ようこそ',
    );
    expect(
      tester
          .getSemantics(
            find.byKey(const ValueKey('message-semantics:pending-empty')),
          )
          .label,
      'Tobkiriの応答, 処理中, 内容なし',
    );
    expect(
      tester
          .getSemantics(
            find.byKey(const ValueKey('message-semantics:pending-content')),
          )
          .label,
      'Tobkiriの応答, 処理中, 内容: 途中',
    );
    expect(
      tester
          .getSemantics(
            find.byKey(const ValueKey('message-semantics:error-content')),
          )
          .label,
      'Tobkiriの応答, 送信に失敗しました, 内容: 再試行してください',
    );
    expect(
      tester
          .getSemantics(
            find.byKey(const ValueKey('message-semantics:error-empty')),
          )
          .label,
      'Tobkiriの応答, 送信に失敗しました, 内容なし',
    );
    expect(
      tester
          .getSemantics(find.byKey(const ValueKey('message-semantics:empty')))
          .label,
      'Tobkiriの応答, 内容なし',
    );
    expect(find.text('送信に失敗しました'), findsNWidgets(2));
    expect(find.byIcon(Icons.error_outline), findsNWidgets(2));
    expect(find.text('処理中'), findsNWidgets(2));

    expect(find.bySemanticsLabel(RegExp('こんにちは')), findsOneWidget);
    expect(find.bySemanticsLabel(RegExp('ようこそ')), findsOneWidget);
    semantics.dispose();
  });

  testWidgets('composer exposes ordered localized 48 pixel actions', (
    tester,
  ) async {
    final sent = <String>[];
    var added = false;
    var stopped = false;
    await tester.pumpWidget(
      _app(
        ComposerBar(
          busy: false,
          onAdd: () => added = true,
          onSend: (text) async {
            sent.add(text);
            return const ComposerSendResult.accepted();
          },
          onStop: () {},
        ),
      ),
    );

    final add = find.byKey(const ValueKey('composer-add'));
    final field = find.byKey(const ValueKey('composer-field'));
    final send = find.byKey(const ValueKey('composer-send'));
    expect(tester.getSize(add), const Size(48, 48));
    expect(tester.getSize(send), const Size(48, 48));
    expect(tester.getTopLeft(add).dx, lessThan(tester.getTopLeft(field).dx));
    expect(tester.getTopLeft(field).dx, lessThan(tester.getTopLeft(send).dx));
    expect(tester.getSemantics(add).label, 'オプションを追加');
    expect(tester.getSemantics(field).label, 'メッセージ入力欄');
    expect(tester.getSemantics(send).label, 'メッセージを送信');
    expect(
      tester
          .getSemantics(send)
          .getSemanticsData()
          .hasAction(SemanticsAction.tap),
      isFalse,
    );

    await tester.tap(add);
    expect(added, isTrue);
    await tester.enterText(find.byType(TextField), '送信テスト');
    await tester.pump();
    expect(
      tester
          .getSemantics(send)
          .getSemanticsData()
          .hasAction(SemanticsAction.tap),
      isTrue,
    );
    await tester.tap(send);
    await tester.pump();
    expect(sent, ['送信テスト']);

    await tester.pumpWidget(
      _app(
        ComposerBar(
          busy: true,
          onSend: (_) async => const ComposerSendResult.accepted(),
          onStop: () => stopped = true,
        ),
      ),
    );
    await tester.pumpAndSettle();
    final stop = find.byKey(const ValueKey('composer-stop'));
    expect(tester.getSize(stop), const Size(48, 48));
    expect(tester.getSemantics(stop).label, '応答を停止');
    await tester.tap(stop);
    expect(stopped, isTrue);
  });

  testWidgets(
    'copy stays hidden until long press and links have safe actions',
    (tester) async {
      Uri? opened;
      String? clipboardText;
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') {
            clipboardText = (call.arguments as Map)['text'] as String?;
          }
          if (call.method == 'Clipboard.getData') {
            return {'text': clipboardText};
          }
          return null;
        },
      );
      addTearDown(
        () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        ),
      );
      const message = ChatMessage(
        id: 'actions',
        role: ChatRole.assistant,
        content: '詳しくは https://example.com/docs を確認してください。',
      );
      await tester.pumpWidget(
        _app(
          MessageView(message: message, openLink: (uri) async => opened = uri),
        ),
      );

      expect(find.byKey(const ValueKey('message-copy')), findsNothing);
      final link = find.byKey(
        const ValueKey('message-link:https://example.com/docs'),
      );
      expect(tester.getSize(link), const Size(48, 48));
      expect(
        tester.getSemantics(link).label,
        contains('https://example.com/docs'),
      );
      await tester.tap(link);
      expect(opened, Uri.parse('https://example.com/docs'));

      await tester.longPress(
        find.byKey(const ValueKey('message-semantics:actions')),
      );
      await tester.pump();
      final copy = find.byKey(const ValueKey('message-copy'));
      expect(copy, findsOneWidget);
      expect(tester.getSize(copy), const Size(48, 48));
      await tester.tap(copy);
      await tester.pump();
      expect(clipboardText, message.content);
    },
  );

  testWidgets('English accessibility labels are stable', (tester) async {
    await tester.pumpWidget(
      _app(
        const MessageView(
          message: ChatMessage(
            id: 'english',
            role: ChatRole.assistant,
            content: '',
            error: true,
          ),
        ),
        locale: const Locale('en'),
      ),
    );
    expect(
      tester
          .getSemantics(find.byKey(const ValueKey('message-semantics:english')))
          .label,
      'Tobkiri response, Message failed, No content',
    );
  });

  testWidgets('canonical chat renders user assistant pending and completion', (
    tester,
  ) async {
    final transport = _FakeTransport([
      const CanonicalChatUpdate(CanonicalChatUpdateKind.accepted),
      const CanonicalChatUpdate(CanonicalChatUpdateKind.delta, content: '成功'),
      const CanonicalChatUpdate(CanonicalChatUpdateKind.done),
    ]);
    await tester.pumpWidget(
      _app(ChatScreen(transport: transport, draftStore: _MemoryDraftStore())),
    );
    await tester.pump();
    await tester.enterText(find.byType(TextField), '質問');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpAndSettle();

    expect(transport.lastExpectedRevision, 7);
    expect(transport.lastText, '質問');
    expect(find.text('質問'), findsOneWidget);
    expect(find.text('成功'), findsOneWidget);
    expect(find.text('処理中'), findsNothing);
  });

  testWidgets('canonical chat exposes a non-color error state', (tester) async {
    final transport = _FakeTransport([
      const CanonicalChatUpdate(CanonicalChatUpdateKind.accepted),
      const CanonicalChatUpdate(
        CanonicalChatUpdateKind.error,
        content: '拒否されました',
      ),
    ]);
    await tester.pumpWidget(
      _app(ChatScreen(transport: transport, draftStore: _MemoryDraftStore())),
    );
    await tester.pump();
    await tester.enterText(find.byType(TextField), '失敗');
    await tester.pump();
    await tester.tap(find.byKey(const ValueKey('composer-send')));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpAndSettle();

    expect(find.text('送信に失敗しました'), findsOneWidget);
    expect(find.byIcon(Icons.error_outline), findsOneWidget);
    expect(find.text('拒否されました'), findsOneWidget);
  });

  testWidgets(
      'chat keeps drafting available but fails closed without connection', (
    tester,
  ) async {
    await tester.pumpWidget(
      _app(
        ChatScreen(
          connectionStore: _EmptyConnectionStore(),
          draftStore: _MemoryDraftStore(),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('ペアリング済みのチャット接続がありません。'), findsOneWidget);
    expect(tester.widget<TextField>(find.byType(TextField)).enabled, isTrue);
  });

  testWidgets('connection setup reports errors and supports keyboard save', (
    tester,
  ) async {
    final store = _WritableConnectionStore();
    await tester.pumpWidget(
      _app(
        ChatScreen(
          connectionStore: store,
          draftStore: _MemoryDraftStore(),
          clientFactory: (_) => _FakeTransport(const []),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('接続方法'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('安全に保存して接続'));
    await tester.pump();
    expect(find.textContaining('PC URL、端末 ID'), findsOneWidget);

    Finder field(String label) => find.byWidgetPredicate(
          (widget) =>
              widget is TextField && widget.decoration?.labelText == label,
        );
    await tester.enterText(field('Tobkiri PC URL'), 'https://pc.example.test');
    await tester.enterText(field('端末 ID'), 'device-1');
    await tester.enterText(field('端末トークン'), 'approved-device-token');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    expect(store.saved?.isValid, isTrue);
    expect(find.text('ペアリング済みのチャット接続がありません。'), findsNothing);
    expect(tester.widget<TextField>(find.byType(TextField)).enabled, isTrue);
  });
}

class _EmptyConnectionStore implements ChatConnectionStore {
  @override
  Future<MobileChatConnection?> load() async => null;

  @override
  Future<void> saveVerified(MobileChatConnection connection) async {}
}

class _WritableConnectionStore implements ChatConnectionStore {
  MobileChatConnection? saved;

  @override
  Future<MobileChatConnection?> load() async => saved;

  @override
  Future<void> saveVerified(MobileChatConnection connection) async {
    saved = connection;
  }
}

class _MemoryDraftStore implements ChatDraftStore {
  final Map<String, String> _drafts = {};

  @override
  Future<String> load(String scope) async => _drafts[scope] ?? '';

  @override
  Future<void> save(String scope, String text) async {
    if (text.isEmpty) {
      _drafts.remove(scope);
    } else {
      _drafts[scope] = text;
    }
  }
}

class _FakeTransport implements ConversationTransport {
  _FakeTransport(this.updates);

  final List<CanonicalChatUpdate> updates;
  int? lastExpectedRevision;
  String? lastText;

  @override
  Future<String> createConversation() async => 'conversation-1';

  @override
  Future<int> revision(String conversationId) async => 7;

  @override
  Stream<CanonicalChatUpdate> send({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {
    lastExpectedRevision = expectedRevision;
    lastText = text;
    for (final update in updates) {
      yield update;
    }
  }

  @override
  Future<void> stop(String conversationId) async {}

  @override
  void close() {}
}
