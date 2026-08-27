import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/accessibility/motion_policy.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/composer_bar.dart';
import 'package:rumi_remote_app/src/chat/message_view.dart';

void main() {
  Widget reducedMotion(Widget child) {
    return MediaQuery(
      data: const MediaQueryData(disableAnimations: true),
      child: MaterialApp(home: Scaffold(body: child)),
    );
  }

  testWidgets('shared motion policy follows the platform accessibility flag',
      (tester) async {
    late BuildContext capturedContext;
    await tester.pumpWidget(reducedMotion(Builder(builder: (context) {
      capturedContext = context;
      return const SizedBox();
    })));

    expect(motionAllowedOf(capturedContext), isFalse);
    expect(
      motionDurationOf(capturedContext, const Duration(milliseconds: 150)),
      Duration.zero,
    );
  });

  testWidgets('typing status stays visible without a looping animation',
      (tester) async {
    await tester.pumpWidget(reducedMotion(MessageView(
      message: ChatMessage(
        id: 'pending',
        role: ChatRole.assistant,
        content: '',
        createdAt: DateTime(2026),
        pending: true,
      ),
    )));
    await tester.pumpAndSettle();

    expect(find.text('処理中...'), findsOneWidget);
    expect(
      find.descendant(
        of: find.byType(MessageView),
        matching: find.byType(FadeTransition),
      ),
      findsNothing,
    );
    expect(tester.hasRunningAnimations, isFalse);
  });

  testWidgets('composer state transition becomes instant', (tester) async {
    await tester.pumpWidget(reducedMotion(ComposerBar(
      busy: false,
      onAdd: () {},
      onSend: (_) {},
      onStop: () {},
    )));

    final container = tester.widget<AnimatedContainer>(
      find.byType(AnimatedContainer),
    );
    expect(container.duration, Duration.zero);
  });
}
