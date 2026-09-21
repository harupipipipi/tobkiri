import 'dart:convert';
import 'package:http/http.dart' as http;
import '../../settings/api_config_store.dart';

class CredentialTransferException implements Exception {
  const CredentialTransferException(this.message, {this.statusCode});
  final String message;
  final int? statusCode;

  @override
  String toString() => message;
}

class CredentialTransfer {
  const CredentialTransfer({
    required this.transferId,
    required this.status,
    this.ciphertext,
    this.nonce,
    this.algorithm,
    this.label,
  });

  final String transferId;
  final String status;
  final String? ciphertext;
  final String? nonce;
  final String? algorithm;
  final String? label;

  bool get isPending => status == 'pending';
  bool get isCompleted => status == 'acked' || status == 'completed';

  factory CredentialTransfer.fromJson(Map<String, dynamic> json) {
    final transfer = json['transfer'] as Map<String, dynamic>? ?? json;
    return CredentialTransfer(
      transferId: transfer['transfer_id'] as String? ?? '',
      status: transfer['status'] as String? ?? json['status'] as String? ?? '',
      ciphertext: transfer['ciphertext'] as String?,
      nonce: transfer['nonce'] as String?,
      algorithm: transfer['algorithm'] as String?,
      label: transfer['label'] as String?,
    );
  }
}

class CredentialTransferClient {
  CredentialTransferClient({http.Client? client})
      : _http = client ?? http.Client();

  final http.Client _http;
  bool _closed = false;

  void close() {
    _closed = true;
    _http.close();
  }

  Map<String, String> _headers(String token) => {
        'Authorization': 'Bearer $token',
        'Accept': 'application/json',
      };

  Uri _uri(String baseUrl, String path) {
    var trimmed = baseUrl.trim();
    if (!trimmed.contains('://')) trimmed = 'https://$trimmed';
    final base = Uri.parse(trimmed);
    return base.replace(path: '${_trimTrailingSlash(base.path)}$path');
  }

  Future<CredentialTransfer> getTransfer(
    PcConnection pc, {
    required String transferId,
  }) async {
    if (!pc.isConfigured) {
      throw const CredentialTransferException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const CredentialTransferException('クライアントは閉じられました。');
    }
    final uri = _uri(
      pc.baseUrl,
      '/api/mobile/v1/credential-transfers/$transferId',
    );
    final resp = await _http
        .get(uri, headers: _headers(pc.token))
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw CredentialTransferException(
        'クレデンシャル取得に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
    return CredentialTransfer.fromJson(_decodeData(resp.body));
  }

  Future<void> ackTransfer(
    PcConnection pc, {
    required String transferId,
  }) async {
    if (!pc.isConfigured) {
      throw const CredentialTransferException('PC接続が設定されていません。');
    }
    if (_closed) {
      throw const CredentialTransferException('クライアントは閉じられました。');
    }
    final uri = _uri(
      pc.baseUrl,
      '/api/mobile/v1/credential-transfers/$transferId/ack',
    );
    final resp = await _http
        .post(uri, headers: _headers(pc.token))
        .timeout(const Duration(seconds: 15));
    if (resp.statusCode < 200 || resp.statusCode >= 300) {
      throw CredentialTransferException(
        'ACK送信に失敗しました (HTTP ${resp.statusCode})',
        statusCode: resp.statusCode,
      );
    }
  }

  Map<String, dynamic> _decodeData(String body) {
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      if (decoded['status'] != 'ok') {
        final err = decoded['error'];
        final msg = err is Map ? err['message'] as String? : null;
        throw CredentialTransferException(msg ?? 'PCからエラーが返されました。');
      }
      return decoded['data'] as Map<String, dynamic>? ?? const {};
    } catch (e) {
      if (e is CredentialTransferException) rethrow;
      throw CredentialTransferException('PC応答の解析に失敗しました: $e');
    }
  }

  String _trimTrailingSlash(String path) {
    if (path.isEmpty || path == '/') return '';
    return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
  }
}
