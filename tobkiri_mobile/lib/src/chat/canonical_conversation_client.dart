import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../mobile_authority.dart';

const mobileChatScopes = <String>{'chat.read', 'chat.write'};

class MobileChatConnection {
  const MobileChatConnection({
    required this.baseUrl,
    required this.deviceId,
    required this.token,
    required this.scopes,
  });

  final String baseUrl;
  final String deviceId;
  final String token;
  final Set<String> scopes;

  bool get isValid {
    final uri = Uri.tryParse(baseUrl.trim());
    return uri != null &&
        uri.hasScheme &&
        const {'http', 'https'}.contains(uri.scheme) &&
        uri.host.isNotEmpty &&
        uri.userInfo.isEmpty &&
        !uri.hasQuery &&
        !uri.hasFragment &&
        deviceId.trim().isNotEmpty &&
        token.trim().isNotEmpty &&
        scopes.containsAll(mobileChatScopes) &&
        scopes.every(mobileChatScopes.contains);
  }

  Map<String, Object> toJson() => {
        'base_url': baseUrl,
        'device_id': deviceId,
        'token': token,
        'scopes': scopes.toList()..sort(),
      };

  factory MobileChatConnection.fromJson(Map<String, dynamic> json) =>
      MobileChatConnection(
        baseUrl: json['base_url'] as String? ?? '',
        deviceId: json['device_id'] as String? ?? '',
        token: json['token'] as String? ?? '',
        scopes: (json['scopes'] as List? ?? const [])
            .map((scope) => scope.toString())
            .toSet(),
      );
}

abstract class ChatConnectionStore {
  Future<MobileChatConnection?> load();

  Future<void> saveVerified(MobileChatConnection connection);
}

class MobileChatConnectionStore implements ChatConnectionStore {
  MobileChatConnectionStore({AuthoritySecretStore? storage})
      : _storage = storage ?? FlutterAuthoritySecretStore();

  static const storageKey = 'tobkiri.mobile.chat_connection.v1';
  final AuthoritySecretStore _storage;

  @override
  Future<void> saveVerified(MobileChatConnection connection) async {
    if (!connection.isValid) {
      throw StateError('chat connection is invalid');
    }
    final encoded = jsonEncode(connection.toJson());
    await _storage.write(storageKey, encoded);
    if (await _storage.read(storageKey) != encoded) {
      await _storage.delete(storageKey);
      throw StateError('chat connection persistence could not be verified');
    }
  }

  @override
  Future<MobileChatConnection?> load() async {
    final encoded = await _storage.read(storageKey);
    if (encoded == null || encoded.isEmpty) return null;
    try {
      final decoded = jsonDecode(encoded);
      if (decoded is! Map) throw const FormatException();
      final connection = MobileChatConnection.fromJson(
        Map<String, dynamic>.from(decoded),
      );
      if (!connection.isValid) throw const FormatException();
      return connection;
    } catch (_) {
      throw StateError('stored chat connection is invalid');
    }
  }

  Future<void> clear() => _storage.delete(storageKey);
}

enum CanonicalChatUpdateKind { delta, done, error, attention }

class CanonicalChatUpdate {
  const CanonicalChatUpdate(
    this.kind, {
    this.content = '',
    this.replace = false,
  });

  final CanonicalChatUpdateKind kind;
  final String content;
  final bool replace;
}

abstract class ConversationTransport {
  Future<String> createConversation();

  Future<int> revision(String conversationId);

  Stream<CanonicalChatUpdate> send({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  });

  Future<void> stop(String conversationId);

  void close();
}

class CanonicalConversationClient implements ConversationTransport {
  CanonicalConversationClient({
    required this.connection,
    http.Client? client,
  }) : _client = client ?? http.Client();

  final MobileChatConnection connection;
  final http.Client _client;
  bool _closed = false;

  Map<String, String> get _headers => {
        'Authorization': 'Bearer ${connection.token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      };

  Uri _uri(String path) {
    final base = Uri.parse(connection.baseUrl.trim());
    final basePath = base.path.endsWith('/')
        ? base.path.substring(0, base.path.length - 1)
        : base.path;
    return base.replace(path: '$basePath$path');
  }

  @override
  Future<String> createConversation() async {
    _checkReady();
    final response = await _client
        .post(
          _uri('/api/mobile/v1/conversations'),
          headers: _headers,
          body: '{}',
        )
        .timeout(const Duration(seconds: 15));
    final data = _decodeResponse(response);
    final conversation = data['conversation'];
    final id = data['conversation_id'] as String? ??
        (conversation is Map ? conversation['id'] as String? : null) ??
        '';
    if (id.isEmpty) throw StateError('conversation response has no id');
    return id;
  }

  @override
  Future<int> revision(String conversationId) async {
    _checkReady();
    final response = await _client
        .get(
          _uri(
            '/api/mobile/v1/conversations/'
            '${Uri.encodeComponent(conversationId)}',
          ),
          headers: _headers,
        )
        .timeout(const Duration(seconds: 15));
    final data = _decodeResponse(response);
    final conversation = data['conversation'];
    final value =
        conversation is Map ? conversation['revision'] : data['revision'];
    return value is num ? value.toInt() : 0;
  }

  @override
  Stream<CanonicalChatUpdate> send({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {
    _checkReady();
    final request = http.Request(
      'POST',
      _uri(
        '/api/mobile/v1/conversations/'
        '${Uri.encodeComponent(conversationId)}/stream',
      ),
    );
    request.headers.addAll({..._headers, 'Accept': 'text/event-stream'});
    request.body = jsonEncode({
      'message': {
        'role': 'user',
        'content': text,
        'client_message_id': clientMessageId,
      },
      'client_message_id': clientMessageId,
      'expected_revision': expectedRevision,
    });
    final response =
        await _client.send(request).timeout(const Duration(seconds: 30));
    if (response.statusCode < 200 || response.statusCode >= 300) {
      await response.stream.drain<void>();
      throw StateError('chat request failed (${response.statusCode})');
    }

    var buffer = '';
    await for (final chunk in response.stream.transform(utf8.decoder)) {
      if (_closed) return;
      buffer += chunk;
      while (buffer.contains('\n')) {
        final end = buffer.indexOf('\n');
        final line = buffer.substring(0, end).trim();
        buffer = buffer.substring(end + 1);
        if (!line.startsWith('data:')) continue;
        final data = line.substring(5).trim();
        if (data == '[DONE]') {
          yield const CanonicalChatUpdate(CanonicalChatUpdateKind.done);
          return;
        }
        final update = _parseEvent(data);
        if (update != null) yield update;
        if (update?.kind == CanonicalChatUpdateKind.done) return;
      }
    }
    yield const CanonicalChatUpdate(CanonicalChatUpdateKind.done);
  }

  @override
  Future<void> stop(String conversationId) async {
    _checkReady();
    final response = await _client
        .post(
          _uri(
            '/api/mobile/v1/conversations/'
            '${Uri.encodeComponent(conversationId)}/stop',
          ),
          headers: _headers,
          body: '{}',
        )
        .timeout(const Duration(seconds: 10));
    _decodeResponse(response);
  }

  CanonicalChatUpdate? _parseEvent(String source) {
    try {
      final json = jsonDecode(source) as Map<String, dynamic>;
      final type = json['type'] as String? ?? '';
      final data = json['data'] is Map
          ? Map<String, dynamic>.from(json['data'] as Map)
          : const <String, dynamic>{};
      if (type == 'delta' || type == 'content_delta') {
        final accumulated =
            json['content'] as String? ?? data['content'] as String? ?? '';
        final delta =
            json['delta'] as String? ?? data['delta'] as String? ?? '';
        final content = accumulated.isNotEmpty ? accumulated : delta;
        return content.isEmpty
            ? null
            : CanonicalChatUpdate(
                CanonicalChatUpdateKind.delta,
                content: content,
                replace: accumulated.isNotEmpty,
              );
      }
      if (type == 'done') {
        return const CanonicalChatUpdate(CanonicalChatUpdateKind.done);
      }
      if (type == 'error') {
        return CanonicalChatUpdate(
          CanonicalChatUpdateKind.error,
          content: '応答を完了できませんでした。',
        );
      }
      if (type == 'approval' || type == 'approval_requested') {
        return const CanonicalChatUpdate(
          CanonicalChatUpdateKind.attention,
          content: '承認タブで権限リクエストを確認してください。',
        );
      }
    } catch (_) {
      return null;
    }
    return null;
  }

  Map<String, dynamic> _decodeResponse(http.Response response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError(
        'mobile API request failed (${response.statusCode})',
      );
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) throw StateError('mobile API response is invalid');
    final json = Map<String, dynamic>.from(decoded);
    if (json['status'] != 'ok') {
      throw StateError('mobile API request failed');
    }
    final data = json['data'];
    return data is Map ? Map<String, dynamic>.from(data) : const {};
  }

  void _checkReady() {
    if (_closed) throw StateError('conversation client is closed');
    if (!connection.isValid) throw StateError('chat connection is invalid');
  }

  @override
  void close() {
    _closed = true;
    _client.close();
  }
}
