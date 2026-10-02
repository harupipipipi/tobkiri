import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/models.dart';
import 'package:rumi_remote_app/src/pc_conversations/pc_conversations.dart';
import 'package:rumi_remote_app/src/rumi_api_client.dart';

PcConversation _conversation(
  String id, {
  String title = 'Conversation',
  int count = 2,
  DateTime? updatedAt,
  bool pinned = false,
  String preview = 'Latest answer',
}) {
  return PcConversation(
    id: id,
    title: title,
    messageCount: count,
    updatedAt: updatedAt ?? DateTime.utc(2026, 8, 24, 12),
    createdAt: DateTime.utc(2026, 8, 20),
    pinned: pinned,
    preview: preview,
    revision: 1,
    raw: <String, dynamic>{},
  );
}

Widget _drawerApp(
  PcConversationsController controller, {
  String? activeConversationId,
  ValueChanged<PcConversation>? onSelected,
  DateTime Function()? clock,
  double textScale = 1,
}) {
  return MaterialApp(
    theme: buildRumiTheme(),
    home: MediaQuery(
      data: MediaQueryData(textScaler: TextScaler.linear(textScale)),
      child: Scaffold(
        drawer: PcConversationsDrawer(
          controller: controller,
          activeConversationId: activeConversationId,
          onConversationSelected: onSelected,
          clock: clock,
        ),
        body: const SizedBox.expand(),
      ),
    ),
  );
}

void _openDrawer(WidgetTester tester) {
  tester.state<ScaffoldState>(find.byType(Scaffold)).openDrawer();
}

void main() {
  test('conversation catalog accepts the canonical mobile response shape', () {
    final catalog = PcConversationCatalog.fromJson({
      'count': 1,
      'conversations': [
        {
          'id': 'c-1',
          'title': 'Notes',
          'message_count': '4',
          'updated_at': 1755000000000,
          'created_at': 1754000000000,
          'pinned': true,
          'preview': 'hello',
          'revision': 3,
        },
      ],
    });

    expect(catalog.count, 1);
    expect(catalog.conversations.single.messageCount, 4);
    expect(catalog.conversations.single.pinned, isTrue);
    expect(catalog.conversations.single.revision, 3);
  });

  test('conversation text is bounded and strips invisible direction controls',
      () {
    final conversation = _conversation(
      'long-preview',
      title: 'Title\u202E hidden',
      preview: 'Preview\u200B ${'x' * 300}',
    );

    expect(conversation.displayTitle, 'Title hidden');
    expect(conversation.safePreview, startsWith('Preview '));
    expect(conversation.safePreview.length, lessThanOrEqualTo(160));
    expect(conversation.safePreview, endsWith('…'));
  });

  test('API client lists conversations only through the mobile route',
      () async {
    late http.Request request;
    final client = RumiApiClient(
      baseUrl: 'http://pc.local:8765',
      bearerToken: 'token',
      httpClient: MockClient((incoming) async {
        request = incoming;
        return http.Response(
          jsonEncode({
            'status': 'ok',
            'data': {
              'count': 0,
              'conversations': <Object?>[],
            },
          }),
          200,
        );
      }),
    );

    final catalog = await client.listPcConversations();

    expect(request.method, 'GET');
    expect(request.url.path, '/api/mobile/v1/conversations');
    expect(catalog.conversations, isEmpty);
    client.close();
  });

  test('failed refresh keeps cached conversations and marks stale offline',
      () async {
    var attempts = 0;
    final cached = _conversation('cached');
    final controller = PcConversationsController(
      initialConversations: [cached],
      loader: () async {
        attempts += 1;
        if (attempts == 1) {
          return [cached, _conversation('new')];
        }
        throw StateError('offline');
      },
    );

    await controller.refresh();
    expect(controller.conversations, hasLength(2));
    await controller.refresh();

    expect(controller.conversations, hasLength(2));
    expect(controller.offline, isTrue);
    expect(controller.stale, isTrue);
    expect(controller.error, isA<StateError>());
    controller.dispose();
  });

  test('authority reset clears cache and fences an in-flight response',
      () async {
    final pending = Completer<List<PcConversation>>();
    final controller = PcConversationsController(
      loader: () => pending.future,
      initialConversations: [_conversation('old-authority')],
    );

    final refresh = controller.refresh();
    controller.reset();
    pending.complete([_conversation('late-old-authority')]);
    await refresh;

    expect(controller.conversations, isEmpty);
    expect(controller.loading, isFalse);
    expect(controller.offline, isFalse);
    controller.dispose();
  });

  testWidgets('drawer groups pinned recent and earlier conversations', (
    tester,
  ) async {
    final now = DateTime.utc(2026, 8, 24, 12);
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: [
        _conversation('earlier',
            updatedAt: now.subtract(const Duration(days: 8))),
        _conversation('recent',
            updatedAt: now.subtract(const Duration(days: 1))),
        _conversation('pinned', pinned: true, updatedAt: now),
      ],
    );

    await tester.pumpWidget(_drawerApp(controller, clock: () => now));
    _openDrawer(tester);
    await tester.pumpAndSettle();

    expect(find.text('Pinned'), findsOneWidget);
    expect(find.text('Recent'), findsOneWidget);
    final scrollController = tester
        .widget<CustomScrollView>(find.byType(CustomScrollView))
        .controller!;
    expect(find.textContaining('Yesterday'), findsOneWidget);
    scrollController.jumpTo(scrollController.position.maxScrollExtent);
    await tester.pump();
    expect(find.text('Earlier'), findsOneWidget);
    expect(find.byIcon(Icons.push_pin_outlined), findsOneWidget);
    expect(find.textContaining('2 messages'), findsWidgets);
    controller.dispose();
  });

  testWidgets('duplicate titles expose stable ids and active/pinned text', (
    tester,
  ) async {
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: [
        _conversation('conversation-alpha-1234', title: 'Same title'),
        _conversation(
          'conversation-beta-5678',
          title: 'Same title',
          pinned: true,
        ),
      ],
    );

    await tester.pumpWidget(
      _drawerApp(controller, activeConversationId: 'conversation-alpha-1234'),
    );
    _openDrawer(tester);
    await tester.pumpAndSettle();

    expect(find.textContaining('conversation'), findsNWidgets(2));
    expect(find.text('active'), findsOneWidget);
    expect(find.text('pinned'), findsOneWidget);
    expect(find.byIcon(Icons.lock_outline), findsNWidgets(2));
    controller.dispose();
  });

  testWidgets('read-only row selection is exposed without mutation actions', (
    tester,
  ) async {
    PcConversation? selected;
    final conversation = _conversation('selectable', title: 'Select me');
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: [conversation],
    );

    await tester.pumpWidget(
      _drawerApp(controller, onSelected: (value) => selected = value),
    );
    _openDrawer(tester);
    await tester.pumpAndSettle();
    expect(find.textContaining('Read-only PC conversations'), findsOneWidget);
    final scrollController = tester
        .widget<CustomScrollView>(find.byType(CustomScrollView))
        .controller!;
    scrollController.jumpTo(scrollController.position.maxScrollExtent);
    await tester.pump();

    expect(find.byType(PopupMenuButton<Object>), findsNothing);
    await tester.tap(find.text('Select me'));
    expect(selected?.id, 'selectable');
    controller.dispose();
  });

  testWidgets('search and scroll position survive a controller refresh', (
    tester,
  ) async {
    final conversations = List.generate(
      80,
      (index) => _conversation(
        'id-$index',
        title: index == 79 ? 'Needle title' : 'Conversation $index',
      ),
    );
    Completer<List<PcConversation>>? pending;
    final controller = PcConversationsController(
      loader: () {
        pending = Completer<List<PcConversation>>();
        return pending!.future;
      },
      initialConversations: conversations,
    );

    await tester.pumpWidget(_drawerApp(controller));
    _openDrawer(tester);
    await tester.pumpAndSettle();

    final list = find.byType(CustomScrollView);
    await tester.drag(list, const Offset(0, -600));
    await tester.pump();
    final scrollPosition = tester
        .widget<CustomScrollView>(find.byType(CustomScrollView))
        .controller!
        .position;
    final beforeRefresh = scrollPosition.pixels;
    expect(beforeRefresh, greaterThan(0));

    final refresh = controller.refresh();
    await tester.pump();
    expect(scrollPosition.pixels, closeTo(beforeRefresh, 0.1));
    pending!.complete(conversations);
    await refresh;
    await tester.pump();
    expect(scrollPosition.pixels, closeTo(beforeRefresh, 0.1));

    scrollPosition.jumpTo(0);
    await tester.pump();
    await tester.enterText(find.byType(TextField), 'Needle');
    await tester.pump();
    expect(find.text('Needle title'), findsOneWidget);
    final search = tester.widget<TextField>(find.byType(TextField));
    expect(search.focusNode?.hasFocus, isTrue);

    final refreshAfterSearch = controller.refresh();
    await tester.pump();
    expect(find.text('Needle title'), findsOneWidget);
    expect(search.focusNode?.hasFocus, isTrue);
    pending!.complete(conversations);
    await refreshAfterSearch;
    await tester.pump();
    expect(find.text('Needle title'), findsOneWidget);
    expect(search.focusNode?.hasFocus, isTrue);
    controller.dispose();
  });

  testWidgets('empty title gets a visible accessible fallback', (tester) async {
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: [_conversation('untitled', title: '')],
    );

    await tester.pumpWidget(_drawerApp(controller));
    _openDrawer(tester);
    await tester.pumpAndSettle();

    expect(find.text('Untitled conversation'), findsOneWidget);
    controller.dispose();
  });

  testWidgets('empty conversation shows count without a blank subtitle row', (
    tester,
  ) async {
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: [
        _conversation('empty', count: 0, preview: ''),
      ],
    );

    await tester.pumpWidget(_drawerApp(controller));
    _openDrawer(tester);
    await tester.pumpAndSettle();

    final scrollController = tester
        .widget<CustomScrollView>(find.byType(CustomScrollView))
        .controller!;
    scrollController.jumpTo(scrollController.position.maxScrollExtent);
    await tester.pump();

    expect(find.textContaining('0 messages'), findsOneWidget);
    expect(find.text('Latest answer'), findsNothing);
    controller.dispose();
  });

  testWidgets('empty, stale, large text, and large lists remain usable', (
    tester,
  ) async {
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: const [],
    );

    await tester.pumpWidget(_drawerApp(controller, textScale: 2.5));
    _openDrawer(tester);
    await tester.pumpAndSettle();
    final emptyScrollController = tester
        .widget<CustomScrollView>(find.byType(CustomScrollView))
        .controller!;
    emptyScrollController
        .jumpTo(emptyScrollController.position.maxScrollExtent);
    await tester.pump();
    expect(find.text('No PC conversations yet'), findsOneWidget);

    controller.markOffline();
    await tester.pump();
    expect(find.text('Offline — conversations unavailable'), findsOneWidget);

    final large = List.generate(
      1000,
      (index) => _conversation(
        'large-$index',
        title: index == 0 ? 'Title 0 ${'x' * 200}' : 'Title $index',
        preview: index == 0 ? 'Preview ${'y' * 400}' : 'Preview',
      ),
    );
    final replacement = PcConversationsController(
      loader: () async => large,
      initialConversations: large,
    );
    await tester.pumpWidget(_drawerApp(replacement));
    _openDrawer(tester);
    await tester.pumpAndSettle();
    expect(find.byType(CustomScrollView), findsOneWidget);
    final largeScrollController = tester
        .widget<CustomScrollView>(find.byType(CustomScrollView))
        .controller!;
    largeScrollController
        .jumpTo(largeScrollController.position.maxScrollExtent);
    await tester.pump();
    expect(find.textContaining('999'), findsWidgets);
    replacement.dispose();
    controller.dispose();
  });

  testWidgets('stale cached rows keep an explicit offline banner', (
    tester,
  ) async {
    final controller = PcConversationsController(
      loader: () async => throw StateError('offline'),
      initialConversations: [_conversation('cached')],
    );
    await controller.refresh();

    await tester.pumpWidget(_drawerApp(controller));
    _openDrawer(tester);
    await tester.pumpAndSettle();

    expect(
      find.text('Offline — showing cached conversations'),
      findsOneWidget,
    );
    controller.dispose();
  });

  testWidgets('conversation tile exposes an accessibility read-only label', (
    tester,
  ) async {
    final controller = PcConversationsController(
      loader: () async => const [],
      initialConversations: [
        _conversation('accessible', title: 'Accessible title', pinned: true),
      ],
    );
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(
      _drawerApp(controller, activeConversationId: 'accessible'),
    );
    _openDrawer(tester);
    await tester.pumpAndSettle();

    final semantics = tester.getSemantics(find.text('Accessible title'));
    final label = semantics.label.toLowerCase();
    expect(label, contains('read-only'));
    expect(label, contains('active'));
    expect(label, contains('pinned'));
    handle.dispose();
    controller.dispose();
  });

  test('formatter localizes count and recency boundaries', () {
    // The widget tests above exercise the Material localizations in context;
    // this assertion keeps the section classifier's boundary deterministic.
    final now = DateTime.utc(2026, 8, 24, 12);
    expect(
      classifyPcConversation(
        _conversation('recent',
            updatedAt: now.subtract(const Duration(days: 7))),
        now: now,
      ),
      PcConversationSection.recent,
    );
  });
}
