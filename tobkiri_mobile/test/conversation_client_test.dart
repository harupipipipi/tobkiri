import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/conversation/conversation_client.dart';
import 'package:rumi_remote_app/src/conversation/conversation_models.dart';
import 'package:rumi_remote_app/src/mobile_authority.dart';

class _MemorySecrets implements AuthoritySecretStore {
  final Map<String, String> values = {};
  bool corruptWrites = false;

  @override
  Future<void> delete(String key) async => values.remove(key);

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = corruptWrites ? '$value-corrupt' : value;
  }
}

const _connection = MobileConversationConnection(
  id: 'pc-1',
  label: 'Studio PC',
  baseUrl: 'https://pc.example:8765',
  deviceId: 'phone-1',
  token: 'dtk-test',
  scopes: {'chat.read', 'chat.write'},
);

void main() {
  test('conversation connection accepts only exact chat scopes and safe URLs',
      () {
    expect(_connection.isValid, isTrue);
    expect(
      MobileConversationConnection(
        id: _connection.id,
        label: _connection.label,
        baseUrl: 'https://user:pass@pc.example:8765',
        deviceId: _connection.deviceId,
        token: _connection.token,
        scopes: _connection.scopes,
      ).isValid,
      isFalse,
    );
    expect(
      MobileConversationConnection(
        id: _connection.id,
        label: _connection.label,
        baseUrl: 'https://pc.example:8765?token=leak',
        deviceId: _connection.deviceId,
        token: _connection.token,
        scopes: _connection.scopes,
      ).isValid,
      isFalse,
    );
    expect(
      MobileConversationConnection(
        id: _connection.id,
        label: _connection.label,
        baseUrl: _connection.baseUrl,
        deviceId: _connection.deviceId,
        token: _connection.token,
        scopes: const {'chat.read', 'chat.write', 'host.execute'},
      ).isValid,
      isFalse,
    );
  });

  test('secure connection store verifies exact persisted bytes', () async {
    final secrets = _MemorySecrets();
    final store = SecureConversationConnectionStore(storage: secrets);
    await store.saveVerified(const [_connection]);

    final loaded = await store.load();
    expect(loaded, hasLength(1));
    expect(loaded.single.id, _connection.id);
    expect(loaded.single.scopes, _connection.scopes);

    secrets.corruptWrites = true;
    await expectLater(
      store.saveVerified(const [_connection]),
      throwsA(isA<StateError>()),
    );
    expect(
      secrets.values[SecureConversationConnectionStore.storageKey],
      isNull,
    );
  });

  test('canonical client lists and creates through mobile scoped routes',
      () async {
    final requests = <http.Request>[];
    final client = MockClient((request) async {
      requests.add(request);
      if (request.method == 'GET' &&
          request.url.path.endsWith('conversations')) {
        return http.Response(
          jsonEncode({
            'status': 'ok',
            'data': {
              'conversations': [
                {
                  'id': 'c-1',
                  'title': 'First',
                  'message_count': 2,
                  'updated_at': '2026-08-24T00:00:00Z',
                  'pinned': true,
                },
              ],
            },
          }),
          200,
        );
      }
      return http.Response(
        jsonEncode({
          'status': 'ok',
          'data': {
            'conversation': {
              'id': 'c-2',
              'title': '',
              'messages': const [],
            },
          },
        }),
        200,
      );
    });
    final api = CanonicalConversationNavigationClient(
      connection: _connection,
      client: client,
    );

    final listed = await api.listConversations();
    final created = await api.createConversation();

    expect(listed.single.displayTitle, 'First');
    expect(created.summary.id, 'c-2');
    expect(requests.map((request) => request.url.path), [
      '/api/mobile/v1/conversations',
      '/api/mobile/v1/conversations',
    ]);
    expect(requests.map((request) => request.method), ['GET', 'POST']);
    expect(
      requests.every(
        (request) => request.headers['authorization'] == 'Bearer dtk-test',
      ),
      isTrue,
    );
    api.close();
  });

  test('canonical client does not expose response bodies in failures',
      () async {
    final api = CanonicalConversationNavigationClient(
      connection: _connection,
      client: MockClient(
        (_) async => http.Response('secret server details dtk-sensitive', 403),
      ),
    );

    await expectLater(
      api.listConversations(),
      throwsA(
        predicate(
          (error) =>
              error.toString().contains('403') &&
              !error.toString().contains('secret') &&
              !error.toString().contains('dtk-sensitive'),
        ),
      ),
    );
    api.close();
  });
}
