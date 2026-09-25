import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/message_view.dart';
import 'package:rumi_remote_app/src/platform/platform_services.dart';

class _FakeUrlLauncher extends PlatformUrlLauncher {
  _FakeUrlLauncher({this.result = true, this.error});

  final bool result;
  final Object? error;
  Uri? opened;

  @override
  Future<bool> open(Uri uri) async {
    if (error != null) throw error!;
    opened = uri;
    return result;
  }
}

class _FakeClipboard extends PlatformClipboard {
  String? copied;

  @override
  Future<void> writeText(String text) async {
    copied = text;
  }
}

Widget _wrap({
  required String markdown,
  required PlatformUrlLauncher launcher,
  PlatformClipboard clipboard = const PlatformClipboard(),
}) {
  return MaterialApp(
    theme: buildRumiTheme(dark: true),
    home: Scaffold(
      body: MessageView(
        message: ChatMessage(
          id: 'assistant-link',
          role: ChatRole.assistant,
          content: markdown,
          createdAt: DateTime(2026, 8, 27),
        ),
        urlLauncher: launcher,
        clipboard: clipboard,
      ),
    ),
  );
}

void main() {
  testWidgets('HTTPS links show destination before opening', (tester) async {
    final launcher = _FakeUrlLauncher();
    await tester.pumpWidget(
      _wrap(
        markdown: '[Friendly label](https://example.com/path?item=1)',
        launcher: launcher,
      ),
    );

    await tester.tap(find.text('Friendly label'));
    await tester.pumpAndSettle();

    expect(launcher.opened, isNull);
    final linkButton = find.widgetWithText(TextButton, 'Friendly label');
    expect(tester.getSize(linkButton).width, greaterThanOrEqualTo(48));
    expect(tester.getSize(linkButton).height, greaterThanOrEqualTo(48));
    expect(
      find.bySemanticsLabel(
        'Friendly label, リンク, リンク先 example.com',
      ),
      findsOneWidget,
    );
    expect(find.text('リンク先を確認'), findsOneWidget);
    expect(find.text('example.com'), findsOneWidget);
    expect(find.text('https://example.com/path?item=1'), findsOneWidget);
    expect(find.bySemanticsLabel('リンク先の確認: example.com'), findsOneWidget);
    expect(find.text('キャンセル'), findsOneWidget);
    expect(find.text('リンクをコピー'), findsOneWidget);
    expect(find.text('開く'), findsOneWidget);

    await tester.tap(find.text('開く'));
    await tester.pumpAndSettle();
    expect(launcher.opened, Uri.parse('https://example.com/path?item=1'));
  });

  testWidgets('copy keeps the app in place and copies the disclosed URL', (
    tester,
  ) async {
    final launcher = _FakeUrlLauncher();
    final clipboard = _FakeClipboard();
    await tester.pumpWidget(
      _wrap(
        markdown: '[Copy target](https://example.com/a?redirect=/next)',
        launcher: launcher,
        clipboard: clipboard,
      ),
    );

    await tester.tap(find.text('Copy target'));
    await tester.pumpAndSettle();
    expect(find.textContaining('別のページへ移動'), findsOneWidget);

    await tester.tap(find.text('リンクをコピー'));
    await tester.pumpAndSettle();

    expect(launcher.opened, isNull);
    expect(clipboard.copied, 'https://example.com/a?redirect=/next');
    expect(find.text('リンクをコピーしました。'), findsOneWidget);
  });

  testWidgets('blocked schemes never reach the platform launcher', (
    tester,
  ) async {
    final launcher = _FakeUrlLauncher();
    await tester.pumpWidget(
      _wrap(
        markdown: '[Open file](file:///private/secret)',
        launcher: launcher,
      ),
    );

    await tester.tap(find.text('Open file'));
    await tester.pump();

    expect(launcher.opened, isNull);
    expect(find.byType(AlertDialog), findsNothing);
    expect(find.text('この種類のリンクは安全のため開けません。'), findsOneWidget);
  });

  testWidgets('launcher failure is explained without losing the message', (
    tester,
  ) async {
    final launcher = _FakeUrlLauncher(result: false);
    await tester.pumpWidget(
      _wrap(
        markdown: '[Unavailable](https://example.com/failure)',
        launcher: launcher,
      ),
    );

    await tester.tap(find.text('Unavailable'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('開く'));
    await tester.pumpAndSettle();

    expect(find.text('Unavailable'), findsOneWidget);
    expect(find.textContaining('リンクを開けませんでした。'), findsOneWidget);
  });

  testWidgets('launcher exceptions are redacted and explained', (
    tester,
  ) async {
    final launcher = _FakeUrlLauncher(error: StateError('secret transport'));
    await tester.pumpWidget(
      _wrap(
        markdown: '[Broken launcher](https://example.com/exception)',
        launcher: launcher,
      ),
    );

    await tester.tap(find.text('Broken launcher'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('開く'));
    await tester.pumpAndSettle();

    expect(find.textContaining('リンクを開けませんでした。'), findsOneWidget);
    expect(find.textContaining('secret transport'), findsNothing);
  });

  testWidgets('link preview remains usable at mobile width and large text', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(320, 568));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final launcher = _FakeUrlLauncher();
    await tester.pumpWidget(
      MediaQuery(
        data: const MediaQueryData(
          size: Size(320, 568),
          textScaler: TextScaler.linear(1.6),
        ),
        child: _wrap(
          markdown:
              '[Long destination](https://example.com/a/very/long/path/that/must/remain/selectable?next=https%3A%2F%2Fother.example)',
          launcher: launcher,
        ),
      ),
    );

    final link = find.text('Long destination');
    await tester.ensureVisible(link);
    await tester.tapAt(tester.getTopLeft(link) + const Offset(12, 12));
    await tester.pumpAndSettle();

    expect(find.text('リンク先を確認'), findsOneWidget);
    expect(find.text('リンクをコピー'), findsOneWidget);
    expect(find.text('開く'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
