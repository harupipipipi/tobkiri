import 'dart:ui' show DisplayFeature, DisplayFeatureState, DisplayFeatureType;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/conversation/adaptive_conversation_screen.dart';
import 'package:rumi_remote_app/src/conversation/conversation_client.dart';
import 'package:rumi_remote_app/src/conversation/conversation_models.dart';

const _studio = MobileConversationConnection(
  id: 'studio',
  label: 'Studio PC',
  baseUrl: 'https://studio.example:8765',
  deviceId: 'phone-1',
  token: 'dtk-studio',
  scopes: {'chat.read', 'chat.write'},
);

const _office = MobileConversationConnection(
  id: 'office',
  label: 'Office PC',
  baseUrl: 'https://office.example:8765',
  deviceId: 'phone-1',
  token: 'dtk-office',
  scopes: {'chat.read', 'chat.write'},
);

class _FakeClient implements ConversationNavigationClient {
  _FakeClient(this.connection, this.created);

  final MobileConversationConnection connection;
  final List<String> created;

  List<ConversationSummary> get summaries => [
        ConversationSummary(
          id: '${connection.id}-1',
          title: connection.id == 'studio' ? 'Design review' : 'Planning',
          preview: 'Preview',
          messageCount: 3,
          updatedAt: DateTime.utc(2026, 8, 24),
          pinned: connection.id == 'studio',
        ),
      ];

  @override
  void close() {}

  @override
  Future<ConversationDetail> createConversation() async {
    created.add(connection.id);
    return ConversationDetail(
      summary: ConversationSummary(
        id: '${connection.id}-new',
        title: '',
        preview: '',
        messageCount: 0,
        updatedAt: DateTime.utc(2026, 8, 24),
        pinned: false,
      ),
      messages: const [],
    );
  }

  @override
  Future<ConversationDetail> getConversation(String conversationId) async {
    final summary = summaries.singleWhere((item) => item.id == conversationId);
    return ConversationDetail(
      summary: summary,
      messages: const [
        ConversationMessagePreview(role: 'user', content: 'Hello'),
        ConversationMessagePreview(role: 'assistant', content: 'Hi'),
      ],
    );
  }

  @override
  Future<List<ConversationSummary>> listConversations() async => summaries;
}

void main() {
  final created = <String>[];

  Widget app({
    double textScale = 1,
    List<DisplayFeature> displayFeatures = const [],
  }) {
    return MaterialApp(
      home: Builder(
        builder: (context) {
          final media = MediaQuery.of(context).copyWith(
            textScaler: TextScaler.linear(textScale),
            displayFeatures: displayFeatures,
          );
          return MediaQuery(
            data: media,
            child: AdaptiveConversationScreen(
              initialConnections: const [_studio, _office],
              clientFactory: (connection) => _FakeClient(connection, created),
            ),
          );
        },
      ),
    );
  }

  tearDown(() => created.clear());

  test('widths classify compact, medium, and expanded layouts', () {
    expect(
      AdaptiveConversationScreen.sizeForWidth(599),
      ConversationNavigationSize.compact,
    );
    expect(
      AdaptiveConversationScreen.sizeForWidth(600),
      ConversationNavigationSize.medium,
    );
    expect(
      AdaptiveConversationScreen.sizeForWidth(1024),
      ConversationNavigationSize.expanded,
    );
  });

  testWidgets('small phone uses a bounded drawer and one primary action',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(320, 568));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(app());
    await tester.pumpAndSettle();

    expect(find.byTooltip('New conversation'), findsOneWidget);
    expect(find.byKey(const ValueKey('persistent-new-conversation')),
        findsNothing);

    await tester.tap(find.byIcon(Icons.menu));
    await tester.pumpAndSettle();
    expect(find.byType(Drawer), findsOneWidget);
    final drawerSize = tester.getSize(find.byType(Drawer));
    expect(drawerSize.width, lessThanOrEqualTo(272));
    expect(
        find.byKey(const ValueKey('conversation-space-list')), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('large text compact layout remains usable without overflow',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(app(textScale: 2));
    await tester.pumpAndSettle();
    await tester.tap(find.byIcon(Icons.menu));
    await tester.pumpAndSettle();

    expect(find.text('Spaces'), findsOneWidget);
    expect(find.textContaining('Conversations · Studio PC'), findsOneWidget);
    expect(find.byTooltip('New conversation'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('tablet uses a persistent pane with one primary action',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(800, 1180));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(app());
    await tester.pumpAndSettle();

    expect(find.byType(Drawer), findsNothing);
    expect(find.byKey(const ValueKey('persistent-new-conversation')),
        findsOneWidget);
    expect(find.byTooltip('New conversation'), findsNothing);
    expect(find.text('Studio PC'), findsOneWidget);
    expect(find.text('Design review'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('landscape medium layout supports keyboard space selection',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(844, 390));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(app());
    await tester.pumpAndSettle();

    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.pumpAndSettle();

    expect(find.textContaining('Conversations · Office PC'), findsOneWidget);
    expect(find.text('Planning'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('desktop separates space and conversation navigation',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(1440, 900));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(app());
    await tester.pumpAndSettle();

    expect(find.byType(Drawer), findsNothing);
    expect(
        find.byKey(const ValueKey('conversation-space-list')), findsOneWidget);
    expect(find.byKey(const ValueKey('conversation-list')), findsOneWidget);
    expect(find.byKey(const ValueKey('persistent-new-conversation')),
        findsOneWidget);

    await tester.tap(find.text('Office PC'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Conversations · Office PC'), findsOneWidget);
    expect(find.text('Planning'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('expanded foldable layout leaves the hinge unobstructed',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(1200, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    const hinge = DisplayFeature(
      bounds: Rect.fromLTWH(480, 0, 24, 800),
      type: DisplayFeatureType.hinge,
      state: DisplayFeatureState.postureFlat,
    );
    await tester.pumpWidget(app(displayFeatures: const [hinge]));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('display-feature-gap')), findsOneWidget);
    expect(
      tester.getSize(find.byKey(const ValueKey('display-feature-gap'))).width,
      24,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('new conversation action creates and selects one conversation',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(800, 1180));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(app());
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('persistent-new-conversation')));
    await tester.pumpAndSettle();

    expect(created, ['studio']);
    expect(find.text('Untitled conversation'), findsNWidgets(2));
    expect(find.text('This conversation is empty.'), findsOneWidget);
  });

  testWidgets('space controls expose selected and connection state semantics',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(800, 1180));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(app());
    await tester.pumpAndSettle();

    expect(
      find.bySemanticsLabel('Studio PC, online'),
      findsOneWidget,
    );
    final tile = find.widgetWithText(RadioListTile<String>, 'Studio PC');
    expect(tester.getSize(tile).height, greaterThanOrEqualTo(48));
    handle.dispose();
  });
}
