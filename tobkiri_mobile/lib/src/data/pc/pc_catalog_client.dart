import 'dart:convert';

import 'package:http/http.dart' as http;

import '../../pc_control_models.dart';
import '../../pc_control_state.dart';
import '../../settings/api_config_store.dart';
import 'pc_catalog.dart';

class PcCatalogFetchException implements Exception {
  const PcCatalogFetchException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class PcCatalogClient {
  PcCatalogClient({http.Client? client}) : _http = client ?? http.Client();

  final http.Client _http;
  bool _closed = false;

  void close() {
    _closed = true;
    _http.close();
  }

  Map<String, String> _headers(PcConnection pc) => {
    'Authorization': 'Bearer ${pc.token.trim()}',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
  };

  Uri _uri(String baseUrl, String path) {
    var trimmed = baseUrl.trim();
    if (trimmed.isEmpty) {
      throw const PcCatalogFetchException('PC接続URLが設定されていません。');
    }
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final base = Uri.parse(trimmed);
    return base.replace(path: '${_trimTrailingSlash(base.path)}$path');
  }

  Future<PcBootstrap> fetchBootstrap(PcConnection pc) async {
    final resp = await _get(pc, '/api/mobile/v1/bootstrap');
    final data = _decodeData(resp.body);
    return PcBootstrap.fromJson(data);
  }

  Future<PcMobileManifest> fetchMobileManifest(PcConnection pc) async {
    final resp = await _get(pc, '/api/mobile/v1/manifest');
    final data = _decodeData(resp.body);
    return PcMobileManifest.fromJson(data);
  }

  Future<PcCatalog> fetchCapabilities(
    PcConnection pc, {
    String? providerFilter,
    bool includeTemplates = true,
  }) async {
    final params = <String, String>{};
    if (providerFilter != null && providerFilter.isNotEmpty) {
      params['provider'] = providerFilter;
    }
    if (!includeTemplates) {
      params['include_templates'] = 'false';
    }
    final resp = await _get(pc, '/api/mobile/v1/capabilities', params);
    final data = _decodeData(resp.body);
    return PcCatalog.fromJson(data);
  }

  Future<PcCommandExecuteResult> executeCommand(
    PcConnection pc, {
    required String command,
    Map<String, dynamic> args = const {},
    String? conversationId,
    String mode = 'chat',
  }) async {
    final resp = await _post(pc, '/api/mobile/v1/commands/execute', {
      'command': command,
      'args': args,
      'mode': mode,
      if (conversationId != null && conversationId.trim().isNotEmpty)
        'conversation_id': conversationId.trim(),
    });
    final data = _decodeData(resp.body);
    return PcCommandExecuteResult.fromJson(data);
  }

  Future<PcRuntimeSnapshot> fetchControlSnapshot(
    PcConnection pc,
    Set<String> stateRefs,
  ) async {
    final refs =
        stateRefs
            .map((item) => item.trim())
            .where((item) => item.isNotEmpty)
            .toList()
          ..sort();
    final resp = await _post(pc, '/api/mobile/v1/control-states/query', {
      'state_refs': refs,
    });
    return PcRuntimeSnapshot.fromJson(_decodeData(resp.body));
  }

  Future<PcCommandResult> invokeControlCommand(
    PcConnection pc,
    PcControlRequest request, {
    String? conversationId,
    String mode = 'chat',
  }) async {
    final resp = await _post(pc, '/api/mobile/v1/control-commands/invoke', {
      'command_ref': request.definition.commandRef,
      'args': request.definition.arguments(request.value),
      'mode': mode,
      'invocation_id': request.invocationId,
      'expected_revision': request.expectedRevision,
      'idempotency_key': request.idempotencyKey,
      'client_sequence': request.clientSequence,
      if (conversationId != null && conversationId.trim().isNotEmpty)
        'conversation_id': conversationId.trim(),
    });
    return PcCommandResult.fromJson(_decodeData(resp.body));
  }

  Future<Map<String, dynamic>> invokeTool(
    PcConnection pc, {
    required String toolName,
    Map<String, dynamic> arguments = const {},
    Map<String, dynamic>? context,
  }) async {
    final resp = await _post(pc, '/api/mobile/v1/tools/invoke', {
      'tool_name': toolName,
      'arguments': arguments,
      if (context != null && context.isNotEmpty) 'context': context,
    });
    return _decodeData(resp.body);
  }

  Future<http.Response> _get(
    PcConnection pc,
    String path, [
    Map<String, String>? query,
  ]) async {
    if (!pc.isConfigured) {
      throw const PcCatalogFetchException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const PcCatalogFetchException('クライアントは閉じられました。');
    }
    var uri = _uri(pc.baseUrl, path);
    if (query != null && query.isNotEmpty) {
      uri = uri.replace(queryParameters: {...uri.queryParameters, ...query});
    }
    final resp = await _http
        .get(uri, headers: _headers(pc))
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw PcCatalogFetchException(
        'PC接続に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    return resp;
  }

  Future<http.Response> _post(
    PcConnection pc,
    String path,
    Map<String, dynamic> body,
  ) async {
    if (!pc.isConfigured) {
      throw const PcCatalogFetchException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const PcCatalogFetchException('クライアントは閉じられました。');
    }
    final resp = await _http
        .post(
          _uri(pc.baseUrl, path),
          headers: _headers(pc),
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw PcCatalogFetchException(
        'PC接続に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    return resp;
  }

  Map<String, dynamic> _decodeData(String body) {
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      if (decoded['status'] != 'ok') {
        final err = decoded['error'];
        final msg = err is Map ? err['message'] as String? : null;
        throw PcCatalogFetchException(msg ?? 'PCからエラーが返されました。');
      }
      return decoded['data'] as Map<String, dynamic>? ?? const {};
    } catch (e) {
      if (e is PcCatalogFetchException) rethrow;
      throw PcCatalogFetchException('PC応答の解析に失敗しました: $e');
    }
  }

  String _trimTrailingSlash(String path) {
    if (path.isEmpty || path == '/') return '';
    return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
  }
}
