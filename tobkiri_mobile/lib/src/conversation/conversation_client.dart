import 'dart:convert';

import 'package:http/http.dart' as http;

import '../mobile_authority.dart';
import 'conversation_models.dart';

abstract class ConversationConnectionStore {
  Future<List<MobileConversationConnection>> load();

  Future<void> saveVerified(List<MobileConversationConnection> connections);
}

class SecureConversationConnectionStore implements ConversationConnectionStore {
  SecureConversationConnectionStore({AuthoritySecretStore? storage})
      : _storage = storage ?? FlutterAuthoritySecretStore();

  static const storageKey = 'tobkiri.mobile.conversation_spaces.v1';
  final AuthoritySecretStore _storage;

  @override
  Future<List<MobileConversationConnection>> load() async {
    final encoded = await _storage.read(storageKey);
    if (encoded == null || encoded.isEmpty) return const [];
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is! List) throw const FormatException();
      final connections = decoded
          .whereType<Map>()
          .map(
            (value) => MobileConversationConnection.fromJson(
              Map<String, dynamic>.from(value),
            ),
          )
          .toList(growable: false);
      if (connections.isEmpty ||
          connections.any((connection) => !connection.isValid) ||
          connections.map((connection) => connection.id).toSet().length !=
              connections.length) {
        throw const FormatException();
      }
      return connections;
    } catch (_) {
      throw StateError('stored conversation spaces are invalid');
    }
  }

  @override
  Future<void> saveVerified(
    List<MobileConversationConnection> connections,
  ) async {
    if (connections.isEmpty ||
        connections.any((connection) => !connection.isValid) ||
        connections.map((connection) => connection.id).toSet().length !=
            connections.length) {
      throw StateError('conversation spaces are invalid');
    }
    final encoded = jsonEncode(
      connections.map((connection) => connection.toJson()).toList(),
    );
    await _storage.write(storageKey, encoded);
    if (await _storage.read(storageKey) != encoded) {
      await _storage.delete(storageKey);
      throw StateError('conversation spaces could not be verified');
    }
  }
}

abstract class ConversationNavigationClient {
  Future<List<ConversationSummary>> listConversations();

  Future<ConversationDetail> createConversation();

  Future<ConversationDetail> getConversation(String conversationId);

  void close();
}

class CanonicalConversationNavigationClient
    implements ConversationNavigationClient {
  CanonicalConversationNavigationClient({
    required this.connection,
    http.Client? client,
    this.timeout = const Duration(seconds: 15),
  }) : _client = client ?? http.Client();

  final MobileConversationConnection connection;
  final http.Client _client;
  final Duration timeout;
  bool _closed = false;

  @override
  Future<List<ConversationSummary>> listConversations() async {
    final data = await _request('GET', '/api/mobile/v1/conversations');
    final values = data['conversations'] as List? ?? const [];
    return values
        .whereType<Map>()
        .map(
          (value) =>
              ConversationSummary.fromJson(Map<String, dynamic>.from(value)),
        )
        .where((summary) => summary.id.isNotEmpty)
        .toList(growable: false);
  }

  @override
  Future<ConversationDetail> createConversation() async {
    final data = await _request(
      'POST',
      '/api/mobile/v1/conversations',
      body: const {},
    );
    return _detailFromData(data);
  }

  @override
  Future<ConversationDetail> getConversation(String conversationId) async {
    final data = await _request(
      'GET',
      '/api/mobile/v1/conversations/${Uri.encodeComponent(conversationId)}',
    );
    return _detailFromData(data);
  }

  ConversationDetail _detailFromData(Map<String, dynamic> data) {
    final value = data['conversation'];
    if (value is! Map) {
      throw StateError('conversation response is invalid');
    }
    final detail = ConversationDetail.fromJson(
      Map<String, dynamic>.from(value),
    );
    if (detail.summary.id.isEmpty) {
      throw StateError('conversation response has no id');
    }
    return detail;
  }

  Future<Map<String, dynamic>> _request(
    String method,
    String path, {
    Map<String, dynamic>? body,
  }) async {
    if (_closed) throw StateError('conversation client is closed');
    if (!connection.isValid) {
      throw StateError('conversation connection is invalid');
    }
    final base = Uri.parse(connection.baseUrl.trim());
    final basePath = base.path.endsWith('/')
        ? base.path.substring(0, base.path.length - 1)
        : base.path;
    final request = http.Request(method, base.replace(path: '$basePath$path'))
      ..headers.addAll({
        'Accept': 'application/json',
        'Authorization': 'Bearer ${connection.token}',
        'X-Rumi-Client': 'rumi-mobile',
        if (body != null) 'Content-Type': 'application/json; charset=utf-8',
      });
    if (body != null) request.body = jsonEncode(body);
    final response = await http.Response.fromStream(
      await _client.send(request).timeout(timeout),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError('conversation request failed (${response.statusCode})');
    }
    final decoded = jsonDecode(utf8.decode(response.bodyBytes));
    if (decoded is! Map || decoded['status'] != 'ok') {
      throw StateError('conversation response is invalid');
    }
    final data = decoded['data'];
    return data is Map ? Map<String, dynamic>.from(data) : const {};
  }

  @override
  void close() {
    _closed = true;
    _client.close();
  }
}
