import 'dart:convert';
import '../data/pc/device_store.dart';
import '../settings/api_config_store.dart';

sealed class QrPayload {
  const QrPayload();
}

class QrPcConnection extends QrPayload {
  const QrPcConnection({required this.baseUrl, required this.token});
  final String baseUrl;
  final String token;

  PcConnection toPcConnection() => PcConnection(baseUrl: baseUrl, token: token);
}

class QrApiImport extends QrPayload {
  const QrApiImport({
    required this.baseUrl,
    required this.apiKey,
    this.providerId,
    this.apiId,
    this.model,
    this.label,
    this.apiCompatibility,
  });
  final String baseUrl;
  final String apiKey;
  final String? providerId;
  final String? apiId;
  final String? model;
  final String? label;
  final String? apiCompatibility;

  ApiConfig toApiConfig({required ApiConfig fallback}) {
    return fallback.copyWith(
      providerId: providerId,
      baseUrl: baseUrl,
      apiKey: apiKey,
      model: model,
      label: label,
      apiCompatibility: apiCompatibility,
    );
  }
}

class QrPairingV2 extends QrPayload {
  const QrPairingV2(this.payload);
  final PairingV2Payload payload;
}

class QrUrl extends QrPayload {
  const QrUrl(this.url);
  final String url;
}

class QrRejected extends QrPayload {
  const QrRejected(this.reason);
  final String reason;
}

class QrUnknown extends QrPayload {
  const QrUnknown(this.raw);
  final String raw;
}

const _maxQrPayloadBytes = 64 * 1024;

QrPayload parseQrPayload(String raw) {
  final trimmed = raw.trim();
  if (trimmed.isEmpty) return const QrUnknown('');
  if (trimmed.length > _maxQrPayloadBytes ||
      utf8.encode(trimmed).length > _maxQrPayloadBytes) {
    return const QrRejected('QRデータが大きすぎるため取り込めません');
  }
  if (trimmed.startsWith('{')) {
    try {
      final json = jsonDecode(trimmed) as Map<String, dynamic>;
      final kind = json['kind'] as String?;
      switch (kind) {
        case 'rumi_pc':
          return QrPcConnection(
            baseUrl: (json['baseUrl'] as String?)?.trim() ?? '',
            token: (json['token'] as String?)?.trim() ?? '',
          );
        case 'rumi_api':
          return const QrRejected(
            'APIキーを含む旧式QRは安全のため取り込めません。'
            '端末に紐づく短時間・一回限りの安全な転送を使用してください。',
          );
        case 'rumi_mobile_pair_v1':
        case 'rumi_pair_v2':
          return QrPairingV2(PairingV2Payload.fromJson(json));
      }
    } catch (_) {
      return QrUnknown(trimmed);
    }
  }
  if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) {
    return QrUrl(trimmed);
  }
  return QrUnknown(trimmed);
}
