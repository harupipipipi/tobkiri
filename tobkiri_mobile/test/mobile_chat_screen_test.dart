import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/mobile_chat_api.dart';
import 'package:rumi_remote_app/src/mobile_chat_screen.dart';

class _FakeGateway implements MobileChatGateway {
  bool closed = false;

  @override
  Future<String> createConversation() async => 'conversation-1';

  @override
  Future<MobileConversationSnapshot> getConversation(String id) async {
    return const MobileConversationSnapshot(
      id: 'conversation-1',
      title: 'テスト会話',
      revision: 3,
      messages: [
        MobileChatMessage(
          id: 'existing-1',
          role: 'assistant',
          content: '以前の回答',
        ),
      ],
    );
  }

  @override
  Future<List<MobileConversationSummary>> listConversations() async {
    return const [
      MobileConversationSummary(
        id: 'conversation-1',
        title: 'テスト会話',
        messageCount: 1,
      ),
    ];
  }

  @override
  Stream<MobileChatStreamEvent> streamMessage({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {
    expect(conversationId, 'conversation-1');
    expect(text, '調べて');
    expect(expectedRevision, 3);
    yield const MobileChatDelta('確認しています');
    yield const MobileChatActivity(
      id: 'tool-1',
      label: '検索 · completed',
      kind: 'tool',
    );
    yield const MobileChatCompleted();
  }

  @override
  Future<void> stop(String conversationId) async {}

  @override
  void close() => closed = true;
}

void main() {
  testWidgets('canonical chat renders streamed content and tool activity', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    final gateway = _FakeGateway();
    await tester.binding.setSurfaceSize(const Size(393, 852));
    addTearDown(() => tester.binding.setSurfaceSize(null));

    await tester.pumpWidget(
      MaterialApp(
        home: MobileChatScreen(
          baseUrl: 'https://unused.example',
          bearerToken: 'unused',
          gateway: gateway,
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('テスト会話'), findsOneWidget);
    expect(find.text('以前の回答'), findsOneWidget);

    await tester.enterText(find.byType(TextField), '調べて');
    await tester.pump();
    final sendButton = find.ancestor(
      of: find.byTooltip('送信'),
      matching: find.byType(IconButton),
    );
    expect(tester.widget<IconButton>(sendButton).onPressed, isNotNull);
    await tester.tap(sendButton);
    await tester.pumpAndSettle();

    expect(find.text('確認しています'), findsOneWidget);
    expect(find.text('検索 · completed'), findsOneWidget);
    final semanticLabels = tester
        .widgetList<Semantics>(find.byType(Semantics))
        .map((widget) => widget.properties.label);
    expect(semanticLabels, contains('アクティビティ: 検索 · completed'));
    expect(tester.takeException(), isNull);
    semantics.dispose();
  });

  testWidgets('empty chat keeps composer available', (tester) async {
    final gateway = _EmptyGateway();
    await tester.pumpWidget(
      MaterialApp(
        home: MobileChatScreen(
          baseUrl: 'https://unused.example',
          bearerToken: 'unused',
          gateway: gateway,
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Tobkiriで会話を始めましょう'), findsOneWidget);
    expect(find.byType(TextField), findsOneWidget);
    expect(find.byTooltip('送信'), findsOneWidget);
  });

  testWidgets('sending after loading history keeps the expanded slice', (
    tester,
  ) async {
    final gateway = _HistoryGateway();
    await tester.binding.setSurfaceSize(const Size(393, 852));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(
      MaterialApp(
        home: MobileChatScreen(
          baseUrl: 'https://unused.example',
          bearerToken: 'unused',
          gateway: gateway,
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.fling(
      find.byType(ListView),
      const Offset(0, 1800),
      1200,
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('以前のメッセージ（1件）'));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), '続けて');
    await tester.tap(find.byTooltip('送信'));
    await tester.pumpAndSettle();

    expect(find.text('以前のメッセージ（3件）'), findsNothing);
  });
}

class _EmptyGateway implements MobileChatGateway {
  @override
  Future<String> createConversation() async => 'new-conversation';

  @override
  Future<MobileConversationSnapshot> getConversation(String id) {
    throw UnimplementedError();
  }

  @override
  Future<List<MobileConversationSummary>> listConversations() async => const [];

  @override
  Stream<MobileChatStreamEvent> streamMessage({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {}

  @override
  Future<void> stop(String conversationId) async {}

  @override
  void close() {}
}

class _HistoryGateway implements MobileChatGateway {
  @override
  Future<String> createConversation() async => 'conversation-history';

  @override
  Future<MobileConversationSnapshot> getConversation(String id) async {
    return MobileConversationSnapshot(
      id: 'conversation-history',
      title: '履歴テスト',
      revision: 2,
      messages: List.generate(
        41,
        (index) => MobileChatMessage(
          id: 'history-${index + 1}',
          role: index.isEven ? 'assistant' : 'user',
          content: '履歴 ${index + 1}',
        ),
      ),
    );
  }

  @override
  Future<List<MobileConversationSummary>> listConversations() async {
    return const [
      MobileConversationSummary(
        id: 'conversation-history',
        title: '履歴テスト',
        messageCount: 41,
      ),
    ];
  }

  @override
  Stream<MobileChatStreamEvent> streamMessage({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {
    yield const MobileChatDelta('続きです');
    yield const MobileChatCompleted();
  }

  @override
  Future<void> stop(String conversationId) async {}

  @override
  void close() {}
}
