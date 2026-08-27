import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:cryptography/cryptography.dart';

import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakeSecureStorage implements SecureKeyValueStorage {
  _FakeSecureStorage({
    Map<String, String>? values,
    this.readError,
    this.writeError,
    this.dropWrites = false,
  }) : _values = {...?values};

  final Map<String, String> _values;
  final Object? readError;
  final Object? writeError;
  final bool dropWrites;
  int readCount = 0;
  int writeCount = 0;

  @override
  Future<String?> read(String key) async {
    readCount += 1;
    if (readError != null) throw readError!;
    return _values[key];
  }

  @override
  Future<void> write(String key, String? value) async {
    writeCount += 1;
    if (writeError != null) throw writeError!;
    if (dropWrites) return;
    if (value == null) {
      _values.remove(key);
    } else {
      _values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

const _validDevice = PairedDevice(
  deviceId: 'mobile-1',
  deviceToken: 'dtk-test',
  approvalToken: 'dtk-approve',
  label: 'iPhone',
  scopes: ['chat.read', 'chat.write'],
  approvalScopes: [
    'authority.request.approve',
    'authority.request.deny',
    'authority.request.list',
    'authority.request.read',
  ],
  pcBaseUrl: 'http://192.168.11.25:8765',
  pcLabel: 'Mac',
  pairingId: 'pair-1',
);

const _missingTokenDevice = PairedDevice(
  deviceId: 'mobile-2',
  deviceToken: '',
  label: 'iPhone',
  scopes: ['chat.read', 'chat.write'],
  pcBaseUrl: 'http://192.168.11.25:8765',
  pcLabel: 'Mac',
  pairingId: 'pair-2',
);

String _encodeBase64Url(List<int> bytes) =>
    base64Url.encode(bytes).replaceAll('=', '');

Uint8List _decodeBase64Url(String value) {
  final text = value.trim();
  return base64Url.decode(
    text.padRight(text.length + ((4 - text.length % 4) % 4), '='),
  );
}

Uint8List _decodeX25519PublicKey(String value) {
  final text = value.trim();
  final raw = text.startsWith('x25519:') ? text.substring(7) : text;
  return _decodeBase64Url(raw);
}

Future<Map<String, dynamic>> _encryptTokenDeliveryForIdentity(
  DeviceIdentity identity,
  Map<String, dynamic> payload, {
  required String pairingId,
  required String deviceId,
}) async {
  const deliveryId = 'tdv-test';
  final ephemeral = await X25519().newKeyPair();
  final ephemeralData = await ephemeral.extract();
  final ephemeralPublic = await ephemeral.extractPublicKey();
  final sharedSecret = await X25519().sharedSecretKey(
    keyPair: SimpleKeyPairData(
      ephemeralData.bytes,
      publicKey: ephemeralPublic,
      type: KeyPairType.x25519,
    ),
    remotePublicKey: SimplePublicKey(
      _decodeX25519PublicKey(identity.encryptionPublicKey),
      type: KeyPairType.x25519,
    ),
  );
  final secretKey = await Hkdf(
    hmac: Hmac.sha256(),
    outputLength: 32,
  ).deriveKey(
    secretKey: sharedSecret,
    nonce: utf8.encode('rumi-mobile-token-delivery-v1'),
    info: utf8.encode('$pairingId:$deviceId:$deliveryId'),
  );
  final aad = utf8.encode(
    'rumi-mobile-token-delivery:v1:$pairingId:$deviceId:$deliveryId',
  );
  final box = await AesGcm.with256bits().encrypt(
    utf8.encode(jsonEncode(payload)),
    secretKey: secretKey,
    aad: aad,
  );
  return {
    'version': 1,
    'delivery_id': deliveryId,
    'alg': 'X25519-HKDF-SHA256-AES-256-GCM',
    'ephemeral_public_key': 'x25519:${_encodeBase64Url(ephemeralPublic.bytes)}',
    'nonce': _encodeBase64Url(box.nonce),
    'ciphertext': _encodeBase64Url(box.cipherText),
    'tag': _encodeBase64Url(box.mac.bytes),
    'aad': _encodeBase64Url(aad),
  };
}

void main() {
  test('preferredPairingBaseUrl prefers reachable LAN addresses', () {
    final selected = preferredPairingBaseUrl(const [
      'http://169.254.193.88:8765',
      'http://172.16.0.2:8765',
      'http://192.168.11.25:8765',
      'http://[fe80::1]:8765',
    ], allowCleartext: true);

    expect(selected, 'http://192.168.11.25:8765');
  });

  test('preferredPairingBaseUrl rejects cleartext when release policy is used',
      () {
    final selected = preferredPairingBaseUrl(const [
      'http://192.168.11.25:8765',
      'https://rumi.example.com',
    ], allowCleartext: false);

    expect(selected, 'https://rumi.example.com');
  });

  test('pcConnectionUrlAllowed enforces release HTTPS policy', () {
    expect(
      pcConnectionUrlAllowed(
        'http://192.168.11.25:8765',
        allowCleartext: false,
      ),
      isFalse,
    );
    expect(pcConnectionUrlAllowed('rumi.example.com'), isTrue);
    expect(
      pcConnectionUrlAllowed(
        'http://192.168.11.25:8765',
        allowCleartext: true,
      ),
      isTrue,
    );
  });

  test('preferredPairingBaseUrl falls back to first usable url', () {
    final selected = preferredPairingBaseUrl(const [
      '',
      'http://[fe80::1]:8765',
      'http://169.254.193.88:8765',
    ], allowCleartext: true);

    expect(selected, 'http://[fe80::1]:8765');
  });

  test('does not persist or load paired devices without a token', () async {
    final store = MobileDeviceStore(storage: _FakeSecureStorage());

    await store.savePairedDevice(_missingTokenDevice);

    expect(await store.loadPairedDevice(), isNull);
    expect(await store.loadPairedDevices(), isEmpty);
  });

  test('clear removes the active device and paired device list', () async {
    final store = MobileDeviceStore(storage: _FakeSecureStorage());

    await store.savePairedDevice(_validDevice);
    expect(await store.loadPairedDevice(), isNotNull);
    expect(await store.loadPairedDevices(), hasLength(1));

    await store.clear();

    expect(await store.loadPairedDevice(), isNull);
    expect(await store.loadPairedDevices(), isEmpty);
  });

  test('keeps multiple PCs paired to the same mobile device id', () async {
    final store = MobileDeviceStore(storage: _FakeSecureStorage());
    final secondPc = PairedDevice(
      deviceId: _validDevice.deviceId,
      deviceToken: 'dtk-second',
      approvalToken: 'dtk-approve-second',
      label: _validDevice.label,
      scopes: _validDevice.scopes,
      approvalScopes: _validDevice.approvalScopes,
      pcBaseUrl: 'http://192.168.11.26:8765',
      pcLabel: 'Studio Mac',
      pairingId: 'pair-2',
    );

    await store.savePairedDevice(_validDevice);
    await store.savePairedDevice(secondPc);

    final devices = await store.loadPairedDevices();
    expect(devices, hasLength(2));
    expect(
      devices.map((d) => d.connectionId),
      containsAll(['pair-1', 'pair-2']),
    );

    await store.removePairedDevice(_validDevice.connectionId);
    final remaining = await store.loadPairedDevices();
    expect(remaining, hasLength(1));
    expect(remaining.single.pcLabel, 'Studio Mac');
  });

  test('migrates legacy pc connection into paired devices once', () async {
    final storage = _FakeSecureStorage();
    final store = MobileDeviceStore(storage: storage);

    await storage.write(
      'rumi.pc_connection.v1',
      jsonEncode({
        'baseUrl': 'http://192.168.11.25:8765',
        'token': 'dtk-legacy',
      }),
    );

    final migrated = await store.loadPairedDevice();
    expect(migrated, isNotNull);
    expect(migrated!.deviceToken, 'dtk-legacy');
    expect(migrated.approvalToken, isEmpty);
    expect(migrated.scopes, ['chat.read', 'chat.write', 'tools.observe']);
    expect(migrated.approvalScopes, isEmpty);
    expect(migrated.canApprovePcTools, isFalse);
    expect(migrated.pcBaseUrl, 'http://192.168.11.25:8765');
    expect(storage._values.containsKey('rumi.pc_connection.v1'), isFalse);

    final devices = await store.loadPairedDevices();
    expect(devices, hasLength(1));
    expect(devices.single.connectionId, migrated.connectionId);

    await store.removePairedDevice(migrated.connectionId);
    expect(await store.loadPairedDevice(), isNull);
    expect(await store.loadPairedDevices(), isEmpty);
  });

  test('migrates old rumi_remote connection keys into paired devices',
      () async {
    final storage = _FakeSecureStorage();
    final store = MobileDeviceStore(storage: storage);

    await storage.write('rumi_remote.base_url', 'http://192.168.11.30:8765');
    await storage.write('rumi_remote.token', 'dtk-old-remote');

    final migrated = await store.loadPairedDevice();
    expect(migrated, isNotNull);
    expect(migrated!.pcBaseUrl, 'http://192.168.11.30:8765');
    expect(migrated.deviceToken, 'dtk-old-remote');
    expect(storage._values.containsKey('rumi_remote.base_url'), isFalse);
    expect(storage._values.containsKey('rumi_remote.token'), isFalse);
  });

  test('migrates legacy FlutterSecureStorage values when new storage is empty',
      () async {
    final storage = _FakeSecureStorage();
    final legacyStorage = _FakeSecureStorage();
    final store = MobileDeviceStore(
      storage: storage,
      legacyStorage: legacyStorage,
    );

    await legacyStorage.write(
      'rumi.pc_connection.v1',
      jsonEncode({
        'baseUrl': 'https://rumi.example.com',
        'token': 'dtk-legacy-secure',
      }),
    );

    final migrated = await store.loadPairedDevice();
    expect(migrated, isNotNull);
    expect(migrated!.pcBaseUrl, 'https://rumi.example.com');
    expect(migrated.deviceToken, 'dtk-legacy-secure');
    expect(storage._values.containsKey('rumi.paired_device.v1'), isTrue);
    expect(legacyStorage._values.containsKey('rumi.pc_connection.v1'), isFalse);
  });

  test('paired device keeps normal and approval tokens separate', () {
    expect(_validDevice.toPcConnection().token, 'dtk-test');
    expect(_validDevice.toPcConnection().approvalToken, 'dtk-approve');
    expect(_validDevice.canApprovePcTools, isTrue);

    final json = _validDevice.toJson();
    final reloaded = PairedDevice.fromJson(json);
    expect(reloaded.deviceToken, 'dtk-test');
    expect(reloaded.approvalToken, 'dtk-approve');
    expect(reloaded.clientToken, 'dtk-test');
    expect(reloaded.approverToken, 'dtk-approve');
    expect(reloaded.scopes, ['chat.read', 'chat.write']);
    expect(reloaded.approvalScopes, [
      'authority.request.approve',
      'authority.request.deny',
      'authority.request.list',
      'authority.request.read',
    ]);
    expect(reloaded.canApprovePcTools, isTrue);
  });

  test('tools.approve in normal scopes does not enable approvals', () {
    const device = PairedDevice(
      deviceId: 'mobile-3',
      deviceToken: 'dtk-test',
      label: 'iPhone',
      scopes: ['chat.read', 'tools.approve'],
      pcBaseUrl: 'http://192.168.11.25:8765',
      pcLabel: 'Mac',
      pairingId: 'pair-3',
    );

    expect(device.canApprovePcTools, isFalse);
  });

  test('device identity stores an Ed25519 signing key', () async {
    final storage = _FakeSecureStorage();
    final store = MobileDeviceStore(storage: storage);

    final identity = await store.loadOrCreateIdentity();
    final signature = await store.signApprovalPayloadHash(
      List.filled(32, '00').join(),
    );

    expect(identity.publicKey, startsWith('ed25519:'));
    expect(identity.encryptionPublicKey, startsWith('x25519:'));
    expect(identity.privateKey, isNotEmpty);
    expect(identity.encryptionPrivateKey, isNotEmpty);
    expect(identity.canDecryptTokenDelivery, isTrue);
    expect(identity.schemaVersion, DeviceIdentity.currentSchemaVersion);
    expect(identity.keyVersion, 1);
    expect(identity.recordBinding, isNotEmpty);
    expect(store.lastIdentityStorageState, DeviceIdentityStorageState.loaded);
    expect(storage.writeCount, 1);
    expect(signature, isNotEmpty);
  });

  test('secure storage read failure never creates a new identity', () async {
    final storage = _FakeSecureStorage(
      readError: StateError('keychain temporarily locked'),
    );
    final store = MobileDeviceStore(storage: storage);

    await expectLater(
      store.loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.lockedOrUnavailable,
        ),
      ),
    );

    expect(storage.writeCount, 0);
    expect(
      store.lastIdentityStorageState,
      DeviceIdentityStorageState.lockedOrUnavailable,
    );
  });

  test('corrupt identity is not overwritten with a new principal', () async {
    final storage = _FakeSecureStorage(values: {
      'rumi.device.identity.v1': '{not valid json',
    });
    final store = MobileDeviceStore(storage: storage);

    await expectLater(
      store.loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.corrupt,
        ),
      ),
    );

    expect(storage.writeCount, 0);
    expect(storage._values['rumi.device.identity.v1'], '{not valid json');
  });

  test('native secure storage corruption is not treated as absence', () async {
    final storage = _FakeSecureStorage(
      readError: StateError('Corrupt secure storage value'),
    );
    final store = MobileDeviceStore(storage: storage);

    await expectLater(
      store.loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.corrupt,
        ),
      ),
    );

    expect(storage.writeCount, 0);
  });

  test('partial encryption identity is blocked without migration', () async {
    final originalStorage = _FakeSecureStorage();
    final original = await MobileDeviceStore(storage: originalStorage)
        .loadOrCreateIdentity();
    final partial = original.toJson()..['encryptionPrivateKey'] = '';
    final encodedPartial = jsonEncode(partial);
    final storage = _FakeSecureStorage(values: {
      'rumi.device.identity.v1': encodedPartial,
    });

    await expectLater(
      MobileDeviceStore(storage: storage).loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.incomplete,
        ),
      ),
    );

    expect(storage.writeCount, 0);
    expect(storage._values['rumi.device.identity.v1'], encodedPartial);
  });

  test('current identity missing integrity metadata is not rewritten',
      () async {
    final originalStorage = _FakeSecureStorage();
    final original = await MobileDeviceStore(storage: originalStorage)
        .loadOrCreateIdentity();
    final partial = original.toJson()..remove('recordBinding');
    final encodedPartial = jsonEncode(partial);
    final storage = _FakeSecureStorage(values: {
      'rumi.device.identity.v1': encodedPartial,
    });

    await expectLater(
      MobileDeviceStore(storage: storage).loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.incomplete,
        ),
      ),
    );

    expect(storage.writeCount, 0);
    expect(storage._values['rumi.device.identity.v1'], encodedPartial);
  });

  test('new identity is not returned when secure storage write fails',
      () async {
    final storage = _FakeSecureStorage(
      writeError: StateError('keychain write failed'),
    );
    final store = MobileDeviceStore(storage: storage);

    await expectLater(
      store.loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.writeFailed,
        ),
      ),
    );

    expect(storage.writeCount, 1);
    expect(storage._values, isEmpty);
  });

  test('new identity is not returned when persistence cannot be verified',
      () async {
    final storage = _FakeSecureStorage(dropWrites: true);
    final store = MobileDeviceStore(storage: storage);

    await expectLater(
      store.loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.writeFailed,
        ),
      ),
    );

    expect(storage.writeCount, 1);
    expect(storage.readCount, 2);
  });

  test('concurrent first-run callers share one persisted identity', () async {
    final storage = _FakeSecureStorage();
    final store = MobileDeviceStore(storage: storage);

    final identities = await Future.wait([
      store.loadOrCreateIdentity(),
      store.loadOrCreateIdentity(),
      store.loadOrCreateIdentity(),
    ]);

    expect(
        identities.map((identity) => identity.deviceId).toSet(), hasLength(1));
    expect(
      identities.map((identity) => identity.publicKey).toSet(),
      hasLength(1),
    );
    expect(storage.writeCount, 1);
  });

  test('legacy identity migration preserves signing principal', () async {
    final originalStorage = _FakeSecureStorage();
    final original = await MobileDeviceStore(storage: originalStorage)
        .loadOrCreateIdentity();
    final legacyJson = original.toJson()
      ..remove('schemaVersion')
      ..remove('keyVersion')
      ..remove('recordBinding')
      ..remove('encryptionPublicKey')
      ..remove('encryptionPrivateKey');
    final storage = _FakeSecureStorage(values: {
      'rumi.device.identity.v1': jsonEncode(legacyJson),
    });

    final migrated =
        await MobileDeviceStore(storage: storage).loadOrCreateIdentity();

    expect(migrated.deviceId, original.deviceId);
    expect(migrated.publicKey, original.publicKey);
    expect(migrated.privateKey, original.privateKey);
    expect(migrated.encryptionPublicKey, startsWith('x25519:'));
    expect(migrated.schemaVersion, DeviceIdentity.currentSchemaVersion);
    expect(storage.writeCount, 1);
  });

  test('legacy identity migration blocks when persistence fails', () async {
    final originalStorage = _FakeSecureStorage();
    final original = await MobileDeviceStore(storage: originalStorage)
        .loadOrCreateIdentity();
    final legacyJson = original.toJson()
      ..remove('schemaVersion')
      ..remove('keyVersion')
      ..remove('recordBinding')
      ..remove('encryptionPublicKey')
      ..remove('encryptionPrivateKey');
    final encodedLegacy = jsonEncode(legacyJson);
    final storage = _FakeSecureStorage(
      values: {'rumi.device.identity.v1': encodedLegacy},
      writeError: StateError('keychain write failed'),
    );

    await expectLater(
      MobileDeviceStore(storage: storage).loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.writeFailed,
        ),
      ),
    );

    expect(storage._values['rumi.device.identity.v1'], encodedLegacy);
  });

  test('stored public key mismatch is rejected cryptographically', () async {
    final storage = _FakeSecureStorage();
    final original =
        await MobileDeviceStore(storage: storage).loadOrCreateIdentity();
    final other = await MobileDeviceStore(storage: _FakeSecureStorage())
        .loadOrCreateIdentity();
    final tampered = original.toJson()
      ..['publicKey'] = other.publicKey
      ..['recordBinding'] = other.recordBinding;
    storage._values['rumi.device.identity.v1'] = jsonEncode(tampered);

    await expectLater(
      MobileDeviceStore(storage: storage).loadOrCreateIdentity(),
      throwsA(
        isA<DeviceIdentityStorageException>().having(
          (error) => error.state,
          'state',
          DeviceIdentityStorageState.cryptographicallyInvalid,
        ),
      ),
    );

    expect(storage.writeCount, 1);
  });

  test('identity remains stable after store restart', () async {
    final storage = _FakeSecureStorage();
    final first =
        await MobileDeviceStore(storage: storage).loadOrCreateIdentity();
    final restarted =
        await MobileDeviceStore(storage: storage).loadOrCreateIdentity();

    expect(restarted.deviceId, first.deviceId);
    expect(restarted.publicKey, first.publicKey);
    expect(restarted.encryptionPublicKey, first.encryptionPublicKey);
    expect(restarted.keyVersion, first.keyVersion);
    expect(storage.writeCount, 1);
  });

  test('decrypts encrypted token delivery envelope', () async {
    final store = MobileDeviceStore(storage: _FakeSecureStorage());
    final identity = await store.loadOrCreateIdentity();
    final envelope = await _encryptTokenDeliveryForIdentity(
      identity,
      {
        'device_token': 'dtk-client',
        'approval_token': 'dtk-approver',
        'scopes': ['chat.read'],
      },
      pairingId: 'pair-1',
      deviceId: identity.deviceId,
    );

    final payload = await store.decryptTokenDeliveryEnvelope(
      envelope,
      pairingId: 'pair-1',
      deviceId: identity.deviceId,
    );

    expect(payload['device_token'], 'dtk-client');
    expect(payload['approval_token'], 'dtk-approver');
    expect(payload['scopes'], ['chat.read']);
  });
}
