import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/composer_bar.dart';
import 'package:rumi_remote_app/src/chat/message_view.dart';

Widget _app(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('messages announce author state and content exactly once', (
    tester,
  ) async {
    final semantics = tester.ensureSemantics();
    await tester.binding.setSurfaceSize(const Size(900, 1800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final messages = [
      ChatMessage(
        id: 'user',
        role: ChatRole.user,
        content: 'こんにちは',
      ),
      ChatMessage(
        id: 'assistant',
        role: ChatRole.assistant,
        content: 'ようこそ',
      ),
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
      ChatMessage(
        id: 'empty',
        role: ChatRole.assistant,
        content: '',
      ),
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

    String label(String id) => tester
        .getSemantics(find.byKey(ValueKey('message-semantics:$id')))
        .label;

    expect(label('user'), 'あなたのメッセージ, 内容: こんにちは');
    expect(label('assistant'), 'Tobkiriの応答, 内容: ようこそ');
    expect(label('pending-empty'), 'Tobkiriの応答, 処理中, 内容なし');
    expect(label('pending-content'), 'Tobkiriの応答, 処理中, 内容: 途中');
    expect(
      label('error-content'),
      'Tobkiriの応答, 送信に失敗しました, 内容: 再試行してください',
    );
    expect(
      label('error-empty'),
      'Tobkiriの応答, 送信に失敗しました, 内容なし',
    );
    expect(label('empty'), 'Tobkiriの応答, 内容なし');
    expect(find.text('送信に失敗しました'), findsNWidgets(2));
    expect(find.byIcon(Icons.error_outline), findsNWidgets(2));
    expect(find.text('処理中...'), findsNWidgets(2));
    expect(find.bySemanticsLabel(RegExp('こんにちは')), findsOneWidget);
    expect(find.bySemanticsLabel(RegExp('ようこそ')), findsOneWidget);
    semantics.dispose();
  });

  testWidgets('composer exposes ordered 48 pixel named actions', (
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
          onSend: sent.add,
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
    expect(sent, ['送信テスト']);

    await tester.pumpWidget(
      _app(
        ComposerBar(
          busy: true,
          onSend: sent.add,
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

  testWidgets('copy is contextual and safe links have named 48 pixel actions', (
    tester,
  ) async {
    Uri? opened;
    String? clipboardText;
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      SystemChannels.platform,
      (call) async {
        if (call.method == 'Clipboard.setData') {
          clipboardText = (call.arguments as Map)['text'] as String?;
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
    final message = ChatMessage(
      id: 'actions',
      role: ChatRole.assistant,
      content:
          '詳しくは https://example.com/docs を確認。 [unsafe](javascript:alert(1))',
    );
    await tester.pumpWidget(
      _app(
        MessageView(
          message: message,
          openLink: (uri) async => opened = uri,
        ),
      ),
    );

    expect(find.byKey(const ValueKey('message-copy')), findsNothing);
    final link = find.byKey(
      const ValueKey('message-link:https://example.com/docs'),
    );
    expect(tester.getSize(link), const Size(48, 48));
    expect(
      tester.getSemantics(link).label,
      'リンク: https://example.com/docs',
    );
    expect(find.byKey(const ValueKey('message-link:javascript:alert(1)')),
        findsNothing);
    await tester.tap(link);
    expect(opened, Uri.parse('https://example.com/docs'));

    await tester.longPress(
      find.byKey(const ValueKey('message-semantics:actions')),
    );
    await tester.pump();
    final copy = find.byKey(const ValueKey('message-copy'));
    expect(copy, findsOneWidget);
    expect(tester.getSize(copy), const Size(48, 48));
    expect(tester.getSemantics(copy).label, 'メッセージをコピー');
    await tester.tap(copy);
    await tester.pump();
    expect(clipboardText, message.content);
  });
}
