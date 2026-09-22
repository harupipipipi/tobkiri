import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/data/pc/pc_approval_client.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakeSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      _values.remove(key);
    } else {
      _values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

const _pc = PcConnection(
  baseUrl: 'http://192.168.1.10:8765',
  token: 'dtk-client',
  approvalToken: 'dtk-approver',
);

http.Response _ok(Map<String, dynamic> data) {
  return http.Response(
    jsonEncode({'status': 'ok', 'data': data}),
    200,
    headers: {'content-type': 'application/json'},
  );
}

void main() {
  test('listPending uses approval token and parses pending requests', () async {
    String? authHeader;
    String? requestedPath;
    final client = MockClient((request) async {
      authHeader = request.headers['Authorization'];
      requestedPath = request.url.path;
      return _ok({
        'pending': [
          {
            'request_id': 'auth_1',
            'status': 'pending',
            'principal_id': 'profile:work',
            'permission_id': 'model.invoke',
            'reason': 'model needs approval',
            'risk_level': 'medium',
            'resource': {'model_display_name': 'GPT'},
          },
        ],
      });
    });

    final approval = PcApprovalClient(
      client: client,
      deviceStore: MobileDeviceStore(storage: _FakeSecureStorage()),
    );
    final requests = await approval.listPending(_pc);
    approval.close();

    expect(authHeader, 'Bearer dtk-approver');
    expect(requestedPath, '/api/authority/requests');
    expect(requests.single.requestId, 'auth_1');
    expect(requests.single.summary, 'GPT');
  });

  test(
    'approve signs challenge payload hash before posting decision',
    () async {
      final storage = _FakeSecureStorage();
      final deviceStore = MobileDeviceStore(storage: storage);
      await deviceStore.loadOrCreateIdentity();
      final paths = <String>[];
      Map<String, dynamic>? approveBody;
      final payloadHash = List.filled(32, '00').join();
      final client = MockClient((request) async {
        paths.add(request.url.path);
        expect(request.headers['Authorization'], 'Bearer dtk-approver');
        if (request.url.path.endsWith('/challenge')) {
          return _ok({
            'challenge': {'challenge_id': 'ach_1'},
            'payload_hash': payloadHash,
            'signature_algorithm': 'ed25519',
          });
        }
        approveBody = jsonDecode(request.body) as Map<String, dynamic>;
        return _ok({
          'request_id': 'auth_1',
          'approved': true,
          'scope': 'once',
          'token': 'one-shot',
        });
      });

      final approval = PcApprovalClient(
        client: client,
        deviceStore: deviceStore,
      );
      final result = await approval.approve(
        _pc,
        const AuthorityRequestItem(
          requestId: 'auth_1',
          status: 'pending',
          principalId: 'profile:work',
          permissionId: 'model.invoke',
          reason: '',
          riskLevel: 'medium',
          resource: {},
        ),
      );
      approval.close();

      expect(paths, [
        '/api/authority/requests/auth_1/challenge',
        '/api/authority/requests/auth_1/approve',
      ]);
      expect(result.approved, isTrue);
      final attestation = approveBody?['attestation'] as Map<String, dynamic>;
      expect(attestation['challenge_id'], 'ach_1');
      expect(attestation['payload_hash'], payloadHash);
      expect(attestation['signature'], isNotEmpty);
    },
  );
}
