import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/settings/defaultspack_mobile_providers.g.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakeSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> values = {};
  int writes = 0;
  String? throwBeforeReadKey;
  String? throwBeforeWriteKey;
  String? throwAfterWriteKey;
  String? corruptWriteKey;
  String? throwBeforeDeleteKey;

  @override
  Future<String?> read(String key) async {
    if (throwBeforeReadKey == key) {
      throw StateError('secure storage unavailable');
    }
    return values[key];
  }

  @override
  Future<void> write(String key, String? value) async {
    writes += 1;
    if (throwBeforeWriteKey == key) {
      throw StateError('secure storage unavailable');
    }
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = corruptWriteKey == key ? 'corrupt' : value;
    }
    if (throwAfterWriteKey == key) {
      throw StateError('timeout after durable write');
    }
  }

  @override
  Future<void> delete(String key) async {
    if (throwBeforeDeleteKey == key) {
      throw StateError('secure storage unavailable');
    }
    values.remove(key);
  }
}

const _api = ApiConfig(
  baseUrl: 'https://api.example.test/v1',
  apiKey: 'secret-api-key',
  model: 'test-model',
);

const _pc = PcConnection(
  baseUrl: 'https://pc.example.test',
  token: 'secret-pc-token',
);

void main() {
  test('mobile notification settings default PC task finish notifications on',
      () async {
    final store = ApiConfigStore(storage: _FakeSecureStorage());

    final settings = await store.loadNotificationSettings();

    expect(settings.pcTaskFinishedEnabled, isTrue);
    expect(settings.delegatePhoneToolsToPcWhenAvailable, isFalse);
  });

  test('mobile notification settings persist PC task finish preference',
      () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);

    await store.saveNotificationSettings(
      const MobileNotificationSettings(pcTaskFinishedEnabled: false),
    );

    final settings = await store.loadNotificationSettings();
    expect(settings.pcTaskFinishedEnabled, isFalse);
  });

  test('mobile settings persist PC tool delegation preference', () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);

    await store.saveNotificationSettings(
      const MobileNotificationSettings(
        delegatePhoneToolsToPcWhenAvailable: true,
      ),
    );

    final settings = await store.loadNotificationSettings();
    expect(settings.pcTaskFinishedEnabled, isTrue);
    expect(settings.delegatePhoneToolsToPcWhenAvailable, isTrue);
  });

  test('provider configs persist and replace by provider id', () async {
    final store = ApiConfigStore(storage: _FakeSecureStorage());

    await store.upsertProviderConfig(
      const MobileProviderConfig(
        providerId: 'openrouter',
        displayName: 'OpenRouter',
        label: 'Router',
        apiKey: 'sk-one',
        baseUrl: 'https://openrouter.ai/api/v1',
        model: 'openai/gpt-4o-mini',
        apiCompatibility: 'openai',
      ),
    );
    await store.upsertProviderConfig(
      const MobileProviderConfig(
        providerId: 'openrouter',
        displayName: 'OpenRouter',
        label: 'Router 2',
        apiKey: 'sk-two',
        baseUrl: 'https://openrouter.ai/api/v1',
        model: 'openai/gpt-4o-mini',
        apiCompatibility: 'openai',
      ),
    );

    final configs = await store.loadProviderConfigs();
    expect(configs, hasLength(1));
    expect(configs.single.providerId, 'openrouter');
    expect(configs.single.label, 'Router 2');
    expect(configs.single.apiKey, 'sk-two');
    expect(configs.single.apiCompatibility, 'openai');
  });

  test('api config persists anthropic-compatible provider mode', () async {
    final store = ApiConfigStore(storage: _FakeSecureStorage());

    await store.saveApi(
      const ApiConfig(
        providerId: 'opencode-zen',
        baseUrl: 'https://opencode.ai/zen',
        apiKey: 'sk-zen',
        model: 'minimax-m3-free',
        apiCompatibility: 'anthropic_messages',
      ),
    );

    final config = await store.loadApi();
    expect(config.providerId, 'opencode-zen');
    expect(config.apiCompatibility, 'anthropic_messages');
  });

  test('settings commit stores one verified revision', () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);

    final result = await store.commitSettings(
      api: _api,
      pc: _pc,
      expectedRevision: 0,
    );

    expect(result.status, SettingsCommitStatus.saved);
    expect(result.snapshot?.revision, 1);
    expect((await store.loadApi()).apiKey, 'secret-api-key');
    expect((await store.loadPc())?.token, 'secret-pc-token');
    expect(storage.values, contains(ApiConfigStore.settingsRecordKey));
    expect(storage.values, isNot(contains(ApiConfigStore.settingsJournalKey)));
  });

  test('invalid settings perform no storage write', () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);

    final result = await store.commitSettings(
      api: _api.copyWith(baseUrl: 'not a url'),
      pc: _pc,
      expectedRevision: 0,
    );

    expect(result.status, SettingsCommitStatus.failed);
    expect(storage.writes, 0);
  });

  test('failed record write preserves the previous revision', () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.commitSettings(api: _api, pc: _pc, expectedRevision: 0);
    storage.throwBeforeWriteKey = ApiConfigStore.settingsRecordKey;

    final result = await store.commitSettings(
      api: _api.copyWith(apiKey: 'replacement'),
      pc: _pc,
      expectedRevision: 1,
    );

    expect(result.status, SettingsCommitStatus.failed);
    expect((await store.loadSettingsRevision()).api.apiKey, 'secret-api-key');
  });

  test('failed journal write never touches the committed revision', () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.commitSettings(api: _api, pc: _pc, expectedRevision: 0);
    final previous = storage.values[ApiConfigStore.settingsRecordKey];
    storage.throwBeforeWriteKey = ApiConfigStore.settingsJournalKey;

    final result = await store.commitSettings(
      api: _api.copyWith(apiKey: 'replacement'),
      pc: _pc,
      expectedRevision: 1,
    );

    expect(result.status, SettingsCommitStatus.failed);
    expect(storage.values[ApiConfigStore.settingsRecordKey], previous);
  });

  test('timeout after a durable record is acknowledged as saved', () async {
    final storage = _FakeSecureStorage()
      ..throwAfterWriteKey = ApiConfigStore.settingsRecordKey;
    final store = ApiConfigStore(storage: storage);

    final result = await store.commitSettings(
      api: _api,
      pc: _pc,
      expectedRevision: 0,
    );

    expect(result.status, SettingsCommitStatus.saved);
    expect((await store.loadSettingsRevision()).revision, 1);
  });

  test('stale revision conflicts without overwriting the newer settings',
      () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.commitSettings(api: _api, pc: _pc, expectedRevision: 0);
    final writesBeforeConflict = storage.writes;

    final result = await store.commitSettings(
      api: _api.copyWith(apiKey: 'stale'),
      pc: _pc,
      expectedRevision: 0,
    );

    expect(result.status, SettingsCommitStatus.conflict);
    expect(storage.writes, writesBeforeConflict);
    expect((await store.loadSettingsRevision()).api.apiKey, 'secret-api-key');
  });

  test('restart completes an app-killed durable revision from its journal',
      () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.commitSettings(api: _api, pc: _pc, expectedRevision: 0);
    final previous = storage.values[ApiConfigStore.settingsRecordKey]!;
    final next = jsonEncode({
      'schemaVersion': 1,
      'revision': 2,
      'api': _api.copyWith(apiKey: 'after-restart').toJson(),
      'pc': _pc.toJson(),
    });
    storage.values[ApiConfigStore.settingsRecordKey] = next;
    storage.values[ApiConfigStore.settingsJournalKey] = jsonEncode({
      'schemaVersion': 1,
      'previousRecord': previous,
      'nextRecord': next,
    });

    final recovered = await ApiConfigStore(
      storage: storage,
    ).loadSettingsRevision();

    expect(recovered.revision, 2);
    expect(recovered.api.apiKey, 'after-restart');
    expect(storage.values, isNot(contains(ApiConfigStore.settingsJournalKey)));
  });

  test('a verified revision remains readable when journal cleanup fails',
      () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.commitSettings(api: _api, pc: _pc, expectedRevision: 0);
    final current = storage.values[ApiConfigStore.settingsRecordKey]!;
    storage.values[ApiConfigStore.settingsJournalKey] = jsonEncode({
      'schemaVersion': 1,
      'previousRecord': null,
      'nextRecord': current,
    });
    storage.throwBeforeDeleteKey = ApiConfigStore.settingsJournalKey;

    final recovered = await ApiConfigStore(
      storage: storage,
    ).loadSettingsRevision();

    expect(recovered.revision, 1);
    expect(recovered.api.apiKey, 'secret-api-key');
    expect(storage.values, contains(ApiConfigStore.settingsJournalKey));
  });

  test('restart rolls an incomplete record back to the previous revision',
      () async {
    final storage = _FakeSecureStorage();
    final store = ApiConfigStore(storage: storage);
    await store.commitSettings(api: _api, pc: _pc, expectedRevision: 0);
    final previous = storage.values[ApiConfigStore.settingsRecordKey]!;
    final next = jsonEncode({
      'schemaVersion': 1,
      'revision': 2,
      'api': _api.copyWith(apiKey: 'partial').toJson(),
      'pc': _pc.toJson(),
    });
    storage.values[ApiConfigStore.settingsRecordKey] = 'partial-record';
    storage.values[ApiConfigStore.settingsJournalKey] = jsonEncode({
      'schemaVersion': 1,
      'previousRecord': previous,
      'nextRecord': next,
    });

    final recovered = await ApiConfigStore(
      storage: storage,
    ).loadSettingsRevision();

    expect(recovered.revision, 1);
    expect(recovered.api.apiKey, 'secret-api-key');
    expect(storage.values, isNot(contains(ApiConfigStore.settingsJournalKey)));
  });

  test('storage unavailable returns a typed read failure', () async {
    final storage = _FakeSecureStorage()
      ..throwBeforeReadKey = ApiConfigStore.settingsJournalKey;
    final store = ApiConfigStore(storage: storage);

    expect(
      store.loadSettingsRevision(),
      throwsA(
        isA<SettingsStorageException>()
            .having((error) => error.code, 'code', 'read_failed')
            .having(
              (error) => error.message,
              'message',
              isNot(contains('secret-api-key')),
            ),
      ),
    );
  });

  test('defaultspack mobile provider catalog includes direct providers',
      () async {
    final byId = {
      for (final provider in defaultspackMobileProviderConfigs)
        provider.providerId: provider,
    };

    expect(byId['openai']?.baseUrl, 'https://api.openai.com/v1');
    expect(byId['google']?.apiCompatibility, 'openai');
    expect(byId['anthropic']?.apiCompatibility, 'anthropic_messages');
    expect(
      byId['xiaomi-token-plan-ams']?.baseUrl,
      'https://token-plan-ams.xiaomimimo.com/v1',
    );
  });
}
