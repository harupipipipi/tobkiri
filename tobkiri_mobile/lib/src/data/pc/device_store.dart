import 'dart:convert';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:uuid/uuid.dart';

import '../../settings/api_config_store.dart';

class DeviceIdentity {
  const DeviceIdentity({
    required this.deviceId,
    required this.deviceLabel,
    required this.publicKey,
    this.encryptionPublicKey = '',
    this.encryptionPrivateKey = '',
    this.privateKey = '',
    this.keyType = 'ed25519',
  });

  final String deviceId;
  final String deviceLabel;
  final String publicKey;
  final String encryptionPublicKey;
  final String encryptionPrivateKey;
  final String privateKey;
  final String keyType;

  bool get canSignApproval =>
      keyType == 'ed25519' &&
      publicKey.trim().startsWith('ed25519:') &&
      privateKey.trim().isNotEmpty;
  bool get canDecryptTokenDelivery =>
      encryptionPublicKey.trim().startsWith('x25519:') &&
      encryptionPrivateKey.trim().isNotEmpty;

  Map<String, dynamic> toJson() => {
        'deviceId': deviceId,
        'deviceLabel': deviceLabel,
        'publicKey': publicKey,
        'encryptionPublicKey': encryptionPublicKey,
        'encryptionPrivateKey': encryptionPrivateKey,
        'privateKey': privateKey,
        'keyType': keyType,
      };

  factory DeviceIdentity.fromJson(Map<String, dynamic> json) {
    return DeviceIdentity(
      deviceId: json['deviceId'] as String? ?? '',
      deviceLabel: json['deviceLabel'] as String? ?? '',
      publicKey: json['publicKey'] as String? ?? '',
      encryptionPublicKey: json['encryptionPublicKey'] as String? ?? '',
      encryptionPrivateKey: json['encryptionPrivateKey'] as String? ?? '',
      privateKey: json['privateKey'] as String? ?? '',
      keyType: json['keyType'] as String? ?? 'ed25519',
    );
  }

  DeviceIdentity copyWith({
    String? encryptionPublicKey,
    String? encryptionPrivateKey,
  }) {
    return DeviceIdentity(
      deviceId: deviceId,
      deviceLabel: deviceLabel,
      publicKey: publicKey,
      encryptionPublicKey: encryptionPublicKey ?? this.encryptionPublicKey,
      encryptionPrivateKey: encryptionPrivateKey ?? this.encryptionPrivateKey,
      privateKey: privateKey,
      keyType: keyType,
    );
  }
}

class PairedDevice {
  const PairedDevice({
    required this.deviceId,
    required this.deviceToken,
    this.approvalToken = '',
    required this.label,
    required this.scopes,
    this.approvalScopes = const [],
    required this.pcBaseUrl,
    required this.pcLabel,
    required this.pairingId,
  });

  final String deviceId;
  final String deviceToken;
  final String approvalToken;
  final String label;
  final List<String> scopes;
  final List<String> approvalScopes;
  final String pcBaseUrl;
  final String pcLabel;
  final String pairingId;

  String get clientToken => deviceToken;
  String get approverToken => approvalToken;

  bool get canReadPcConversations => scopes.contains('chat.read');
  bool get canWritePcConversations => scopes.contains('chat.write');
  bool get canObservePcTools => scopes.contains('tools.observe');
  bool get canApprovePcTools =>
      approvalToken.trim().isNotEmpty &&
      approvalScopes.contains('authority.request.approve') &&
      approvalScopes.contains('authority.request.deny');
  bool get canRequestCredentialCopy => scopes.contains('credentials.request');
  bool get isConfigured =>
      deviceToken.trim().isNotEmpty && pcBaseUrl.trim().isNotEmpty;
  String get displayPcLabel => friendlyPcLabel(pcLabel, pcBaseUrl);
  String get connectionId => pairedDeviceConnectionId(this);

  PcConnection toPcConnection() => PcConnection(
        baseUrl: pcBaseUrl,
        token: deviceToken,
        approvalToken: approvalToken,
      );

  Map<String, dynamic> toJson() => {
        'deviceId': deviceId,
        'deviceToken': deviceToken,
        'approvalToken': approvalToken,
        'clientToken': deviceToken,
        'approverToken': approvalToken,
        'label': label,
        'scopes': scopes,
        'approvalScopes': approvalScopes,
        'pcBaseUrl': pcBaseUrl,
        'pcLabel': pcLabel,
        'pairingId': pairingId,
      };

  factory PairedDevice.fromJson(Map<String, dynamic> json) {
    return PairedDevice(
      deviceId: json['deviceId'] as String? ?? '',
      deviceToken: (json['clientToken'] as String?) ??
          (json['deviceToken'] as String?) ??
          '',
      approvalToken: (json['approverToken'] as String?) ??
          (json['approvalToken'] as String?) ??
          '',
      label: json['label'] as String? ?? '',
      scopes: (json['scopes'] as List? ?? []).map((e) => e.toString()).toList(),
      approvalScopes: (json['approvalScopes'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
      pcBaseUrl: json['pcBaseUrl'] as String? ?? '',
      pcLabel: json['pcLabel'] as String? ?? '',
      pairingId: json['pairingId'] as String? ?? '',
    );
  }
}

class _EncryptionKeyPair {
  const _EncryptionKeyPair({
    required this.publicKey,
    required this.privateKey,
  });

  final String publicKey;
  final String privateKey;
}

String friendlyPcLabel(String? label, String baseUrl) {
  final trimmed = (label ?? '').trim();
  if (trimmed.isNotEmpty && !_looksLikeUrl(trimmed)) return trimmed;

  final host = _hostFromBaseUrl(baseUrl);
  if (host.isEmpty || _looksLikeIpAddress(host)) return 'PC';
  final withoutLocal = host
      .replaceFirst(RegExp(r'\.local$', caseSensitive: false), '')
      .replaceFirst(RegExp(r'\.lan$', caseSensitive: false), '');
  return withoutLocal.isEmpty ? 'PC' : withoutLocal;
}

String pairedDeviceConnectionId(PairedDevice device) {
  for (final candidate in [
    device.pairingId,
    _hostFromBaseUrl(device.pcBaseUrl),
    device.pcBaseUrl,
    device.deviceId,
  ]) {
    final normalized = _safeConnectionId(candidate);
    if (normalized.isNotEmpty) return normalized;
  }
  return 'pc';
}

String _safeConnectionId(String value) {
  final safe = value
      .trim()
      .toLowerCase()
      .replaceAll(RegExp(r'[^a-z0-9._-]+'), '-')
      .replaceAll(RegExp(r'-+'), '-')
      .replaceAll(RegExp(r'^-|-$'), '');
  return safe;
}

const bool _isProductBuild = bool.fromEnvironment('dart.vm.product');

String preferredPairingBaseUrl(
  List<String> baseUrls, {
  bool? allowCleartext,
}) {
  final allowHttp = allowCleartext ?? !_isProductBuild;
  var selected = '';
  var selectedScore = -1;
  for (final rawUrl in baseUrls) {
    final url = rawUrl.trim();
    if (url.isEmpty) continue;
    if (!allowHttp && _urlScheme(url) == 'http') continue;
    final score = _pairingBaseUrlScore(url);
    if (score > selectedScore) {
      selected = url;
      selectedScore = score;
    }
  }
  return selected;
}

bool pcConnectionUrlAllowed(
  String url, {
  bool? allowCleartext,
}) {
  final scheme = _urlScheme(url);
  if (scheme.isEmpty) return false;
  if (scheme == 'http') return allowCleartext ?? !_isProductBuild;
  return scheme == 'https';
}

String _urlScheme(String url) {
  final normalized = url.contains('://') ? url : 'https://$url';
  return Uri.tryParse(normalized)?.scheme.toLowerCase() ?? '';
}

int _pairingBaseUrlScore(String url) {
  final host = _hostFromBaseUrl(url).toLowerCase();
  if (host.isEmpty) return 0;
  if (host == 'localhost' || host == '127.0.0.1' || host == '::1') return 10;
  if (host.startsWith('169.254.') || host.startsWith('fe80:')) return 20;
  if (host.startsWith('192.168.')) return 100;
  if (host.startsWith('10.')) return 95;
  if (_isPrivate172Address(host)) return 95;
  if (!_looksLikeIpAddress(host)) return 90;
  return 50;
}

bool _isPrivate172Address(String host) {
  final match = RegExp(r'^172\.(\d{1,3})\.').firstMatch(host);
  if (match == null) return false;
  final secondOctet = int.tryParse(match.group(1) ?? '');
  return secondOctet != null && secondOctet >= 16 && secondOctet <= 31;
}

bool _looksLikeUrl(String value) {
  final lower = value.toLowerCase();
  return lower.startsWith('http://') || lower.startsWith('https://');
}

String _hostFromBaseUrl(String baseUrl) {
  final trimmed = baseUrl.trim();
  if (trimmed.isEmpty) return '';
  final normalized = trimmed.contains('://') ? trimmed : 'http://$trimmed';
  return Uri.tryParse(normalized)?.host ?? '';
}

bool _looksLikeIpAddress(String value) {
  return RegExp(r'^\d{1,3}(\.\d{1,3}){3}$').hasMatch(value) ||
      value.contains(':');
}

class PairingV2Payload {
  const PairingV2Payload({
    required this.pairingId,
    required this.code,
    required this.pickupSecret,
    required this.baseUrls,
    required this.serverPublicKey,
    required this.expiresAt,
    this.manifestUrl = '',
    this.roles = const [],
  });

  final String pairingId;
  final String code;
  final String pickupSecret;
  final List<String> baseUrls;
  final String serverPublicKey;
  final int expiresAt;
  final String manifestUrl;
  final List<String> roles;

  bool get isExpired => DateTime.now().millisecondsSinceEpoch > expiresAt;

  factory PairingV2Payload.fromJson(Map<String, dynamic> json) {
    final rawBaseUrls = (json['baseUrls'] as List?) ??
        (json['base_urls'] as List?) ??
        [
          if ((json['baseUrl'] as String? ?? '').trim().isNotEmpty)
            json['baseUrl'],
          if ((json['base_url'] as String? ?? '').trim().isNotEmpty)
            json['base_url'],
        ];
    return PairingV2Payload(
      pairingId:
          json['pairingId'] as String? ?? json['pairing_id'] as String? ?? '',
      code: json['code'] as String? ?? '',
      pickupSecret: json['pickupSecret'] as String? ??
          json['pickup_secret'] as String? ??
          '',
      baseUrls: rawBaseUrls.map((e) => e.toString()).toList(),
      serverPublicKey: json['serverPublicKey'] as String? ??
          json['server_public_key'] as String? ??
          '',
      expiresAt:
          (json['expiresAt'] as num? ?? json['expires_at'] as num?)?.toInt() ??
              0,
      manifestUrl: json['manifestUrl'] as String? ??
          json['manifest_url'] as String? ??
          '',
      roles: (json['roles'] as List? ?? [])
          .map((e) => e.toString())
          .where((e) => e.trim().isNotEmpty)
          .toList(),
    );
  }
}

const _migratedPcScopes = <String>[
  'chat.read',
  'chat.write',
  'tools.observe',
];

const _migratedApprovalScopes = <String>[
  'authority.request.approve',
  'authority.request.deny',
  'authority.request.list',
  'authority.request.read',
];

class MobileDeviceStore {
  MobileDeviceStore({
    SecureKeyValueStorage? storage,
    SecureKeyValueStorage? legacyStorage,
  })  : _storage = storage ?? PlatformSecureStorage(),
        _legacyStorage = legacyStorage ??
            (storage == null ? LegacyFlutterSecureStorage() : null);

  static const _identityKey = 'rumi.device.identity.v1';
  static const _pairedKey = 'rumi.paired_device.v1';
  static const _pairedListKey = 'rumi.paired_devices.v1';
  static const _legacyPcKey = 'rumi.pc_connection.v1';
  static const _legacyRemoteBaseUrlKey = 'rumi_remote.base_url';
  static const _legacyRemoteTokenKey = 'rumi_remote.token';

  final SecureKeyValueStorage _storage;
  final SecureKeyValueStorage? _legacyStorage;
  final _uuid = const Uuid();

  Future<DeviceIdentity> loadOrCreateIdentity() async {
    try {
      final raw = await _storage.read(_identityKey);
      if (raw != null && raw.trim().isNotEmpty) {
        final identity = DeviceIdentity.fromJson(
          jsonDecode(raw) as Map<String, dynamic>,
        );
        if (identity.deviceId.trim().isNotEmpty && identity.canSignApproval) {
          return await _ensureEncryptionKey(identity);
        }
      }
    } catch (_) {
      // fall through to create new
    }
    final identity = await _createIdentity();
    try {
      await _storage.write(_identityKey, jsonEncode(identity.toJson()));
    } catch (_) {
      // ignore secure storage failures
    }
    return identity;
  }

  Future<String> signApprovalPayloadHash(String payloadHash) async {
    final identity = await loadOrCreateIdentity();
    if (!identity.canSignApproval) {
      throw StateError('approval signing key is not available');
    }
    final publicKeyBytes = _decodePublicKey(identity.publicKey);
    final keyPair = SimpleKeyPairData(
      _decodeBase64Url(identity.privateKey),
      publicKey: SimplePublicKey(publicKeyBytes, type: KeyPairType.ed25519),
      type: KeyPairType.ed25519,
    );
    final signature = await Ed25519().sign(
      _hexToBytes(payloadHash),
      keyPair: keyPair,
    );
    return _encodeBase64Url(signature.bytes);
  }

  Future<DeviceIdentity> _createIdentity() async {
    final signingKeyPair = await Ed25519().newKeyPair();
    final signingKeyPairData = await signingKeyPair.extract();
    final signingPublicKey = await signingKeyPair.extractPublicKey();
    final encryption = await _createEncryptionKeyPair();
    return DeviceIdentity(
      deviceId: 'mobile-${_uuid.v4().substring(0, 12)}',
      deviceLabel: 'Rumi Mobile',
      publicKey: 'ed25519:${_encodeBase64Url(signingPublicKey.bytes)}',
      encryptionPublicKey: encryption.publicKey,
      encryptionPrivateKey: encryption.privateKey,
      privateKey: _encodeBase64Url(signingKeyPairData.bytes),
      keyType: 'ed25519',
    );
  }

  Future<DeviceIdentity> _ensureEncryptionKey(DeviceIdentity identity) async {
    if (identity.canDecryptTokenDelivery) return identity;
    final encryption = await _createEncryptionKeyPair();
    final upgraded = identity.copyWith(
      encryptionPublicKey: encryption.publicKey,
      encryptionPrivateKey: encryption.privateKey,
    );
    try {
      await _storage.write(_identityKey, jsonEncode(upgraded.toJson()));
    } catch (_) {
      // ignore secure storage failures
    }
    return upgraded;
  }

  Future<_EncryptionKeyPair> _createEncryptionKeyPair() async {
    final keyPair = await X25519().newKeyPair();
    final keyPairData = await keyPair.extract();
    final publicKey = await keyPair.extractPublicKey();
    return _EncryptionKeyPair(
      publicKey: 'x25519:${_encodeBase64Url(publicKey.bytes)}',
      privateKey: _encodeBase64Url(keyPairData.bytes),
    );
  }

  Future<Map<String, dynamic>> decryptTokenDeliveryEnvelope(
    Map<String, dynamic> envelope, {
    required String pairingId,
    required String deviceId,
  }) async {
    final identity = await loadOrCreateIdentity();
    if (!identity.canDecryptTokenDelivery) {
      throw StateError('token delivery decryption key is not available');
    }
    final version = envelope['version'] as int? ?? 0;
    final alg = envelope['alg'] as String? ?? '';
    if (version != 1 || alg != 'X25519-HKDF-SHA256-AES-256-GCM') {
      throw FormatException('unsupported token delivery envelope');
    }
    final deliveryId = envelope['delivery_id'] as String? ?? '';
    final remotePublicKey = SimplePublicKey(
      _decodePrefixedPublicKey(
        envelope['ephemeral_public_key'] as String? ?? '',
        'x25519:',
      ),
      type: KeyPairType.x25519,
    );
    final localPublicKey = SimplePublicKey(
      _decodePrefixedPublicKey(identity.encryptionPublicKey, 'x25519:'),
      type: KeyPairType.x25519,
    );
    final localKeyPair = SimpleKeyPairData(
      _decodeBase64Url(identity.encryptionPrivateKey),
      publicKey: localPublicKey,
      type: KeyPairType.x25519,
    );
    final sharedSecret = await X25519().sharedSecretKey(
      keyPair: localKeyPair,
      remotePublicKey: remotePublicKey,
    );
    final secretKey = await Hkdf(
      hmac: Hmac.sha256(),
      outputLength: 32,
    ).deriveKey(
      secretKey: sharedSecret,
      nonce: utf8.encode('rumi-mobile-token-delivery-v1'),
      info: utf8.encode('$pairingId:$deviceId:$deliveryId'),
    );
    final box = SecretBox(
      _decodeBase64Url(envelope['ciphertext'] as String? ?? ''),
      nonce: _decodeBase64Url(envelope['nonce'] as String? ?? ''),
      mac: Mac(_decodeBase64Url(envelope['tag'] as String? ?? '')),
    );
    final aad = _decodeBase64Url(envelope['aad'] as String? ?? '');
    final clear = await AesGcm.with256bits().decrypt(
      box,
      secretKey: secretKey,
      aad: aad,
    );
    final decoded = jsonDecode(utf8.decode(clear));
    if (decoded is! Map) {
      throw FormatException('invalid token delivery payload');
    }
    return Map<String, dynamic>.from(decoded);
  }

  Future<PairedDevice?> loadPairedDevice() async {
    try {
      final raw = await _storage.read(_pairedKey);
      if (raw != null && raw.trim().isNotEmpty) {
        final device = PairedDevice.fromJson(
          jsonDecode(raw) as Map<String, dynamic>,
        );
        if (device.isConfigured) return device;
      }
    } catch (_) {
      // ignore malformed paired device state
    }
    final migrated = await _migrateLegacyPcConnection();
    if (migrated != null) return migrated;
    return null;
  }

  Future<void> savePairedDevice(PairedDevice? device) async {
    try {
      if (device == null) {
        await _storage.delete(_pairedKey);
      } else if (!device.isConfigured) {
        await _storage.delete(_pairedKey);
      } else {
        await _storage.write(_pairedKey, jsonEncode(device.toJson()));
        await addPairedDevice(device);
      }
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<List<PairedDevice>> loadPairedDevices() async {
    try {
      final raw = await _storage.read(_pairedListKey);
      if (raw != null && raw.trim().isNotEmpty) {
        final list = jsonDecode(raw) as List;
        final devices = list
            .map((e) => PairedDevice.fromJson(e as Map<String, dynamic>))
            .where((device) => device.isConfigured)
            .toList();
        if (devices.isNotEmpty) return devices;
      }
    } catch (_) {
      // fall through
    }
    final devices = <PairedDevice>[];
    final single = await loadPairedDevice();
    if (single != null) {
      devices.add(single);
      await savePairedDevices(devices);
    }
    return devices;
  }

  Future<PairedDevice?> _migrateLegacyPcConnection() async {
    try {
      final pc = await _loadLegacyPcConnection();
      if (pc == null) return null;
      if (!pc.isConfigured) {
        await _deleteLegacyConnectionKeys();
        return null;
      }
      final identity = await loadOrCreateIdentity();
      final connectionId = _safeConnectionId(pc.baseUrl);
      final device = PairedDevice(
        deviceId: identity.deviceId,
        deviceToken: pc.token,
        approvalToken: pc.approvalToken,
        label: identity.deviceLabel,
        scopes: _migratedPcScopes,
        approvalScopes: pc.approvalToken.trim().isEmpty
            ? const []
            : _migratedApprovalScopes,
        pcBaseUrl: pc.baseUrl,
        pcLabel: friendlyPcLabel('', pc.baseUrl),
        pairingId: connectionId.isEmpty ? 'legacy-pc' : 'legacy-$connectionId',
      );
      if (!device.isConfigured) {
        await _storage.delete(_legacyPcKey);
        return null;
      }
      await _storage.write(_pairedKey, jsonEncode(device.toJson()));
      await _storage.write(_pairedListKey, jsonEncode([device.toJson()]));
      await _deleteLegacyConnectionKeys();
      return device;
    } catch (_) {
      return null;
    }
  }

  Future<PcConnection?> _loadLegacyPcConnection() async {
    for (final storage in _legacyCandidateStorages()) {
      final raw = await storage.read(_legacyPcKey);
      if (raw != null && raw.trim().isNotEmpty) {
        try {
          return PcConnection.fromJson(jsonDecode(raw) as Map<String, dynamic>);
        } catch (_) {
          // Try the older split keys below.
        }
      }
      final split = await _loadLegacyRemoteConnection(storage);
      if (split != null) return split;
    }
    return null;
  }

  Iterable<SecureKeyValueStorage> _legacyCandidateStorages() sync* {
    yield _storage;
    final legacy = _legacyStorage;
    if (legacy != null && !identical(legacy, _storage)) yield legacy;
  }

  Future<PcConnection?> _loadLegacyRemoteConnection(
    SecureKeyValueStorage storage,
  ) async {
    final baseUrl = (await storage.read(_legacyRemoteBaseUrlKey))?.trim() ?? '';
    final token = (await storage.read(_legacyRemoteTokenKey))?.trim() ?? '';
    if (baseUrl.isEmpty && token.isEmpty) return null;
    return PcConnection(baseUrl: baseUrl, token: token);
  }

  Future<void> _deleteLegacyConnectionKeys() async {
    for (final storage in _legacyCandidateStorages()) {
      await storage.delete(_legacyPcKey);
      await storage.delete(_legacyRemoteBaseUrlKey);
      await storage.delete(_legacyRemoteTokenKey);
    }
  }

  Future<void> savePairedDevices(List<PairedDevice> devices) async {
    try {
      final configured = devices.where((device) => device.isConfigured);
      await _storage.write(
        _pairedListKey,
        jsonEncode(configured.map((d) => d.toJson()).toList()),
      );
    } catch (_) {
      // ignore
    }
  }

  Future<void> addPairedDevice(PairedDevice device) async {
    if (!device.isConfigured) return;
    final devices = await loadPairedDevices();
    devices.removeWhere((d) => d.connectionId == device.connectionId);
    devices.add(device);
    await savePairedDevices(devices);
  }

  Future<void> removePairedDevice(String connectionId) async {
    final devices = await loadPairedDevices();
    devices.removeWhere((d) => d.connectionId == connectionId);
    await savePairedDevices(devices);
    final single = await loadPairedDevice();
    if (single != null && single.connectionId == connectionId) {
      await _storage.delete(_pairedKey);
    }
  }

  Future<void> clear() async {
    try {
      await _storage.delete(_pairedKey);
      await _storage.delete(_pairedListKey);
      await _deleteLegacyConnectionKeys();
    } catch (_) {
      // ignore
    }
  }
}

String _encodeBase64Url(List<int> bytes) =>
    base64Url.encode(bytes).replaceAll('=', '');

Uint8List _decodeBase64Url(String value) {
  final text = value.trim();
  return base64Url.decode(
    text.padRight(text.length + ((4 - text.length % 4) % 4), '='),
  );
}

Uint8List _decodePublicKey(String value) {
  final text = value.trim();
  final raw = text.startsWith('ed25519:') ? text.substring(8) : text;
  return _decodeBase64Url(raw);
}

Uint8List _decodePrefixedPublicKey(String value, String prefix) {
  final text = value.trim();
  final raw = text.startsWith(prefix) ? text.substring(prefix.length) : text;
  return _decodeBase64Url(raw);
}

Uint8List _hexToBytes(String value) {
  final text = value.trim();
  if (text.length.isOdd) {
    throw FormatException('invalid hex length');
  }
  final out = Uint8List(text.length ~/ 2);
  for (var i = 0; i < out.length; i++) {
    out[i] = int.parse(text.substring(i * 2, i * 2 + 2), radix: 16);
  }
  return out;
}
