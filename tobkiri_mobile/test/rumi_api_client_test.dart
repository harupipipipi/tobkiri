import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/models.dart';
import 'package:rumi_remote_app/src/rumi_api_client.dart';

void main() {
  test('normalizes host-only URLs', () {
    final uri = RumiApiClient.normalizeBaseUri('192.168.1.24:8765/');

    expect(uri.scheme, 'http');
    expect(uri.host, '192.168.1.24');
    expect(uri.port, 8765);
    expect(uri.path, '');
  });

  test('health uses /health and does not require auth', () async {
    late http.Request captured;
    final client = RumiApiClient(
      baseUrl: 'http://pc.local:8765',
      bearerToken: '',
      httpClient: MockClient((request) async {
        captured = request;
        return http.Response(
          jsonEncode({
            'status': 'healthy',
            'service': 'rumi',
            'timestamp': '2026-05-16T00:00:00Z',
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      }),
    );

    final health = await client.health();

    expect(captured.url.path, '/health');
    expect(captured.headers.containsKey('Authorization'), isFalse);
    expect(captured.headers['X-Rumi-Client'], 'rumi-mobile');
    expect(health.isHealthy, isTrue);
  });

  test('module list sends bearer token and parses success envelope', () async {
    late http.Request captured;
    final client = RumiApiClient(
      baseUrl: 'http://pc.local:8765',
      bearerToken: 'token-123',
      httpClient: MockClient((request) async {
        captured = request;
        return http.Response(
          jsonEncode({
            'success': true,
            'data': {
              'modules': [
                {
                  'module_id': 'chat',
                  'kind': 'backend',
                  'state': 'enabled',
                  'display_name': 'Chat',
                  'dependencies': ['ai_client'],
                },
              ],
            },
          }),
          200,
        );
      }),
    );

    final catalog = await client.listModules();

    expect(captured.url.path, '/api/defaultspack/modules');
    expect(captured.headers['Authorization'], 'Bearer token-123');
    expect(catalog.modules, hasLength(1));
    expect(catalog.modules.single.id, 'chat');
    expect(catalog.modules.single.dependencies, ['ai_client']);
  });

  test('module action posts action route and refreshes detail', () async {
    final paths = <String>[];
    final client = RumiApiClient(
      baseUrl: 'http://pc.local:8765/root',
      bearerToken: 'token-123',
      httpClient: MockClient((request) async {
        paths.add(request.url.path);
        if (request.method == 'POST') {
          return http.Response(
            jsonEncode({
              'success': true,
              'data': {
                'module_id': 'chat',
                'state': 'disabled',
                'updated': true,
              },
            }),
            200,
          );
        }
        return http.Response(
          jsonEncode({
            'success': true,
            'data': {
              'module_id': 'chat',
              'kind': 'backend',
              'state': 'disabled',
              'display_name': 'Chat',
            },
          }),
          200,
        );
      }),
    );

    final module = await client.moduleAction('chat', ModuleAction.disable);

    expect(paths, [
      '/root/api/defaultspack/modules/chat/disable',
      '/root/api/defaultspack/modules/chat',
    ]);
    expect(module.state, 'disabled');
  });

  test('marks one-tap footgun module actions as destructive', () {
    expect(ModuleAction.enable.destructive, isFalse);
    expect(ModuleAction.disable.destructive, isTrue);
    expect(ModuleAction.reload.destructive, isTrue);
    expect(ModuleAction.rollback.destructive, isTrue);
  });

  test('command protocol clients use only v1 routes and structured bodies',
      () async {
    final requests = <http.Request>[];
    final client = RumiApiClient(
      baseUrl: 'http://pc.local:8765',
      bearerToken: 'token-123',
      httpClient: MockClient((request) async {
        requests.add(request);
        return http.Response(
          jsonEncode({
            'status': 'ok',
            'data': <String, Object?>{'status': 'succeeded'},
          }),
          200,
        );
      }),
    );

    await client.commandCatalog();
    await client.invokeCommand(
      'defaultspack:terminal',
      args: const <String, Object?>{'cmd': 'python -V'},
      invocationId: 'mobile-invocation',
      mode: 'coding',
      profileId: 'mobile-profile',
      catalogRevision: 'catalog-1',
      expectedRevision: 7,
      idempotencyKey: 'mobile-idempotency-1',
      clientSequence: 3,
    );
    await client.resumeCommand(
      'defaultspack:terminal',
      'approval-token',
      args: const <String, Object?>{'cmd': 'python -V'},
      invocationId: 'mobile-invocation',
      mode: 'coding',
      profileId: 'mobile-profile',
      expectedRevision: 7,
      idempotencyKey: 'mobile-idempotency-1',
    );
    await client.commandInvocationEvents('mobile-invocation');
    await client.commandOfflineQueue(
      'pending',
      limit: 10,
    );

    expect(
      requests.map((request) => request.url.path),
      <String>[
        '/api/command-protocol/v1/catalog',
        '/api/command-protocol/v1/invoke',
        '/api/command-protocol/v1/resume',
        '/api/command-protocol/v1/invocations/events/query',
        '/api/command-protocol/v1/offline',
      ],
    );
    expect(
      jsonDecode(requests[1].body),
      containsPair('command_ref', 'defaultspack:terminal'),
    );
    final invokeBody = jsonDecode(requests[1].body) as Map<String, dynamic>;
    expect(invokeBody['mode'], 'coding');
    expect(invokeBody['expected_revision'], 7);
    expect(invokeBody['idempotency_key'], 'mobile-idempotency-1');
    final resumeBody = jsonDecode(requests[2].body) as Map<String, dynamic>;
    expect(resumeBody['invocation_id'], 'mobile-invocation');
    expect(resumeBody['approval_token'], 'approval-token');
    expect(
        requests
            .every((request) => !request.url.path.contains('/api/ui/commands')),
        isTrue);
  });

  test('throws RumiApiException for API error envelopes', () async {
    final client = RumiApiClient(
      baseUrl: 'http://pc.local:8765',
      bearerToken: 'token-123',
      httpClient: MockClient((request) async {
        return http.Response(
          jsonEncode({
            'success': false,
            'error': 'Unauthorized',
          }),
          401,
        );
      }),
    );

    expect(
      client.listModules(),
      throwsA(
        isA<RumiApiException>().having(
          (error) => error.message,
          'message',
          'Unauthorized',
        ),
      ),
    );
  });
}
