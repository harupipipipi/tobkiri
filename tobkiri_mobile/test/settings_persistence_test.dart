import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FailingSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> values = {};
  final Set<int> failOperations = {};
  int operationCount = 0;

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String? value) async {
    _startOperation();
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    _startOperation();
    values.remove(key);
  }

  void _startOperation() {
    operationCount += 1;
    if (failOperations.remove(operationCount)) {
      throw StateError('injected secure storage failure');
    }
  }
}

const _originalApi = ApiConfig(
  providerId: 'openai',
  baseUrl: 'https://api.openai.com/v1',
  apiKey: 'old-key',
  model: 'old-model',
);

const _nextApi = ApiConfig(
  providerId: 'openai-compatible',
  baseUrl: 'https://example.invalid/v1',
  apiKey: 'new-key',
  model: 'new-model',
);

const _originalPc = PcConnection(
  baseUrl: 'https://old-pc.example',
  token: 'old-token',
);

const _nextPc = PcConnection(
  baseUrl: 'https://new-pc.example',
  token: 'new-token',
);

void main() {
  test('settings bundle leaves saved values unchanged on first-write failure',
      () async {
    final storage = _FailingSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.saveApi(_originalApi);
    await store.savePc(_originalPc);
    storage.operationCount = 0;
    storage.failOperations.add(1);

    await expectLater(
      store.saveApiAndPcOrRollback(_nextApi, _nextPc),
      throwsA(isA<SettingsPersistenceException>()),
    );

    expect((await store.loadApi()).apiKey, _originalApi.apiKey);
    expect((await store.loadPc())?.token, _originalPc.token);
  });

  test('settings bundle rolls API back when the PC write fails', () async {
    final storage = _FailingSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.saveApi(_originalApi);
    await store.savePc(_originalPc);
    storage.operationCount = 0;
    storage.failOperations.add(2);

    await expectLater(
      store.saveApiAndPcOrRollback(_nextApi, _nextPc),
      throwsA(
        isA<SettingsPersistenceException>()
            .having((error) => error.reconciled, 'reconciled', isTrue),
      ),
    );

    expect((await store.loadApi()).apiKey, _originalApi.apiKey);
    expect((await store.loadPc())?.token, _originalPc.token);
  });

  test('settings bundle reports unreconciled partial rollback', () async {
    final storage = _FailingSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.saveApi(_originalApi);
    await store.savePc(_originalPc);
    storage.operationCount = 0;
    storage.failOperations.addAll({2, 3});

    await expectLater(
      store.saveApiAndPcOrRollback(_nextApi, _nextPc),
      throwsA(
        isA<SettingsPersistenceException>()
            .having((error) => error.reconciled, 'reconciled', isFalse),
      ),
    );

    expect((await store.loadApi()).apiKey, _nextApi.apiKey);
    expect((await store.loadPc())?.token, _originalPc.token);
  });

  test('strict notification persistence reports storage failure', () async {
    final storage = _FailingSecureStorage()..failOperations.add(1);
    final store = ApiConfigStore(storage: storage);

    await expectLater(
      store.saveNotificationSettingsOrThrow(
        const MobileNotificationSettings(pcTaskFinishedEnabled: false),
      ),
      throwsA(isA<SettingsPersistenceException>()),
    );
  });

  test('strict provider and favorite persistence report storage failure',
      () async {
    final storage = _FailingSecureStorage()..failOperations.addAll({1, 2});
    final store = ApiConfigStore(storage: storage);

    await expectLater(
      store.saveProviderConfigsOrThrow(const []),
      throwsA(isA<SettingsPersistenceException>()),
    );
    await expectLater(
      store.saveModelFavoritesOrThrow(const []),
      throwsA(isA<SettingsPersistenceException>()),
    );
  });
}
