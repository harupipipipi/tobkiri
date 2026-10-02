import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:cryptography/cryptography.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/mobile_authority.dart';

class _MemorySecrets implements AuthoritySecretStore {
  final values = <String, String>{};
  bool corruptWrites = false;

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = corruptWrites ? 'corrupt' : value;
  }
}

class _Signer implements AuthorityPayloadSigner {
  String? payloadHash;

  @override
  Future<String> signPayloadHash(String payloadHash) async {
    this.payloadHash = payloadHash;
    return 'device-signature';
  }
}

MobileAuthorityConnection _connection({Set<String>? scopes}) =>
    MobileAuthorityConnection(
      baseUrl: 'https://pc.example.test/root',
      deviceId: 'device-1',
      approvalToken: 'approval-token',
      approvalScopes: scopes ?? mobileAuthorityScopes,
    );

Map<String, Object?> _requestJson({
  String id = 'req-1',
  String status = 'pending',
  String expiresAt = '2099-01-01T00:00:00Z',
}) =>
    {
      'request_id': id,
      'status': status,
      'principal_id': 'profile:profile-1:agent-1',
      'permission_id': 'terminal.execute',
      'reason': 'Run reviewed command',
      'risk_level': 'critical',
      'resource': {
        'command': 'pwd',
        'target_paths': ['/safe/workspace'],
      },
      'created_at': '2098-12-31T23:00:00Z',
      'expires_at': expiresAt,
      'conversation_id': 'conversation-1',
      'profile_id': 'profile-1',
      'node_id': 'node-1',
      'graph_id': 'graph-1',
      'allowed_scopes': ['once'],
      'display_metadata': {
        'title': 'Run guarded process',
        'summary': 'Execute pwd in the reviewed workspace.',
        'access_summary': 'pwd / /safe/workspace',
        'typed_confirmation_required': true,
        'confirmation_phrase': 'RUN PWD',
        'audit_text': 'The signed decision is recorded locally.',
      },
    };

AuthorityRequestItem _pending({String expiresAt = '2099-01-01T00:00:00Z'}) =>
    AuthorityRequestItem.fromJson(_requestJson(expiresAt: expiresAt));

http.Response _ok(Map<String, Object?> data) => http.Response(
      jsonEncode({'success': true, 'data': data}),
      200,
      headers: {'content-type': 'application/json'},
    );

Future<Map<String, Object?>> _challenge(String decision) async {
  final issuedAt = DateTime.now().toUtc();
  final payload = <String, Object?>{
    'approval_expires_in_seconds': 300,
    'challenge_id': 'challenge-1',
    'decision': decision,
    'device_id': 'device-1',
    'expires_at': issuedAt.add(const Duration(minutes: 5)).toIso8601String(),
    'issued_at': issuedAt.toIso8601String(),
    'nonce': 'nonce-1',
    'permission_id': 'terminal.execute',
    'profile_id': 'profile-1',
    'request_id': 'req-1',
    'resource_hash':
        '989edd5ff19f89cb30150220366afc43412aa949a5290304e9aa7c9a9f38b033',
    'scope': 'once',
    'token_id': 'token-1',
  };
  final digest = await Sha256().hash(utf8.encode(jsonEncode(payload)));
  final payloadHash = digest.bytes
      .map((value) => value.toRadixString(16).padLeft(2, '0'))
      .join();
  return {
    'request_id': 'req-1',
    'payload_hash': payloadHash,
    'challenge': payload,
  };
}

void main() {
  test('connection storage is read-back verified and exact-scope only',
      () async {
    final secrets = _MemorySecrets();
    final store = MobileAuthorityConnectionStore(storage: secrets);
    await store.saveVerified(_connection());
    expect((await store.load())!.approvalToken, 'approval-token');

    expect(_connection(scopes: {'authority.request.list'}).isValid, isFalse);
    expect(
      _connection(scopes: {...mobileAuthorityScopes, 'chat.read'}).isValid,
      isFalse,
    );
    expect(
      MobileAuthorityConnection(
        baseUrl: 'https://pc.example.test',
        deviceId: 'device-1',
        approvalToken: 'normal-session-token',
        approvalScopes: const {'chat.read'},
      ).isValid,
      isFalse,
    );

    secrets.corruptWrites = true;
    await expectLater(
      store.saveVerified(_connection()),
      throwsA(isA<StateError>()),
    );
    expect(secrets.values, isEmpty);
  });

  test('projects the complete backend presentation contract', () {
    final request = AuthorityRequestItem.fromJson(_requestJson());

    expect(request.title, 'Run guarded process');
    expect(request.consequence, 'Execute pwd in the reviewed workspace.');
    expect(request.target, 'pwd / /safe/workspace');
    expect(request.profileId, 'profile-1');
    expect(request.conversationId, 'conversation-1');
    expect(request.nodeId, 'node-1');
    expect(request.graphId, 'graph-1');
    expect(request.allowedScopes, ['once']);
    expect(request.typedConfirmationRequired, isTrue);
    expect(request.confirmationPhrase, 'RUN PWD');
    expect(request.scopeLabel, contains('one execution'));
    expect(request.persistenceLabel, contains('Not remembered'));
    expect(request.riskExplanation, contains('security-sensitive'));
  });

  test('list preserves valid siblings and reports invalid or duplicate rows',
      () async {
    late http.Request captured;
    final client = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async {
        captured = request;
        return _ok({
          'requests': [
            _requestJson(),
            {'request_id': 'broken', 'status': 'pending'},
            _requestJson(),
            _requestJson(id: 'req-settled', status: 'approved'),
            'not-a-map',
          ],
        });
      }),
    );

    final result = await client.listPendingWithDiagnostics();
    expect(result.requests.map((item) => item.requestId), ['req-1']);
    expect(result.invalidItemCount, 3);
    expect(result.isPartial, isTrue);
    expect(captured.url.path, '/root/api/authority/requests');
    expect(captured.url.queryParameters, {'status': 'pending'});
    expect(captured.headers['Authorization'], 'Bearer approval-token');
  });

  test('malformed rows fail closed without hiding valid siblings', () async {
    final client = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async => _ok({
            'requests': [
              _requestJson(),
              {
                'request_id': 42,
                'status': 'pending',
                'permission_id': 'file.write',
              },
              {
                ..._requestJson(id: 'req-bad-risk'),
                'risk_level': {'unexpected': true},
              },
            ],
          })),
    );

    final result = await client.listRequestsWithDiagnostics();
    expect(
      result.requests.map((item) => item.requestId),
      ['req-1', 'req-bad-risk'],
    );
    expect(result.requests.last.hasRequiredDecisionContext, isFalse);
    expect(result.requests.last.isHighImpact, isTrue);
    expect(result.invalidItemCount, 1);
  });

  test('refuses a request changed after the user reviewed it', () async {
    var calls = 0;
    final client = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async {
        calls++;
        return _ok({
          'request': {
            ..._requestJson(),
            'resource': {
              'command': 'rm -rf reviewed-subdirectory',
              'target_paths': ['/safe/workspace/reviewed-subdirectory'],
            },
          },
        });
      }),
    );

    await expectLater(
      client.approve(_pending(), confirmationText: 'RUN PWD'),
      throwsA(
        isA<AuthorityClientException>().having(
          (error) => error.kind,
          'kind',
          AuthorityFailureKind.stale,
        ),
      ),
    );
    expect(calls, 1);
  });

  for (final decision in ['approve', 'deny']) {
    test('$decision binds a fresh request, signed challenge, and exact result',
        () async {
      final requests = <http.Request>[];
      final signer = _Signer();
      var settled = false;
      final client = MobileAuthorityClient(
        connection: _connection(),
        signer: signer,
        client: MockClient((request) async {
          requests.add(request);
          if (request.method == 'GET') {
            return _ok({
              'request': _requestJson(
                status: settled
                    ? (decision == 'approve' ? 'approved' : 'denied')
                    : 'pending',
              ),
            });
          }
          if (request.url.path.endsWith('/challenge')) {
            return _ok(await _challenge(decision));
          }
          settled = true;
          return _ok({
            'request_id': 'req-1',
            if (decision == 'approve') 'approved': true,
            if (decision == 'deny') 'denied': true,
            if (decision == 'approve') 'scope': 'once',
            if (decision == 'approve') 'permission_id': 'terminal.execute',
            if (decision == 'approve') 'expires_at': '2099-01-01T00:05:00Z',
          });
        }),
      );

      if (decision == 'approve') {
        await client.approve(_pending(), confirmationText: 'RUN PWD');
      } else {
        await client.deny(_pending(), reason: 'Not expected');
      }

      expect(requests, hasLength(4));
      expect(signer.payloadHash, isNotEmpty);
      final challengeBody =
          jsonDecode(requests[1].body) as Map<String, dynamic>;
      expect(challengeBody, {'decision': decision, 'scope': 'once'});
      final settleBody = jsonDecode(requests[2].body) as Map<String, dynamic>;
      expect(settleBody['approved'], isNull);
      if (decision == 'approve') {
        expect(settleBody['scope'], 'once');
        expect(settleBody['config'], {'confirmation_text': 'RUN PWD'});
      } else {
        expect(settleBody['reason'], 'Not expected');
      }
      expect(settleBody['attestation'], {
        'challenge_id': 'challenge-1',
        'payload_hash': signer.payloadHash,
        'signature': 'device-signature',
        'signature_algorithm': 'ed25519',
      });
    });
  }

  test('fails closed on mismatched challenge or incomplete settlement',
      () async {
    var phase = 0;
    final client = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async {
        phase++;
        if (phase == 1) return _ok({'request': _requestJson()});
        return _ok({
          ...await _challenge('approve'),
          'request_id': 'different-request',
        });
      }),
    );

    await expectLater(
      client.approve(_pending(), confirmationText: 'RUN PWD'),
      throwsA(
        isA<AuthorityClientException>().having(
          (error) => error.kind,
          'kind',
          AuthorityFailureKind.malformedResponse,
        ),
      ),
    );
    expect(phase, 2);

    var settlementPhase = 0;
    final incomplete = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async {
        settlementPhase++;
        if (settlementPhase == 1) return _ok({'request': _requestJson()});
        if (settlementPhase == 2) return _ok(await _challenge('approve'));
        return _ok({'request_id': 'req-1'});
      }),
    );
    await expectLater(
      incomplete.approve(_pending(), confirmationText: 'RUN PWD'),
      throwsA(
        isA<AuthorityClientException>().having(
          (error) => error.kind,
          'kind',
          AuthorityFailureKind.malformedResponse,
        ),
      ),
    );
  });

  test('rejects non-pending and expired requests before network use', () async {
    var calls = 0;
    final client = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async {
        calls++;
        return _ok({});
      }),
    );
    final denied = AuthorityRequestItem.fromJson(
      _requestJson(status: 'denied'),
    );
    await expectLater(
      client.approve(denied),
      throwsA(
        isA<AuthorityClientException>().having(
          (error) => error.kind,
          'kind',
          AuthorityFailureKind.alreadySettled,
        ),
      ),
    );
    await expectLater(
      client.approve(_pending(expiresAt: '2000-01-01T00:00:00Z')),
      throwsA(
        isA<AuthorityClientException>().having(
          (error) => error.kind,
          'kind',
          AuthorityFailureKind.expired,
        ),
      ),
    );
    expect(calls, 0);
  });

  test('classifies settled, expired, stale, offline, and partial responses',
      () async {
    Future<AuthorityClientException> failureFor(http.Client client) async {
      final authority = MobileAuthorityClient(
        connection: _connection(),
        signer: _Signer(),
        client: client,
      );
      try {
        await authority.listPending();
        fail('request should fail');
      } on AuthorityClientException catch (error) {
        return error;
      }
    }

    final approved =
        await failureFor(MockClient((request) async => http.Response(
              jsonEncode({
                'success': false,
                'error': 'Authority request is approved',
              }),
              409,
            )));
    expect(approved.kind, AuthorityFailureKind.alreadySettled);
    expect(approved.settledStatus, 'approved');

    final expired =
        await failureFor(MockClient((request) async => http.Response(
              jsonEncode(
                  {'success': false, 'error': 'Authority request expired'}),
              409,
            )));
    expect(expired.kind, AuthorityFailureKind.expired);

    final stale = await failureFor(MockClient((request) async => http.Response(
          jsonEncode({'success': false, 'error': 'private missing detail'}),
          404,
        )));
    expect(stale.kind, AuthorityFailureKind.stale);
    expect(stale.toString(), isNot(contains('private missing detail')));

    final offline = await failureFor(
      MockClient((request) async => throw http.ClientException('private host')),
    );
    expect(offline.kind, AuthorityFailureKind.offline);
    expect(offline.toString(), isNot(contains('private host')));

    final malformed = await failureFor(
      MockClient((request) async => http.Response('not-json-secret', 200)),
    );
    expect(malformed.kind, AuthorityFailureKind.malformedResponse);
    expect(malformed.toString(), isNot(contains('not-json-secret')));

    final nestedError =
        await failureFor(MockClient((request) async => http.Response(
              jsonEncode({
                'success': false,
                'error': {
                  'code': 'expired',
                  'message': 'secret backend detail',
                },
              }),
              409,
            )));
    expect(nestedError.kind, AuthorityFailureKind.expired);
    expect(nestedError.toString(), isNot(contains('secret backend detail')));
  });

  test('server error never exposes response body or approval token', () async {
    final client = MobileAuthorityClient(
      connection: _connection(),
      signer: _Signer(),
      client: MockClient((request) async => http.Response(
            jsonEncode({'error': 'secret-body-value'}),
            500,
          )),
    );
    await expectLater(
      client.listPending(),
      throwsA(
        isA<AuthorityClientException>()
            .having(
              (error) => error.kind,
              'kind',
              AuthorityFailureKind.server,
            )
            .having(
              (error) => error.toString(),
              'redacted body',
              isNot(contains('secret-body-value')),
            )
            .having(
              (error) => error.toString(),
              'redacted token',
              isNot(contains('approval-token')),
            ),
      ),
    );
  });
}
