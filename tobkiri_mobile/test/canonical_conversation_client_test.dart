import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/chat/canonical_conversation_client.dart';
import 'package:rumi_remote_app/src/chat/chat_draft_store.dart';
import 'package:rumi_remote_app/src/mobile_authority.dart';

void main() {
  const connection = MobileChatConnection(
    baseUrl: 'https://pc.example.test:8765',
    deviceId: 'device-1',
    token: 'device-token-for-test',
    scopes: {'chat.read', 'chat.write'},
  );

  test('chat connection requires exact read and write scopes', () {
    expect(connection.isValid, isTrue);
    expect(
      const MobileChatConnection(
        baseUrl: 'https://pc.example.test',
        deviceId: 'device-1',
        token: 'token',
        scopes: {'chat.read'},
      ).isValid,
      isFalse,
    );
    expect(
      const MobileChatConnection(
        baseUrl: 'https://user:password@pc.example.test?token=unsafe',
        deviceId: 'device-1',
        token: 'token',
        scopes: {'chat.read', 'chat.write'},
      ).isValid,
      isFalse,
    );
    expect(
      const MobileChatConnection(
        baseUrl: 'https://pc.example.test',
        deviceId: 'device-1',
        token: 'token',
        scopes: {'chat.read', 'chat.write', 'terminal.execute'},
      ).isValid,
      isFalse,
    );
  });

  test('chat connection storage is read-back verified', () async {
    final secrets = _MemorySecrets();
    final store = MobileChatConnectionStore(storage: secrets);
    await store.saveVerified(connection);
    final loaded = await store.load();
    expect(loaded?.baseUrl, connection.baseUrl);
    expect(loaded?.deviceId, connection.deviceId);
    expect(loaded?.scopes, mobileChatScopes);

    secrets.corruptWrites = true;
    await expectLater(
      store.saveVerified(connection),
      throwsA(isA<StateError>()),
    );
    expect(await store.load(), isNull);
  });

  test('protected draft storage isolates route scopes and deletes empties',
      () async {
    final secrets = _MemorySecrets();
    final drafts = MobileChatDraftStore(storage: secrets);
    await drafts.save('pc:one', 'first draft');
    await drafts.save('pc:two', 'second draft');

    expect(await drafts.load('pc:one'), 'first draft');
    expect(await drafts.load('pc:two'), 'second draft');

    await drafts.save('pc:one', '');
    expect(await drafts.load('pc:one'), isEmpty);
    expect(await drafts.load('pc:two'), 'second draft');
  });

  test(
    'client uses only canonical conversation routes and safe payload',
    () async {
      final requests = <http.Request>[];
      final client = CanonicalConversationClient(
        connection: connection,
        client: MockClient((request) async {
          requests.add(request);
          expect(
            request.headers['authorization'],
            'Bearer ${connection.token}',
          );
          switch ('${request.method} ${request.url.path}') {
            case 'POST /api/mobile/v1/conversations':
              return http.Response(
                jsonEncode({
                  'status': 'ok',
                  'data': {'conversation_id': 'conversation-1'},
                }),
                200,
              );
            case 'GET /api/mobile/v1/conversations/conversation-1':
              return http.Response(
                jsonEncode({
                  'status': 'ok',
                  'data': {
                    'conversation': {'revision': 4},
                  },
                }),
                200,
              );
            case 'POST /api/mobile/v1/conversations/conversation-1/stream':
              final body = jsonDecode(request.body) as Map<String, dynamic>;
              expect(body.keys, {
                'message',
                'client_message_id',
                'idempotency_key',
                'expected_revision',
              });
              expect(body['idempotency_key'], 'message-1');
              expect(body['expected_revision'], 4);
              expect(body.toString(), isNot(contains('approved')));
              expect(body.toString(), isNot(contains('yolo')));
              expect(body.toString(), isNot(contains('allow_shell')));
              expect(body.toString(), isNot(contains('allow_file_write')));
              return http.Response(
                'data: {"type":"user_message",'
                '"message":{"id":"message-1"}}\n\n'
                'data: {"type":"content_delta","delta":"hello"}\n\n'
                'data: [DONE]\n\n',
                200,
                headers: {'content-type': 'text/event-stream'},
              );
            case 'POST /api/mobile/v1/conversations/conversation-1/stop':
              return http.Response(
                jsonEncode({'status': 'ok', 'data': {}}),
                200,
              );
          }
          return http.Response('not found', 404);
        }),
      );

      final id = await client.createConversation();
      final revision = await client.revision(id);
      final updates = await client
          .send(
            conversationId: id,
            text: 'question',
            clientMessageId: 'message-1',
            expectedRevision: revision,
          )
          .toList();
      await client.stop(id);

      expect(id, 'conversation-1');
      expect(revision, 4);
      expect(updates.map((update) => update.kind), [
        CanonicalChatUpdateKind.accepted,
        CanonicalChatUpdateKind.delta,
        CanonicalChatUpdateKind.done,
      ]);
      expect(updates[1].content, 'hello');
      expect(requests.map((request) => request.url.path), [
        '/api/mobile/v1/conversations',
        '/api/mobile/v1/conversations/conversation-1',
        '/api/mobile/v1/conversations/conversation-1/stream',
        '/api/mobile/v1/conversations/conversation-1/stop',
      ]);
      client.close();
    },
  );

  test('client does not expose a failed response body', () async {
    final client = CanonicalConversationClient(
      connection: connection,
      client: MockClient(
        (_) async => http.Response('secret internal diagnostic', 500),
      ),
    );
    await expectLater(
      client.createConversation(),
      throwsA(
        isA<StateError>()
            .having((error) => error.toString(), 'message', contains('500'))
            .having(
              (error) => error.toString(),
              'redaction',
              isNot(contains('secret internal diagnostic')),
            ),
      ),
    );
    client.close();
  });
}

class _MemorySecrets implements AuthoritySecretStore {
  final values = <String, String>{};
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
