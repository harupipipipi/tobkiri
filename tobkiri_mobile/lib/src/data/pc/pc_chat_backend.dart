import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:uuid/uuid.dart';

import '../../chat/chat_models.dart';
import '../../domain/chat_event.dart';
import '../../domain/conversation_backend.dart';
import '../../domain/conversation_locator.dart';
import '../../settings/api_config_store.dart';

class PcConversationBackend implements ConversationBackend {
  PcConversationBackend({
    required this.connection,
    this.deviceId,
    http.Client? client,
  })  : _http = client ?? http.Client(),
        _uuid = const Uuid();

  final PcConnection connection;
  final String? deviceId;
  final http.Client _http;
  final Uuid _uuid;
  bool _closed = false;

  @override
  ConversationAuthorityKind get authority => ConversationAuthorityKind.pc;

  @override
  bool get isConfigured => connection.isConfigured;

  void close() {
    _closed = true;
    _http.close();
  }

  Map<String, String> get _headers => {
        'Authorization': 'Bearer ${connection.token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      };

  Uri _uri(String path) {
    var trimmed = connection.baseUrl.trim();
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final base = Uri.parse(trimmed);
    return base.replace(path: '${_trimTrailingSlash(base.path)}$path');
  }

  @override
  Future<List<ConversationSummary>> listConversations() async {
    _checkReady();
    final uri = _uri('/api/mobile/v1/conversations');
    final resp = await _http
        .get(uri, headers: _headers)
        .timeout(const Duration(seconds: 15));
    _checkResponse(resp);
    final data = _decodeData(resp.body);
    final list = data['conversations'] as List? ?? const [];
    return list
        .map((e) => _parseConversationSummary(e as Map<String, dynamic>))
        .toList();
  }

  @override
  Future<ConversationSnapshot> getConversation(
    ConversationLocator locator,
  ) async {
    _checkReady();
    final uri = _uri('/api/mobile/v1/conversations/${locator.conversationId}');
    final resp = await _http
        .get(uri, headers: _headers)
        .timeout(const Duration(seconds: 15));
    _checkResponse(resp);
    final data = _decodeData(resp.body);
    final convoJson = data['conversation'] as Map<String, dynamic>? ?? data;
    final conversation = _parseConversation(convoJson);
    return ConversationSnapshot(
      locator: locator,
      conversation: conversation,
      revision: conversation.revision,
    );
  }

  @override
  Future<ConversationLocator> createConversation(
    CreateConversationRequest request,
  ) async {
    _checkReady();
    final uri = _uri('/api/mobile/v1/conversations');
    final body = <String, dynamic>{};
    if (request.title != null && request.title!.trim().isNotEmpty) {
      body['title'] = request.title!.trim();
    }
    final resp = await _http
        .post(uri, headers: _headers, body: jsonEncode(body))
        .timeout(const Duration(seconds: 15));
    _checkResponse(resp);
    final data = _decodeData(resp.body);
    final id = data['conversation_id'] as String? ??
        (data['conversation'] as Map<String, dynamic>?)?['id'] as String? ??
        '';
    return ConversationLocator.pc(id, deviceId: deviceId);
  }

  @override
  Stream<ChatEvent> sendMessage({
    required ConversationLocator locator,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
    String? model,
    String? profileId,
    Map<String, dynamic>? params,
  }) async* {
    _checkReady();

    final runId = _uuid.v4();
    final assistantId = _uuid.v4();

    yield ChatRunStarted(
      locator: locator,
      runId: runId,
      assistantMessageId: assistantId,
    );

    final uri = _uri(
      '/api/mobile/v1/conversations/${locator.conversationId}/stream',
    );
    final requestParams = <String, dynamic>{
      if (params != null) ...params,
    };
    final selectedModel = (model ?? profileId ?? '').trim();
    final selectedProfile = (profileId ?? model ?? '').trim();
    if (selectedModel.isNotEmpty) {
      requestParams['model'] = selectedModel;
    }
    if (selectedProfile.isNotEmpty) {
      requestParams['profile_id'] = selectedProfile;
    }
    final metadata = requestParams.remove('metadata');

    final payload = <String, dynamic>{
      'message': {
        'role': 'user',
        'content': text,
        'client_message_id': clientMessageId,
        if (metadata is Map<String, dynamic>) 'metadata': metadata,
      },
      'client_message_id': clientMessageId,
      'expected_revision': expectedRevision,
      if (selectedModel.isNotEmpty) 'model': selectedModel,
      if (selectedProfile.isNotEmpty) 'profile_id': selectedProfile,
      if (requestParams.isNotEmpty) 'params': requestParams,
    };
    final body = jsonEncode(payload);

    try {
      final request = http.Request('POST', uri);
      request.headers.addAll({..._headers, 'Accept': 'text/event-stream'});
      request.body = body;

      final streamed =
          await _http.send(request).timeout(const Duration(seconds: 30));

      if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
        final errBody = await streamed.stream.bytesToString();
        yield ChatErrorEvent(
          locator: locator,
          runId: runId,
          assistantMessageId: assistantId,
          message: 'PC API エラー (HTTP ${streamed.statusCode}): $errBody',
        );
        return;
      }

      final buffer = StringBuffer();
      final contentBuffer = StringBuffer();

      await for (final chunk in streamed.stream.transform(utf8.decoder)) {
        if (_closed) break;
        buffer.write(chunk);
        while (true) {
          final nl = buffer.toString().indexOf('\n');
          if (nl < 0) break;
          final line = buffer.toString().substring(0, nl);
          final remaining = buffer.toString().substring(nl + 1);
          buffer.clear();
          buffer.write(remaining);
          final trimmed = line.trim();
          if (trimmed.isEmpty) continue;
          if (!trimmed.startsWith('data:')) continue;
          final data = trimmed.substring(5).trim();
          if (data == '[DONE]') {
            yield ChatMessageCommitted(
              locator: locator,
              runId: runId,
              messageId: assistantId,
              content: contentBuffer.toString(),
              error: false,
            );
            yield ChatRunCompleted(locator: locator, runId: runId);
            return;
          }
          final event = _parseSseEvent(
            data,
            locator: locator,
            runId: runId,
            assistantId: assistantId,
            contentBuffer: contentBuffer,
          );
          if (event != null) yield event;
        }
      }

      if (!_closed) {
        yield ChatMessageCommitted(
          locator: locator,
          runId: runId,
          messageId: assistantId,
          content: contentBuffer.toString(),
          error: false,
        );
        yield ChatRunCompleted(locator: locator, runId: runId);
      }
    } catch (error) {
      yield ChatErrorEvent(
        locator: locator,
        runId: runId,
        assistantMessageId: assistantId,
        message: 'PC通信エラー: $error',
      );
    }
  }

  @override
  Future<void> stop(String conversationId) async {
    _checkReady();
    final uri = _uri('/api/mobile/v1/conversations/$conversationId/stop');
    try {
      await _http
          .post(uri, headers: _headers)
          .timeout(const Duration(seconds: 10));
    } catch (_) {
      // ignore stop errors
    }
  }

  ChatEvent? _parseSseEvent(
    String data, {
    required ConversationLocator locator,
    required String runId,
    required String assistantId,
    required StringBuffer contentBuffer,
  }) {
    try {
      final json = jsonDecode(data) as Map<String, dynamic>;
      final type = json['type'] as String? ?? 'delta';

      switch (type) {
        case 'content_delta':
        case 'delta':
          final data = json['data'] as Map<String, dynamic>? ?? const {};
          final delta = json['delta'] as String? ??
              data['delta'] as String? ??
              json['text'] as String? ??
              '';
          final content =
              json['content'] as String? ?? data['content'] as String? ?? '';
          if (content.isNotEmpty) {
            contentBuffer.clear();
            contentBuffer.write(content);
          } else if (delta.isNotEmpty) {
            contentBuffer.write(delta);
          }
          if (delta.isNotEmpty || content.isNotEmpty) {
            return ChatDelta(
              locator: locator,
              runId: runId,
              assistantMessageId: assistantId,
              delta: delta,
              accumulatedContent:
                  content.isNotEmpty ? content : contentBuffer.toString(),
            );
          }
          return null;

        case 'message':
        case 'done':
          final content = _messageContent(json['message']);
          if (content.isEmpty) return null;
          contentBuffer.clear();
          contentBuffer.write(content);
          return ChatDelta(
            locator: locator,
            runId: runId,
            assistantMessageId: assistantId,
            delta: '',
            accumulatedContent: content,
          );

        case 'status':
        case 'tool_selection_started':
        case 'tool_selection_completed':
        case 'thinking_delta':
          final data = json['data'] as Map<String, dynamic>? ?? const {};
          final message = json['message'] as String? ??
              data['message'] as String? ??
              (type == 'thinking_delta' ? '考えています' : '');
          if (message.isEmpty) return null;
          return ChatStatusEvent(
            locator: locator,
            runId: runId,
            message: message,
            phase: json['phase'] as String? ?? data['phase'] as String? ?? type,
          );

        case 'tool_call':
        case 'tool_call_started':
        case 'tool_call_completed':
        case 'tool_call_delta':
          final data = json['data'] as Map<String, dynamic>? ?? const {};
          final status = json['tool_status'] as String? ??
              json['status'] as String? ??
              data['tool_status'] as String? ??
              data['status'] as String? ??
              (type == 'tool_call_completed' ? 'completed' : 'running');
          return ToolCallEvent(
            locator: locator,
            runId: runId,
            toolId: json['tool_id'] as String? ??
                json['tool_call_id'] as String? ??
                data['tool_id'] as String? ??
                data['tool_call_id'] as String? ??
                '',
            toolName: json['tool_name'] as String? ??
                data['tool_name'] as String? ??
                '',
            status: status,
            arguments: (json['arguments'] as Map<String, dynamic>?) ??
                (data['arguments'] as Map<String, dynamic>?) ??
                const {},
            summary: json['summary'] as String? ??
                data['summary'] as String? ??
                json['result_summary'] as String? ??
                data['result_summary'] as String? ??
                json['message'] as String?,
            output: json['output'] as String? ?? data['output'] as String?,
          );

        case 'approval':
        case 'approval_requested':
          final data = json['data'] as Map<String, dynamic>? ?? const {};
          return ApprovalEvent(
            locator: locator,
            runId: runId,
            approvalId: json['approval_id'] as String? ??
                json['request_id'] as String? ??
                data['approval_id'] as String? ??
                data['request_id'] as String? ??
                '',
            toolName: json['tool_name'] as String? ??
                data['tool_name'] as String? ??
                '',
            prompt: json['prompt'] as String? ??
                data['prompt'] as String? ??
                json['message'] as String? ??
                data['message'] as String? ??
                '',
            arguments: (json['arguments'] as Map<String, dynamic>?) ??
                (data['arguments'] as Map<String, dynamic>?) ??
                const {},
            approved: json['approved'] as bool? ?? false,
            pending: json['pending'] as bool? ?? true,
          );

        case 'error':
          final errorValue = json['error'];
          final fallbackMessage = json['message'] as String? ?? 'Unknown error';
          final errorMessage = errorValue is Map
              ? errorValue['message'] as String? ?? fallbackMessage
              : errorValue?.toString() ?? fallbackMessage;
          return ChatErrorEvent(
            locator: locator,
            runId: runId,
            assistantMessageId: assistantId,
            message: errorMessage,
          );

        default:
          return null;
      }
    } catch (_) {
      return null;
    }
  }

  String _messageContent(Object? message) {
    if (message is Map<String, dynamic>) {
      final content = message['content'];
      if (content is String) return content;
      if (content is List) {
        return content.map((part) {
          if (part is String) return part;
          if (part is Map && part['text'] is String) {
            return part['text'] as String;
          }
          return '';
        }).join();
      }
    }
    return '';
  }

  ConversationSummary _parseConversationSummary(Map<String, dynamic> json) {
    return ConversationSummary(
      id: _stringValue(json['id']),
      title: _stringValue(json['title'], fallback: '無題'),
      authority: ConversationAuthorityKind.pc,
      messageCount: (json['message_count'] as num?)?.toInt() ?? 0,
      updatedAt: _dateTimeValue(json['updated_at']),
      pinned: _boolValue(json['pinned'] ?? json['is_pinned']),
      revision: (json['revision'] as num?)?.toInt() ?? 0,
    );
  }

  Conversation _parseConversation(Map<String, dynamic> json) {
    final messages = (json['messages'] as List? ?? [])
        .map((m) => _parseMessage(m as Map<String, dynamic>))
        .toList();
    return Conversation(
      id: _stringValue(json['id']),
      title: _stringValue(json['title'], fallback: '無題'),
      messages: messages,
      createdAt: _dateTimeValue(json['created_at']),
      updatedAt: _dateTimeValue(json['updated_at']),
      pinned: _boolValue(json['pinned'] ?? json['is_pinned']),
      revision: (json['revision'] as num?)?.toInt() ?? 0,
      authority: ConversationAuthorityKind.pc,
    );
  }

  ChatMessage _parseMessage(Map<String, dynamic> json) {
    return ChatMessage(
      id: _stringValue(json['id']),
      role: ChatRole.fromString(
        _stringValue(json['role'], fallback: 'assistant'),
      ),
      content: _messageContent(json),
      createdAt: _dateTimeValueOrNull(json['created_at']),
      pending: _boolValue(json['pending']),
      error: _boolValue(json['error']),
    );
  }

  String _stringValue(Object? value, {String fallback = ''}) {
    if (value == null) return fallback;
    if (value is String) return value;
    return value.toString();
  }

  bool _boolValue(Object? value) {
    if (value is bool) return value;
    if (value is num) return value != 0;
    if (value is String) {
      final normalized = value.trim().toLowerCase();
      return normalized == 'true' || normalized == '1' || normalized == 'yes';
    }
    return false;
  }

  DateTime _dateTimeValue(Object? value) {
    return _dateTimeValueOrNull(value) ?? DateTime.now();
  }

  DateTime? _dateTimeValueOrNull(Object? value) {
    if (value == null) return null;
    if (value is DateTime) return value;
    if (value is String) {
      final trimmed = value.trim();
      if (trimmed.isEmpty) return null;
      final parsed = DateTime.tryParse(trimmed);
      if (parsed != null) return parsed;
      final numeric = num.tryParse(trimmed);
      if (numeric != null) return _dateTimeFromEpoch(numeric);
      return null;
    }
    if (value is num) return _dateTimeFromEpoch(value);
    return null;
  }

  DateTime _dateTimeFromEpoch(num value) {
    final raw = value.round();
    final millis = raw > 1000000000000 ? raw : raw * 1000;
    return DateTime.fromMillisecondsSinceEpoch(millis, isUtc: true).toLocal();
  }

  Map<String, dynamic> _decodeData(String body) {
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      if (decoded['status'] != 'ok') {
        final err = decoded['error'];
        final msg = err is Map ? err['message'] as String? : null;
        throw StateError(msg ?? 'PCからエラーが返されました。');
      }
      return decoded['data'] as Map<String, dynamic>? ?? const {};
    } catch (e) {
      if (e is StateError) rethrow;
      throw StateError('PC応答の解析に失敗しました: $e');
    }
  }

  void _checkReady() {
    if (!connection.isConfigured) {
      throw StateError('PC接続が設定されていません。');
    }
    if (_closed) {
      throw StateError('クライアントは閉じられました。');
    }
  }

  void _checkResponse(http.Response resp) {
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw StateError('PC API エラー (HTTP ${resp.statusCode}): ${resp.body}');
    }
  }

  String _trimTrailingSlash(String path) {
    if (path.isEmpty || path == '/') return '';
    return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
  }
}
