import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../data/local/mobile_tool_runtime.dart';
import '../settings/api_config_store.dart';
import 'chat_models.dart';

sealed class OpenAiClientEvent {
  const OpenAiClientEvent();
}

class OpenAiContentDelta extends OpenAiClientEvent {
  const OpenAiContentDelta(this.delta);
  final String delta;
}

class OpenAiStatusUpdate extends OpenAiClientEvent {
  const OpenAiStatusUpdate(this.message, {this.phase = ''});
  final String message;
  final String phase;
}

class OpenAiToolCallUpdate extends OpenAiClientEvent {
  const OpenAiToolCallUpdate({
    required this.call,
    required this.status,
    this.result,
  });

  final MobileToolCall call;
  final String status;
  final MobileToolResult? result;
}

class _OpenAiToolCallsReady extends OpenAiClientEvent {
  const _OpenAiToolCallsReady(this.calls);
  final List<MobileToolCall> calls;
}

class OpenAiClient {
  OpenAiClient({http.Client? client}) : _http = client ?? http.Client();

  final http.Client _http;
  bool _cancelled = false;

  void cancel() => _cancelled = true;
  void close() => _http.close();

  Stream<String> streamChat({
    required ApiConfig config,
    required List<ChatMessage> history,
  }) async* {
    _cancelled = false;
    if (!config.isConfigured) {
      throw const OpenAiException('APIのURLとキーを設定してください。');
    }
    if (config.apiCompatibility == 'anthropic_messages' ||
        config.providerId == 'anthropic') {
      yield* _streamAnthropic(config: config, history: history);
      return;
    }

    final uri = _chatCompletionsUri(config.baseUrl);
    final body = jsonEncode({
      'model': config.model,
      'messages': _buildMessages(config, history),
      'stream': true,
      'temperature': config.temperature,
    });

    final request = http.Request('POST', uri);
    request.headers.addAll({
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
      'Authorization': 'Bearer ${config.apiKey.trim()}',
    });
    request.body = body;

    final streamed =
        await _http.send(request).timeout(const Duration(seconds: 30));
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      final text = await streamed.stream.bytesToString();
      throw OpenAiException(
        _friendlyHttpError(streamed.statusCode, text),
        statusCode: streamed.statusCode,
      );
    }

    final buffer = StringBuffer();
    await for (final chunk in streamed.stream.transform(utf8.decoder)) {
      if (_cancelled) {
        break;
      }
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
        if (data == '[DONE]') return;
        final delta = _parseDelta(data);
        if (delta.isNotEmpty) {
          yield delta;
        }
      }
    }
  }

  Stream<OpenAiClientEvent> streamAgentChat({
    required ApiConfig config,
    required List<ChatMessage> history,
    MobileToolRuntime toolRuntime = const MobileToolRuntime(),
    int maxToolRounds = 4,
  }) async* {
    _cancelled = false;
    if (!config.isConfigured) {
      throw const OpenAiException('APIのURLとキーを設定してください。');
    }
    if (config.apiCompatibility == 'anthropic_messages' ||
        config.providerId == 'anthropic') {
      yield* _streamAnthropicAgentChat(
        config: config,
        history: history,
        toolRuntime: toolRuntime,
        maxToolRounds: maxToolRounds,
      );
      return;
    }
    if (config.apiCompatibility != 'openai') {
      yield const OpenAiStatusUpdate(
        'このプロバイダーではJSON tool protocolでスマホ内toolを試します。',
        phase: 'tools_text_protocol',
      );
      yield* _streamTextToolProtocolAgentChat(
        config: config,
        history: history,
        toolRuntime: toolRuntime,
        maxToolRounds: maxToolRounds,
      );
      return;
    }

    final messages = _buildAgentMessages(config, history);
    final tools = toolRuntime.openAiTools();
    var toolRounds = 0;

    while (!_cancelled) {
      final turnText = StringBuffer();
      final toolCalls = <MobileToolCall>[];
      try {
        await for (final event in _streamOpenAiTurn(
          config: config,
          messages: messages,
          tools: toolRounds < maxToolRounds ? tools : const [],
        )) {
          switch (event) {
            case OpenAiContentDelta():
              turnText.write(event.delta);
              yield event;
            case _OpenAiToolCallsReady():
              toolCalls.addAll(event.calls);
            case OpenAiStatusUpdate():
              yield event;
            case OpenAiToolCallUpdate():
              yield event;
          }
        }
      } on OpenAiException catch (error) {
        if (toolRounds == 0 && _looksLikeToolCompatibilityError(error)) {
          yield const OpenAiStatusUpdate(
            'このモデルはnative tool呼び出しを受け付けませんでした。JSON tool protocolで続行します。',
            phase: 'tools_fallback',
          );
          yield* _streamTextToolProtocolAgentChat(
            config: config,
            history: history,
            toolRuntime: toolRuntime,
            maxToolRounds: maxToolRounds,
          );
          return;
        }
        rethrow;
      }

      if (toolCalls.isEmpty) return;
      toolRounds += 1;
      messages.add(_assistantToolCallMessage(turnText.toString(), toolCalls));
      final externalToolCount = toolCalls
          .where((call) =>
              !MobileToolRuntime.isAssistantProgressToolName(call.name))
          .length;
      if (externalToolCount > 0) {
        yield OpenAiStatusUpdate(
          '$externalToolCount 個のtoolを実行しています',
          phase: 'tool_execution',
        );
      }

      for (final call in toolCalls) {
        if (_cancelled) return;
        final isProgress =
            MobileToolRuntime.isAssistantProgressToolName(call.name);
        if (!isProgress) {
          yield OpenAiToolCallUpdate(call: call, status: 'running');
        }
        final result = await toolRuntime.executeAsync(call);
        yield OpenAiToolCallUpdate(
          call: call,
          status: result.ok ? 'completed' : 'failed',
          result: result,
        );
        messages.add({
          'role': 'tool',
          'tool_call_id': call.id,
          'content': result.toToolMessageContent(),
        });
      }
    }
  }

  List<Map<String, String>> _buildMessages(
      ApiConfig config, List<ChatMessage> history) {
    final messages = <Map<String, String>>[];
    if (config.systemPrompt.trim().isNotEmpty) {
      messages.add({'role': 'system', 'content': config.systemPrompt.trim()});
    }
    for (final m in history) {
      if (m.content.trim().isEmpty && m.role != ChatRole.user) continue;
      messages.add({'role': m.role.value, 'content': m.content});
    }
    return messages;
  }

  List<Map<String, dynamic>> _buildAgentMessages(
      ApiConfig config, List<ChatMessage> history) {
    final messages = <Map<String, dynamic>>[];
    final system = _agentSystemPrompt(config);
    if (system.trim().isNotEmpty) {
      messages.add({'role': 'system', 'content': system.trim()});
    }
    for (final m in history) {
      if (m.content.trim().isEmpty && m.role != ChatRole.user) continue;
      messages.add({'role': m.role.value, 'content': m.content});
    }
    return messages;
  }

  String _agentSystemPrompt(ApiConfig config) {
    return [
      config.systemPrompt.trim(),
      'You are running inside Rumi Mobile using the defaultspack mobile agent template.',
      'Template ids: template=$mobileAgentTemplateId, ai_input=$mobileAgentAiInputId, tool_policy=$mobileAgentToolPolicyId.',
      'For multi-step work, act like a small defaultspack agent: create or update a plan with agent_plan, track concrete work with todo or tool_task_board, and check agent_status/agent_progress when useful.',
      'Use available tools when they help. If a requested defaultspack tool is host-bound, explain the unavailable reason and suggest switching to the PC space.',
      'For unknown or defaultspack-specific tools, inspect tool_schema or tool_search before claiming they are unavailable.',
      mobileAssistantProgressSystemInstruction,
    ].where((part) => part.isNotEmpty).join('\n\n');
  }

  Stream<OpenAiClientEvent> _streamOpenAiTurn({
    required ApiConfig config,
    required List<Map<String, dynamic>> messages,
    required List<Map<String, dynamic>> tools,
  }) async* {
    final uri = _chatCompletionsUri(config.baseUrl);
    final body = <String, dynamic>{
      'model': config.model,
      'messages': messages,
      'stream': true,
      'temperature': config.temperature,
      if (tools.isNotEmpty) 'tools': tools,
      if (tools.isNotEmpty) 'tool_choice': 'auto',
    };

    final request = http.Request('POST', uri);
    request.headers.addAll({
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
      'Authorization': 'Bearer ${config.apiKey.trim()}',
    });
    request.body = jsonEncode(body);

    final streamed =
        await _http.send(request).timeout(const Duration(seconds: 30));
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      final text = await streamed.stream.bytesToString();
      throw OpenAiException(
        _friendlyHttpError(streamed.statusCode, text),
        statusCode: streamed.statusCode,
      );
    }

    final buffer = StringBuffer();
    final toolAccumulator = _ToolCallAccumulator();
    await for (final chunk in streamed.stream.transform(utf8.decoder)) {
      if (_cancelled) break;
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
        if (data == '[DONE]') break;
        final event = _parseOpenAiAgentEvent(data, toolAccumulator);
        if (event != null) yield event;
      }
    }
    final calls = toolAccumulator.calls();
    if (calls.isNotEmpty) yield _OpenAiToolCallsReady(calls);
  }

  OpenAiClientEvent? _parseOpenAiAgentEvent(
    String json,
    _ToolCallAccumulator toolAccumulator,
  ) {
    try {
      final decoded = jsonDecode(json) as Map<String, dynamic>;
      final choices = decoded['choices'] as List?;
      if (choices == null || choices.isEmpty) return null;
      final choice = choices.first as Map<String, dynamic>;
      final delta = choice['delta'] as Map<String, dynamic>? ?? const {};
      final toolCalls = delta['tool_calls'];
      if (toolCalls is List) {
        toolAccumulator.add(toolCalls);
      }
      final content = delta['content'];
      if (content is String && content.isNotEmpty) {
        return OpenAiContentDelta(content);
      }
    } catch (_) {
      return null;
    }
    return null;
  }

  Map<String, dynamic> _assistantToolCallMessage(
    String content,
    List<MobileToolCall> calls,
  ) {
    return {
      'role': 'assistant',
      'content': content.trim().isEmpty ? null : content,
      'tool_calls': [
        for (final call in calls)
          {
            'id': call.id,
            'type': 'function',
            'function': {
              'name': call.name,
              'arguments': jsonEncode(call.arguments),
            },
          },
      ],
    };
  }

  bool _looksLikeToolCompatibilityError(OpenAiException error) {
    final message = error.message.toLowerCase();
    return error.statusCode == 400 &&
        (message.contains('tool') ||
            message.contains('function') ||
            message.contains('unsupported'));
  }

  Stream<OpenAiClientEvent> _streamTextToolProtocolAgentChat({
    required ApiConfig config,
    required List<ChatMessage> history,
    required MobileToolRuntime toolRuntime,
    required int maxToolRounds,
  }) async* {
    final messages =
        _buildTextToolProtocolMessages(config, history, toolRuntime);
    var toolRounds = 0;

    while (!_cancelled) {
      final text = await _collectOpenAiTextTurn(
        config: config,
        messages: messages,
      );
      if (_cancelled) return;
      final parsed = _parseTextToolProtocolResponse(text);

      if (parsed.calls.isEmpty) {
        final finalText =
            parsed.finalText.trim().isEmpty ? text : parsed.finalText;
        if (finalText.isNotEmpty) yield OpenAiContentDelta(finalText);
        return;
      }

      if (toolRounds >= maxToolRounds) {
        yield const OpenAiContentDelta(
          'tool実行回数の上限に達したため、ここまでの結果で停止しました。',
        );
        return;
      }

      toolRounds += 1;
      messages.add({'role': 'assistant', 'content': text.trim()});
      final externalToolCount = parsed.calls
          .where((call) =>
              !MobileToolRuntime.isAssistantProgressToolName(call.name))
          .length;
      if (externalToolCount > 0) {
        yield OpenAiStatusUpdate(
          '$externalToolCount 個のtoolを実行しています',
          phase: 'tool_execution',
        );
      }

      final toolResults = <Map<String, dynamic>>[];
      for (final call in parsed.calls) {
        if (_cancelled) return;
        final isProgress =
            MobileToolRuntime.isAssistantProgressToolName(call.name);
        if (!isProgress) {
          yield OpenAiToolCallUpdate(call: call, status: 'running');
        }
        final result = await toolRuntime.executeAsync(call);
        yield OpenAiToolCallUpdate(
          call: call,
          status: result.ok ? 'completed' : 'failed',
          result: result,
        );
        toolResults.add({
          'id': call.id,
          'name': call.name,
          'status': result.ok ? 'ok' : 'error',
          'summary': result.summary,
          'output': result.output,
        });
      }

      messages.add({
        'role': 'user',
        'content': [
          'Tool results JSON:',
          jsonEncode({'tool_results': toolResults}),
          'Continue. If more tools are required, respond only with the tool_calls JSON object. Otherwise answer the user normally.',
        ].join('\n'),
      });
    }
  }

  List<Map<String, dynamic>> _buildTextToolProtocolMessages(
    ApiConfig config,
    List<ChatMessage> history,
    MobileToolRuntime toolRuntime,
  ) {
    final messages = <Map<String, dynamic>>[];
    final system = [
      _agentSystemPrompt(config),
      _textToolProtocolInstruction(toolRuntime),
    ].where((part) => part.trim().isNotEmpty).join('\n\n');
    if (system.trim().isNotEmpty) {
      messages.add({'role': 'system', 'content': system.trim()});
    }
    for (final m in history) {
      if (m.content.trim().isEmpty && m.role != ChatRole.user) continue;
      messages.add({'role': m.role.value, 'content': m.content});
    }
    return messages;
  }

  String _textToolProtocolInstruction(MobileToolRuntime toolRuntime) {
    final available =
        toolRuntime.availableTools.where((tool) => tool.available).map((tool) {
      final names = tool.openAiNames.take(4).join(', ');
      return {'names': names, 'description': tool.description};
    }).toList();
    final catalogCount = toolRuntime.knownDefaultspackToolAgentCount;
    final hostBoundCount = toolRuntime.knownUnavailableDefaultspackToolCount;
    return [
      'JSON tool protocol fallback:',
      'When a tool helps, respond ONLY with a JSON object and no markdown:',
      '{"tool_calls":[{"id":"call_1","name":"tool_name","arguments":{}}]}',
      'When no more tools are needed, answer the user normally. You may also respond as {"final":"..."} if you need structured final text.',
      'Available phone-executable tools:',
      jsonEncode(available),
      'Generated defaultspack catalog: $catalogCount tool/agent manifests are known to the phone runtime. $hostBoundCount are not phone-executable and require PC/defaultspack runtime.',
      toolRuntime.pcDelegationAvailable
          ? 'PC tool delegation is enabled. Use one tool surface: call the defaultspack tool name directly when you know it. Phone-compatible tools run locally; host-bound tools route automatically to the connected PC runtime. Use tool_search or tool_schema only when you need discovery or schema details; tool_invoke is optional compatibility.'
          : 'For unknown/defaultspack tools, call tool_search or tool_schema first. If a tool result says unavailable, explain that reason and suggest switching to the PC space.',
    ].join('\n');
  }

  Future<String> _collectOpenAiTextTurn({
    required ApiConfig config,
    required List<Map<String, dynamic>> messages,
  }) async {
    final uri = _chatCompletionsUri(config.baseUrl);
    final body = jsonEncode({
      'model': config.model,
      'messages': messages,
      'stream': true,
      'temperature': config.temperature,
    });

    final request = http.Request('POST', uri);
    request.headers.addAll({
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
      'Authorization': 'Bearer ${config.apiKey.trim()}',
    });
    request.body = body;

    final streamed =
        await _http.send(request).timeout(const Duration(seconds: 30));
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      final text = await streamed.stream.bytesToString();
      throw OpenAiException(
        _friendlyHttpError(streamed.statusCode, text),
        statusCode: streamed.statusCode,
      );
    }

    final content = StringBuffer();
    final buffer = StringBuffer();
    await for (final chunk in streamed.stream.transform(utf8.decoder)) {
      if (_cancelled) break;
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
        if (data == '[DONE]') return content.toString();
        content.write(_parseDelta(data));
      }
    }
    return content.toString();
  }

  _TextToolProtocolResponse _parseTextToolProtocolResponse(String text) {
    final candidates = <String>[_stripJsonCodeFence(text.trim())];
    final embeddedJson = _firstJsonObject(text);
    if (embeddedJson != null && embeddedJson.trim().isNotEmpty) {
      candidates.add(embeddedJson);
    }
    for (final candidate in candidates) {
      try {
        final decoded = jsonDecode(candidate);
        final parsed = _textToolProtocolFromDecoded(decoded);
        if (parsed != null) return parsed;
      } catch (_) {
        // Try the next candidate.
      }
    }
    return _TextToolProtocolResponse(finalText: text, calls: const []);
  }

  _TextToolProtocolResponse? _textToolProtocolFromDecoded(Object? decoded) {
    if (decoded is List) {
      final calls = _textToolCallsFromList(decoded);
      if (calls.isNotEmpty) {
        return _TextToolProtocolResponse(finalText: '', calls: calls);
      }
      return null;
    }
    if (decoded is! Map) return null;
    final json = Map<String, dynamic>.from(decoded);
    final toolCalls = json['tool_calls'] ?? json['tools'];
    if (toolCalls is List) {
      return _TextToolProtocolResponse(
        finalText: '',
        calls: _textToolCallsFromList(toolCalls),
      );
    }
    final singleName = json['name'] ?? json['tool'] ?? json['tool_name'];
    if (singleName is String && singleName.trim().isNotEmpty) {
      final call = _textToolCallFromMap(json, 0);
      if (call != null) {
        return _TextToolProtocolResponse(finalText: '', calls: [call]);
      }
    }
    final finalText = json['final'] ?? json['answer'] ?? json['content'];
    if (finalText is String) {
      return _TextToolProtocolResponse(
        finalText: finalText,
        calls: const [],
      );
    }
    return null;
  }

  List<MobileToolCall> _textToolCallsFromList(List<dynamic> rawCalls) {
    final calls = <MobileToolCall>[];
    for (var i = 0; i < rawCalls.length; i += 1) {
      final raw = rawCalls[i];
      if (raw is! Map) continue;
      final call = _textToolCallFromMap(Map<String, dynamic>.from(raw), i);
      if (call != null) calls.add(call);
    }
    return calls;
  }

  MobileToolCall? _textToolCallFromMap(Map<String, dynamic> raw, int index) {
    final name = (raw['name'] ?? raw['tool'] ?? raw['tool_name'] ?? '')
        .toString()
        .trim();
    if (name.isEmpty) return null;
    final id = (raw['id'] ?? raw['tool_call_id'] ?? 'call_text_$index')
        .toString()
        .trim();
    return MobileToolCall(
      id: id.isEmpty ? 'call_text_$index' : id,
      name: name,
      arguments: _decodeTextToolArguments(
        raw['arguments'] ?? raw['args'] ?? raw['input'] ?? const {},
      ),
    );
  }

  Map<String, dynamic> _decodeTextToolArguments(Object? raw) {
    if (raw is Map<String, dynamic>) return raw;
    if (raw is Map) return raw.map((key, value) => MapEntry('$key', value));
    if (raw is String) {
      final trimmed = raw.trim();
      if (trimmed.isEmpty) return const {};
      try {
        final decoded = jsonDecode(trimmed);
        if (decoded is Map<String, dynamic>) return decoded;
        if (decoded is Map) {
          return decoded.map((key, value) => MapEntry('$key', value));
        }
      } catch (_) {
        return {'input': trimmed};
      }
      return {'input': trimmed};
    }
    return const {};
  }

  String _stripJsonCodeFence(String text) {
    final trimmed = text.trim();
    if (!trimmed.startsWith('```')) return trimmed;
    final lines = trimmed.split(RegExp(r'\r?\n'));
    if (lines.length < 2 || !lines.last.trim().startsWith('```')) {
      return trimmed;
    }
    return lines.sublist(1, lines.length - 1).join('\n').trim();
  }

  String? _firstJsonObject(String text) {
    final start = text.indexOf('{');
    if (start < 0) return null;
    var depth = 0;
    var inString = false;
    var escaped = false;
    for (var i = start; i < text.length; i += 1) {
      final char = text[i];
      if (inString) {
        if (escaped) {
          escaped = false;
        } else if (char == '\\') {
          escaped = true;
        } else if (char == '"') {
          inString = false;
        }
        continue;
      }
      if (char == '"') {
        inString = true;
      } else if (char == '{') {
        depth += 1;
      } else if (char == '}') {
        depth -= 1;
        if (depth == 0) return text.substring(start, i + 1);
      }
    }
    return null;
  }

  Stream<OpenAiClientEvent> _streamAnthropicAgentChat({
    required ApiConfig config,
    required List<ChatMessage> history,
    required MobileToolRuntime toolRuntime,
    required int maxToolRounds,
  }) async* {
    final messages = _buildAnthropicAgentMessages(history);
    final system = _agentSystemPrompt(config);
    final tools = _anthropicTools(toolRuntime.openAiTools());
    var toolRounds = 0;

    while (!_cancelled) {
      final turnText = StringBuffer();
      final toolCalls = <MobileToolCall>[];
      await for (final event in _streamAnthropicTurn(
        config: config,
        system: system,
        messages: messages,
        tools: toolRounds < maxToolRounds ? tools : const [],
      )) {
        switch (event) {
          case OpenAiContentDelta():
            turnText.write(event.delta);
            yield event;
          case _OpenAiToolCallsReady():
            toolCalls.addAll(event.calls);
          case OpenAiStatusUpdate():
            yield event;
          case OpenAiToolCallUpdate():
            yield event;
        }
      }

      if (toolCalls.isEmpty) return;
      toolRounds += 1;
      messages.add(_anthropicAssistantToolUseMessage(
        turnText.toString(),
        toolCalls,
      ));
      final externalToolCount = toolCalls
          .where((call) =>
              !MobileToolRuntime.isAssistantProgressToolName(call.name))
          .length;
      if (externalToolCount > 0) {
        yield OpenAiStatusUpdate(
          '$externalToolCount 個のtoolを実行しています',
          phase: 'tool_execution',
        );
      }

      final toolResults = <Map<String, dynamic>>[];
      for (final call in toolCalls) {
        if (_cancelled) return;
        final isProgress =
            MobileToolRuntime.isAssistantProgressToolName(call.name);
        if (!isProgress) {
          yield OpenAiToolCallUpdate(call: call, status: 'running');
        }
        final result = await toolRuntime.executeAsync(call);
        yield OpenAiToolCallUpdate(
          call: call,
          status: result.ok ? 'completed' : 'failed',
          result: result,
        );
        toolResults.add({
          'type': 'tool_result',
          'tool_use_id': call.id,
          'content': result.toToolMessageContent(),
          if (!result.ok) 'is_error': true,
        });
      }
      messages.add({'role': 'user', 'content': toolResults});
    }
  }

  Stream<String> _streamAnthropic({
    required ApiConfig config,
    required List<ChatMessage> history,
  }) async* {
    final uri = _anthropicMessagesUri(config.baseUrl);
    final body = jsonEncode({
      'model': config.model,
      'messages': _buildAnthropicMessages(history),
      'stream': true,
      'max_tokens': 4096,
      if (config.systemPrompt.trim().isNotEmpty)
        'system': config.systemPrompt.trim(),
    });

    final request = http.Request('POST', uri);
    request.headers.addAll({
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
      'x-api-key': config.apiKey.trim(),
      'anthropic-version': '2023-06-01',
    });
    request.body = body;

    final streamed =
        await _http.send(request).timeout(const Duration(seconds: 30));
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      final text = await streamed.stream.bytesToString();
      throw OpenAiException(
        _friendlyHttpError(streamed.statusCode, text),
        statusCode: streamed.statusCode,
      );
    }

    final buffer = StringBuffer();
    await for (final chunk in streamed.stream.transform(utf8.decoder)) {
      if (_cancelled) break;
      buffer.write(chunk);
      while (true) {
        final eventEnd = buffer.toString().indexOf('\n\n');
        if (eventEnd < 0) break;
        final block = buffer.toString().substring(0, eventEnd);
        final remaining = buffer.toString().substring(eventEnd + 2);
        buffer.clear();
        buffer.write(remaining);
        final delta = _parseAnthropicDelta(block);
        if (delta.isNotEmpty) yield delta;
      }
    }
  }

  Stream<OpenAiClientEvent> _streamAnthropicTurn({
    required ApiConfig config,
    required String system,
    required List<Map<String, dynamic>> messages,
    required List<Map<String, dynamic>> tools,
  }) async* {
    final uri = _anthropicMessagesUri(config.baseUrl);
    final body = jsonEncode({
      'model': config.model,
      'messages': messages,
      'stream': true,
      'max_tokens': 4096,
      if (system.trim().isNotEmpty) 'system': system.trim(),
      if (tools.isNotEmpty) 'tools': tools,
      if (tools.isNotEmpty) 'tool_choice': {'type': 'auto'},
    });

    final request = http.Request('POST', uri);
    request.headers.addAll({
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
      'x-api-key': config.apiKey.trim(),
      'anthropic-version': '2023-06-01',
    });
    request.body = body;

    final streamed =
        await _http.send(request).timeout(const Duration(seconds: 30));
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      final text = await streamed.stream.bytesToString();
      throw OpenAiException(
        _friendlyHttpError(streamed.statusCode, text),
        statusCode: streamed.statusCode,
      );
    }

    final buffer = StringBuffer();
    final toolAccumulator = _AnthropicToolCallAccumulator();
    await for (final chunk in streamed.stream.transform(utf8.decoder)) {
      if (_cancelled) break;
      buffer.write(chunk);
      while (true) {
        final eventEnd = buffer.toString().indexOf('\n\n');
        if (eventEnd < 0) break;
        final block = buffer.toString().substring(0, eventEnd);
        final remaining = buffer.toString().substring(eventEnd + 2);
        buffer.clear();
        buffer.write(remaining);
        final event = _parseAnthropicAgentEvent(block, toolAccumulator);
        if (event != null) yield event;
      }
    }
    final calls = toolAccumulator.calls();
    if (calls.isNotEmpty) yield _OpenAiToolCallsReady(calls);
  }

  List<Map<String, String>> _buildAnthropicMessages(List<ChatMessage> history) {
    final messages = <Map<String, String>>[];
    for (final message in history) {
      if (message.content.trim().isEmpty) continue;
      if (message.role == ChatRole.system) continue;
      messages.add({
        'role': message.role == ChatRole.user ? 'user' : 'assistant',
        'content': message.content,
      });
    }
    if (messages.isEmpty || messages.first['role'] != 'user') {
      messages.insert(0, {'role': 'user', 'content': ''});
    }
    return messages;
  }

  List<Map<String, dynamic>> _buildAnthropicAgentMessages(
      List<ChatMessage> history) {
    final messages = <Map<String, dynamic>>[];
    for (final message in history) {
      if (message.content.trim().isEmpty) continue;
      if (message.role == ChatRole.system) continue;
      messages.add({
        'role': message.role == ChatRole.user ? 'user' : 'assistant',
        'content': message.content,
      });
    }
    if (messages.isEmpty || messages.first['role'] != 'user') {
      messages.insert(0, {'role': 'user', 'content': ''});
    }
    return messages;
  }

  Map<String, dynamic> _anthropicAssistantToolUseMessage(
    String content,
    List<MobileToolCall> calls,
  ) {
    final blocks = <Map<String, dynamic>>[];
    final text = content.trim();
    if (text.isNotEmpty) {
      blocks.add({'type': 'text', 'text': text});
    }
    for (final call in calls) {
      blocks.add({
        'type': 'tool_use',
        'id': call.id,
        'name': call.name,
        'input': call.arguments,
      });
    }
    return {'role': 'assistant', 'content': blocks};
  }

  List<Map<String, dynamic>> _anthropicTools(
    List<Map<String, dynamic>> openAiTools,
  ) {
    return [
      for (final tool in openAiTools)
        if (tool['function'] is Map)
          {
            'name': '${tool['function']['name']}',
            'description': '${tool['function']['description'] ?? ''}',
            'input_schema':
                tool['function']['parameters'] as Map<String, dynamic>? ??
                    const {'type': 'object', 'additionalProperties': true},
          },
    ];
  }

  String _parseDelta(String json) {
    try {
      final decoded = jsonDecode(json) as Map<String, dynamic>;
      final choices = decoded['choices'] as List?;
      if (choices == null || choices.isEmpty) return '';
      final choice = choices.first as Map<String, dynamic>;
      final delta = choice['delta'] as Map<String, dynamic>?;
      if (delta == null) return '';
      final content = delta['content'];
      return content is String ? content : '';
    } catch (_) {
      return '';
    }
  }

  Uri _chatCompletionsUri(String baseUrl) {
    var trimmed = baseUrl.trim();
    if (trimmed.isEmpty) {
      throw const OpenAiException('APIのURLを設定してください。');
    }
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final uri = Uri.parse(trimmed);
    if (uri.host.isEmpty) {
      throw OpenAiException('APIのURLが不正です: $baseUrl');
    }
    final path = _trimTrailingSlash(uri.path);
    if (path.endsWith('/chat/completions')) return uri;
    return uri.replace(path: '$path/chat/completions');
  }

  Uri _anthropicMessagesUri(String baseUrl) {
    var trimmed = baseUrl.trim();
    if (trimmed.isEmpty) {
      throw const OpenAiException('APIのURLを設定してください。');
    }
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final uri = Uri.parse(trimmed);
    if (uri.host.isEmpty) {
      throw OpenAiException('APIのURLが不正です: $baseUrl');
    }
    final path = _trimTrailingSlash(uri.path);
    if (path.endsWith('/messages')) return uri;
    if (path.endsWith('/v1')) return uri.replace(path: '$path/messages');
    return uri.replace(path: '$path/v1/messages');
  }

  String _parseAnthropicDelta(String block) {
    for (final line in block.split(RegExp(r'\r?\n'))) {
      final trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      final data = trimmed.substring(5).trim();
      if (data.isEmpty || data == '[DONE]') continue;
      try {
        final decoded = jsonDecode(data) as Map<String, dynamic>;
        final type = decoded['type'] as String? ?? '';
        if (type == 'content_block_delta') {
          final delta = decoded['delta'] as Map<String, dynamic>? ?? const {};
          final text = delta['text'];
          return text is String ? text : '';
        }
        if (type == 'error') {
          final error = decoded['error'] as Map<String, dynamic>? ?? const {};
          throw OpenAiException(
              error['message'] as String? ?? 'Anthropic error');
        }
      } on OpenAiException {
        rethrow;
      } catch (_) {
        return '';
      }
    }
    return '';
  }

  OpenAiClientEvent? _parseAnthropicAgentEvent(
    String block,
    _AnthropicToolCallAccumulator toolAccumulator,
  ) {
    for (final line in block.split(RegExp(r'\r?\n'))) {
      final trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      final data = trimmed.substring(5).trim();
      if (data.isEmpty || data == '[DONE]') continue;
      try {
        final decoded = jsonDecode(data) as Map<String, dynamic>;
        final type = decoded['type'] as String? ?? '';
        if (type == 'content_block_start') {
          toolAccumulator.start(decoded);
          final contentBlock =
              decoded['content_block'] as Map<String, dynamic>? ?? const {};
          if (contentBlock['type'] == 'text' &&
              contentBlock['text'] is String &&
              (contentBlock['text'] as String).isNotEmpty) {
            return OpenAiContentDelta(contentBlock['text'] as String);
          }
        } else if (type == 'content_block_delta') {
          final delta = decoded['delta'] as Map<String, dynamic>? ?? const {};
          if (delta['type'] == 'input_json_delta') {
            toolAccumulator.addDelta(decoded);
          } else if (delta['type'] == 'text_delta') {
            final text = delta['text'];
            if (text is String && text.isNotEmpty) {
              return OpenAiContentDelta(text);
            }
          }
        } else if (type == 'error') {
          final error = decoded['error'] as Map<String, dynamic>? ?? const {};
          throw OpenAiException(
              error['message'] as String? ?? 'Anthropic error');
        }
      } on OpenAiException {
        rethrow;
      } catch (_) {
        return null;
      }
    }
    return null;
  }

  String _friendlyHttpError(int code, String body) {
    try {
      final decoded = jsonDecode(body);
      if (decoded is Map<String, dynamic>) {
        final err = decoded['error'];
        if (err is Map && err['message'] is String) {
          return 'HTTP $code: ${err['message']}';
        }
        if (decoded['message'] is String) {
          return 'HTTP $code: ${decoded['message']}';
        }
      }
    } catch (_) {
      // ignore
    }
    return 'HTTP $code';
  }

  String _trimTrailingSlash(String path) {
    if (path.isEmpty || path == '/') return '';
    return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
  }
}

class _TextToolProtocolResponse {
  const _TextToolProtocolResponse({
    required this.finalText,
    required this.calls,
  });

  final String finalText;
  final List<MobileToolCall> calls;
}

class _ToolCallAccumulator {
  final Map<int, _ToolCallDraft> _drafts = {};

  void add(List<dynamic> deltas) {
    for (final raw in deltas) {
      if (raw is! Map) continue;
      final index = (raw['index'] as num?)?.toInt() ?? _drafts.length;
      final draft = _drafts.putIfAbsent(index, () => _ToolCallDraft());
      final id = raw['id'];
      if (id is String && id.isNotEmpty) draft.id = id;
      final function = raw['function'];
      if (function is Map) {
        final name = function['name'];
        if (name is String && name.isNotEmpty) draft.name = name;
        final arguments = function['arguments'];
        if (arguments is String && arguments.isNotEmpty) {
          draft.arguments.write(arguments);
        }
      }
    }
  }

  List<MobileToolCall> calls() {
    final calls = <MobileToolCall>[];
    final indexes = _drafts.keys.toList()..sort();
    for (final index in indexes) {
      final draft = _drafts[index]!;
      final name = draft.name.trim();
      if (name.isEmpty) continue;
      calls.add(
        MobileToolCall(
          id: draft.id.trim().isEmpty ? 'call_$index' : draft.id.trim(),
          name: name,
          arguments: _decodeArguments(draft.arguments.toString()),
        ),
      );
    }
    return calls;
  }

  Map<String, dynamic> _decodeArguments(String raw) {
    final trimmed = raw.trim();
    if (trimmed.isEmpty) return const {};
    try {
      final decoded = jsonDecode(trimmed);
      if (decoded is Map<String, dynamic>) return decoded;
      if (decoded is Map) {
        return decoded.map((key, value) => MapEntry('$key', value));
      }
    } catch (_) {
      return {'input': trimmed};
    }
    return {'input': trimmed};
  }
}

class _ToolCallDraft {
  String id = '';
  String name = '';
  final StringBuffer arguments = StringBuffer();
}

class _AnthropicToolCallAccumulator {
  final Map<int, _ToolCallDraft> _drafts = {};

  void start(Map<String, dynamic> event) {
    final index = (event['index'] as num?)?.toInt() ?? _drafts.length;
    final contentBlock =
        event['content_block'] as Map<String, dynamic>? ?? const {};
    if (contentBlock['type'] != 'tool_use') return;
    final draft = _drafts.putIfAbsent(index, () => _ToolCallDraft());
    final id = contentBlock['id'];
    if (id is String && id.isNotEmpty) draft.id = id;
    final name = contentBlock['name'];
    if (name is String && name.isNotEmpty) draft.name = name;
    final input = contentBlock['input'];
    if (input is Map && input.isNotEmpty) {
      draft.arguments.write(jsonEncode(input));
    }
  }

  void addDelta(Map<String, dynamic> event) {
    final index = (event['index'] as num?)?.toInt() ?? _drafts.length;
    final delta = event['delta'] as Map<String, dynamic>? ?? const {};
    final partial = delta['partial_json'];
    if (partial is! String || partial.isEmpty) return;
    final draft = _drafts.putIfAbsent(index, () => _ToolCallDraft());
    draft.arguments.write(partial);
  }

  List<MobileToolCall> calls() {
    final calls = <MobileToolCall>[];
    final indexes = _drafts.keys.toList()..sort();
    for (final index in indexes) {
      final draft = _drafts[index]!;
      final name = draft.name.trim();
      if (name.isEmpty) continue;
      calls.add(
        MobileToolCall(
          id: draft.id.trim().isEmpty ? 'toolu_$index' : draft.id.trim(),
          name: name,
          arguments: _decodeArguments(draft.arguments.toString()),
        ),
      );
    }
    return calls;
  }

  Map<String, dynamic> _decodeArguments(String raw) {
    final trimmed = raw.trim();
    if (trimmed.isEmpty) return const {};
    try {
      final decoded = jsonDecode(trimmed);
      if (decoded is Map<String, dynamic>) return decoded;
      if (decoded is Map) {
        return decoded.map((key, value) => MapEntry('$key', value));
      }
    } catch (_) {
      return {'input': trimmed};
    }
    return {'input': trimmed};
  }
}

class OpenAiException implements Exception {
  const OpenAiException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}
