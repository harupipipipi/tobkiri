import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/settings/defaultspack_mobile_providers.g.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakeSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String? value) async {
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
