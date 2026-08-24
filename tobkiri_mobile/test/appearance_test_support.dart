import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_secure_storage/test/test_flutter_secure_storage_platform.dart';
import 'package:flutter_secure_storage_platform_interface/flutter_secure_storage_platform_interface.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/rumi_api_client.dart';
import 'package:rumi_remote_app/src/secure_settings_store.dart';

class SecureStorageHarness {
  SecureStorageHarness({Map<String, String>? values})
      : values = values ?? <String, String>{};

  final Map<String, String> values;
  late FlutterSecureStoragePlatform _previous;

  void install() {
    _previous = FlutterSecureStoragePlatform.instance;
    FlutterSecureStoragePlatform.instance =
        TestFlutterSecureStoragePlatform(values);
  }

  void restore() {
    FlutterSecureStoragePlatform.instance = _previous;
  }

  SecureSettingsStore createSettingsStore() =>
      SecureSettingsStore(storage: const FlutterSecureStorage());
}

http.Response jsonResponse(Object? body, {int statusCode = 200}) =>
    http.Response(
      jsonEncode(body),
      statusCode,
      headers: const {'content-type': 'application/json'},
    );

RumiApiClient createMockApiClient(
  RumiRemoteSettings settings,
  Future<http.Response> Function(http.Request request) handler,
) {
  return RumiApiClient(
    baseUrl: settings.baseUrl,
    bearerToken: settings.token,
    httpClient: MockClient(handler),
  );
}

const fixtureSettings = <String, String>{
  'rumi_remote.base_url': 'http://pc.example.test:8765',
  'rumi_remote.token': 'fixture-token',
  'rumi_remote.auto_refresh': 'false',
};

const fixtureModules = <Map<String, Object?>>[
  {
    'module_id': 'chat',
    'kind': 'backend',
    'state': 'enabled',
    'display_name': 'Chat',
    'description': 'Remote chat module',
    'dependencies': ['ai_client'],
    'updated_at': '2026-05-16T00:00:00Z',
  },
  {
    'module_id': 'browser',
    'kind': 'tool',
    'state': 'degraded',
    'display_name': 'Browser',
    'description': 'Browser companion',
    'dependencies': [],
    'last_error': 'Waiting for the companion',
  },
];

Future<http.Response> healthyHomeResponse(http.Request request) async {
  switch (request.url.path) {
    case '/health':
      return jsonResponse({
        'status': 'healthy',
        'service': 'tobkiri',
        'timestamp': '2026-05-16T00:00:00Z',
      });
    case '/api/defaultspack/modules':
      return jsonResponse({
        'success': true,
        'data': {'modules': fixtureModules},
      });
    case '/api/defaultspack/migration/status':
      return jsonResponse({
        'success': true,
        'data': {'status': 'ready', 'migrated': 2, 'total': 2},
      });
    case '/api/defaultspack/pack-requests':
      return jsonResponse({
        'success': true,
        'data': {
          'requests': [
            {
              'request_id': 'pack-request-1',
              'kind': 'reload',
              'status': 'pending',
              'summary': 'Reload browser companion',
            },
          ],
        },
      });
    default:
      return jsonResponse({'success': true, 'data': {}});
  }
}
