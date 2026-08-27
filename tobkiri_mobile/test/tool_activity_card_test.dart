import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/domain/chat_event.dart';
import 'package:rumi_remote_app/src/domain/conversation_locator.dart';
import 'package:rumi_remote_app/src/features/tools/tool_activity_card.dart';

void main() {
  final locator = ConversationLocator.local('activity-test');

  Widget wrap(
    Widget child, {
    double textScale = 1,
    bool disableAnimations = false,
  }) {
    return MaterialApp(
      theme: buildRumiTheme(dark: true),
      home: MediaQuery(
        data: MediaQueryData(
          size: const Size(320, 640),
          textScaler: TextScaler.linear(textScale),
          disableAnimations: disableAnimations,
        ),
        child: Scaffold(
          body: SingleChildScrollView(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: child,
            ),
          ),
        ),
      ),
    );
  }

  ToolCallEvent event({
    String toolName = 'unknown_internal_tool_v9',
    String status = 'running',
    Map<String, dynamic> arguments = const {},
    String? summary,
    String? output,
    String? error,
    DateTime? startedAt,
    DateTime? endedAt,
    Duration? duration,
  }) {
    return ToolCallEvent(
      locator: locator,
      runId: 'run-1',
      toolId: 'tool-1',
      toolName: toolName,
      status: status,
      arguments: arguments,
      summary: summary,
      output: output,
      error: error,
      startedAt: startedAt,
      endedAt: endedAt,
      duration: duration,
    );
  }

  testWidgets('running activity has localized semantics and supported stop',
      (tester) async {
    final semantics = tester.ensureSemantics();
    var stopped = false;
    await tester.pumpWidget(wrap(ToolActivityCard(
      event: event(
        arguments: const {
          'path': '/workspace/src/main.dart',
          'api_key': 'secret-value',
        },
        summary: '対象ファイルを確認しています',
        startedAt: DateTime(2026, 8, 27, 12, 30),
      ),
      onStop: () => stopped = true,
    )));

    expect(find.text('ツール操作'), findsOneWidget);
    expect(find.text('実行中'), findsOneWidget);
    expect(find.textContaining('/workspace/src/main.dart'), findsOneWidget);
    expect(find.textContaining('unknown_internal_tool_v9'), findsNothing);
    expect(find.textContaining('secret-value'), findsNothing);
    expect(
      find.bySemanticsLabel(RegExp('ツール操作.*実行中.*対象')),
      findsOneWidget,
    );
    expect(
      find.bySemanticsLabel('開く: ツール操作、実行中'),
      findsOneWidget,
    );

    await tester.tap(find.text('ツール操作'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 250));
    expect(find.textContaining('開始 12:30:00'), findsOneWidget);
    expect(find.text('ツール操作を停止'), findsOneWidget);
    await tester.ensureVisible(find.text('ツール操作を停止'));
    await tester.pump();
    await tester.tap(find.text('ツール操作を停止'));
    expect(stopped, isTrue);
    semantics.dispose();
  });

  testWidgets('completed long output is expandable and copyable',
      (tester) async {
    final longSummary = List.filled(40, '長い説明').join(' ');
    await tester.pumpWidget(wrap(ToolActivityCard(
      event: event(
        toolName: 'file_write',
        status: 'completed',
        arguments: const {'output_path': '/workspace/report.txt'},
        summary: longSummary,
        output: 'report.txt を作成しました',
        startedAt: DateTime(2026, 8, 27, 12, 30),
        endedAt: DateTime(2026, 8, 27, 12, 30, 3),
        duration: const Duration(seconds: 3),
      ),
    )));

    expect(find.text('完了'), findsOneWidget);
    final collapsed = tester.widget<Text>(find.text(longSummary));
    expect(collapsed.maxLines, 2);

    await tester.tap(find.text('ファイル書き込み'));
    await tester.pumpAndSettle();
    expect(find.text('結果 / 出力'), findsOneWidget);
    expect(find.text('report.txt を作成しました'), findsOneWidget);
    expect(find.textContaining('終了 12:30:03'), findsOneWidget);
    expect(find.textContaining('所要 3秒'), findsOneWidget);
    expect(find.text('ファイル書き込みの詳細をコピー'), findsOneWidget);
  });

  testWidgets('failed activity redacts cause and offers safe recovery',
      (tester) async {
    await tester.pumpWidget(wrap(ToolActivityCard(
      event: event(
        toolName: 'terminal',
        status: 'failed',
        error: 'Authorization: Bearer abc.def.ghi token=top-secret failed',
        output: 'sk-supersecretvalue',
      ),
    )));
    await tester.tap(find.text('ターミナル操作'));
    await tester.pumpAndSettle();

    expect(find.text('失敗'), findsWidgets);
    expect(find.text('安全な原因'), findsOneWidget);
    expect(find.textContaining('[redacted]'), findsWidgets);
    expect(find.textContaining('abc.def.ghi'), findsNothing);
    expect(find.textContaining('top-secret'), findsNothing);
    expect(find.textContaining('自動Retryは行いません'), findsOneWidget);
    expect(find.textContaining('再試行'), findsNothing);
  });

  testWidgets('cancel and interruption states are text, not icon only',
      (tester) async {
    await tester.pumpWidget(wrap(Column(
      children: [
        ToolActivityCard(event: event(status: 'cancel_requested')),
        ToolActivityCard(event: event(status: 'cancelled')),
        ToolActivityCard(event: event(status: 'interrupted')),
        ToolActivityCard(event: event(status: 'approval_required')),
        ToolActivityCard(
          event: event(
            status: 'cancel_failed',
            error: '停止要求を完了できませんでした。',
          ),
        ),
      ],
    )));

    expect(find.text('停止を要求中'), findsOneWidget);
    expect(find.text('停止済み'), findsOneWidget);
    expect(find.text('中断 / 一部完了'), findsOneWidget);
    expect(find.text('承認待ち'), findsOneWidget);
    expect(find.text('失敗'), findsOneWidget);
    expect(find.textContaining('を停止'), findsNothing);
  });

  testWidgets('narrow large text and reduced motion keep details inspectable',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(320, 640));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(wrap(
      ToolActivityCard(
        event: event(
          toolName: 'browser',
          status: 'completed',
          arguments: const {
            'url': 'https://example.com/path?token=secret',
          },
          summary: List.filled(20, '長いローカライズ文').join(' '),
          output: List.filled(20, '完全な出力').join('\n'),
        ),
      ),
      textScale: 2,
      disableAnimations: true,
    ));
    await tester.tap(find.text('ブラウザ操作'));
    await tester.pump();

    expect(find.textContaining('https://example.com/path'), findsWidgets);
    expect(find.textContaining('token=secret'), findsNothing);
    expect(find.text('結果 / 出力'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  test('safe snapshot round-trips through chat history', () {
    final message = ChatMessage(
      id: 'assistant-1',
      role: ChatRole.assistant,
      content: 'done',
      toolActivities: [
        ToolActivitySnapshot(
          toolId: 'tool-1',
          toolName: 'file_read',
          status: 'completed',
          arguments: const {'path': '/workspace/readme.md'},
          summary: 'READMEを確認',
          startedAt: DateTime.utc(2026, 8, 27, 1),
          endedAt: DateTime.utc(2026, 8, 27, 1, 0, 2),
          duration: const Duration(seconds: 2),
        ),
      ],
    );

    final restored = ChatMessage.fromJson(message.toJson());
    expect(restored.toolActivities, hasLength(1));
    expect(restored.toolActivities.single.status, 'completed');
    expect(restored.toolActivities.single.arguments['path'],
        '/workspace/readme.md');
    expect(restored.toolActivities.single.duration, const Duration(seconds: 2));
  });

  test('safe arguments omit secrets and strip URL credentials', () {
    final safe = safeToolActivityArguments(const {
      'path': '/workspace/file.txt',
      'url': 'https://user:pass@example.com/path?token=secret#fragment',
      'command': 'echo secret',
      'api_key': 'sk-secretvalue',
    });

    expect(safe['path'], '/workspace/file.txt');
    expect(safe['url'], 'https://example.com/path');
    expect(safe, isNot(contains('command')));
    expect(safe, isNot(contains('api_key')));
  });
}
