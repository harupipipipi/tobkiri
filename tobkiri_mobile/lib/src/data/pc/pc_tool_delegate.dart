import 'dart:convert';

import '../../settings/api_config_store.dart';
import '../local/mobile_tool_runtime.dart';
import 'pc_catalog_client.dart';

class PcToolExecutionDelegate implements MobileToolDelegate {
  PcToolExecutionDelegate({
    required this.connection,
    PcCatalogClient Function()? createClient,
  }) : _createClient = createClient ?? PcCatalogClient.new;

  final PcConnection connection;
  final PcCatalogClient Function() _createClient;

  @override
  Future<MobileToolResult> invoke(MobileToolCall call) async {
    final request = _pcToolRequest(call);
    if (request.toolName.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'PC tool name is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'MISSING_PARAM',
            'message': 'PC tool name is required',
          },
        }),
      );
    }

    final client = _createClient();
    try {
      final data = await client.invokeTool(
        connection,
        toolName: request.toolName,
        arguments: request.arguments,
      );
      final isError = data['is_error'] == true;
      final result = '${data['result'] ?? data['message'] ?? ''}';
      final summary = _compactSummary(
        result.isNotEmpty ? result : '${data['tool_name'] ?? request.toolName}',
      );
      return MobileToolResult(
        ok: !isError,
        summary: 'PC ${request.toolName}: $summary',
        output: jsonEncode({
          'status': isError ? 'error' : 'ok',
          'data': {
            ...data,
            'requested_tool_name': request.requestedName,
            'execution_location': 'pc',
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'PC ${request.toolName}: failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'PC_TOOL_INVOKE_FAILED',
            'message': '$error',
            'tool_name': request.toolName,
            'execution_location': 'pc',
          },
        }),
      );
    } finally {
      client.close();
    }
  }
}

class _PcToolRequest {
  const _PcToolRequest({
    required this.toolName,
    required this.requestedName,
    required this.arguments,
  });

  final String toolName;
  final String requestedName;
  final Map<String, dynamic> arguments;
}

_PcToolRequest _pcToolRequest(MobileToolCall call) {
  final name = call.name.trim();
  final normalized = name.toLowerCase();
  if (normalized == 'tool_invoke' ||
      normalized == 'defaultspack.tool.invoke' ||
      normalized == 'defaultspack_tool_invoke' ||
      normalized == 'defaults_tool_invoke') {
    final requested =
        '${call.arguments['tool_name'] ?? call.arguments['tool_id'] ?? call.arguments['name'] ?? ''}'
            .trim();
    return _PcToolRequest(
      toolName: requested,
      requestedName: requested,
      arguments: _invokeArguments(call.arguments),
    );
  }
  return _PcToolRequest(
    toolName: name,
    requestedName: name,
    arguments: call.arguments,
  );
}

Map<String, dynamic> _invokeArguments(Map<String, dynamic> args) {
  for (final key in ['arguments', 'args', 'input']) {
    final value = args[key];
    if (value is Map<String, dynamic>) return value;
    if (value is Map) return value.map((key, value) => MapEntry('$key', value));
    if (value is String && value.trim().isNotEmpty) {
      try {
        final decoded = jsonDecode(value);
        if (decoded is Map<String, dynamic>) return decoded;
        if (decoded is Map) {
          return decoded.map((key, value) => MapEntry('$key', value));
        }
      } catch (_) {
        return {'input': value.trim()};
      }
    }
  }
  return const {};
}

String _compactSummary(String value) {
  final normalized = value.replaceAll(RegExp(r'\s+'), ' ').trim();
  if (normalized.length <= 96) return normalized;
  return '${normalized.substring(0, 96)}...';
}
