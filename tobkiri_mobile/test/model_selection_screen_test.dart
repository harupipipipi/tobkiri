import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/chat/model_selection_screen.dart';
import 'package:rumi_remote_app/src/data/pc/pc_catalog.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

void main() {
  Widget wrap(Widget child) => MaterialApp(
        theme: buildRumiTheme(dark: true),
        home: child,
      );

  ProfileEntry profile({
    required String id,
    required String model,
    bool configured = true,
    bool local = false,
    bool requiresApiKey = false,
    bool vision = false,
    bool tools = false,
    bool thinking = false,
  }) =>
      ProfileEntry(
        profileId: id,
        providerId: 'openai',
        modelId: model,
        displayName: model,
        qualifiedModelId: 'openai/$model',
        providerDisplayName: 'OpenAI',
        label: model,
        type: 'chat',
        configured: configured,
        local: local,
        requiresApiKey: requiresApiKey,
        favorite: false,
        maxContext: 128000,
        supportsThinking: thinking,
        supportsVision: vision,
        supportsToolCalling: tools,
        thinkingLevels: const <String>[],
        defaultThinkingLevel: null,
        speedTier: 'balanced',
        costTier: 'unknown',
        capabilityTags: const <String>[],
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
    expect(find.byIcon(Icons.check_circle), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
      'PC model options expose search count, selected state, and disabled reason',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    await tester.pumpWidget(
      wrap(
        ModelSelectionScreen.pc(
          profiles: [
            profile(
              id: 'ready',
              model: 'ready-model',
              vision: true,
              tools: true,
            ),
            profile(
              id: 'needs-key',
              model: 'needs-key-model',
              configured: false,
              requiresApiKey: true,
            ),
          ],
          activeModelId: 'ready',
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('モデルを検索'), findsOneWidget);
    expect(find.text('2件のモデル'), findsOneWidget);
    final selectedSemantics = tester.widget<Semantics>(
      find.byKey(const ValueKey('model-option-ready')),
    );
    expect(selectedSemantics.properties.selected, isTrue);
    expect(find.textContaining('画像・ツール'), findsOneWidget);
    expect(find.textContaining('API設定が必要です'), findsOneWidget);

    await tester.tap(find.text('needs-key-model'));
    await tester.pumpAndSettle();

    expect(find.text('needs-key-model は利用できません'), findsOneWidget);
    expect(find.text('OpenAI のAPI設定が必要です'), findsOneWidget);
    expect(find.text('設定を開く'), findsOneWidget);
  });

  testWidgets('empty catalog can refresh without losing the selection screen',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    var refreshCount = 0;
    await tester.pumpWidget(
      wrap(
        ModelSelectionScreen.pc(
          profiles: const <ProfileEntry>[],
          activeModelId: '',
          onRefreshPcProfiles: () async {
            refreshCount += 1;
            return [profile(id: 'refreshed', model: 'refreshed-model')];
          },
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('0件のモデル'), findsOneWidget);
    expect(find.text('選べるPCモデルがありません'), findsOneWidget);
    await tester.tap(find.text('再取得'));
    await tester.pumpAndSettle();

    expect(refreshCount, 1);
    expect(find.text('1件のモデル'), findsOneWidget);
    expect(find.text('refreshed-model'), findsOneWidget);
  });

  testWidgets('custom model is provider-bound, validated, and reviewed',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    const provider = MobileProviderConfig(
      providerId: 'openai',
      displayName: 'OpenAI',
      label: 'Work OpenAI',
      apiKey: 'sk-openai',
      baseUrl: 'https://api.openai.com/v1',
      model: 'gpt-5.4',
    );
    await tester.pumpWidget(
      wrap(
        const ModelSelectionScreen.local(
          providers: [provider],
          activeModelId: 'gpt-5.4',
          activeProviderId: 'openai',
        ),
      ),
    );
    await tester.pumpAndSettle();

    await tester.tap(find.text('モデル名を直接入力'));
    await tester.pumpAndSettle();
    expect(find.text('プロバイダー: Work OpenAI'), findsOneWidget);

    await tester.enterText(find.byType(TextFormField), 'invalid model');
    await tester.tap(find.text('次へ'));
    await tester.pump();
    expect(find.textContaining('モデルIDとして有効な文字'), findsOneWidget);

    await tester.enterText(find.byType(TextFormField), 'org/model:revision');
    await tester.tap(find.text('次へ'));
    await tester.pumpAndSettle();

    expect(find.text('変更内容を確認'), findsOneWidget);
    expect(find.textContaining('モデルID: org/model:revision'), findsOneWidget);
    expect(find.textContaining('サーバー上の存在を確認できません'), findsOneWidget);
    expect(find.text('このモデルを選択'), findsOneWidget);
  });

  testWidgets('long model names remain usable with large text', (tester) async {
    await tester.binding.setSurfaceSize(const Size(393, 852));
    const longModel =
        'organization/very-long-model-name-with-revision-and-capability-suffix';
    await tester.pumpWidget(
      MaterialApp(
        theme: buildRumiTheme(dark: true),
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(
            textScaler: const TextScaler.linear(2),
          ),
          child: child!,
        ),
        home: ModelSelectionScreen.pc(
          profiles: [profile(id: 'long', model: longModel)],
          activeModelId: 'long',
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text(longModel), findsOneWidget);
    expect(find.text('1件のモデル'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
