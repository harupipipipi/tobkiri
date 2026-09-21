import 'dart:convert';

import 'package:http/http.dart' as http;

import '../../settings/api_config_store.dart';
import 'device_store.dart';

class PcApprovalException implements Exception {
  const PcApprovalException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class AuthorityRequestItem {
  const AuthorityRequestItem({
    required this.requestId,
    required this.status,
    required this.principalId,
    required this.permissionId,
    required this.reason,
    required this.riskLevel,
    required this.resource,
  });

  final String requestId;
  final String status;
  final String principalId;
  final String permissionId;
  final String reason;
  final String riskLevel;
  final Map<String, dynamic> resource;

  String get title => permissionId.isEmpty ? 'Authority request' : permissionId;
  String get summary {
    final app = resource['app_display_name']?.toString().trim() ?? '';
    final model = resource['model_display_name']?.toString().trim() ?? '';
    final domain = resource['domain']?.toString().trim() ?? '';
    final parts = [app, model, domain].where((p) => p.isNotEmpty).toList();
    if (parts.isNotEmpty) return parts.join(' · ');
    return reason.isNotEmpty ? reason : principalId;
  }

  factory AuthorityRequestItem.fromJson(Map<String, dynamic> json) {
    final resource = json['resource'] is Map
        ? Map<String, dynamic>.from(json['resource'] as Map)
        : <String, dynamic>{};
    return AuthorityRequestItem(
      requestId: json['request_id'] as String? ?? '',
      status: json['status'] as String? ?? '',
      principalId: json['principal_id'] as String? ?? '',
      permissionId: json['permission_id'] as String? ?? '',
      reason: json['reason'] as String? ?? '',
      riskLevel: json['risk_level'] as String? ?? 'low',
      resource: resource,
    );
  }
}

class ApprovalDecisionResult {
  const ApprovalDecisionResult({
    required this.requestId,
    required this.approved,
    required this.denied,
    this.token = '',
  });

  final String requestId;
  final bool approved;
  final bool denied;
  final String token;

  factory ApprovalDecisionResult.fromJson(Map<String, dynamic> json) {
    return ApprovalDecisionResult(
      requestId: json['request_id'] as String? ?? '',
      approved: json['approved'] as bool? ?? false,
      denied: json['denied'] as bool? ?? false,
      token: json['token'] as String? ?? '',
    );
  }
}

class PcApprovalClient {
  PcApprovalClient({
    http.Client? client,
    required MobileDeviceStore deviceStore,
  })  : _http = client ?? http.Client(),
        _deviceStore = deviceStore;

  final http.Client _http;
  final MobileDeviceStore _deviceStore;
  bool _closed = false;

  void close() {
    _closed = true;
    _http.close();
  }

  Future<List<AuthorityRequestItem>> listPending(PcConnection pc) async {
    final resp = await _get(pc, '/api/authority/requests', const {
      'status': 'pending',
    });
    final data = _decodeData(resp.body);
    final raw =
        (data['pending'] as List?) ?? (data['requests'] as List?) ?? const [];
    return raw
        .whereType<Map>()
        .map(
          (item) =>
              AuthorityRequestItem.fromJson(Map<String, dynamic>.from(item)),
        )
        .where((item) => item.requestId.isNotEmpty && item.status == 'pending')
        .toList();
  }

  Future<AuthorityRequestItem> getRequest(
    PcConnection pc,
    String requestId,
  ) async {
    final resp = await _get(
      pc,
      '/api/authority/requests/${Uri.encodeComponent(requestId)}',
    );
    final data = _decodeData(resp.body);
    return AuthorityRequestItem.fromJson(data);
  }

  Future<ApprovalDecisionResult> approve(
    PcConnection pc,
    AuthorityRequestItem request,
  ) async {
    final attestation = await _challengeAndSign(
      pc,
      request.requestId,
      decision: 'approve',
    );
    final resp = await _post(
      pc,
      '/api/authority/requests/${Uri.encodeComponent(request.requestId)}/approve',
      {'scope': 'once', 'attestation': attestation},
    );
    return ApprovalDecisionResult.fromJson(_decodeData(resp.body));
  }

  Future<ApprovalDecisionResult> deny(
    PcConnection pc,
    AuthorityRequestItem request, {
    String reason = '',
  }) async {
    final attestation = await _challengeAndSign(
      pc,
      request.requestId,
      decision: 'deny',
    );
    final resp = await _post(
      pc,
      '/api/authority/requests/${Uri.encodeComponent(request.requestId)}/deny',
      {'reason': reason, 'attestation': attestation},
    );
    return ApprovalDecisionResult.fromJson(_decodeData(resp.body));
  }

  Future<Map<String, dynamic>> _challengeAndSign(
    PcConnection pc,
    String requestId, {
    required String decision,
  }) async {
    final resp = await _post(
      pc,
      '/api/authority/requests/${Uri.encodeComponent(requestId)}/challenge',
      {'decision': decision, 'scope': 'once'},
    );
    final data = _decodeData(resp.body);
    final payloadHash = data['payload_hash'] as String? ?? '';
    final challenge = data['challenge'] is Map
        ? Map<String, dynamic>.from(data['challenge'] as Map)
        : <String, dynamic>{};
    final challengeId = challenge['challenge_id'] as String? ?? '';
    if (payloadHash.isEmpty || challengeId.isEmpty) {
      throw const PcApprovalException('PC承認チャレンジが不完全です。');
    }
    final signature = await _deviceStore.signApprovalPayloadHash(payloadHash);
    return {
      'challenge_id': challengeId,
      'payload_hash': payloadHash,
      'signature': signature,
      'signature_algorithm': 'ed25519',
    };
  }

  Future<http.Response> _get(
    PcConnection pc,
    String path, [
    Map<String, String>? query,
  ]) async {
    _ensureConfigured(pc);
    var uri = _uri(pc.baseUrl, path);
    if (query != null && query.isNotEmpty) {
      uri = uri.replace(queryParameters: {...uri.queryParameters, ...query});
    }
    final resp = await _http
        .get(uri, headers: _headers(pc))
        .timeout(const Duration(seconds: 15));
    _ensureOk(resp);
    return resp;
  }

  Future<http.Response> _post(
    PcConnection pc,
    String path,
    Map<String, dynamic> body,
  ) async {
    _ensureConfigured(pc);
    final resp = await _http
        .post(
          _uri(pc.baseUrl, path),
          headers: _headers(pc),
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 15));
    _ensureOk(resp);
    return resp;
  }

  Map<String, String> _headers(PcConnection pc) => {
        'Authorization': 'Bearer ${pc.approvalToken.trim()}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
      };

  void _ensureConfigured(PcConnection pc) {
    if (_closed) {
      throw const PcApprovalException('クライアントは閉じられました。');
    }
    if (pc.baseUrl.trim().isEmpty || pc.approvalToken.trim().isEmpty) {
      throw const PcApprovalException('PC承認tokenが設定されていません。');
    }
  }

  void _ensureOk(http.Response resp) {
    if (resp.statusCode >= 200 && resp.statusCode < 300) return;
    throw PcApprovalException(
      'PC承認APIに失敗しました (HTTP ${resp.statusCode})',
      statusCode: resp.statusCode,
    );
  }

  Uri _uri(String baseUrl, String path) {
    var trimmed = baseUrl.trim();
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final base = Uri.parse(trimmed);
    return base.replace(path: '${_trimTrailingSlash(base.path)}$path');
  }

  Map<String, dynamic> _decodeData(String body) {
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      if (decoded['status'] != 'ok') {
        final err = decoded['error'];
        final msg = err is Map ? err['message'] as String? : null;
        throw PcApprovalException(msg ?? 'PCからエラーが返されました。');
      }
      return decoded['data'] as Map<String, dynamic>? ?? const {};
    } catch (e) {
      if (e is PcApprovalException) rethrow;
      throw PcApprovalException('PC承認API応答の解析に失敗しました: $e');
    }
  }

  String _trimTrailingSlash(String path) {
    if (path.isEmpty || path == '/') return '';
    return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
  }
}
