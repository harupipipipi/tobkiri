import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/conversation_timeline.dart';

void main() {
  testWidgets('streamed content follows the latest entry at the bottom', (
    tester,
  ) async {
    var items = _items(12);
    await _pumpTimeline(tester, items);
    final scrollController = _scrollController(tester);

    expect(
      scrollController.position.pixels,
      closeTo(scrollController.position.maxScrollExtent, 0.5),
    );

    items = _items(12, lastRevision: 1);
    await _pumpTimeline(tester, items);

    expect(
      scrollController.position.pixels,
      closeTo(scrollController.position.maxScrollExtent, 0.5),
    );
    expect(find.text('message 11 revision 1'), findsOneWidget);
  });

  testWidgets(
    'streamed content does not pull the reader down after scroll-up',
    (tester) async {
      var items = _items(12);
      await _pumpTimeline(tester, items);
      final scrollController = _scrollController(tester);

      await tester.drag(find.byType(ListView), const Offset(0, 180));
      await tester.pump();
      final pausedPixels = scrollController.position.pixels;
      expect(
        pausedPixels,
        lessThan(scrollController.position.maxScrollExtent - 96),
      );
      expect(find.text('最新へ'), findsOneWidget);

      items = [
        ...items,
        const ConversationTimelineItem(
          id: 'message-12',
          child: SizedBox(height: 80, child: Text('message 12')),
        ),
      ];
      await _pumpTimeline(tester, items);

      expect(scrollController.position.pixels, closeTo(pausedPixels, 0.5));
      expect(find.text('最新へ'), findsOneWidget);
      expect(find.text('1'), findsOneWidget);
    },
  );

  testWidgets('tool activity contributes an unread indication while paused', (
    tester,
  ) async {
    var items = _items(12);
    await _pumpTimeline(tester, items);
    final scrollController = _scrollController(tester);
    await tester.drag(find.byType(ListView), const Offset(0, 180));
    await tester.pump();
    final pausedPixels = scrollController.position.pixels;

    items = [
      ...items,
      const ConversationTimelineItem(
        id: 'tool-1',
        isActivity: true,
        child: SizedBox(height: 56, child: Text('検索中')),
      ),
    ];
    await _pumpTimeline(tester, items);

    expect(find.text('最新へ'), findsOneWidget);
    expect(find.text('1'), findsOneWidget);
    expect(scrollController.position.pixels, closeTo(pausedPixels, 0.5));
  });

  testWidgets('prepends and height changes keep a visible item anchored', (
    tester,
  ) async {
    var items = _items(14);
    await _pumpTimeline(tester, items);
    final scrollController = _scrollController(tester);
    await tester.drag(find.byType(ListView), const Offset(0, 250));
    await tester.pump();

    final beforePrepend = tester.getTopLeft(find.text('message 5')).dy;
    items = [
      const ConversationTimelineItem(
        id: 'message-old-1',
        child: SizedBox(height: 72, child: Text('older 1')),
      ),
      const ConversationTimelineItem(
        id: 'message-old-2',
        child: SizedBox(height: 88, child: Text('older 2')),
      ),
      ...items,
    ];
    await _pumpTimeline(tester, items);

    final afterPrepend = tester.getTopLeft(find.text('message 5')).dy;
    expect(afterPrepend, closeTo(beforePrepend, 1.0));

    final beforeResize = tester.getTopLeft(find.text('message 5')).dy;
    items = _items(14, expandedIndex: 1, expandedHeight: 150);
    items = [
      const ConversationTimelineItem(
        id: 'message-old-1',
        child: SizedBox(height: 72, child: Text('older 1')),
      ),
      const ConversationTimelineItem(
        id: 'message-old-2',
        child: SizedBox(height: 88, child: Text('older 2')),
      ),
      ...items,
    ];
    await _pumpTimeline(tester, items);

    final afterResize = tester.getTopLeft(find.text('message 5')).dy;
    expect(afterResize, closeTo(beforeResize, 1.0));
    expect(
      scrollController.position.pixels,
      lessThan(scrollController.position.maxScrollExtent - 40),
    );
  });

  testWidgets('rotation keeps the visible anchor stable', (tester) async {
    final items = _items(14);
    await _pumpTimeline(tester, items);
    await tester.drag(find.byType(ListView), const Offset(0, 250));
    await tester.pump();
    final before = tester.getTopLeft(find.text('message 5')).dy;

    await tester.binding.setSurfaceSize(const Size(480, 320));
    await tester.pump();
    await tester.pump();

    final after = tester.getTopLeft(find.text('message 5')).dy;
    expect(after, closeTo(before, 1.0));
  });

  testWidgets('keyboard viewport resize keeps the visible anchor stable', (
    tester,
  ) async {
    final items = _items(14);
    await _pumpTimeline(tester, items, viewInsets: EdgeInsets.zero);
    await tester.drag(find.byType(ListView), const Offset(0, 250));
    await tester.pump();
    final before = tester.getTopLeft(find.text('message 5')).dy;

    await _pumpTimeline(
      tester,
      items,
      viewInsets: const EdgeInsets.only(bottom: 180),
    );

    final after = tester.getTopLeft(find.text('message 5')).dy;
    expect(after, closeTo(before, 1.0));
  });

  testWidgets('reduced motion returns immediately from the latest affordance', (
    tester,
  ) async {
    var items = _items(12);
    await _pumpTimeline(tester, items, reducedMotion: true);
    final scrollController = _scrollController(tester);
    await tester.drag(find.byType(ListView), const Offset(0, 180));
    await tester.pump();

    items = [
      ...items,
      const ConversationTimelineItem(
        id: 'message-12',
        child: SizedBox(height: 80, child: Text('message 12')),
      ),
    ];
    await _pumpTimeline(tester, items, reducedMotion: true);
    expect(find.text('最新へ'), findsOneWidget);

    await tester.tap(find.text('最新へ'));
    await tester.pump();

    expect(
      scrollController.position.pixels,
      closeTo(scrollController.position.maxScrollExtent, 0.5),
    );
    expect(find.text('最新へ'), findsNothing);
  });
}

Future<void> _pumpTimeline(
  WidgetTester tester,
  List<ConversationTimelineItem> items, {
  EdgeInsets viewInsets = EdgeInsets.zero,
  bool? reducedMotion,
}) async {
  await tester.binding.setSurfaceSize(const Size(320, 480));
  await tester.pumpWidget(
    MaterialApp(
      home: MediaQuery(
        data: MediaQueryData(
          size: const Size(320, 480),
          viewInsets: viewInsets,
        ),
        child: Scaffold(
          resizeToAvoidBottomInset: true,
          body: ConversationTimeline(
            items: items,
            reducedMotion: reducedMotion,
          ),
        ),
      ),
    ),
  );
  await tester.pump();
}

ScrollController _scrollController(WidgetTester tester) {
  return tester.widget<ListView>(find.byType(ListView)).controller!;
}

List<ConversationTimelineItem> _items(
  int count, {
  int? lastRevision,
  int? expandedIndex,
  double expandedHeight = 80,
}) {
  return List<ConversationTimelineItem>.generate(count, (index) {
    final revision = index == count - 1 ? (lastRevision ?? 0) : 0;
    final height = index == expandedIndex ? expandedHeight : 80.0;
    return ConversationTimelineItem(
      id: 'message-$index',
      revision: revision,
      child: SizedBox(
        height: height,
        child: Align(
          alignment: Alignment.centerLeft,
          child: Text(
            'message $index${revision == 0 ? '' : ' revision $revision'}',
          ),
        ),
      ),
    );
  });
}
