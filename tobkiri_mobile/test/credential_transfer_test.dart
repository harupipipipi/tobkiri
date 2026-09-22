import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:rumi_remote_app/src/data/pc/credential_transfer_client.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

const _pc = PcConnection(baseUrl: 'http://192.168.1.10:8765', token: 'tok');

http.Response _ok(Map<String, dynamic> data) {
  return http.Response(jsonEncode({'status': 'ok', 'data': data}), 200,
      headers: {'content-type': 'application/json'});
}

void main() {
  group('CredentialTransfer', () {
    test('parses from json', () {
      final t = CredentialTransfer.fromJson({
        'transfer_id': 't1',
        'status': 'pending',
        'ciphertext': 'encrypted',
        'nonce': 'nonce-1',
        'algorithm': 'x25519-aes-gcm',
        'label': 'Main',
      });
      expect(t.transferId, 't1');
      expect(t.isPending, isTrue);
      expect(t.ciphertext, 'encrypted');
      expect(t.nonce, 'nonce-1');
      expect(t.algorithm, 'x25519-aes-gcm');
      expect(t.label, 'Main');
    });

    test('isCompleted for completed status', () {
      final t = CredentialTransfer.fromJson({
        'transfer_id': 't1',
        'status': 'completed',
      });
      expect(t.isCompleted, isTrue);
      expect(t.isPending, isFalse);
    });
  });

  group('CredentialTransferClient', () {
    test('getTransfer fetches transfer by id', () async {
      String? requestedPath;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        return _ok({
          'transfer': {
            'transfer_id': 't1',
            'status': 'completed',
            'ciphertext': 'encrypted',
          },
        });
      });

      final ctClient = CredentialTransferClient(client: client);
      final transfer = await ctClient.getTransfer(_pc, transferId: 't1');
      ctClient.close();

      expect(requestedPath, '/api/mobile/v1/credential-transfers/t1');
      expect(transfer.transferId, 't1');
      expect(transfer.isCompleted, isTrue);
      expect(transfer.ciphertext, 'encrypted');
    });

    test('ackTransfer sends POST to ack endpoint', () async {
      String? requestedPath;
      String? authHeader;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        authHeader = request.headers['Authorization'];
        return _ok({});
      });

      final ctClient = CredentialTransferClient(client: client);
      await ctClient.ackTransfer(_pc, transferId: 't1');
      ctClient.close();

      expect(requestedPath, '/api/mobile/v1/credential-transfers/t1/ack');
      expect(authHeader, 'Bearer tok');
    });

    test('throws on non-ok response', () async {
      final client = MockClient((request) async {
        return http.Response(
            jsonEncode({
              'status': 'error',
              'error': {'message': 'not found'}
            }),
            404);
      });

      final ctClient = CredentialTransferClient(client: client);
      expect(
        () => ctClient.getTransfer(_pc, transferId: 't1'),
        throwsA(isA<CredentialTransferException>()),
      );
      ctClient.close();
    });
  });
}
