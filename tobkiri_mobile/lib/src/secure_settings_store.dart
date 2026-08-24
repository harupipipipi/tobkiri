import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// The independently loaded sources that make up mobile Settings.
enum SettingsDataSource {
  apiConfiguration('API configuration'),
  pairedDevice('Paired device'),
  notifications('Notifications'),
  deviceIdentity('Device identity');

  const SettingsDataSource(this.label);

  final String label;
}

/// A redacted, stable diagnostic for one failed Settings source.
class SettingsLoadFailure {
  const SettingsLoadFailure({required this.source, required this.code});

  final SettingsDataSource source;
  final String code;
}

/// Persisted connection settings for the Kernel API.
class RumiRemoteSettings {
  const RumiRemoteSettings({
    required this.baseUrl,
    required this.token,
    required this.autoRefresh,
  });

  static const defaults = RumiRemoteSettings(
    baseUrl: 'http://127.0.0.1:8765',
    token: '',
    autoRefresh: false,
  );

  final String baseUrl;
  final String token;
  final bool autoRefresh;
}

/// Non-secret paired-device details safe to display in diagnostics.
class PairedDeviceSummary {
  const PairedDeviceSummary({required this.deviceId, required this.baseUrl});

  final String deviceId;
  final String baseUrl;
}

/// Mobile notification preferences stored independently from API settings.
class MobileNotificationSettings {
  const MobileNotificationSettings({required this.enabled});

  static const defaults = MobileNotificationSettings(enabled: true);

  final bool enabled;
}

/// Non-secret identity metadata safe to expose in Settings.
class DeviceIdentitySummary {
  const DeviceIdentitySummary({required this.deviceId});

  final String deviceId;
}

/// Results from independently loading every Settings source.
class SettingsLoadResult {
  const SettingsLoadResult({
    required this.apiSettings,
    required this.pairedDevice,
    required this.notifications,
    required this.deviceIdentity,
    required this.failures,
  });

  final RumiRemoteSettings? apiSettings;
  final PairedDeviceSummary? pairedDevice;
  final MobileNotificationSettings? notifications;
  final DeviceIdentitySummary? deviceIdentity;
  final List<SettingsLoadFailure> failures;

  bool get hasFailures => failures.isNotEmpty;

  bool failed(SettingsDataSource source) =>
      failures.any((failure) => failure.source == source);
}

/// Persistence contract consumed by the Settings UI.
abstract class SettingsRepository {
  Future<SettingsLoadResult> loadAll();

  Future<void> saveApi(RumiRemoteSettings settings);

  Future<void> saveNotifications(MobileNotificationSettings settings);

  /// Reset editable preferences without deleting pairing or device identity.
  Future<void> resetEditableSettings();
}

/// Minimal key/value boundary used to test secure-storage failures.
abstract class SettingsKeyValueStore {
  Future<String?> read(String key);

  Future<void> write(String key, String value);

  Future<void> delete(String key);
}

class _FlutterSettingsKeyValueStore implements SettingsKeyValueStore {
  _FlutterSettingsKeyValueStore({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

class _SettingsStoreException implements Exception {
  const _SettingsStoreException(this.code);

  final String code;
}

class _LoadOutcome<T> {
  const _LoadOutcome.value(this.value) : failure = null;

  const _LoadOutcome.failure(this.failure) : value = null;

  final T? value;
  final SettingsLoadFailure? failure;
}

/// Secure-storage implementation with fail-closed, per-source loading.
class SecureSettingsStore implements SettingsRepository {
  SecureSettingsStore({SettingsKeyValueStore? storage})
      : _storage = storage ?? _FlutterSettingsKeyValueStore();

  static const baseUrlKey = 'rumi_remote.base_url';
  static const tokenKey = 'rumi_remote.token';
  static const autoRefreshKey = 'rumi_remote.auto_refresh';
  static const schemaVersionKey = 'rumi_remote.settings_schema.v1';
  static const resetPendingKey = 'rumi_remote.settings_reset_pending.v1';
  static const notificationKey = 'rumi_remote.notifications.v1';
  static const pairedDeviceKey = 'rumi.mobile.authority_connection.v1';
  static const deviceIdentityKey = 'rumi.mobile.credential_identity.v1';

  final SettingsKeyValueStore _storage;

  @override
  Future<SettingsLoadResult> loadAll() async {
    final outcomes = await Future.wait<_LoadOutcome<Object?>>([
      _capture<RumiRemoteSettings>(
        SettingsDataSource.apiConfiguration,
        _loadApi,
      ),
      _capture<PairedDeviceSummary?>(
        SettingsDataSource.pairedDevice,
        _loadPairedDevice,
      ),
      _capture<MobileNotificationSettings>(
        SettingsDataSource.notifications,
        _loadNotifications,
      ),
      _capture<DeviceIdentitySummary?>(
        SettingsDataSource.deviceIdentity,
        _loadDeviceIdentity,
      ),
    ]);

    return SettingsLoadResult(
      apiSettings: outcomes[0].value as RumiRemoteSettings?,
      pairedDevice: outcomes[1].value as PairedDeviceSummary?,
      notifications: outcomes[2].value as MobileNotificationSettings?,
      deviceIdentity: outcomes[3].value as DeviceIdentitySummary?,
      failures: [
        for (final outcome in outcomes)
          if (outcome.failure != null) outcome.failure!,
      ],
    );
  }

  Future<_LoadOutcome<Object?>> _capture<T>(
    SettingsDataSource source,
    Future<T> Function() load,
  ) async {
    try {
      return _LoadOutcome<Object?>.value(await load());
    } on _SettingsStoreException catch (error) {
      return _LoadOutcome<Object?>.failure(
        SettingsLoadFailure(source: source, code: error.code),
      );
    } catch (_) {
      return _LoadOutcome<Object?>.failure(
        SettingsLoadFailure(source: source, code: 'read-unavailable'),
      );
    }
  }

  Future<RumiRemoteSettings> _loadApi() async {
    final values = await Future.wait([
      _storage.read(resetPendingKey),
      _storage.read(schemaVersionKey),
      _storage.read(baseUrlKey),
      _storage.read(tokenKey),
      _storage.read(autoRefreshKey),
    ]);
    if ((values[0]?.trim() ?? '').isNotEmpty) {
      throw const _SettingsStoreException('reset-incomplete');
    }
    final rawSchema = values[1]?.trim() ?? '';
    if (rawSchema.isNotEmpty) {
      final schema = int.tryParse(rawSchema);
      if (schema == null || schema < 0) {
        throw const _SettingsStoreException('corrupt-migration');
      }
      if (schema > 1) {
        throw const _SettingsStoreException('incompatible-schema');
      }
    }

    final rawRefresh = values[4];
    if (rawRefresh != null && rawRefresh != 'true' && rawRefresh != 'false') {
      throw const _SettingsStoreException('corrupt-migration');
    }
    return RumiRemoteSettings(
      baseUrl: _withDefault(values[2], RumiRemoteSettings.defaults.baseUrl),
      token: values[3] ?? '',
      autoRefresh: rawRefresh == 'true',
    );
  }

  Future<PairedDeviceSummary?> _loadPairedDevice() async {
    final raw = await _storage.read(pairedDeviceKey);
    if (raw == null || raw.trim().isEmpty) return null;
    final decoded = _decodeMap(raw, 'invalid-paired-device');
    final deviceId = _requiredString(
      decoded,
      'device_id',
      'invalid-paired-device',
    );
    final baseUrl = _requiredString(
      decoded,
      'base_url',
      'invalid-paired-device',
    );
    final uri = Uri.tryParse(baseUrl);
    final approvalToken = decoded['approval_token'];
    final approvalScopes = decoded['approval_scopes'];
    const requiredScopes = <String>{
      'authority.request.list',
      'authority.request.read',
      'authority.request.approve',
      'authority.request.deny',
    };
    final scopes = approvalScopes is List
        ? approvalScopes.map((value) => '$value').toSet()
        : const <String>{};
    if (uri == null ||
        !uri.hasScheme ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        approvalToken is! String ||
        approvalToken.trim().isEmpty ||
        scopes.length != requiredScopes.length ||
        !scopes.containsAll(requiredScopes)) {
      throw const _SettingsStoreException('invalid-paired-device');
    }
    return PairedDeviceSummary(deviceId: deviceId, baseUrl: baseUrl);
  }

  Future<MobileNotificationSettings> _loadNotifications() async {
    final raw = await _storage.read(notificationKey);
    if (raw == null || raw.trim().isEmpty) {
      return MobileNotificationSettings.defaults;
    }
    final decoded = _decodeMap(raw, 'invalid-notifications');
    final enabled = decoded['enabled'];
    if (enabled is! bool) {
      throw const _SettingsStoreException('invalid-notifications');
    }
    return MobileNotificationSettings(enabled: enabled);
  }

  Future<DeviceIdentitySummary?> _loadDeviceIdentity() async {
    final raw = await _storage.read(deviceIdentityKey);
    if (raw == null || raw.trim().isEmpty) return null;
    final decoded = _decodeMap(raw, 'invalid-device-identity');
    final publicKey = decoded['signing_public_key'];
    final privateKey = decoded['signing_private_key'];
    final publicBytes = publicKey is String && publicKey.startsWith('ed25519:')
        ? _decodeBase64Url(publicKey.substring('ed25519:'.length))
        : null;
    final privateBytes =
        privateKey is String ? _decodeBase64Url(privateKey) : null;
    if (publicBytes?.length != 32 || privateBytes?.length != 32) {
      throw const _SettingsStoreException('invalid-device-identity');
    }
    return DeviceIdentitySummary(
      deviceId: _requiredString(
        decoded,
        'device_id',
        'invalid-device-identity',
      ),
    );
  }

  @override
  Future<void> saveApi(RumiRemoteSettings settings) async {
    await Future.wait([
      _storage.write(schemaVersionKey, '1'),
      _storage.write(baseUrlKey, settings.baseUrl.trim()),
      _storage.write(tokenKey, settings.token.trim()),
      _storage.write(autoRefreshKey, settings.autoRefresh.toString()),
    ]);
  }

  @override
  Future<void> saveNotifications(MobileNotificationSettings settings) async {
    await _storage.write(
      notificationKey,
      jsonEncode(<String, Object>{'enabled': settings.enabled}),
    );
  }

  @override
  Future<void> resetEditableSettings() async {
    await _storage.write(resetPendingKey, '1');
    for (final key in [
      schemaVersionKey,
      baseUrlKey,
      tokenKey,
      autoRefreshKey,
      notificationKey,
    ]) {
      await _storage.delete(key);
    }
    await _storage.delete(resetPendingKey);
  }
}

List<int>? _decodeBase64Url(String value) {
  try {
    return base64Url.decode(base64Url.normalize(value.trim()));
  } catch (_) {
    return null;
  }
}

Map<String, dynamic> _decodeMap(String raw, String code) {
  try {
    final value = jsonDecode(raw);
    if (value is Map) return Map<String, dynamic>.from(value);
  } catch (_) {
    // Converted to a stable, redacted diagnostic below.
  }
  throw _SettingsStoreException(code);
}

String _requiredString(Map<String, dynamic> value, String key, String code) {
  final text = value[key] is String ? (value[key] as String).trim() : '';
  if (text.isEmpty) throw _SettingsStoreException(code);
  return text;
}

String _withDefault(String? value, String fallback) {
  final trimmed = value?.trim() ?? '';
  return trimmed.isEmpty ? fallback : trimmed;
}
