import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:cryptography/cryptography.dart';
import '../../settings/api_config_store.dart';
import 'device_store.dart';

class PcPairingException implements Exception {
  const PcPairingException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class PairingClaimResponse {
  const PairingClaimResponse({required this.pairingId, required this.status});
  final String pairingId;
  final String status;

  factory PairingClaimResponse.fromJson(Map<String, dynamic> json) {
    final pairing = json['pairing'] as Map<String, dynamic>? ?? json;
    return PairingClaimResponse(
      pairingId: pairing['pairing_id'] as String? ?? '',
      status: pairing['status'] as String? ?? '',
    );
  }
}

class PairingStatusResponse {
  const PairingStatusResponse({
    required this.pairingId,
    required this.status,
    this.deviceToken,
    this.approvalToken,
    this.scopes = const [],
    this.approvalScopes = const [],
    this.pcLabel,
    this.tokenDeliveryEnvelope,
  });

  final String pairingId;
  final String status;
  final String? deviceToken;
  final String? approvalToken;
  final List<String> scopes;
  final List<String> approvalScopes;
  final String? pcLabel;
  final Map<String, dynamic>? tokenDeliveryEnvelope;

  bool get isAccepted => status == 'approved' || status == 'accepted';
  bool get hasDeviceToken => deviceToken?.trim().isNotEmpty ?? false;
  bool get hasApprovalToken => approvalToken?.trim().isNotEmpty ?? false;
  bool get hasTokenDeliveryEnvelope =>
      tokenDeliveryEnvelope?.isNotEmpty ?? false;
  bool get isReady =>
      isAccepted && (hasDeviceToken || hasTokenDeliveryEnvelope);
  String get deliveryId =>
      tokenDeliveryEnvelope?['delivery_id'] as String? ?? '';

  factory PairingStatusResponse.fromJson(Map<String, dynamic> json) {
    final pairing = json['pairing'] as Map<String, dynamic>? ?? json;
    final device = json['device'] as Map<String, dynamic>?;
    final server = json['server'] as Map<String, dynamic>?;
    final rawScopes = (json['scopes'] as List?) ??
        (device?['scopes'] as List?) ??
        (pairing['scopes'] as List?) ??
        (pairing['capabilities'] as List?) ??
        const [];
    final rawApprovalScopes = (json['approval_scopes'] as List?) ??
        (device?['approval_scopes'] as List?) ??
        const [];
    return PairingStatusResponse(
      pairingId: pairing['pairing_id'] as String? ?? '',
      status: pairing['status'] as String? ?? '',
      deviceToken: json['client_access_token'] as String? ??
          json['device_token'] as String? ??
          pairing['client_access_token'] as String? ??
          pairing['device_token'] as String?,
      approvalToken: json['approver_access_token'] as String? ??
          json['approval_token'] as String? ??
          pairing['approver_access_token'] as String? ??
          pairing['approval_token'] as String?,
      scopes: rawScopes.map((e) => e.toString()).toList(),
      approvalScopes: rawApprovalScopes.map((e) => e.toString()).toList(),
      pcLabel: json['pc_label'] as String? ??
          pairing['pc_label'] as String? ??
          server?['label'] as String?,
      tokenDeliveryEnvelope: _mapOrNull(
        json['token_delivery_envelope'] ?? json['tokenDeliveryEnvelope'],
      ),
    );
  }
}

class PcPairingClient {
  PcPairingClient({http.Client? client}) : _http = client ?? http.Client();

  final http.Client _http;
  bool _closed = false;

  void close() {
    _closed = true;
    _http.close();
  }

  Map<String, String> _headers(String token) => {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        if (token.trim().isNotEmpty) 'Authorization': 'Bearer $token',
      };

  bool _hasBaseUrl(PcConnection pc) => pc.baseUrl.trim().isNotEmpty;

  Uri _uri(String baseUrl, String path) {
    var trimmed = baseUrl.trim();
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final base = Uri.parse(trimmed);
    return base.replace(path: '${_trimTrailingSlash(base.path)}$path');
  }

  Future<PairingClaimResponse> claim(
    PcConnection pc, {
    required String pairingId,
    required String code,
    required DeviceIdentity device,
    required List<String> requestedCapabilities,
  }) async {
    if (!_hasBaseUrl(pc)) {
      throw const PcPairingException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const PcPairingException('クライアントは閉じられました。');
    }
    final uri = _uri(pc.baseUrl, '/api/mobile/v1/pairings/$pairingId/claim');
    final body = jsonEncode({
      'code': code,
      'device_id': device.deviceId,
      'device_label': device.deviceLabel,
      'public_key': device.publicKey,
      'device_encryption_public_key': device.encryptionPublicKey,
      'requested_capabilities': requestedCapabilities,
    });
    final resp = await _http
        .post(uri, headers: _headers(pc.token), body: body)
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw PcPairingException(
        'ペアリング要求に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    return PairingClaimResponse.fromJson(_decodeData(resp.body));
  }

  Future<PairingStatusResponse> pollStatus(
    PcConnection pc, {
    required String pairingId,
    String? code,
  }) async {
    if (!_hasBaseUrl(pc)) {
      throw const PcPairingException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const PcPairingException('クライアントは閉じられました。');
    }
    final uri =
        _uri(pc.baseUrl, '/api/mobile/v1/pairings/$pairingId/status').replace(
      queryParameters: {
        if (code != null && code.trim().isNotEmpty) 'code': code.trim(),
      },
    );
    final resp = await _http
        .get(uri, headers: _headers(pc.token))
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw PcPairingException(
        'ステータス取得に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    return PairingStatusResponse.fromJson(_decodeData(resp.body));
  }

  Future<PairingStatusResponse> pickupTokenDelivery(
    PcConnection pc, {
    required String pairingId,
    required String pickupSecret,
    required String deviceId,
  }) async {
    if (!_hasBaseUrl(pc)) {
      throw const PcPairingException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const PcPairingException('クライアントは閉じられました。');
    }
    final uri =
        _uri(pc.baseUrl, '/api/mobile/v1/pairings/$pairingId/token/pickup');
    final resp = await _http
        .post(
          uri,
          headers: _headers(pc.token),
          body: jsonEncode({
            'pickup_secret': pickupSecret,
            'device_id': deviceId,
          }),
        )
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw PcPairingException(
        'トークン取得に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    return PairingStatusResponse.fromJson(_decodeData(resp.body));
  }

  Future<void> ackTokenDelivery(
    PcConnection pc, {
    required String pairingId,
    required String pickupSecret,
    required String deviceId,
    required String deliveryId,
  }) async {
    if (!_hasBaseUrl(pc)) {
      throw const PcPairingException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const PcPairingException('クライアントは閉じられました。');
    }
    final uri =
        _uri(pc.baseUrl, '/api/mobile/v1/pairings/$pairingId/token/ack');
    final resp = await _http
        .post(
          uri,
          headers: _headers(pc.token),
          body: jsonEncode({
            'pickup_secret': pickupSecret,
            'device_id': deviceId,
            'delivery_id': deliveryId,
          }),
        )
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw PcPairingException(
        'トークン受領確認に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    _decodeData(resp.body);
  }

  Map<String, dynamic> _decodeData(String body) {
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      if (decoded['status'] != 'ok') {
        final err = decoded['error'];
        final msg = err is Map ? err['message'] as String? : null;
        throw PcPairingException(msg ?? 'PCからエラーが返されました。');
      }
      return decoded['data'] as Map<String, dynamic>? ?? const {};
    } catch (e) {
      if (e is PcPairingException) rethrow;
      throw PcPairingException('PC応答の解析に失敗しました: $e');
    }
  }

  String _trimTrailingSlash(String path) {
    if (path.isEmpty || path == '/') return '';
    return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
  }
}

Map<String, dynamic>? _mapOrNull(Object? value) {
  if (value is Map) return Map<String, dynamic>.from(value);
  return null;
}

Future<String> claimVerificationCode({
  required String pairingId,
  required DeviceIdentity device,
  required List<String> requestedCapabilities,
}) async {
  final payload = <String, Object>{
    'claimed_capabilities': _normalizedScopes(requestedCapabilities),
    'claimed_device_encryption_public_key': device.encryptionPublicKey,
    'claimed_device_id': device.deviceId,
    'claimed_device_public_key': device.publicKey,
    'pairing_id': pairingId,
  };
  final digest = await Sha256().hash(utf8.encode(jsonEncode(payload)));
  final code = _base32Prefix(digest.bytes, 8);
  return '${code.substring(0, 4)}-${code.substring(4)}';
}

List<String> _normalizedScopes(List<String> scopes) {
  final values = scopes
      .map((e) => e.trim())
      .where((e) => e.isNotEmpty)
      .toSet()
      .toList()
    ..sort();
  return values;
}

String _base32Prefix(List<int> bytes, int length) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  final buffer = StringBuffer();
  var bits = 0;
  var value = 0;
  for (final byte in bytes) {
    value = (value << 8) | (byte & 0xff);
    bits += 8;
    while (bits >= 5 && buffer.length < length) {
      buffer.write(alphabet[(value >> (bits - 5)) & 0x1f]);
      bits -= 5;
    }
    if (buffer.length >= length) break;
  }
  if (buffer.length < length && bits > 0) {
    buffer.write(alphabet[(value << (5 - bits)) & 0x1f]);
  }
  return buffer.toString().padRight(length, 'A').substring(0, length);
}
