import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:cryptography/cryptography.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/authority_approval_screen.dart';
import 'package:rumi_remote_app/src/mobile_authority.dart';

class _MemorySecrets implements AuthoritySecretStore {
  final values = <String, String>{};

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async => values[key] = value;
}

class _TestSigner implements AuthorityPayloadSigner {
  @override
  Future<String> signPayloadHash(String payloadHash) async => 'signature';
}

MobileAuthorityConnection _connection() => MobileAuthorityConnection(
      baseUrl: 'https://pc.example.test',
      deviceId: 'phone-1',
      approvalToken: 'approval-token',
      approvalScopes: mobileAuthorityScopes,
    );

Map<String, Object?> _request({
  String id = 'req-1',
  String risk = 'high',
  String status = 'pending',
  String consequence = 'Run pwd in the reviewed workspace without truncation.',
  bool typed = false,
  String expiresAt = '2099-01-01T01:00:00Z',
  bool includeStringSecret = false,
}) =>
    {
      'request_id': id,
      'status': status,
      'principal_id': 'agent-1',
      'permission_id': 'terminal.execute',
      'reason': 'Continue the task requested in this conversation.',
      'risk_level': risk,
      'created_at': '2099-01-01T00:00:00Z',
      'expires_at': expiresAt,
      'profile_id': 'profile-1',
      'allowed_scopes': ['once'],
      'display_metadata': {
        'title': 'Run a reviewed terminal command',
        'summary': consequence,
        'access_summary': 'pwd in /safe/workspace',
        'typed_confirmation_required': typed,
        if (typed) 'confirmation_phrase': 'RUN PWD',
        'audit_text': 'The signed decision is recorded in the local audit log.',
      },
      'resource': {
        'command': 'pwd',
        'target_paths': ['/safe/workspace'],
        'approval_token': 'must-not-render',
        'nested': {
          'password': 'nested-secret',
          'safe': 'visible-after-expansion',
        },
        'url': 'https://user:pass@example.test/run?token=query-secret',
        if (includeStringSecret)
          'body': '{"password":"body-secret","safe":"visible-body"}',
        if (includeStringSecret) 'headers': 'X-API-Key: header-secret',
      },
    };

http.Response _ok(Map<String, Object?> data) => http.Response(
      jsonEncode({'success': true, 'data': data}),
      200,
      headers: {'content-type': 'application/json'},
    );

Future<Map<String, Object?>> _challenge(
  String requestId,
  String decision,
) async {
  final issuedAt = DateTime.now().toUtc();
  final payload = <String, Object?>{
    'approval_expires_in_seconds': 300,
    'challenge_id': 'challenge-$requestId',
    'decision': decision,
    'device_id': 'phone-1',
    'expires_at': issuedAt.add(const Duration(minutes: 5)).toIso8601String(),
    'issued_at': issuedAt.toIso8601String(),
    'nonce': 'nonce-$requestId',
    'permission_id': 'terminal.execute',
    'profile_id': 'profile-1',
    'request_id': requestId,
    'resource_hash':
        'd486d3b5cee4a7901cd9af6aba5465e2e0b6c40e9397b56da58149b13f83cea4',
    'scope': 'once',
    'token_id': 'token-1',
  };
  final digest = await Sha256().hash(utf8.encode(jsonEncode(payload)));
  final payloadHash = digest.bytes
      .map((value) => value.toRadixString(16).padLeft(2, '0'))
      .join();
  return {
    'request_id': requestId,
    'payload_hash': payloadHash,
    'challenge': payload,
  };
}

Future<MobileAuthorityConnectionStore> _pairedStore() async {
  final store = MobileAuthorityConnectionStore(storage: _MemorySecrets());
  await store.saveVerified(_connection());
  return store;
}

Future<void> _pump(
  WidgetTester tester, {
  required MobileAuthorityConnectionStore store,
  MockClient? httpClient,
  double textScale = 1,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      builder: (context, child) => MediaQuery(
        data: MediaQuery.of(context).copyWith(
          textScaler: TextScaler.linear(textScale),
        ),
        child: child!,
      ),
      home: AuthorityApprovalScreen(
        connectionStore: store,
        clientFactory: (connection) => MobileAuthorityClient(
          connection: connection,
          signer: _TestSigner(),
          client: httpClient ?? MockClient((request) async => _ok({})),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _scrollTo(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    300,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('fails closed when no paired approval connection exists',
      (tester) async {
    final store = MobileAuthorityConnectionStore(storage: _MemorySecrets());
    await _pump(tester, store: store);

    expect(find.text('Authority approvals'), findsOneWidget);
    expect(find.textContaining('Pair this device'), findsOneWidget);
    expect(find.text('Review and approve once'), findsNothing);
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('shows the full user-first contract and redacts nested details',
      (tester) async {
    const fullConsequence =
        'This deliberately long consequence remains complete because it is the only explanation of what the command changes and where it runs.';
    await _pump(
      tester,
      store: await _pairedStore(),
      httpClient: MockClient((request) async => _ok({
            'requests': [
              _request(
                consequence: fullConsequence,
                includeStringSecret: true,
              ),
            ],
          })),
    );

    expect(find.text(fullConsequence), findsOneWidget);
    expect(find.text('pwd in /safe/workspace'), findsOneWidget);
    expect(find.text('profile-1'), findsOneWidget);
    expect(find.text('phone-1'), findsOneWidget);
    expect(find.text('This request only (one execution)'), findsOneWidget);
    expect(find.textContaining('Not remembered'), findsOneWidget);
    expect(find.textContaining('must-not-render'), findsNothing);
    expect(find.textContaining('nested-secret'), findsNothing);
    expect(find.textContaining('query-secret'), findsNothing);
    expect(find.textContaining('body-secret'), findsNothing);
    expect(find.textContaining('header-secret'), findsNothing);

    await _scrollTo(
      tester,
      find.byKey(const ValueKey('technical-req-1')),
    );
    await tester.tap(find.byKey(const ValueKey('technical-req-1')));
    await tester.pumpAndSettle();
    expect(find.textContaining('visible-after-expansion'), findsOneWidget);
    expect(find.textContaining('[redacted]'), findsWidgets);
    expect(find.textContaining('must-not-render'), findsNothing);
    expect(find.textContaining('nested-secret'), findsNothing);
    expect(find.textContaining('query-secret'), findsNothing);
  });

  testWidgets('critical approval requires the exact phrase and remains settled',
      (tester) async {
    var settleCalls = 0;
    Map<String, dynamic>? settleBody;
    final item = _request(risk: 'critical', typed: true);
    var settled = false;
    final client = MockClient((request) async {
      if (request.method == 'GET' &&
          request.url.path == '/api/authority/requests') {
        return _ok({
          'requests': [item],
        });
      }
      if (request.method == 'GET') {
        return _ok({
          'request': {...item, 'status': settled ? 'approved' : 'pending'},
        });
      }
      if (request.url.path.endsWith('/challenge')) {
        return _ok(await _challenge('req-1', 'approve'));
      }
      settleCalls++;
      settled = true;
      settleBody = jsonDecode(request.body) as Map<String, dynamic>;
      return _ok({
        'request_id': 'req-1',
        'approved': true,
        'scope': 'once',
        'permission_id': 'terminal.execute',
        'expires_at': '2099-01-01T00:05:00Z',
      });
    });
    await _pump(tester, store: await _pairedStore(), httpClient: client);

    await _scrollTo(tester, find.text('Review and approve once'));
    await tester.tap(find.text('Review and approve once'));
    await tester.pumpAndSettle();
    expect(find.text('Type to confirm this critical action'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Confirm and approve'),
          )
          .onPressed,
      isNull,
    );

    await tester.enterText(
      find.byKey(const Key('typed-confirmation-field')),
      'wrong',
    );
    await tester.pump();
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Confirm and approve'),
          )
          .onPressed,
      isNull,
    );
    await tester.enterText(
      find.byKey(const Key('typed-confirmation-field')),
      'RUN PWD',
    );
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Confirm and approve'));
    await tester.pumpAndSettle();

    expect(settleCalls, 1);
    expect(settleBody?['scope'], 'once');
    expect(settleBody?['config'], {'confirmation_text': 'RUN PWD'});
    expect(find.text('APPROVED'), findsOneWidget);
    expect(find.textContaining('Approved once'), findsOneWidget);
    expect(find.text('Review and approve once'), findsNothing);
  });

  testWidgets('denial reason is optional and the denied status remains visible',
      (tester) async {
    String? sentReason;
    final item = _request(risk: 'low');
    var settled = false;
    final client = MockClient((request) async {
      if (request.method == 'GET' &&
          request.url.path == '/api/authority/requests') {
        return _ok({
          'requests': [item],
        });
      }
      if (request.method == 'GET') {
        return _ok({
          'request': {...item, 'status': settled ? 'denied' : 'pending'},
        });
      }
      if (request.url.path.endsWith('/challenge')) {
        return _ok(await _challenge('req-1', 'deny'));
      }
      settled = true;
      sentReason = (jsonDecode(request.body) as Map<String, dynamic>)['reason']
          as String?;
      return _ok({'request_id': 'req-1', 'denied': true});
    });
    await _pump(tester, store: await _pairedStore(), httpClient: client);

    await _scrollTo(tester, find.text('Deny with optional reason'));
    await tester.tap(find.text('Deny with optional reason'));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const Key('denial-reason-field')),
      '  Unexpected request  ',
    );
    await tester.tap(find.widgetWithText(FilledButton, 'Deny'));
    await tester.pumpAndSettle();

    expect(sentReason, 'Unexpected request');
    expect(find.text('DENIED'), findsOneWidget);
    expect(find.textContaining('Reason: Unexpected request'), findsOneWidget);
    expect(find.text('Deny with optional reason'), findsNothing);
  });

  testWidgets('keeps valid siblings when a list response is partial',
      (tester) async {
    await _pump(
      tester,
      store: await _pairedStore(),
      httpClient: MockClient((request) async => _ok({
            'requests': [
              _request(id: 'req-1', risk: 'low'),
              {'status': 'pending', 'permission_id': 'file.write'},
              _request(id: 'req-2', risk: 'medium'),
            ],
          })),
    );

    expect(find.textContaining('1 incomplete request'), findsOneWidget);
    expect(find.byKey(const ValueKey('req-1')), findsOneWidget);
    await tester.drag(
      find.byKey(const Key('authority-approval-list')),
      const Offset(0, -900),
    );
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('req-2')), findsOneWidget);
  });

  testWidgets('shows offline retry and stale settlement states in place',
      (tester) async {
    var listCalls = 0;
    final item = _request(risk: 'low');
    final client = MockClient((request) async {
      if (request.url.path == '/api/authority/requests') {
        listCalls++;
        if (listCalls == 1) throw http.ClientException('private network text');
        return _ok({
          'requests': [item],
        });
      }
      return http.Response(
        jsonEncode({
          'success': false,
          'error': 'Authority request is approved',
        }),
        409,
      );
    });
    await _pump(tester, store: await _pairedStore(), httpClient: client);

    expect(find.textContaining('offline or unreachable'), findsOneWidget);
    expect(find.textContaining('private network text'), findsNothing);
    await tester.tap(find.text('Retry'));
    await tester.pumpAndSettle();
    await _scrollTo(tester, find.text('Review and approve once'));
    await tester.tap(find.text('Review and approve once'));
    await tester.pumpAndSettle();

    expect(find.text('APPROVED'), findsOneWidget);
    expect(find.textContaining('already approved'), findsOneWidget);
    expect(find.text('Review and approve once'), findsNothing);
  });

  testWidgets('uppercase high risk still requires intentional review',
      (tester) async {
    await _pump(
      tester,
      store: await _pairedStore(),
      httpClient: MockClient((request) async => _ok({
            'requests': [_request(risk: 'HIGH')],
          })),
    );

    await _scrollTo(tester, find.text('Review and approve once'));
    await tester.tap(find.text('Review and approve once'));
    await tester.pumpAndSettle();
    expect(find.text('Review this high-impact action'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Confirm and approve'),
          )
          .onPressed,
      isNull,
    );
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
  });

  testWidgets('incomplete decision context disables both decisions',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(800, 1400));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final incomplete = _request();
    incomplete['profile_id'] = '';
    incomplete['expires_at'] = 'not-a-time';
    incomplete['resource'] = <String, Object?>{};
    incomplete['display_metadata'] = <String, Object?>{
      'title': 'Incomplete request',
    };
    await _pump(
      tester,
      store: await _pairedStore(),
      httpClient: MockClient((request) async => _ok({
            'requests': [incomplete],
          })),
    );

    await _scrollTo(tester, find.text('Review and approve once'));
    expect(
      tester
          .widget<ButtonStyleButton>(
            find
                .ancestor(
                  of: find.text('Review and approve once'),
                  matching: find.byWidgetPredicate(
                    (widget) => widget is ButtonStyleButton,
                  ),
                )
                .first,
          )
          .onPressed,
      isNull,
    );
    await _scrollTo(tester, find.text('Deny with optional reason'));
    expect(
      tester
          .widget<ButtonStyleButton>(
            find
                .ancestor(
                  of: find.text('Deny with optional reason'),
                  matching: find.byWidgetPredicate(
                    (widget) => widget is ButtonStyleButton,
                  ),
                )
                .first,
          )
          .onPressed,
      isNull,
    );
  });

  testWidgets('authoritative settled status survives screen recreation',
      (tester) async {
    final client = MockClient((request) async => _ok({
          'requests': [_request(status: 'approved')],
        }));
    final store = await _pairedStore();
    await _pump(tester, store: store, httpClient: client);
    expect(find.text('APPROVED'), findsOneWidget);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpAndSettle();
    await _pump(tester, store: store, httpClient: client);
    expect(find.text('APPROVED'), findsOneWidget);
    expect(find.text('Review and approve once'), findsNothing);
  });

  testWidgets('multiple pending requests settle independently', (tester) async {
    await tester.binding.setSurfaceSize(const Size(800, 2200));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final items = <String, Map<String, Object?>>{
      'req-1': _request(id: 'req-1', risk: 'low'),
      'req-2': _request(id: 'req-2', risk: 'low'),
    };
    final client = MockClient((request) async {
      if (request.method == 'GET' &&
          request.url.path == '/api/authority/requests') {
        return _ok({'requests': items.values.toList()});
      }
      final segments = request.url.pathSegments;
      final requestId = segments[segments.indexOf('requests') + 1];
      if (request.method == 'GET') {
        return _ok({'request': items[requestId]!});
      }
      if (request.url.path.endsWith('/challenge')) {
        final decision = (jsonDecode(request.body)
            as Map<String, dynamic>)['decision'] as String;
        return _ok(await _challenge(requestId, decision));
      }
      final approved = request.url.path.endsWith('/approve');
      items[requestId] = {
        ...items[requestId]!,
        'status': approved ? 'approved' : 'denied',
      };
      return _ok({
        'request_id': requestId,
        if (approved) 'approved': true else 'denied': true,
        if (approved) 'scope': 'once',
        if (approved) 'permission_id': 'terminal.execute',
        if (approved) 'expires_at': '2099-01-01T00:05:00Z',
      });
    });
    await _pump(tester, store: await _pairedStore(), httpClient: client);

    await tester.tap(find.text('Review and approve once').first);
    await tester.pumpAndSettle();
    expect(find.text('APPROVED'), findsOneWidget);

    await tester.tap(find.text('Deny with optional reason'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Deny'));
    await tester.pumpAndSettle();
    expect(find.text('DENIED'), findsOneWidget);
    expect(find.text('APPROVED'), findsOneWidget);
  });

  testWidgets('pending request expires while the screen remains open',
      (tester) async {
    final expiresAt = DateTime.now()
        .toUtc()
        .add(const Duration(milliseconds: 50))
        .toIso8601String();
    await _pump(
      tester,
      store: await _pairedStore(),
      httpClient: MockClient((request) async => _ok({
            'requests': [_request(risk: 'low', expiresAt: expiresAt)],
          })),
    );
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 100)),
    );
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pumpAndSettle();
    expect(find.text('EXPIRED'), findsOneWidget);
    expect(find.text('Review and approve once'), findsNothing);
  });

  testWidgets('exposes TalkBack and VoiceOver semantics for the decision card',
      (tester) async {
    final semantics = tester.ensureSemantics();
    await _pump(
      tester,
      store: await _pairedStore(),
      httpClient: MockClient((request) async => _ok({
            'requests': [_request()],
          })),
    );

    expect(
      find.bySemanticsLabel(
        RegExp(
          'Authority approval request: Run a reviewed terminal command.*Consequence: Run pwd.*Target: pwd in /safe/workspace',
        ),
      ),
      findsOneWidget,
    );
    expect(find.bySemanticsLabel('Review and approve once'), findsOneWidget);
    expect(find.bySemanticsLabel('Deny with optional reason'), findsOneWidget);
    semantics.dispose();
  });

  testWidgets(
      '320px layout with 200 percent text and long copy has no overflow',
      (tester) async {
    await tester.binding.setSurfaceSize(const Size(320, 568));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    final longCopy = List.filled(
      12,
      'A long consequence explains every affected resource and remains readable.',
    ).join(' ');
    await _pump(
      tester,
      store: await _pairedStore(),
      textScale: 2,
      httpClient: MockClient((request) async => _ok({
            'requests': [_request(consequence: longCopy)],
          })),
    );

    expect(find.text(longCopy), findsOneWidget);
    expect(tester.takeException(), isNull);
    await _scrollTo(tester, find.text('Review and approve once'));
    expect(tester.takeException(), isNull);
    expect(find.text('Review and approve once'), findsOneWidget);
  });
}
