import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'generated/command_protocol_models.dart' as protocol;
import 'models.dart';

enum ModuleAction {
  enable('enable', 'Enable', destructive: false),
  disable('disable', 'Disable', destructive: true),
  reload('reload', 'Reload', destructive: true),
  rollback('rollback', 'Rollback', destructive: true);

  const ModuleAction(
    this.pathSegment,
    this.label, {
    required this.destructive,
  });

  final String pathSegment;
  final String label;
  final bool destructive;
}

class RumiApiClient {
  RumiApiClient({
    required String baseUrl,
    required String bearerToken,
    this.timeout = const Duration(seconds: 30),
    http.Client? httpClient,
  })  : baseUri = normalizeBaseUri(baseUrl),
        bearerToken = bearerToken.trim(),
        _http = httpClient ?? http.Client();

  final Uri baseUri;
  final String bearerToken;
  final Duration timeout;
  final http.Client _http;

  static Uri normalizeBaseUri(String input) {
    final trimmed = input.trim();
    if (trimmed.isEmpty) {
      throw const RumiApiException('Server URL is required');
    }
    final withScheme = trimmed.contains('://') ? trimmed : 'http://$trimmed';
    final uri = Uri.parse(withScheme);
    if (!uri.hasScheme || uri.host.isEmpty) {
      throw RumiApiException('Invalid server URL: $input');
    }
    return uri.replace(path: _trimTrailingSlash(uri.path));
  }

  Future<RumiHealth> health() async {
    final data = await _request('GET', '/health', requireAuth: false);
    return RumiHealth.fromJson(data);
  }

  Future<ModuleCatalog> listModules() async {
    final data = await _request('GET', '/api/defaultspack/modules');
    return ModuleCatalog.fromJson(data);
  }

  Future<RumiModule> getModule(String moduleId) async {
    final data = await _request(
      'GET',
      '/api/defaultspack/modules/${Uri.encodeComponent(moduleId)}',
    );
    return RumiModule.fromJson(data);
  }

  Future<RumiModule> moduleAction(
    String moduleId,
    ModuleAction action,
  ) async {
    final data = await _request(
      'POST',
      '/api/defaultspack/modules/'
          '${Uri.encodeComponent(moduleId)}/${action.pathSegment}',
    );
    final actionMap = asMap(data);
    if (actionMap['module_id'] != null && actionMap['state'] != null) {
      final current = await getModule(moduleId);
      return current;
    }
    return RumiModule.fromJson(data);
  }

  Future<MigrationStatus> migrationStatus() async {
    final data = await _request('GET', '/api/defaultspack/migration/status');
    return MigrationStatus.fromJson(data);
  }

  Future<Map<String, dynamic>> commandCatalog() async {
    return asMap(await _request('GET', '/api/command-protocol/v1/catalog'));
  }

  Future<Map<String, dynamic>> invokeCommand(
    String commandRef, {
    Map<String, Object?> args = const {},
    String? conversationId,
    String? invocationId,
    String mode = 'chat',
    String? profileId,
    String? catalogRevision,
    int? expectedRevision,
    String? idempotencyKey,
    int? clientSequence,
  }) async {
    final request = protocol.CommandInvocationRequest(
      commandRef: commandRef,
      args: args,
      invocationId:
          invocationId ?? 'mobile-${DateTime.now().microsecondsSinceEpoch}',
      mode: protocol.CommandMode.values.byName(mode),
      conversationId: conversationId,
      profileId: profileId,
      catalogRevision: catalogRevision,
      expectedRevision: expectedRevision,
      idempotencyKey: idempotencyKey,
      clientSequence: clientSequence,
    );
    return asMap(await _request(
      'POST',
      '/api/command-protocol/v1/invoke',
      body: request.toJson(),
    ));
  }

  Future<Map<String, dynamic>> resumeCommand(
    String commandRef,
    String approvalToken, {
    Map<String, Object?> args = const {},
    String? conversationId,
    String? invocationId,
    String mode = 'chat',
    String? profileId,
    String? catalogRevision,
    int? expectedRevision,
    String? idempotencyKey,
    int? clientSequence,
  }) async {
    final request = protocol.CommandInvocationRequest(
      commandRef: commandRef,
      args: args,
      invocationId:
          invocationId ?? 'mobile-${DateTime.now().microsecondsSinceEpoch}',
      mode: protocol.CommandMode.values.byName(mode),
      conversationId: conversationId,
      profileId: profileId,
      catalogRevision: catalogRevision,
      expectedRevision: expectedRevision,
      idempotencyKey: idempotencyKey,
      clientSequence: clientSequence,
      approvalToken: approvalToken,
    );
    return asMap(await _request(
      'POST',
      '/api/command-protocol/v1/resume',
      body: request.toJson(),
    ));
  }

  Future<Map<String, dynamic>> commandInvocationEvents(
    String invocationId, {
    int afterSequence = 0,
    int limit = 500,
    String? profileId,
    String? conversationId,
  }) async {
    return asMap(await _request(
      'POST',
      '/api/command-protocol/v1/invocations/events/query',
      body: <String, Object?>{
        'invocation_id': invocationId,
        'after_sequence': afterSequence,
        'limit': limit,
        if (profileId != null) 'profile_id': profileId,
        if (conversationId != null) 'conversation_id': conversationId,
      },
    ));
  }

  Future<Map<String, dynamic>> commandOfflineQueue(
    String action, {
    String? commandRef,
    Map<String, Object?> args = const {},
    String? idempotencyKey,
    int? expectedRevision,
    int? limit,
  }) async {
    return asMap(await _request(
      'POST',
      '/api/command-protocol/v1/offline',
      body: <String, Object?>{
        'action': action,
        if (commandRef != null) 'command_ref': commandRef,
        if (args.isNotEmpty) 'args': args,
        if (idempotencyKey != null) 'idempotency_key': idempotencyKey,
        if (expectedRevision != null) 'expected_revision': expectedRevision,
        if (limit != null) 'limit': limit,
      },
    ));
  }

  Future<List<PackRequest>> listPackRequests() async {
    final data = await _request('GET', '/api/defaultspack/pack-requests');
    final items =
        _listPayload(data, keys: const ['requests', 'items', 'pack_requests']);
    return items.map(PackRequest.fromJson).toList(growable: false);
  }

  void close() => _http.close();

  Future<Object?> _request(
    String method,
    String path, {
    Map<String, Object?>? body,
    bool requireAuth = true,
  }) async {
    if (requireAuth && bearerToken.isEmpty) {
      throw const RumiApiException('Bearer token is required');
    }
    final request = http.Request(method, _uri(path));
    request.headers
        .addAll(_headers(hasBody: body != null, requireAuth: requireAuth));
    if (body != null) {
      request.body = jsonEncode(body);
    }

    final streamed = await _http.send(request).timeout(timeout);
    final response = await http.Response.fromStream(streamed);
    final decoded = _decodeBody(response);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw RumiApiException(
        _errorMessage(decoded, fallback: response.reasonPhrase ?? 'HTTP error'),
        statusCode: response.statusCode,
      );
    }
    return _unwrapEnvelope(decoded);
  }

  Uri _uri(String path) {
    final basePath = _trimTrailingSlash(baseUri.path);
    final normalizedPath = path.startsWith('/') ? path : '/$path';
    return baseUri.replace(path: '$basePath$normalizedPath');
  }

  Map<String, String> _headers({
    required bool hasBody,
    required bool requireAuth,
  }) {
    return {
      'Accept': 'application/json',
      'X-Rumi-Client': 'rumi-mobile',
      if (hasBody) 'Content-Type': 'application/json; charset=utf-8',
      if (requireAuth) 'Authorization': 'Bearer $bearerToken',
    };
  }
}

Object? _decodeBody(http.Response response) {
  if (response.body.trim().isEmpty) {
    return null;
  }
  try {
    return jsonDecode(utf8.decode(response.bodyBytes));
  } on FormatException {
    throw RumiApiException(
      'Server returned a non-JSON response',
      statusCode: response.statusCode,
    );
  }
}

Object? _unwrapEnvelope(Object? decoded) {
  if (decoded is Map<String, dynamic>) {
    if (decoded['success'] == false) {
      throw RumiApiException(_errorMessage(decoded));
    }
    if (decoded['status'] == 'error') {
      throw RumiApiException(_errorMessage(decoded));
    }
    if (decoded.containsKey('data')) {
      return decoded['data'];
    }
  }
  return decoded;
}

String _errorMessage(
  Object? decoded, {
  String fallback = 'Rumi request failed',
}) {
  if (decoded is Map<String, dynamic>) {
    final error = decoded['error'];
    if (error is String && error.trim().isNotEmpty) {
      return error;
    }
    if (error is Map && error['message'] != null) {
      return '${error['message']}';
    }
    final message = decoded['message'];
    if (message is String && message.trim().isNotEmpty) {
      return message;
    }
  }
  return fallback;
}

List<Object?> _listPayload(Object? data, {required List<String> keys}) {
  if (data is List) {
    return data;
  }
  if (data is Map<String, dynamic>) {
    for (final key in keys) {
      final value = data[key];
      if (value is List) {
        return value;
      }
    }
  }
  return const [];
}

String _trimTrailingSlash(String path) {
  if (path == '/' || path.isEmpty) {
    return '';
  }
  return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
}
