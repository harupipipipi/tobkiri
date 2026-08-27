import 'dart:convert';

import 'package:cryptography/cryptography.dart';
import 'package:uuid/uuid.dart';

import '../../settings/api_config_store.dart';

/// Explicit outcomes for loading the mobile device's cryptographic identity.
enum DeviceIdentityStorageState {
  absent,
  loaded,
  lockedOrUnavailable,
  permissionDenied,
  corrupt,
  incompatible,
  incomplete,
  writeFailed,
  migrationRequired,
  cryptographicallyInvalid,
}

/// A recoverable, redacted failure to load or persist the device identity.
class DeviceIdentityStorageException implements Exception {
  const DeviceIdentityStorageException(this.state, this.message);

  final DeviceIdentityStorageState state;
  final String message;

  @override
  String toString() => 'DeviceIdentityStorageException: $message';
}

/// Versioned mobile device identity stored only in platform secure storage.
///
/// The cryptography package exposes exportable Ed25519 and X25519 keys, so the
/// private material remains in the protected fallback: iOS Keychain or an
/// Android Keystore-backed AES-GCM record. Callers must never log [toJson].
class DeviceIdentity {
  const DeviceIdentity({
    required this.deviceId,
    required this.deviceLabel,
    required this.publicKey,
    this.encryptionPublicKey = '',
    this.encryptionPrivateKey = '',
    this.privateKey = '',
    this.keyType = 'ed25519',
    this.schemaVersion = 1,
    this.keyVersion = 1,
    this.recordBinding = '',
  });

  static const currentSchemaVersion = 2;

  final String deviceId;
  final String deviceLabel;
  final String publicKey;
  final String encryptionPublicKey;
  final String encryptionPrivateKey;
  final String privateKey;
  final String keyType;
  final int schemaVersion;
  final int keyVersion;
  final String recordBinding;

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
        'schemaVersion': schemaVersion,
        'keyVersion': keyVersion,
        'recordBinding': recordBinding,
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
      schemaVersion: json['schemaVersion'] as int? ?? 1,
      keyVersion: json['keyVersion'] as int? ?? 1,
      recordBinding: json['recordBinding'] as String? ?? '',
    );
  }

  DeviceIdentity copyWith({
    String? encryptionPublicKey,
    String? encryptionPrivateKey,
    int? schemaVersion,
    int? keyVersion,
    String? recordBinding,
  }) {
    return DeviceIdentity(
      deviceId: deviceId,
      deviceLabel: deviceLabel,
      publicKey: publicKey,
      encryptionPublicKey: encryptionPublicKey ?? this.encryptionPublicKey,
      encryptionPrivateKey: encryptionPrivateKey ?? this.encryptionPrivateKey,
      privateKey: privateKey,
      keyType: keyType,
      schemaVersion: schemaVersion ?? this.schemaVersion,
      keyVersion: keyVersion ?? this.keyVersion,
      recordBinding: recordBinding ?? this.recordBinding,
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

/// Loads, verifies, migrates, and durably persists one device identity.
class DurableDeviceIdentityStore {
  DurableDeviceIdentityStore({
    required SecureKeyValueStorage storage,
    Uuid uuid = const Uuid(),
  })  : _storage = storage,
        _uuid = uuid;

  static const _identityKey = 'rumi.device.identity.v1';

  final SecureKeyValueStorage _storage;
  final Uuid _uuid;
  Future<DeviceIdentity>? _identityLoad;
  DeviceIdentityStorageState _lastStorageState =
      DeviceIdentityStorageState.absent;

  DeviceIdentityStorageState get lastStorageState => _lastStorageState;

  Future<DeviceIdentity> loadOrCreateIdentity() {
    final pending = _identityLoad;
    if (pending != null) return pending;
    final load = _loadOrCreateIdentity();
    _identityLoad = load;
    return load.whenComplete(() {
      if (identical(_identityLoad, load)) _identityLoad = null;
    });
  }

  Future<DeviceIdentity> _loadOrCreateIdentity() async {
    final String? raw;
    try {
      raw = await _storage.read(_identityKey);
    } catch (error) {
      throw _storageError(error);
    }

    if (raw != null) return _loadStoredIdentity(raw);

    _lastStorageState = DeviceIdentityStorageState.absent;
    final identity = await _createIdentity();
    await _persistIdentity(identity);
    _lastStorageState = DeviceIdentityStorageState.loaded;
    return identity;
  }

  Future<DeviceIdentity> _loadStoredIdentity(String raw) async {
    if (raw.trim().isEmpty) {
      throw _error(
        DeviceIdentityStorageState.incomplete,
        'The saved device identity is incomplete. Restore it or reset it '
        'explicitly.',
      );
    }

    final Map<String, dynamic> json;
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map) throw const FormatException();
      json = Map<String, dynamic>.from(decoded);
    } catch (_) {
      throw _error(
        DeviceIdentityStorageState.corrupt,
        'The saved device identity is corrupt. Restore it or reset it '
        'explicitly.',
      );
    }

    final schemaValue = json['schemaVersion'];
    if (schemaValue != null && schemaValue is! int) {
      throw _error(
        DeviceIdentityStorageState.corrupt,
        'The saved device identity version is invalid.',
      );
    }
    final schemaVersion = schemaValue as int? ?? 1;
    if (schemaVersion < 1 ||
        schemaVersion > DeviceIdentity.currentSchemaVersion) {
      throw _error(
        DeviceIdentityStorageState.incompatible,
        'The saved device identity uses an unsupported version.',
      );
    }

    final DeviceIdentity identity;
    try {
      identity = DeviceIdentity.fromJson(json);
    } catch (_) {
      throw _error(
        DeviceIdentityStorageState.corrupt,
        'The saved device identity fields are invalid.',
      );
    }
    await _validateSigningIdentity(identity);

    final hasEncryptionPublicKey =
        identity.encryptionPublicKey.trim().isNotEmpty;
    final hasEncryptionPrivateKey =
        identity.encryptionPrivateKey.trim().isNotEmpty;
    if (hasEncryptionPublicKey != hasEncryptionPrivateKey) {
      throw _error(
        DeviceIdentityStorageState.incomplete,
        'The saved device encryption identity is incomplete.',
      );
    }

    if (schemaVersion < DeviceIdentity.currentSchemaVersion) {
      _lastStorageState = DeviceIdentityStorageState.migrationRequired;
      return _migrateIdentity(identity);
    }

    if (!hasEncryptionPublicKey ||
        json['keyVersion'] is! int ||
        identity.recordBinding.trim().isEmpty) {
      throw _error(
        DeviceIdentityStorageState.incomplete,
        'The saved device identity record is incomplete.',
      );
    }

    await _validateEncryptionIdentity(identity);
    await _validateRecordBinding(identity);
    _lastStorageState = DeviceIdentityStorageState.loaded;
    return identity;
  }

  Future<DeviceIdentity> _createIdentity() async {
    final signingKeyPair = await Ed25519().newKeyPair();
    final signingKeyPairData = await signingKeyPair.extract();
    final signingPublicKey = await signingKeyPair.extractPublicKey();
    final encryption = await _createEncryptionKeyPair();
    final identity = DeviceIdentity(
      deviceId: 'mobile-${_uuid.v4().substring(0, 12)}',
      deviceLabel: 'Tobkiri Mobile',
      publicKey: 'ed25519:${_encodeBase64Url(signingPublicKey.bytes)}',
      encryptionPublicKey: encryption.publicKey,
      encryptionPrivateKey: encryption.privateKey,
      privateKey: _encodeBase64Url(signingKeyPairData.bytes),
      keyType: 'ed25519',
      schemaVersion: DeviceIdentity.currentSchemaVersion,
      keyVersion: 1,
    );
    return _withRecordBinding(identity);
  }

  Future<DeviceIdentity> _migrateIdentity(DeviceIdentity identity) async {
    var upgraded = identity;
    if (!identity.canDecryptTokenDelivery) {
      final encryption = await _createEncryptionKeyPair();
      upgraded = identity.copyWith(
        encryptionPublicKey: encryption.publicKey,
        encryptionPrivateKey: encryption.privateKey,
      );
    }
    upgraded = await _withRecordBinding(
      upgraded.copyWith(
        schemaVersion: DeviceIdentity.currentSchemaVersion,
        keyVersion: identity.keyVersion < 1 ? 1 : identity.keyVersion,
      ),
    );
    await _validateEncryptionIdentity(upgraded);
    await _persistIdentity(upgraded);
    _lastStorageState = DeviceIdentityStorageState.loaded;
    return upgraded;
  }

  Future<void> _persistIdentity(DeviceIdentity identity) async {
    final encoded = jsonEncode(identity.toJson());
    try {
      await _storage.write(_identityKey, encoded);
    } catch (_) {
      throw _error(
        DeviceIdentityStorageState.writeFailed,
        'The device identity could not be saved. Unlock secure storage and '
        'retry.',
      );
    }

    final String? persisted;
    try {
      persisted = await _storage.read(_identityKey);
    } catch (error) {
      throw _storageError(error);
    }
    if (persisted != encoded) {
      throw _error(
        DeviceIdentityStorageState.writeFailed,
        'The saved device identity could not be verified. Unlock secure '
        'storage and retry.',
      );
    }
  }

  Future<DeviceIdentity> _withRecordBinding(DeviceIdentity identity) async {
    final binding = await _recordBinding(identity);
    return identity.copyWith(recordBinding: binding);
  }

  Future<void> _validateRecordBinding(DeviceIdentity identity) async {
    final expected = await _recordBinding(identity);
    if (identity.recordBinding != expected) {
      throw _error(
        DeviceIdentityStorageState.cryptographicallyInvalid,
        'The saved device identity failed its integrity check.',
      );
    }
  }

  Future<String> _recordBinding(DeviceIdentity identity) async {
    final fields = [
      identity.schemaVersion.toString(),
      identity.keyVersion.toString(),
      identity.deviceId,
      identity.keyType,
      identity.publicKey,
      identity.encryptionPublicKey,
    ];
    final hash = await Sha256().hash(utf8.encode(fields.join('\n')));
    return _encodeBase64Url(hash.bytes);
  }

  Future<void> _validateSigningIdentity(DeviceIdentity identity) async {
    if (identity.deviceId.trim().isEmpty ||
        identity.keyVersion < 1 ||
        !identity.canSignApproval) {
      throw _error(
        DeviceIdentityStorageState.incomplete,
        'The saved device signing identity is incomplete.',
      );
    }
    try {
      final privateKey = _decodeBase64Url(identity.privateKey);
      final publicKey = _decodePrefixedPublicKey(
        identity.publicKey,
        'ed25519:',
      );
      if (privateKey.length != 32 || publicKey.length != 32) {
        throw const FormatException();
      }
      final derived = await Ed25519().newKeyPairFromSeed(privateKey);
      final derivedPublic = await derived.extractPublicKey();
      if (!_constantTimeBytesEqual(derivedPublic.bytes, publicKey)) {
        throw const FormatException();
      }
    } catch (_) {
      throw _error(
        DeviceIdentityStorageState.cryptographicallyInvalid,
        'The saved device signing identity is cryptographically invalid.',
      );
    }
  }

  Future<void> _validateEncryptionIdentity(DeviceIdentity identity) async {
    if (!identity.canDecryptTokenDelivery) {
      throw _error(
        DeviceIdentityStorageState.incomplete,
        'The saved device encryption identity is incomplete.',
      );
    }
    try {
      final privateKey = _decodeBase64Url(identity.encryptionPrivateKey);
      final publicKey = _decodePrefixedPublicKey(
        identity.encryptionPublicKey,
        'x25519:',
      );
      if (privateKey.length != 32 || publicKey.length != 32) {
        throw const FormatException();
      }
      final derived = await X25519().newKeyPairFromSeed(privateKey);
      final derivedPublic = await derived.extractPublicKey();
      if (!_constantTimeBytesEqual(derivedPublic.bytes, publicKey)) {
        throw const FormatException();
      }
    } catch (_) {
      throw _error(
        DeviceIdentityStorageState.cryptographicallyInvalid,
        'The saved device encryption identity is cryptographically invalid.',
      );
    }
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

  bool _constantTimeBytesEqual(List<int> left, List<int> right) {
    if (left.length != right.length) return false;
    var difference = 0;
    for (var index = 0; index < left.length; index += 1) {
      difference |= left[index] ^ right[index];
    }
    return difference == 0;
  }

  DeviceIdentityStorageException _storageError(Object error) {
    final description = error.toString().toLowerCase();
    if (description.contains('corrupt') || description.contains('decode')) {
      return _error(
        DeviceIdentityStorageState.corrupt,
        'The saved device identity is corrupt. Restore it or reset it '
        'explicitly.',
      );
    }
    final permissionDenied = description.contains('permission') ||
        description.contains('denied') ||
        description.contains('unauthorized');
    return _error(
      permissionDenied
          ? DeviceIdentityStorageState.permissionDenied
          : DeviceIdentityStorageState.lockedOrUnavailable,
      permissionDenied
          ? 'Secure storage denied access to the device identity.'
          : 'The device identity is temporarily unavailable. Unlock secure '
              'storage and retry.',
    );
  }

  DeviceIdentityStorageException _error(
    DeviceIdentityStorageState state,
    String message,
  ) {
    _lastStorageState = state;
    return DeviceIdentityStorageException(state, message);
  }
}

String _encodeBase64Url(List<int> bytes) =>
    base64Url.encode(bytes).replaceAll('=', '');

List<int> _decodeBase64Url(String value) {
  final text = value.trim();
  return base64Url.decode(
    text.padRight(text.length + ((4 - text.length % 4) % 4), '='),
  );
}

List<int> _decodePrefixedPublicKey(String value, String prefix) {
  final text = value.trim();
  if (!text.startsWith(prefix)) throw const FormatException();
  return _decodeBase64Url(text.substring(prefix.length));
}
