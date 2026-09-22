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

class QrUnknown extends QrPayload {
  const QrUnknown(this.raw);
  final String raw;
}

QrPayload parseQrPayload(String raw) {
  final trimmed = raw.trim();
  if (trimmed.isEmpty) return const QrUnknown('');
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
          return QrApiImport(
            baseUrl: (json['baseUrl'] as String?)?.trim() ?? '',
            apiKey: (json['apiKey'] as String?)?.trim() ??
                (json['api_key'] as String?)?.trim() ??
                '',
            providerId: (json['providerId'] as String?)?.trim() ??
                (json['provider_id'] as String?)?.trim(),
            apiId: (json['apiId'] as String?)?.trim() ??
                (json['api_id'] as String?)?.trim(),
            model: (json['model'] as String?)?.trim(),
            label: (json['label'] as String?)?.trim(),
            apiCompatibility: (json['apiCompatibility'] as String?)?.trim() ??
                (json['api_compatibility'] as String?)?.trim(),
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
