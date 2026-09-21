import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/chat/model_selection_screen.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

void main() {
  Widget wrap(Widget child) => MaterialApp(
        theme: buildRumiTheme(dark: true),
        home: child,
      );

  testWidgets('model selection page sorts by ABC and filters by search',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    final providers = [
      const MobileProviderConfig(
        providerId: 'openai',
        displayName: 'OpenAI',
        label: 'Zebra',
        apiKey: 'sk-openai',
        baseUrl: 'https://api.openai.com/v1',
        model: 'gpt-5.4',
      ),
      const MobileProviderConfig(
        providerId: 'anthropic',
        displayName: 'Anthropic',
        label: 'Alpha',
        apiKey: 'sk-anthropic',
        baseUrl: 'https://api.anthropic.com',
        model: 'claude-sonnet-4-0',
      ),
      const MobileProviderConfig(
        providerId: 'google',
        displayName: 'Google Gemini',
        label: 'Bravo',
        apiKey: 'sk-google',
        baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai',
        model: 'gemini-2.5-pro',
      ),
    ];

    await tester.pumpWidget(wrap(ModelSelectionScreen.local(
      providers: providers,
      activeModelId: 'gemini-2.5-pro',
      activeProviderId: 'google',
    )));
    await tester.pumpAndSettle();

    expect(find.text('モデルを選択'), findsOneWidget);
    expect(find.text('Alpha'), findsOneWidget);
    expect(find.text('Bravo'), findsOneWidget);
    expect(find.text('Zebra'), findsOneWidget);
    expect(
      tester.getTopLeft(find.text('Alpha')).dy,
      lessThan(tester.getTopLeft(find.text('Bravo')).dy),
    );
    expect(
      tester.getTopLeft(find.text('Bravo')).dy,
      lessThan(tester.getTopLeft(find.text('Zebra')).dy),
    );

    await tester.enterText(find.byType(TextField), 'gemini');
    await tester.pump();

    expect(find.text('Alpha'), findsNothing);
    expect(find.text('Bravo'), findsOneWidget);
    expect(find.text('Zebra'), findsNothing);
    expect(find.byIcon(Icons.check), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
