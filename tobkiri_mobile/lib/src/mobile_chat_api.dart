import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// A conversation returned by Tobkiri's finite mobile API.
class MobileConversationSummary {
  const MobileConversationSummary({
    required this.id,
    required this.title,
    required this.messageCount,
  });

  final String id;
  final String title;
  final int messageCount;
}

/// A message rendered by the canonical mobile chat surface.
class MobileChatMessage {
  const MobileChatMessage({
    required this.id,
    required this.role,
    required this.content,
    this.pending = false,
    this.error = false,
  });

  final String id;
  final String role;
  final String content;
  final bool pending;
  final bool error;

  MobileChatMessage copyWith({String? content, bool? pending, bool? error}) {
    return MobileChatMessage(
      id: id,
      role: role,
      content: content ?? this.content,
      pending: pending ?? this.pending,
      error: error ?? this.error,
    );
  }
}

/// An exact conversation snapshot and its optimistic-write revision.
class MobileConversationSnapshot {
  const MobileConversationSnapshot({
    required this.id,
    required this.title,
    required this.revision,
    required this.messages,
  });

  final String id;
  final String title;
  final int revision;
  final List<MobileChatMessage> messages;
}

/// A normalized event from the mobile conversation SSE route.
sealed class MobileChatStreamEvent {
  const MobileChatStreamEvent();
}

class MobileChatDelta extends MobileChatStreamEvent {
  const MobileChatDelta(this.content);

  final String content;
}

class MobileChatActivity extends MobileChatStreamEvent {
  const MobileChatActivity({
    required this.id,
    required this.label,
    required this.kind,
    this.pending = false,
  });

  final String id;
  final String label;
  final String kind;
  final bool pending;
}

class MobileChatCompleted extends MobileChatStreamEvent {
  const MobileChatCompleted();
}

class MobileChatFailed extends MobileChatStreamEvent {
  const MobileChatFailed(this.message);

  final String message;
}

/// The chat operations used by [MobileChatScreen].
abstract interface class MobileChatGateway {
  Future<List<MobileConversationSummary>> listConversations();

  Future<MobileConversationSnapshot> getConversation(String conversationId);

  Future<String> createConversation();

  Stream<MobileChatStreamEvent> streamMessage({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  });

  Future<void> stop(String conversationId);

  void close();
}

/// HTTP implementation bound only to `/api/mobile/v1` routes.
class HttpMobileChatGateway implements MobileChatGateway {
  HttpMobileChatGateway({
    required String baseUrl,
    required String bearerToken,
    http.Client? httpClient,
    this.timeout = const Duration(seconds: 30),
  })  : _baseUri = _normalizeBaseUri(baseUrl),
        _bearerToken = bearerToken.trim(),
        _http = httpClient ?? http.Client();

  final Uri _baseUri;
  final String _bearerToken;
  final http.Client _http;
  final Duration timeout;
  bool _closed = false;

  @override
  Future<List<MobileConversationSummary>> listConversations() async {
    final data = await _jsonRequest('GET', '/api/mobile/v1/conversations');
    final items = data['conversations'];
    if (items is! List) {
      return const [];
    }
    return items
        .whereType<Map>()
        .map((item) {
          return MobileConversationSummary(
            id: '${item['id'] ?? ''}',
            title: '${item['title'] ?? '無題'}',
            messageCount: (item['message_count'] as num?)?.toInt() ?? 0,
          );
        })
        .where((item) => item.id.isNotEmpty)
        .toList(growable: false);
  }

  @override
  Future<MobileConversationSnapshot> getConversation(
    String conversationId,
  ) async {
    final encodedId = Uri.encodeComponent(conversationId);
    final data = await _jsonRequest(
      'GET',
      '/api/mobile/v1/conversations/$encodedId',
    );
    final rawConversation = data['conversation'];
    final conversation = rawConversation is Map ? rawConversation : data;
    final rawMessages = conversation['messages'];
    final messages = rawMessages is List
        ? rawMessages
            .whereType<Map>()
            .map(_parseMessage)
            .toList(growable: false)
        : const <MobileChatMessage>[];
    return MobileConversationSnapshot(
      id: '${conversation['id'] ?? conversationId}',
      title: '${conversation['title'] ?? '無題'}',
      revision: (conversation['revision'] as num?)?.toInt() ?? 0,
      messages: messages,
    );
  }

  @override
  Future<String> createConversation() async {
    final data = await _jsonRequest(
      'POST',
      '/api/mobile/v1/conversations',
      body: const {},
    );
    final conversation = data['conversation'];
    final id = data['conversation_id'] ??
        (conversation is Map ? conversation['id'] : null);
    final normalized = '$id'.trim();
    if (id == null || normalized.isEmpty) {
      throw const MobileChatApiException('会話の作成結果にIDがありません。');
    }
    return normalized;
  }

  @override
  Stream<MobileChatStreamEvent> streamMessage({
    required String conversationId,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
  }) async* {
    _ensureReady();
    final encodedId = Uri.encodeComponent(conversationId);
    final request = http.Request(
      'POST',
      _uri('/api/mobile/v1/conversations/$encodedId/stream'),
    );
    request.headers.addAll({
      ..._headers(hasBody: true),
      'Accept': 'text/event-stream',
    });
    request.body = jsonEncode({
      'idempotency_key': clientMessageId,
      'message': {
        'role': 'user',
        'content': text,
        'client_message_id': clientMessageId,
      },
      'client_message_id': clientMessageId,
      'expected_revision': expectedRevision,
    });

    try {
      final response = await _http.send(request).timeout(timeout);
      if (response.statusCode < 200 || response.statusCode >= 300) {
        await response.stream.drain<void>();
        yield MobileChatFailed('Tobkiri API エラー (HTTP ${response.statusCode})');
        return;
      }

      var buffer = '';
      var accumulated = '';
      await for (final chunk in response.stream.transform(utf8.decoder)) {
        if (_closed) {
          return;
        }
        buffer += chunk;
        while (true) {
          final newline = buffer.indexOf('\n');
          if (newline < 0) {
            break;
          }
          final line = buffer.substring(0, newline).trim();
          buffer = buffer.substring(newline + 1);
          if (!line.startsWith('data:')) {
            continue;
          }
          final payload = line.substring(5).trim();
          if (payload == '[DONE]') {
            yield const MobileChatCompleted();
            return;
          }
          final parsed = _parseStreamEvent(payload, accumulated);
          if (parsed == null) {
            continue;
          }
          if (parsed case MobileChatDelta(:final content)) {
            accumulated = content;
          }
          yield parsed;
        }
      }
      yield const MobileChatCompleted();
    } on Object {
      yield const MobileChatFailed('Tobkiriとの通信に失敗しました。');
    }
  }

  @override
  Future<void> stop(String conversationId) async {
    final encodedId = Uri.encodeComponent(conversationId);
    await _jsonRequest(
      'POST',
      '/api/mobile/v1/conversations/$encodedId/stop',
      body: const {},
    );
  }

  @override
  void close() {
    _closed = true;
    _http.close();
  }

  MobileChatStreamEvent? _parseStreamEvent(String payload, String accumulated) {
    try {
      final event = jsonDecode(payload);
      if (event is! Map<String, dynamic>) {
        return null;
      }
      final type = '${event['type'] ?? 'delta'}';
      final data = event['data'] is Map<String, dynamic>
          ? event['data'] as Map<String, dynamic>
          : const <String, dynamic>{};
      switch (type) {
        case 'content_delta':
        case 'delta':
          final complete = '${event['content'] ?? data['content'] ?? ''}';
          final delta = '${event['delta'] ?? data['delta'] ?? ''}';
          final next = complete.isNotEmpty ? complete : '$accumulated$delta';
          return next.isEmpty ? null : MobileChatDelta(next);
        case 'message':
        case 'done':
          final message = event['message'];
          final content = message is Map ? '${message['content'] ?? ''}' : '';
          return content.isEmpty ? null : MobileChatDelta(content);
        case 'status':
        case 'thinking_delta':
        case 'tool_selection_started':
        case 'tool_selection_completed':
          final label = '${event['message'] ?? data['message'] ?? '処理中'}';
          return MobileChatActivity(
            id: '${event['id'] ?? event['phase'] ?? type}',
            label: label,
            kind: 'status',
            pending: true,
          );
        case 'tool_call':
        case 'tool_call_started':
        case 'tool_call_delta':
        case 'tool_call_completed':
          final name = '${event['tool_name'] ?? data['tool_name'] ?? 'ツール'}';
          final status = '${event['status'] ?? data['status'] ?? 'running'}';
          return MobileChatActivity(
            id: '${event['tool_call_id'] ?? data['tool_call_id'] ?? name}',
            label: '$name · $status',
            kind: 'tool',
            pending: status != 'completed' && status != 'failed',
          );
        case 'approval':
        case 'approval_requested':
          final prompt = '${event['prompt'] ?? data['prompt'] ?? '承認が必要です'}';
          return MobileChatActivity(
            id: '${event['approval_id'] ?? data['approval_id'] ?? prompt}',
            label: prompt,
            kind: 'approval',
            pending: event['pending'] as bool? ?? true,
          );
        case 'error':
          return MobileChatFailed(
            '${event['message'] ?? data['message'] ?? '不明なエラー'}',
          );
        default:
          return null;
      }
    } on FormatException {
      return null;
    }
  }

  MobileChatMessage _parseMessage(Map<dynamic, dynamic> raw) {
    final content = raw['content'];
    final normalizedContent = content is String
        ? content
        : content is List
            ? content.map((part) {
                if (part is String) {
                  return part;
                }
                if (part is Map) {
                  return '${part['text'] ?? ''}';
                }
                return '';
              }).join()
            : '';
    return MobileChatMessage(
      id: '${raw['id'] ?? ''}',
      role: '${raw['role'] ?? 'assistant'}',
      content: normalizedContent,
      pending: raw['pending'] as bool? ?? false,
      error: raw['error'] as bool? ?? false,
    );
  }

  Future<Map<String, dynamic>> _jsonRequest(
    String method,
    String path, {
    Map<String, Object?>? body,
  }) async {
    _ensureReady();
    final request = http.Request(method, _uri(path));
    request.headers.addAll(_headers(hasBody: body != null));
    if (body != null) {
      request.body = jsonEncode(body);
    }
    final streamed = await _http.send(request).timeout(timeout);
    final response = await http.Response.fromStream(streamed);
    final Object? decoded;
    try {
      decoded = response.body.trim().isEmpty
          ? const <String, dynamic>{}
          : jsonDecode(utf8.decode(response.bodyBytes));
    } on FormatException {
      throw MobileChatApiException(
        'Tobkiri API の応答形式が不正です (HTTP ${response.statusCode})。',
      );
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw MobileChatApiException(
        'Tobkiri API エラー (HTTP ${response.statusCode})',
      );
    }
    if (decoded is! Map<String, dynamic>) {
      throw const MobileChatApiException('Tobkiri API の応答形式が不正です。');
    }
    if (decoded['status'] == 'error' || decoded['success'] == false) {
      final error = decoded['error'];
      final message = error is Map ? error['message'] : error;
      throw MobileChatApiException('${message ?? 'Tobkiri API エラー'}');
    }
    final data = decoded['data'];
    return data is Map<String, dynamic> ? data : decoded;
  }

  void _ensureReady() {
    if (_closed) {
      throw const MobileChatApiException('クライアントは終了しています。');
    }
    if (_bearerToken.isEmpty) {
      throw const MobileChatApiException('Bearer token が必要です。');
    }
  }

  Uri _uri(String path) {
    final basePath = _trimTrailingSlash(_baseUri.path);
    return _baseUri.replace(path: '$basePath$path');
  }

  Map<String, String> _headers({required bool hasBody}) {
    return {
      'Authorization': 'Bearer $_bearerToken',
      'Accept': 'application/json',
      'X-Rumi-Client': 'tobkiri-mobile',
      if (hasBody) 'Content-Type': 'application/json; charset=utf-8',
    };
  }

  static Uri _normalizeBaseUri(String input) {
    final trimmed = input.trim();
    final withScheme = trimmed.contains('://') ? trimmed : 'http://$trimmed';
    final uri = Uri.parse(withScheme);
    if (!uri.hasScheme || uri.host.isEmpty) {
      throw const MobileChatApiException('Server URL が不正です。');
    }
    return uri.replace(path: _trimTrailingSlash(uri.path));
  }

  static String _trimTrailingSlash(String value) {
    if (value.isEmpty || value == '/') {
      return '';
    }
    return value.endsWith('/') ? value.substring(0, value.length - 1) : value;
  }
}

class MobileChatApiException implements Exception {
  const MobileChatApiException(this.message);

  final String message;

  @override
  String toString() => message;
}
