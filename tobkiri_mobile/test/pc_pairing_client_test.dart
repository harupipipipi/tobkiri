import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/data/pc/pc_pairing_client.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

const _pc = PcConnection(baseUrl: 'http://192.168.1.10:8765', token: 'tok');

http.Response _ok(Map<String, dynamic> data) {
  return http.Response(
    jsonEncode({'status': 'ok', 'data': data}),
    200,
    headers: {'content-type': 'application/json'},
  );
}

const _identity = DeviceIdentity(
  deviceId: 'mobile-abc123',
  deviceLabel: 'Rumi Mobile',
  publicKey: 'pk-test',
  encryptionPublicKey: 'x25519:test-public',
);

void main() {
  group('friendlyPcLabel', () {
    test('does not display raw http URL labels', () {
      expect(
        friendlyPcLabel(
          'http://192.168.11.25:8765',
          'http://192.168.11.25:8765',
        ),
        'PC',
      );
      expect(
        friendlyPcLabel('', 'http://haru-macbook.local:8765'),
        'haru-macbook',
      );
      expect(
        friendlyPcLabel('Haru MacBook', 'http://192.168.11.25:8765'),
        'Haru MacBook',
      );
    });
  });

  group('PairingClaimResponse', () {
    test('parses from json', () {
      final resp = PairingClaimResponse.fromJson({
        'pairing': {'pairing_id': 'p1', 'status': 'pending'},
      });
      expect(resp.pairingId, 'p1');
      expect(resp.status, 'pending');
    });

    test('claim verification code matches PC review fingerprint', () async {
      final code = await claimVerificationCode(
        pairingId: 'p1',
        device: _identity,
        requestedCapabilities: const ['chat.write', 'chat.read', 'chat.read'],
      );

      expect(code, 'CTL5-FIWE');
    });
  });

  group('PairingStatusResponse', () {
    test('isAccepted when status is accepted', () {
      final resp = PairingStatusResponse.fromJson({
        'pairing': {
          'pairing_id': 'p1',
          'status': 'accepted',
          'device_token': 'dt-xxx',
          'scopes': ['chat.read', 'chat.write'],
          'pc_label': 'MacBook',
        },
      });
      expect(resp.isAccepted, isTrue);
      expect(resp.isReady, isTrue);
      expect(resp.deviceToken, 'dt-xxx');
      expect(resp.scopes, ['chat.read', 'chat.write']);
      expect(resp.pcLabel, 'MacBook');
    });

    test('isAccepted false for pending', () {
      final resp = PairingStatusResponse.fromJson({
        'pairing': {'pairing_id': 'p1', 'status': 'pending'},
      });
      expect(resp.isAccepted, isFalse);
      expect(resp.isReady, isFalse);
      expect(resp.deviceToken, isNull);
    });

    test('accepted status is not ready without a device token', () {
      final resp = PairingStatusResponse.fromJson({
        'pairing': {'pairing_id': 'p1', 'status': 'approved'},
      });

      expect(resp.isAccepted, isTrue);
      expect(resp.hasDeviceToken, isFalse);
      expect(resp.isReady, isFalse);
    });

    test('accepted status is ready with encrypted token delivery envelope', () {
      final resp = PairingStatusResponse.fromJson({
        'pairing': {'pairing_id': 'p1', 'status': 'approved'},
        'token_delivery_envelope': {
          'version': 1,
          'delivery_id': 'tdv-test',
          'alg': 'X25519-HKDF-SHA256-AES-256-GCM',
        },
      });

      expect(resp.isAccepted, isTrue);
      expect(resp.hasDeviceToken, isFalse);
      expect(resp.hasTokenDeliveryEnvelope, isTrue);
      expect(resp.deliveryId, 'tdv-test');
      expect(resp.isReady, isTrue);
    });

    test('parses top-level pc label and token from status response', () {
      final resp = PairingStatusResponse.fromJson({
        'pairing': {'pairing_id': 'p1', 'status': 'approved'},
        'device_token': 'dtk-123',
        'approval_token': 'dtk-approve',
        'scopes': ['chat.read'],
        'approval_scopes': [
          'authority.request.approve',
          'authority.request.deny',
        ],
        'pc_label': 'Haru MacBook',
      });

      expect(resp.isAccepted, isTrue);
      expect(resp.isReady, isTrue);
      expect(resp.deviceToken, 'dtk-123');
      expect(resp.approvalToken, 'dtk-approve');
      expect(resp.hasApprovalToken, isTrue);
      expect(resp.pcLabel, 'Haru MacBook');
      expect(resp.scopes, ['chat.read']);
      expect(resp.approvalScopes, [
        'authority.request.approve',
        'authority.request.deny',
      ]);
    });

    test('parses client and approver token aliases', () {
      final resp = PairingStatusResponse.fromJson({
        'pairing': {'pairing_id': 'p1', 'status': 'approved'},
        'client_access_token': 'dtk-client',
        'approver_access_token': 'dtk-approver',
        'scopes': ['chat.read'],
        'approval_scopes': ['authority.request.approve'],
      });

      expect(resp.deviceToken, 'dtk-client');
      expect(resp.approvalToken, 'dtk-approver');
      expect(resp.hasApprovalToken, isTrue);
    });
  });

  group('PcPairingClient', () {
    test('claim sends POST to /api/mobile/v1/pairings/{id}/claim', () async {
      String? authHeader;
      String? requestBody;
      final client = MockClient((request) async {
        authHeader = request.headers['Authorization'];
        requestBody = request.body;
        return _ok({
          'pairing': {'pairing_id': 'p1', 'status': 'pending'},
        });
      });

      final pairingClient = PcPairingClient(client: client);
      final resp = await pairingClient.claim(
        _pc,
        pairingId: 'p1',
        code: 'abc123',
        device: _identity,
        requestedCapabilities: const ['chat.read', 'chat.write'],
      );
      pairingClient.close();

      expect(authHeader, 'Bearer tok');
      expect(resp.pairingId, 'p1');
      expect(resp.status, 'pending');

      final body = jsonDecode(requestBody!) as Map<String, dynamic>;
      expect(body['code'], 'abc123');
      expect(body['device_id'], 'mobile-abc123');
      expect(body['device_encryption_public_key'], 'x25519:test-public');
      expect(body['requested_capabilities'], ['chat.read', 'chat.write']);
    });

    test(
      'pollStatus sends GET without pickup secrets',
      () async {
        String? requestedPath;
        Map<String, String>? requestedQuery;
        final client = MockClient((request) async {
          requestedPath = request.url.path;
          requestedQuery = request.url.queryParameters;
          return _ok({
            'pairing': {
              'pairing_id': 'p1',
              'status': 'accepted',
              'device_token': 'dt-123',
              'scopes': ['chat.read'],
            },
          });
        });

        final pairingClient = PcPairingClient(client: client);
        final resp = await pairingClient.pollStatus(
          _pc,
          pairingId: 'p1',
        );
        pairingClient.close();

        expect(requestedPath, '/api/mobile/v1/pairings/p1/status');
        expect(requestedQuery?.containsKey('pickup_secret'), isFalse);
        expect(requestedQuery?.containsKey('device_id'), isFalse);
        expect(resp.isAccepted, isTrue);
        expect(resp.deviceToken, 'dt-123');
      },
    );

    test('pickupTokenDelivery posts pickup secret in request body', () async {
      String? requestedPath;
      Map<String, dynamic>? body;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        body = jsonDecode(request.body) as Map<String, dynamic>;
        return _ok({
          'pairing': {'pairing_id': 'p1', 'status': 'approved'},
          'token_delivery_envelope': {
            'version': 1,
            'delivery_id': 'tdv-test',
            'alg': 'X25519-HKDF-SHA256-AES-256-GCM',
          },
        });
      });

      final pairingClient = PcPairingClient(client: client);
      final resp = await pairingClient.pickupTokenDelivery(
        _pc,
        pairingId: 'p1',
        pickupSecret: 'pup_123',
        deviceId: 'mobile-abc123',
      );
      pairingClient.close();

      expect(requestedPath, '/api/mobile/v1/pairings/p1/token/pickup');
      expect(body?['pickup_secret'], 'pup_123');
      expect(body?['device_id'], 'mobile-abc123');
      expect(resp.hasTokenDeliveryEnvelope, isTrue);
      expect(resp.deliveryId, 'tdv-test');
    });

    test('ackTokenDelivery posts to token ack route', () async {
      String? requestedPath;
      Map<String, dynamic>? body;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        body = jsonDecode(request.body) as Map<String, dynamic>;
        return _ok({
          'pairing': {'pairing_id': 'p1', 'status': 'approved'},
        });
      });

      final pairingClient = PcPairingClient(client: client);
      await pairingClient.ackTokenDelivery(
        _pc,
        pairingId: 'p1',
        pickupSecret: 'pup_123',
        deviceId: 'mobile-abc123',
        deliveryId: 'tdv-test',
      );
      pairingClient.close();

      expect(requestedPath, '/api/mobile/v1/pairings/p1/token/ack');
      expect(body?['pickup_secret'], 'pup_123');
      expect(body?['device_id'], 'mobile-abc123');
      expect(body?['delivery_id'], 'tdv-test');
    });

    test('throws PcPairingException on non-ok response', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({
            'status': 'error',
            'error': {'message': 'not found'},
          }),
          404,
        );
      });

      final pairingClient = PcPairingClient(client: client);
      expect(
        () => pairingClient.pollStatus(_pc, pairingId: 'p1'),
        throwsA(isA<PcPairingException>()),
      );
      pairingClient.close();
    });

    test('throws when pc not configured', () async {
      final client = MockClient((request) async => _ok({}));
      final pairingClient = PcPairingClient(client: client);
      expect(
        () => pairingClient.claim(
          const PcConnection(baseUrl: '', token: ''),
          pairingId: 'p1',
          code: 'abc',
          device: _identity,
          requestedCapabilities: const [],
        ),
        throwsA(isA<PcPairingException>()),
      );
      pairingClient.close();
    });
  });
}
