import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;

import 'package:rumi_remote_app/src/data/pc/pc_catalog.dart';
import 'package:rumi_remote_app/src/data/pc/pc_catalog_client.dart';
import 'package:rumi_remote_app/src/pc_control_models.dart';
import 'package:rumi_remote_app/src/pc_control_state.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

const _pc = PcConnection(baseUrl: 'http://192.168.1.10:8765', token: 'tok');

http.Response _ok(Map<String, dynamic> data) {
  return http.Response(
    jsonEncode({'status': 'ok', 'data': data}),
    200,
    headers: {'content-type': 'application/json'},
  );
}

void main() {
  group('PcCatalog model parsing', () {
    test('PcBootstrap.fromJson parses server + capabilities', () {
      final b = PcBootstrap.fromJson({
        'server': {'device_id': 'mac', 'label': 'MacBook', 'version': '1.2'},
        'capabilities': {
          'chat': true,
          'tools': true,
          'approvals': false,
          'credential_transfer': true,
        },
        'cursor': 'event-9',
      });
      expect(b.deviceId, 'mac');
      expect(b.label, 'MacBook');
      expect(b.capabilities.chat, isTrue);
      expect(b.capabilities.approvals, isFalse);
      expect(b.cursor, 'event-9');
    });

    test('PcCatalog.fromJson parses providers/models/profiles/templates', () {
      final catalog = PcCatalog.fromJson({
        'providers': [
          {
            'provider_id': 'openai',
            'display_name': 'OpenAI',
            'kind': 'cloud',
            'configured': true,
            'openai_compatible': true,
            'local': false,
            'catalog_only': false,
            'default_model': 'gpt-5.4',
            'default_base_url': 'https://api.openai.com/v1',
            'default_model_for': {'chat': 'gpt-5.4'},
            'capabilities': ['chat', 'tool_calls'],
            'env_vars': ['OPENAI_API_KEY'],
            'base_url_envs': ['OPENAI_BASE_URL'],
            'configured_api_count': 1,
          },
        ],
        'models': [
          {
            'id': 'openai/gpt-5.4',
            'provider_id': 'openai',
            'model_id': 'gpt-5.4',
            'display_name': 'GPT 5.4',
            'type': 'chat',
            'enabled': true,
            'max_context': 128000,
            'supports_thinking': true,
            'supports_vision': true,
            'supports_tool_calling': true,
            'thinking_levels': ['low', 'medium', 'high'],
            'default_thinking_level': 'medium',
            'speed_tier': 'fast',
            'cost_tier': 'medium',
            'capability_tags': ['thinking', 'vision'],
          },
        ],
        'profiles': [],
        'templates': [
          {
            'entry_id': 't1',
            'name': '要約',
            'description': 'テキスト要約',
            'source_type': 'prompt',
            'tags': ['writing'],
            'updated_at': '2026-01-01T00:00:00Z',
          },
        ],
        'tools': [
          {
            'tool_id': 'web_search',
            'service_id': 'web',
            'name': 'Web Search',
            'summary': 'Search the web',
            'tags': ['web', 'mobile-compatible'],
            'mobile_compatible': true,
            'execution_location': 'pc',
            'mobile': {
              'compatible': true,
              'available': true,
              'execution_location': 'pc',
            },
          },
          {
            'tool_id': 'desktop_input',
            'service_id': 'computer',
            'name': 'Desktop Input',
            'tags': ['desktop'],
            'mobile_compatible': false,
            'mobile_unavailable_reason': 'PC側のhost runtimeが必要です',
          },
        ],
        'runtime': {
          'preferred_model': 'openai/gpt-5.4',
          'thinking_level': 'high',
          'deepthink_enabled': true,
          'favorite_profiles': ['openai/gpt-5.4'],
        },
        'commands': [
          {
            'id': 'model',
            'name': 'model',
            'label': 'Model',
            'category': 'model',
            'visibility': 'default',
            'risk': 'low',
            'args': [
              {'name': 'query', 'type': 'string'},
            ],
            'execution': {'type': 'model_command', 'action': 'select'},
          },
        ],
      });
      expect(catalog.providers.length, 1);
      expect(catalog.providers.first.providerId, 'openai');
      expect(catalog.providers.first.configured, isTrue);
      expect(
        catalog.providers.first.defaultBaseUrl,
        'https://api.openai.com/v1',
      );
      expect(catalog.providers.first.defaultModelFor['chat'], 'gpt-5.4');
      expect(catalog.models.first.modelId, 'gpt-5.4');
      expect(catalog.models.first.maxContext, 128000);
      expect(catalog.templates.first.name, '要約');
      expect(catalog.modelsForProvider('openai').length, 1);
      expect(catalog.modelsForProvider('groq'), isEmpty);
      expect(catalog.configuredProviders.length, 1);
      expect(catalog.runtime.preferredModel, 'openai/gpt-5.4');
      expect(catalog.runtime.deepthinkEnabled, isTrue);
      expect(catalog.commands.single.name, 'model');
      expect(catalog.commands.single.args.single.name, 'query');
      expect(catalog.tools.length, 2);
      expect(catalog.mobileCompatibleTools.single.toolId, 'web_search');
      expect(catalog.mobileCompatibleTools.single.hasMobileTag, isTrue);
      expect(catalog.tools.last.mobileUnavailableReason, contains('host'));
    });

    test('PcMobileManifest drops authority routes from mobile route list', () {
      final manifest = PcMobileManifest.fromJson({
        'kind': 'rumi_mobile_manifest_v1',
        'version': 1,
        'routes': [
          {'method': 'GET', 'path': '/api/mobile/v1/bootstrap'},
          {'method': 'POST', 'path': '/api/authority/requests/a1/approve'},
        ],
        'authority_routes': [
          {'method': 'POST', 'path': '/api/authority/requests/a1/approve'},
        ],
      });

      expect(manifest.kind, 'rumi_mobile_manifest_v1');
      expect(
        manifest.routes.map((r) => r.path),
        contains('/api/mobile/v1/bootstrap'),
      );
      expect(
        manifest.routes.any((r) => r.path.startsWith('/api/authority/')),
        isFalse,
      );
      expect(manifest.authorityRoutes, isEmpty);
    });
  });

  group('PcCatalogClient', () {
    test('fetchBootstrap calls /api/mobile/v1/bootstrap with bearer', () async {
      String? authHeader;
      String? requestedPath;
      final client = MockClient((request) async {
        authHeader = request.headers['Authorization'];
        requestedPath = request.url.path;
        return _ok({
          'server': {'device_id': 'mac', 'label': 'MacBook', 'version': '1'},
          'capabilities': {
            'chat': true,
            'tools': true,
            'approvals': true,
            'credential_transfer': false,
          },
          'cursor': 'event-0',
        });
      });

      final pcClient = PcCatalogClient(client: client);
      final b = await pcClient.fetchBootstrap(_pc);
      pcClient.close();

      expect(authHeader, 'Bearer tok');
      expect(requestedPath, '/api/mobile/v1/bootstrap');
      expect(b.label, 'MacBook');
      expect(b.capabilities.chat, isTrue);
    });

    test(
      'fetchMobileManifest calls /api/mobile/v1/manifest with client token',
      () async {
        String? authHeader;
        String? requestedPath;
        final client = MockClient((request) async {
          authHeader = request.headers['Authorization'];
          requestedPath = request.url.path;
          return _ok({
            'kind': 'rumi_mobile_manifest_v1',
            'version': 1,
            'routes': [
              {'method': 'GET', 'path': '/api/mobile/v1/bootstrap'},
            ],
            'authority_routes': [],
          });
        });

        final pcClient = PcCatalogClient(client: client);
        final manifest = await pcClient.fetchMobileManifest(_pc);
        pcClient.close();

        expect(authHeader, 'Bearer tok');
        expect(requestedPath, '/api/mobile/v1/manifest');
        expect(manifest.routes.single.path, '/api/mobile/v1/bootstrap');
      },
    );

    test('fetchCapabilities passes provider filter as query param', () async {
      Map<String, String>? query;
      final client = MockClient((request) async {
        query = request.url.queryParameters;
        return _ok({
          'providers': [],
          'models': [],
          'profiles': [],
          'templates': [],
        });
      });

      final pcClient = PcCatalogClient(client: client);
      await pcClient.fetchCapabilities(
        _pc,
        providerFilter: 'openai',
        includeTemplates: false,
      );
      pcClient.close();

      expect(query?['provider'], 'openai');
      expect(query?['include_templates'], 'false');
    });

    test('fetchCapabilities returns catalog from response data', () async {
      final client = MockClient((request) async {
        return _ok({
          'providers': [
            {
              'provider_id': 'groq',
              'display_name': 'Groq',
              'configured': false,
            },
          ],
          'models': [
            {
              'id': 'groq/m1',
              'provider_id': 'groq',
              'model_id': 'm1',
              'display_name': 'M1',
            },
          ],
          'profiles': [],
          'templates': [],
        });
      });

      final pcClient = PcCatalogClient(client: client);
      final catalog = await pcClient.fetchCapabilities(_pc);
      pcClient.close();

      expect(catalog.providers.first.providerId, 'groq');
      expect(catalog.models.first.modelId, 'm1');
    });

    test('executeCommand posts to mobile command bridge', () async {
      String? requestedPath;
      Map<String, dynamic>? body;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        body = jsonDecode(request.body) as Map<String, dynamic>;
        return _ok({
          'command': {
            'id': 'model',
            'name': 'model',
            'label': 'Model',
            'category': 'model',
            'visibility': 'default',
            'risk': 'low',
            'execution': {'type': 'model_command', 'action': 'select'},
          },
          'executed': true,
          'selected_model': {'profile_id': 'openai/gpt-5.4'},
        });
      });

      final pcClient = PcCatalogClient(client: client);
      final result = await pcClient.executeCommand(
        _pc,
        command: 'model',
        args: {'query': 'gpt'},
        conversationId: 'c1',
      );
      pcClient.close();

      expect(requestedPath, '/api/mobile/v1/commands/execute');
      expect(body?['command'], 'model');
      expect((body?['args'] as Map)['query'], 'gpt');
      expect(body?['conversation_id'], 'c1');
      expect(result.executed, isTrue);
      expect(result.selectedModel?.effectiveProfileId, 'openai/gpt-5.4');
    });

    test('runtime control query uses the scoped mobile state facade', () async {
      String? requestedPath;
      Map<String, dynamic>? body;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        body = jsonDecode(request.body) as Map<String, dynamic>;
        return _ok({
          'snapshot_id': 'snapshot-4',
          'snapshot_revision': 4,
          'states': [
            {
              'state_ref': pcThinkingLevelStateRef,
              'value': 'high',
              'revision': 4,
              'freshness': 'authoritative',
            },
          ],
        });
      });

      final pcClient = PcCatalogClient(client: client);
      final snapshot = await pcClient.fetchControlSnapshot(_pc, {
        pcThinkingLevelStateRef,
      });
      pcClient.close();

      expect(requestedPath, '/api/mobile/v1/control-states/query');
      expect(body?['state_refs'], [pcThinkingLevelStateRef]);
      expect(snapshot.snapshotRevision, 4);
      expect(snapshot.states[pcThinkingLevelStateRef]?.value, 'high');
    });

    test(
      'runtime control invoke carries concurrency identity and parses state',
      () async {
        String? requestedPath;
        Map<String, dynamic>? body;
        final client = MockClient((request) async {
          requestedPath = request.url.path;
          body = jsonDecode(request.body) as Map<String, dynamic>;
          return _ok({
            'status': 'succeeded',
            'operation_id': 'mobile-control-1',
            'command_ref': 'defaultspack:think',
            'client_sequence': 9,
            'state_changes': [
              {
                'state_ref': pcThinkingLevelStateRef,
                'value': 'xhigh',
                'revision': 5,
                'freshness': 'authoritative',
              },
            ],
          });
        });
        final request = PcControlRequest(
          definition: pcControlDefinitions.firstWhere(
            (definition) => definition.id == 'thinking',
          ),
          value: 'xhigh',
          invocationId: 'mobile-control-1',
          clientSequence: 9,
          expectedRevision: 4,
          idempotencyKey: 'mobile-control-1',
        );

        final pcClient = PcCatalogClient(client: client);
        final result = await pcClient.invokeControlCommand(
          _pc,
          request,
          conversationId: 'conversation-1',
        );
        pcClient.close();

        expect(requestedPath, '/api/mobile/v1/control-commands/invoke');
        expect(body?['command_ref'], 'defaultspack:think');
        expect(body?['client_sequence'], 9);
        expect(body?['expected_revision'], 4);
        expect(body?['idempotency_key'], 'mobile-control-1');
        expect(body?['conversation_id'], 'conversation-1');
        expect(result.disposition, PcCommandDisposition.accepted);
        expect(result.stateChanges.single.value, 'xhigh');
      },
    );

    test('invokeTool posts to mobile tool invoke bridge', () async {
      String? requestedPath;
      Map<String, dynamic>? body;
      final client = MockClient((request) async {
        requestedPath = request.url.path;
        body = jsonDecode(request.body) as Map<String, dynamic>;
        return _ok({
          'tool_name': 'python_exec',
          'result': '1',
          'is_error': false,
          'permission': {'allowed': true},
        });
      });

      final pcClient = PcCatalogClient(client: client);
      final result = await pcClient.invokeTool(
        _pc,
        toolName: 'python_exec',
        arguments: {'code': 'print(1)'},
      );
      pcClient.close();

      expect(requestedPath, '/api/mobile/v1/tools/invoke');
      expect(body?['tool_name'], 'python_exec');
      expect((body?['arguments'] as Map)['code'], 'print(1)');
      expect(result['tool_name'], 'python_exec');
      expect(result['result'], '1');
    });

    test('throws on non-ok status', () async {
      final client = MockClient((request) async {
        return http.Response(
          jsonEncode({
            'status': 'error',
            'error': {'message': 'bad'},
          }),
          500,
        );
      });
      final pcClient = PcCatalogClient(client: client);
      expect(
        () => pcClient.fetchBootstrap(_pc),
        throwsA(isA<PcCatalogFetchException>()),
      );
    });

    test('throws when pc not configured', () async {
      final client = MockClient((request) async => _ok({}));
      final pcClient = PcCatalogClient(client: client);
      expect(
        () =>
            pcClient.fetchBootstrap(const PcConnection(baseUrl: '', token: '')),
        throwsA(isA<PcCatalogFetchException>()),
      );
    });
  });
}
