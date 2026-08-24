import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/secure_settings_store.dart';

void main() {
  group('SecureSettingsStore', () {
    test(
      'API-store failure preserves independently loaded device data',
      () async {
        final storage = _MemorySettingsStore(
          values: {
            SecureSettingsStore.baseUrlKey: 'https://secret.example.test',
            SecureSettingsStore.pairedDeviceKey: jsonEncode({
              'device_id': 'paired-1',
              'base_url': 'https://pc.example.test',
              'approval_token': 'secret-token',
              'approval_scopes': [
                'authority.request.list',
                'authority.request.read',
                'authority.request.approve',
                'authority.request.deny',
              ],
            }),
            SecureSettingsStore.notificationKey: jsonEncode({'enabled': false}),
            SecureSettingsStore.deviceIdentityKey: jsonEncode({
              'device_id': 'identity-1',
              'signing_public_key': 'ed25519:${_keyBytes(1)}',
              'signing_private_key': _keyBytes(2),
            }),
          },
          readFailures: {SecureSettingsStore.baseUrlKey},
        );

        final result = await SecureSettingsStore(storage: storage).loadAll();

        expect(result.apiSettings, isNull);
        expect(result.pairedDevice?.deviceId, 'paired-1');
        expect(result.notifications?.enabled, isFalse);
        expect(result.deviceIdentity?.deviceId, 'identity-1');
        expect(result.failures, hasLength(1));
        expect(
          result.failures.single.source,
          SettingsDataSource.apiConfiguration,
        );
        expect(result.failures.single.code, 'read-unavailable');
      },
    );

    test(
      'paired-device read failure does not discard API or identity',
      () async {
        final storage = _MemorySettingsStore(
          values: {
            SecureSettingsStore.baseUrlKey: 'https://pc.example.test',
            SecureSettingsStore.tokenKey: 'never-render-this-token',
            SecureSettingsStore.deviceIdentityKey: jsonEncode({
              'device_id': 'identity-2',
              'signing_public_key': 'ed25519:${_keyBytes(3)}',
              'signing_private_key': _keyBytes(4),
            }),
          },
          readFailures: {SecureSettingsStore.pairedDeviceKey},
        );

        final result = await SecureSettingsStore(storage: storage).loadAll();

        expect(result.apiSettings?.baseUrl, 'https://pc.example.test');
        expect(result.apiSettings?.token, 'never-render-this-token');
        expect(result.deviceIdentity?.deviceId, 'identity-2');
        expect(result.failures.single.source, SettingsDataSource.pairedDevice);
      },
    );

    test(
      'notification failure is isolated from other Settings sources',
      () async {
        final storage = _MemorySettingsStore(
          values: {
            SecureSettingsStore.notificationKey: '{"enabled":"not-a-bool"}',
          },
        );

        final result = await SecureSettingsStore(storage: storage).loadAll();

        expect(result.apiSettings, isNotNull);
        expect(result.notifications, isNull);
        expect(result.failures.single.source, SettingsDataSource.notifications);
        expect(result.failures.single.code, 'invalid-notifications');
      },
    );

    test('paired-device diagnostics reject credential-bearing URLs', () async {
      final storage = _MemorySettingsStore(
        values: {
          SecureSettingsStore.pairedDeviceKey: jsonEncode({
            'device_id': 'paired-credential-url',
            'base_url': 'https://user:secret@pc.example.test',
            'approval_token': 'approval-secret',
            'approval_scopes': [
              'authority.request.list',
              'authority.request.read',
              'authority.request.approve',
              'authority.request.deny',
            ],
          }),
        },
      );

      final result = await SecureSettingsStore(storage: storage).loadAll();

      expect(result.pairedDevice, isNull);
      expect(result.failures.single.source, SettingsDataSource.pairedDevice);
      expect(result.failures.single.code, 'invalid-paired-device');
    });

    test('identity summary requires signer-compatible key lengths', () async {
      final storage = _MemorySettingsStore(
        values: {
          SecureSettingsStore.deviceIdentityKey: jsonEncode({
            'device_id': 'identity-corrupt',
            'signing_public_key': 'ed25519:not-base64',
            'signing_private_key': _keyBytes(5),
          }),
        },
      );

      final result = await SecureSettingsStore(storage: storage).loadAll();

      expect(result.deviceIdentity, isNull);
      expect(result.failures.single.source, SettingsDataSource.deviceIdentity);
      expect(result.failures.single.code, 'invalid-device-identity');
    });

    test(
      'corrupt migration metadata does not become editable defaults',
      () async {
        final storage = _MemorySettingsStore(
          values: {
            SecureSettingsStore.schemaVersionKey: 'not-a-version',
            SecureSettingsStore.baseUrlKey: 'https://recoverable.example.test',
            SecureSettingsStore.tokenKey: 'recoverable-secret',
          },
        );

        final result = await SecureSettingsStore(storage: storage).loadAll();

        expect(result.apiSettings, isNull);
        expect(
          result.failures.single.source,
          SettingsDataSource.apiConfiguration,
        );
        expect(result.failures.single.code, 'corrupt-migration');
        expect(
          storage.values[SecureSettingsStore.tokenKey],
          'recoverable-secret',
        );
      },
    );

    test(
      'reset clears editable settings but preserves pairing and identity',
      () async {
        final storage = _MemorySettingsStore(
          values: {
            SecureSettingsStore.schemaVersionKey: '1',
            SecureSettingsStore.baseUrlKey: 'https://pc.example.test',
            SecureSettingsStore.tokenKey: 'secret',
            SecureSettingsStore.autoRefreshKey: 'true',
            SecureSettingsStore.notificationKey: jsonEncode({'enabled': false}),
            SecureSettingsStore.pairedDeviceKey: 'paired-record',
            SecureSettingsStore.deviceIdentityKey: 'identity-record',
          },
        );

        await SecureSettingsStore(storage: storage).resetEditableSettings();

        expect(storage.values[SecureSettingsStore.schemaVersionKey], isNull);
        expect(storage.values[SecureSettingsStore.baseUrlKey], isNull);
        expect(storage.values[SecureSettingsStore.tokenKey], isNull);
        expect(storage.values[SecureSettingsStore.autoRefreshKey], isNull);
        expect(storage.values[SecureSettingsStore.notificationKey], isNull);
        expect(storage.values[SecureSettingsStore.resetPendingKey], isNull);
        expect(
          storage.values[SecureSettingsStore.pairedDeviceKey],
          'paired-record',
        );
        expect(
          storage.values[SecureSettingsStore.deviceIdentityKey],
          'identity-record',
        );
      },
    );

    test('interrupted reset remains explicit and can safely resume', () async {
      final storage = _MemorySettingsStore(
        values: {
          SecureSettingsStore.baseUrlKey: 'https://pc.example.test',
          SecureSettingsStore.tokenKey: 'recoverable-token',
          SecureSettingsStore.pairedDeviceKey: 'paired-record',
          SecureSettingsStore.deviceIdentityKey: 'identity-record',
        },
        deleteFailures: {SecureSettingsStore.tokenKey},
      );
      final store = SecureSettingsStore(storage: storage);

      await expectLater(store.resetEditableSettings(), throwsStateError);
      expect(storage.values[SecureSettingsStore.resetPendingKey], '1');
      final interrupted = await store.loadAll();
      expect(interrupted.apiSettings, isNull);
      expect(interrupted.failures.first.code, 'reset-incomplete');
      expect(
        storage.values[SecureSettingsStore.pairedDeviceKey],
        'paired-record',
      );
      expect(
        storage.values[SecureSettingsStore.deviceIdentityKey],
        'identity-record',
      );

      storage.deleteFailures.clear();
      await store.resetEditableSettings();
      expect(storage.values[SecureSettingsStore.resetPendingKey], isNull);
      final completed = await store.loadAll();
      expect(
        completed.apiSettings?.baseUrl,
        RumiRemoteSettings.defaults.baseUrl,
      );
      expect(completed.apiSettings?.token, isEmpty);
    });
  });
}

String _keyBytes(int value) => base64Url.encode(List<int>.filled(32, value));

class _MemorySettingsStore implements SettingsKeyValueStore {
  _MemorySettingsStore({
    Map<String, String>? values,
    Set<String>? readFailures,
    Set<String>? deleteFailures,
  })  : values = {...?values},
        readFailures = {...?readFailures},
        deleteFailures = {...?deleteFailures};

  final Map<String, String> values;
  final Set<String> readFailures;
  final Set<String> deleteFailures;

  @override
  Future<void> delete(String key) async {
    if (deleteFailures.contains(key)) throw StateError('delete unavailable');
    values.remove(key);
  }

  @override
  Future<String?> read(String key) async {
    if (readFailures.contains(key)) throw StateError('sensitive storage error');
    return values[key];
  }

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }
}
