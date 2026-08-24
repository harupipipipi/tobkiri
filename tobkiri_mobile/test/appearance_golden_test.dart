import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/appearance_settings.dart';
import 'package:rumi_remote_app/src/app_theme.dart';
import 'package:rumi_remote_app/src/authority_approval_screen.dart';
import 'package:rumi_remote_app/src/mobile_authority.dart';
import 'package:rumi_remote_app/src/rumi_remote_home.dart';

import 'appearance_test_support.dart';

const _goldenKey = ValueKey<String>('appearance-golden-root');

class _CrossPlatformGoldenComparator extends LocalFileComparator {
  _CrossPlatformGoldenComparator(super.testFile);

  static const _maximumRasterizationDifference = 0.02;

  @override
  Future<bool> compare(Uint8List imageBytes, Uri golden) async {
    final result = await GoldenFileComparator.compareLists(
      imageBytes,
      await getGoldenBytes(golden),
    );
    final isCrossPlatformRasterizationOnly =
        result.diffPercent <= _maximumRasterizationDifference;
    result.dispose();
    if (isCrossPlatformRasterizationOnly) {
      return true;
    }
    return super.compare(imageBytes, golden);
  }
}

ThemeData _themeFor(Brightness brightness) => brightness == Brightness.light
    ? buildRumiLightTheme()
    : buildRumiDarkTheme();

Future<void> _pumpHome(
  WidgetTester tester, {
  required Brightness brightness,
  Future<http.Response> Function(http.Request request) response =
      healthyHomeResponse,
}) async {
  await tester.binding.setSurfaceSize(const Size(393, 852));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final storage = SecureStorageHarness(values: {...fixtureSettings});
  storage.install();
  addTearDown(storage.restore);

  await tester.pumpWidget(
    MaterialApp(
      theme: _themeFor(brightness),
      builder: (context, child) => RepaintBoundary(
        key: _goldenKey,
        child: child!,
      ),
      home: RumiRemoteHome(
        appearanceMode: AppearanceMode.system,
        onAppearanceChanged: (_) async {},
        settingsStore: storage.createSettingsStore(),
        clientFactory: (settings) => createMockApiClient(settings, response),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(seconds: 1));
}

Future<void> _pumpErrorHome(
  WidgetTester tester, {
  required Brightness brightness,
}) async {
  Future<http.Response> errorResponse(http.Request request) async {
    return jsonResponse(
      {
        'success': false,
        'error': 'Kernel API unavailable',
      },
      statusCode: 503,
    );
  }

  await _pumpHome(
    tester,
    brightness: brightness,
    response: errorResponse,
  );
}

class _MemorySecrets implements AuthoritySecretStore {
  final values = <String, String>{};

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async => values[key] = value;
}

MobileAuthorityConnection _approvalConnection() =>
    const MobileAuthorityConnection(
      baseUrl: 'https://pc.example.test',
      deviceId: 'device-1',
      approvalToken: 'approval-token',
      approvalScopes: mobileAuthorityScopes,
    );

Map<String, Object?> _approvalRequest() => {
      'request_id': 'request-1',
      'status': 'pending',
      'principal_id': 'agent-1',
      'permission_id': 'terminal.execute',
      'reason': 'Run a reviewed command',
      'risk_level': 'high',
      'resource': {'command': 'pwd'},
    };

Future<void> _pumpApproval(
  WidgetTester tester, {
  required Brightness brightness,
}) async {
  await tester.binding.setSurfaceSize(const Size(393, 852));
  addTearDown(() => tester.binding.setSurfaceSize(null));
  final store = MobileAuthorityConnectionStore(storage: _MemorySecrets());
  await store.saveVerified(_approvalConnection());
  final client = MockClient((request) async => jsonResponse({
        'success': true,
        'data': {
          'requests': [_approvalRequest()],
        },
      }));

  await tester.pumpWidget(
    MaterialApp(
      theme: _themeFor(brightness),
      builder: (context, child) => RepaintBoundary(
        key: _goldenKey,
        child: child!,
      ),
      home: AuthorityApprovalScreen(
        connectionStore: store,
        clientFactory: (connection) => MobileAuthorityClient(
          connection: connection,
          signer: _GoldenSigner(),
          client: client,
        ),
      ),
    ),
  );
  await tester.pump();
  await tester.pump(const Duration(seconds: 1));
}

class _GoldenSigner implements AuthorityPayloadSigner {
  @override
  Future<String> signPayloadHash(String payloadHash) async => 'signature';
}

void main() {
  final testFile = Uri.file(
    '${Directory.current.path}/test/appearance_golden_test.dart',
  );
  goldenFileComparator = _CrossPlatformGoldenComparator(testFile);

  for (final brightness in Brightness.values) {
    final suffix = brightness.name;

    testWidgets('core management navigation golden ($suffix)', (tester) async {
      await _pumpHome(tester, brightness: brightness);

      expect(find.text('Modules'), findsNWidgets(2));
      expect(find.text('Chat'), findsOneWidget);
      expect(find.text('Browser'), findsOneWidget);
      expect(find.text('Needs care'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await expectLater(
        find.byType(Overlay).last,
        matchesGoldenFile('goldens/core_management_navigation_$suffix.png'),
      );
    });

    testWidgets('settings sheet golden ($suffix)', (tester) async {
      await _pumpHome(tester, brightness: brightness);
      await tester.tap(find.byTooltip('Settings'));
      await tester.pump(const Duration(milliseconds: 500));

      expect(find.text('Appearance'), findsOneWidget);
      expect(find.text('System'), findsOneWidget);
      expect(find.text('Light'), findsOneWidget);
      expect(find.text('Dark'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await expectLater(
        find.byType(Overlay).last,
        matchesGoldenFile('goldens/settings_sheet_$suffix.png'),
      );
    });

    testWidgets('approval state golden ($suffix)', (tester) async {
      await _pumpApproval(tester, brightness: brightness);

      expect(find.text('Authority approvals'), findsOneWidget);
      expect(find.text('terminal.execute'), findsOneWidget);
      expect(find.text('Approve once'), findsOneWidget);
      expect(find.text('Deny'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await expectLater(
        find.byType(Overlay).last,
        matchesGoldenFile('goldens/approval_$suffix.png'),
      );
    });

    testWidgets('approval confirmation dialog golden ($suffix)',
        (tester) async {
      await _pumpApproval(tester, brightness: brightness);
      await tester.tap(find.text('Approve once'));
      await tester.pump(const Duration(milliseconds: 500));

      expect(find.text('Confirm high-impact action'), findsOneWidget);
      expect(find.text('Confirm and approve'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await expectLater(
        find.byKey(_goldenKey),
        matchesGoldenFile('goldens/approval_dialog_$suffix.png'),
      );
    });

    testWidgets('connection error state golden ($suffix)', (tester) async {
      await _pumpErrorHome(tester, brightness: brightness);

      expect(find.text('Kernel API unavailable'), findsOneWidget);
      expect(find.byIcon(Icons.error_outline), findsOneWidget);
      expect(tester.takeException(), isNull);
      await expectLater(
        find.byType(Overlay).last,
        matchesGoldenFile('goldens/error_$suffix.png'),
      );
    });
  }
}
