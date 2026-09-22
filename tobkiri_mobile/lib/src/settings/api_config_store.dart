import 'dart:convert';

import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class ApiConfig {
  const ApiConfig({
    required this.baseUrl,
    required this.apiKey,
    required this.model,
    this.providerId = 'openai',
    this.label = '',
    this.systemPrompt = '',
    this.temperature = 0.7,
    this.apiCompatibility = 'openai',
  });

  static const empty = ApiConfig(
    baseUrl: 'https://api.openai.com/v1',
    apiKey: '',
    model: 'gpt-4o-mini',
  );

  static const defaults = ApiConfig(
    baseUrl: 'https://api.openai.com/v1',
    apiKey: '',
    model: 'gpt-4o-mini',
  );

  final String baseUrl;
  final String apiKey;
  final String model;
  final String providerId;
  final String label;
  final String systemPrompt;
  final double temperature;
  final String apiCompatibility;

  bool get isConfigured =>
      baseUrl.trim().isNotEmpty && apiKey.trim().isNotEmpty;

  ApiConfig copyWith({
    String? baseUrl,
    String? apiKey,
    String? model,
    String? providerId,
    String? label,
    String? systemPrompt,
    double? temperature,
    String? apiCompatibility,
  }) {
    return ApiConfig(
      baseUrl: baseUrl ?? this.baseUrl,
      apiKey: apiKey ?? this.apiKey,
      model: model ?? this.model,
      providerId: providerId ?? this.providerId,
      label: label ?? this.label,
      systemPrompt: systemPrompt ?? this.systemPrompt,
      temperature: temperature ?? this.temperature,
      apiCompatibility: apiCompatibility ?? this.apiCompatibility,
    );
  }

  Map<String, dynamic> toJson() => {
        'baseUrl': baseUrl,
        'apiKey': apiKey,
        'model': model,
        'providerId': providerId,
        'label': label,
        'systemPrompt': systemPrompt,
        'temperature': temperature,
        'apiCompatibility': apiCompatibility,
      };

  factory ApiConfig.fromJson(Map<String, dynamic> json) => ApiConfig(
        baseUrl: json['baseUrl'] as String? ?? defaults.baseUrl,
        apiKey: json['apiKey'] as String? ?? '',
        model: json['model'] as String? ?? defaults.model,
        providerId: json['providerId'] as String? ?? 'openai',
        label: json['label'] as String? ?? '',
        systemPrompt: json['systemPrompt'] as String? ?? '',
        temperature: (json['temperature'] as num?)?.toDouble() ?? 0.7,
        apiCompatibility: json['apiCompatibility'] as String? ??
            _defaultApiCompatibility(json['providerId'] as String? ?? ''),
      );
}

String _defaultApiCompatibility(String providerId) {
  return providerId == 'anthropic' || providerId == 'opencode-zen'
      ? 'anthropic_messages'
      : 'openai';
}

class MobileProviderConfig {
  const MobileProviderConfig({
    required this.providerId,
    required this.displayName,
    required this.label,
    required this.apiKey,
    required this.baseUrl,
    required this.model,
    this.openaiCompatible = true,
    this.local = false,
    this.catalogOnly = false,
    this.apiCompatibility = 'openai',
  });

  final String providerId;
  final String displayName;
  final String label;
  final String apiKey;
  final String baseUrl;
  final String model;
  final bool openaiCompatible;
  final bool local;
  final bool catalogOnly;
  final String apiCompatibility;

  String get effectiveLabel {
    final custom = label.trim();
    if (custom.isNotEmpty) return custom;
    final display = displayName.trim();
    if (display.isNotEmpty) return display;
    return providerId;
  }

  bool get isConfigured =>
      providerId.trim().isNotEmpty &&
      baseUrl.trim().isNotEmpty &&
      model.trim().isNotEmpty &&
      (apiKey.trim().isNotEmpty || local);

  MobileProviderConfig copyWith({
    String? providerId,
    String? displayName,
    String? label,
    String? apiKey,
    String? baseUrl,
    String? model,
    bool? openaiCompatible,
    bool? local,
    bool? catalogOnly,
    String? apiCompatibility,
  }) {
    return MobileProviderConfig(
      providerId: providerId ?? this.providerId,
      displayName: displayName ?? this.displayName,
      label: label ?? this.label,
      apiKey: apiKey ?? this.apiKey,
      baseUrl: baseUrl ?? this.baseUrl,
      model: model ?? this.model,
      openaiCompatible: openaiCompatible ?? this.openaiCompatible,
      local: local ?? this.local,
      catalogOnly: catalogOnly ?? this.catalogOnly,
      apiCompatibility: apiCompatibility ?? this.apiCompatibility,
    );
  }

  ApiConfig toApiConfig({
    String systemPrompt = '',
    double temperature = 0.7,
  }) {
    return ApiConfig(
      providerId: providerId,
      baseUrl: baseUrl,
      apiKey: apiKey,
      model: model,
      label: effectiveLabel,
      systemPrompt: systemPrompt,
      temperature: temperature,
      apiCompatibility: apiCompatibility,
    );
  }

  Map<String, dynamic> toJson() => {
        'providerId': providerId,
        'displayName': displayName,
        'label': label,
        'apiKey': apiKey,
        'baseUrl': baseUrl,
        'model': model,
        'openaiCompatible': openaiCompatible,
        'local': local,
        'catalogOnly': catalogOnly,
        'apiCompatibility': apiCompatibility,
      };

  factory MobileProviderConfig.fromJson(Map<String, dynamic> json) {
    return MobileProviderConfig(
      providerId: json['providerId'] as String? ?? '',
      displayName: json['displayName'] as String? ?? '',
      label: json['label'] as String? ?? '',
      apiKey: json['apiKey'] as String? ?? '',
      baseUrl: json['baseUrl'] as String? ?? '',
      model: json['model'] as String? ?? '',
      openaiCompatible: json['openaiCompatible'] as bool? ?? true,
      local: json['local'] as bool? ?? false,
      catalogOnly: json['catalogOnly'] as bool? ?? false,
      apiCompatibility: json['apiCompatibility'] as String? ??
          _defaultApiCompatibility(json['providerId'] as String? ?? ''),
    );
  }
}

class ModelFavoriteConfig {
  const ModelFavoriteConfig({
    required this.source,
    required this.providerId,
    required this.modelId,
    this.profileId = '',
    this.label = '',
    this.pcLabel = '',
  });

  final String source;
  final String providerId;
  final String modelId;
  final String profileId;
  final String label;
  final String pcLabel;

  static const sourceMobile = 'mobile';
  static const sourcePc = 'pc';

  String get key {
    if (source == sourcePc) {
      final profile = profileId.trim().isNotEmpty
          ? profileId.trim()
          : '$providerId/$modelId';
      return '$sourcePc:$profile';
    }
    return '$sourceMobile:$providerId:$modelId';
  }

  String get effectiveLabel {
    final custom = label.trim();
    if (custom.isNotEmpty) return custom;
    final model = modelId.trim();
    if (model.isNotEmpty) return model;
    final profile = profileId.trim();
    if (profile.isNotEmpty) return profile;
    return providerId;
  }

  bool get isPc => source == sourcePc;
  bool get isMobile => source == sourceMobile;

  bool matchesMobileProvider(MobileProviderConfig provider) {
    if (!isMobile) return false;
    return provider.providerId == providerId && provider.model == modelId;
  }

  bool matchesPcProfile({
    required String effectiveProfileId,
    required String qualifiedModelId,
    required String providerId,
    required String modelId,
  }) {
    if (!isPc) return false;
    final profile = profileId.trim();
    if (profile.isNotEmpty &&
        (profile == effectiveProfileId || profile == qualifiedModelId)) {
      return true;
    }
    return this.providerId == providerId && this.modelId == modelId;
  }

  ModelFavoriteConfig copyWith({
    String? source,
    String? providerId,
    String? modelId,
    String? profileId,
    String? label,
    String? pcLabel,
  }) {
    return ModelFavoriteConfig(
      source: source ?? this.source,
      providerId: providerId ?? this.providerId,
      modelId: modelId ?? this.modelId,
      profileId: profileId ?? this.profileId,
      label: label ?? this.label,
      pcLabel: pcLabel ?? this.pcLabel,
    );
  }

  Map<String, dynamic> toJson() => {
        'source': source,
        'providerId': providerId,
        'modelId': modelId,
        'profileId': profileId,
        'label': label,
        'pcLabel': pcLabel,
      };

  factory ModelFavoriteConfig.fromJson(Map<String, dynamic> json) {
    return ModelFavoriteConfig(
      source: json['source'] as String? ?? sourceMobile,
      providerId:
          json['providerId'] as String? ?? json['provider_id'] as String? ?? '',
      modelId: json['modelId'] as String? ?? json['model_id'] as String? ?? '',
      profileId:
          json['profileId'] as String? ?? json['profile_id'] as String? ?? '',
      label: json['label'] as String? ?? '',
      pcLabel: json['pcLabel'] as String? ?? json['pc_label'] as String? ?? '',
    );
  }

  factory ModelFavoriteConfig.fromMobileProvider(
    MobileProviderConfig provider,
  ) {
    return ModelFavoriteConfig(
      source: sourceMobile,
      providerId: provider.providerId,
      modelId: provider.model,
      label: provider.effectiveLabel,
    );
  }
}

class PcConnection {
  const PcConnection({
    required this.baseUrl,
    required this.token,
    this.approvalToken = '',
  });

  final String baseUrl;
  final String token;
  final String approvalToken;

  String get clientToken => token;
  String get approverToken => approvalToken;
  bool get isConfigured => baseUrl.trim().isNotEmpty && token.trim().isNotEmpty;
  bool get canApprove => approvalToken.trim().isNotEmpty;

  Map<String, dynamic> toJson() => {
        'baseUrl': baseUrl,
        'token': token,
        'approvalToken': approvalToken,
      };

  factory PcConnection.fromJson(Map<String, dynamic> json) => PcConnection(
        baseUrl: json['baseUrl'] as String? ?? '',
        token: json['token'] as String? ?? '',
        approvalToken: json['approvalToken'] as String? ?? '',
      );
}

class MobileNotificationSettings {
  const MobileNotificationSettings({
    this.pcTaskFinishedEnabled = true,
    this.delegatePhoneToolsToPcWhenAvailable = false,
  });

  static const defaults = MobileNotificationSettings();

  final bool pcTaskFinishedEnabled;
  final bool delegatePhoneToolsToPcWhenAvailable;

  MobileNotificationSettings copyWith({
    bool? pcTaskFinishedEnabled,
    bool? delegatePhoneToolsToPcWhenAvailable,
  }) {
    return MobileNotificationSettings(
      pcTaskFinishedEnabled:
          pcTaskFinishedEnabled ?? this.pcTaskFinishedEnabled,
      delegatePhoneToolsToPcWhenAvailable:
          delegatePhoneToolsToPcWhenAvailable ??
              this.delegatePhoneToolsToPcWhenAvailable,
    );
  }

  Map<String, dynamic> toJson() => {
        'pcTaskFinishedEnabled': pcTaskFinishedEnabled,
        'delegatePhoneToolsToPcWhenAvailable':
            delegatePhoneToolsToPcWhenAvailable,
      };

  factory MobileNotificationSettings.fromJson(Map<String, dynamic> json) {
    return MobileNotificationSettings(
      pcTaskFinishedEnabled: json['pcTaskFinishedEnabled'] as bool? ??
          json['notifyPcTaskFinished'] as bool? ??
          true,
      delegatePhoneToolsToPcWhenAvailable:
          json['delegatePhoneToolsToPcWhenAvailable'] as bool? ??
              json['usePcToolsWhenAvailable'] as bool? ??
              false,
    );
  }
}

abstract class SecureKeyValueStorage {
  Future<String?> read(String key);
  Future<void> write(String key, String? value);
  Future<void> delete(String key);
}

class PlatformSecureStorage implements SecureKeyValueStorage {
  PlatformSecureStorage({MethodChannel? channel})
      : _channel =
            channel ?? const MethodChannel('ai.rumi.remote/secure_storage');

  final MethodChannel _channel;

  @override
  Future<String?> read(String key) async {
    return _channel.invokeMethod<String>('read', {'key': key});
  }

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      await delete(key);
      return;
    }
    await _channel.invokeMethod<void>('write', {'key': key, 'value': value});
  }

  @override
  Future<void> delete(String key) async {
    await _channel.invokeMethod<void>('delete', {'key': key});
  }
}

class LegacyFlutterSecureStorage implements SecureKeyValueStorage {
  LegacyFlutterSecureStorage({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(
                encryptedSharedPreferences: true,
              ),
            );

  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      await delete(key);
      return;
    }
    await _storage.write(key: key, value: value);
  }

  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

class ApiConfigStore {
  ApiConfigStore({SecureKeyValueStorage? storage})
      : _storage = storage ?? PlatformSecureStorage();

  static const _apiKey = 'rumi.api_config.v1';
  static const _providerConfigsKey = 'rumi.mobile_provider_configs.v1';
  static const _modelFavoritesKey = 'rumi.mobile_model_favorites.v1';
  static const _modelRuntimeSettingsKey =
      'rumi.mobile_model_runtime_settings.v1';
  static const _promptRecordsKey = 'rumi.mobile_prompt_records.v1';
  static const _memoryRecordsKey = 'rumi.mobile_memory_records.v1';
  static const _memoFoldersKey = 'rumi.mobile_memo_folders.v1';
  static const _memoNotesKey = 'rumi.mobile_memo_notes.v1';
  static const _knowledgeRecordsKey = 'rumi.mobile_knowledge_records.v1';
  static const _pcKey = 'rumi.pc_connection.v1';
  static const _notificationKey = 'rumi.mobile_notifications.v1';

  final SecureKeyValueStorage _storage;

  Future<ApiConfig> loadApi() async {
    try {
      final raw = await _storage.read(_apiKey);
      if (raw == null || raw.trim().isEmpty) return ApiConfig.defaults;
      return ApiConfig.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } catch (_) {
      return ApiConfig.defaults;
    }
  }

  Future<void> saveApi(ApiConfig config) async {
    try {
      await _storage.write(_apiKey, jsonEncode(config.toJson()));
    } catch (_) {
      // ignore secure storage failures (e.g. simulator keychain unavailable)
    }
  }

  Future<List<MobileProviderConfig>> loadProviderConfigs() async {
    try {
      final raw = await _storage.read(_providerConfigsKey);
      if (raw == null || raw.trim().isEmpty) return [];
      final list = jsonDecode(raw) as List;
      return list
          .whereType<Map>()
          .map((entry) => MobileProviderConfig.fromJson(
                Map<String, dynamic>.from(entry),
              ))
          .where((entry) => entry.providerId.trim().isNotEmpty)
          .toList();
    } catch (_) {
      return [];
    }
  }

  Future<void> saveProviderConfigs(List<MobileProviderConfig> configs) async {
    try {
      final byProvider = <String, MobileProviderConfig>{};
      for (final config in configs) {
        final providerId = config.providerId.trim();
        if (providerId.isEmpty) continue;
        byProvider[providerId] = config;
      }
      await _storage.write(
        _providerConfigsKey,
        jsonEncode(byProvider.values.map((c) => c.toJson()).toList()),
      );
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<void> upsertProviderConfig(MobileProviderConfig config) async {
    final configs = await loadProviderConfigs();
    final providerId = config.providerId.trim();
    if (providerId.isEmpty) return;
    final next = [
      for (final existing in configs)
        if (existing.providerId != providerId) existing,
      config,
    ];
    await saveProviderConfigs(next);
  }

  Future<void> deleteProviderConfig(String providerId) async {
    final normalized = providerId.trim();
    if (normalized.isEmpty) return;
    final configs = await loadProviderConfigs();
    await saveProviderConfigs(
      configs.where((config) => config.providerId != normalized).toList(),
    );
  }

  Future<List<ModelFavoriteConfig>> loadModelFavorites() async {
    try {
      final raw = await _storage.read(_modelFavoritesKey);
      if (raw == null || raw.trim().isEmpty) return [];
      final list = jsonDecode(raw) as List;
      return _dedupeModelFavorites(
        list
            .whereType<Map>()
            .map((entry) => ModelFavoriteConfig.fromJson(
                  Map<String, dynamic>.from(entry),
                ))
            .where((entry) => entry.key.trim().isNotEmpty)
            .toList(),
      );
    } catch (_) {
      return [];
    }
  }

  Future<void> saveModelFavorites(List<ModelFavoriteConfig> favorites) async {
    try {
      final deduped = _dedupeModelFavorites(favorites);
      await _storage.write(
        _modelFavoritesKey,
        jsonEncode(deduped.map((favorite) => favorite.toJson()).toList()),
      );
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<void> upsertModelFavorite(ModelFavoriteConfig favorite) async {
    final favorites = await loadModelFavorites();
    await saveModelFavorites([
      for (final existing in favorites)
        if (existing.key != favorite.key) existing,
      favorite,
    ]);
  }

  Future<void> deleteModelFavorite(String key) async {
    final normalized = key.trim();
    if (normalized.isEmpty) return;
    final favorites = await loadModelFavorites();
    await saveModelFavorites(
      favorites.where((favorite) => favorite.key != normalized).toList(),
    );
  }

  Future<Map<String, dynamic>> loadModelRuntimeSettings() async {
    try {
      final raw = await _storage.read(_modelRuntimeSettingsKey);
      if (raw == null || raw.trim().isEmpty) return {};
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return {};
      return Map<String, dynamic>.from(decoded);
    } catch (_) {
      return {};
    }
  }

  Future<void> saveModelRuntimeSettings(Map<String, dynamic> settings) async {
    try {
      await _storage.write(_modelRuntimeSettingsKey, jsonEncode(settings));
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<List<Map<String, dynamic>>> loadPromptRecords() async {
    try {
      final raw = await _storage.read(_promptRecordsKey);
      if (raw == null || raw.trim().isEmpty) return [];
      final decoded = jsonDecode(raw);
      if (decoded is! List) return [];
      return decoded
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((entry) => '${entry['id'] ?? ''}'.trim().isNotEmpty)
          .toList();
    } catch (_) {
      return [];
    }
  }

  Future<void> savePromptRecords(List<Map<String, dynamic>> records) async {
    try {
      final byId = <String, Map<String, dynamic>>{};
      for (final record in records) {
        final id = '${record['id'] ?? ''}'.trim();
        if (id.isEmpty) continue;
        byId[id] = Map<String, dynamic>.from(record);
      }
      await _storage.write(_promptRecordsKey, jsonEncode(byId.values.toList()));
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<void> upsertPromptRecord(Map<String, dynamic> record) async {
    final id = '${record['id'] ?? ''}'.trim();
    if (id.isEmpty) return;
    final records = await loadPromptRecords();
    await savePromptRecords([
      for (final existing in records)
        if ('${existing['id'] ?? ''}'.trim() != id) existing,
      Map<String, dynamic>.from(record),
    ]);
  }

  Future<void> deletePromptRecord(String id) async {
    final normalized = id.trim();
    if (normalized.isEmpty) return;
    final records = await loadPromptRecords();
    await savePromptRecords(
      records
          .where((record) => '${record['id'] ?? ''}'.trim() != normalized)
          .toList(),
    );
  }

  Future<List<Map<String, dynamic>>> loadMemoryRecords() {
    return _loadRecordList(_memoryRecordsKey);
  }

  Future<void> saveMemoryRecords(List<Map<String, dynamic>> records) {
    return _saveRecordList(_memoryRecordsKey, records);
  }

  Future<void> upsertMemoryRecord(Map<String, dynamic> record) async {
    final id = '${record['id'] ?? ''}'.trim();
    if (id.isEmpty) return;
    final records = await loadMemoryRecords();
    await saveMemoryRecords([
      for (final existing in records)
        if ('${existing['id'] ?? ''}'.trim() != id) existing,
      Map<String, dynamic>.from(record),
    ]);
  }

  Future<void> deleteMemoryRecord(String id) async {
    await _deleteRecord(_memoryRecordsKey, id);
  }

  Future<List<Map<String, dynamic>>> loadMemoFolders() {
    return _loadRecordList(_memoFoldersKey);
  }

  Future<void> saveMemoFolders(List<Map<String, dynamic>> records) {
    return _saveRecordList(_memoFoldersKey, records);
  }

  Future<void> upsertMemoFolder(Map<String, dynamic> record) async {
    await _upsertRecord(_memoFoldersKey, record);
  }

  Future<void> deleteMemoFolder(String id) async {
    await _deleteRecord(_memoFoldersKey, id);
  }

  Future<List<Map<String, dynamic>>> loadMemoNotes() {
    return _loadRecordList(_memoNotesKey);
  }

  Future<void> saveMemoNotes(List<Map<String, dynamic>> records) {
    return _saveRecordList(_memoNotesKey, records);
  }

  Future<void> upsertMemoNote(Map<String, dynamic> record) async {
    await _upsertRecord(_memoNotesKey, record);
  }

  Future<void> deleteMemoNote(String id) async {
    await _deleteRecord(_memoNotesKey, id);
  }

  Future<List<Map<String, dynamic>>> loadKnowledgeRecords() {
    return _loadRecordList(_knowledgeRecordsKey);
  }

  Future<void> saveKnowledgeRecords(List<Map<String, dynamic>> records) {
    return _saveRecordList(_knowledgeRecordsKey, records);
  }

  Future<void> upsertKnowledgeRecord(Map<String, dynamic> record) async {
    await _upsertRecord(_knowledgeRecordsKey, record);
  }

  Future<void> deleteKnowledgeRecord(String id) async {
    await _deleteRecord(_knowledgeRecordsKey, id);
  }

  Future<PcConnection?> loadPc() async {
    try {
      final raw = await _storage.read(_pcKey);
      if (raw == null || raw.trim().isEmpty) return null;
      return PcConnection.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  Future<void> savePc(PcConnection? pc) async {
    try {
      if (pc == null || !pc.isConfigured) {
        await _storage.delete(_pcKey);
        return;
      }
      await _storage.write(_pcKey, jsonEncode(pc.toJson()));
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<MobileNotificationSettings> loadNotificationSettings() async {
    try {
      final raw = await _storage.read(_notificationKey);
      if (raw == null || raw.trim().isEmpty) {
        return MobileNotificationSettings.defaults;
      }
      return MobileNotificationSettings.fromJson(
        jsonDecode(raw) as Map<String, dynamic>,
      );
    } catch (_) {
      return MobileNotificationSettings.defaults;
    }
  }

  Future<void> saveNotificationSettings(
    MobileNotificationSettings settings,
  ) async {
    try {
      await _storage.write(_notificationKey, jsonEncode(settings.toJson()));
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<List<Map<String, dynamic>>> _loadRecordList(String key) async {
    try {
      final raw = await _storage.read(key);
      if (raw == null || raw.trim().isEmpty) return [];
      final decoded = jsonDecode(raw);
      if (decoded is! List) return [];
      return decoded
          .whereType<Map>()
          .map((entry) => Map<String, dynamic>.from(entry))
          .where((entry) => '${entry['id'] ?? ''}'.trim().isNotEmpty)
          .toList();
    } catch (_) {
      return [];
    }
  }

  Future<void> _saveRecordList(
    String key,
    List<Map<String, dynamic>> records,
  ) async {
    try {
      final byId = <String, Map<String, dynamic>>{};
      for (final record in records) {
        final id = '${record['id'] ?? ''}'.trim();
        if (id.isEmpty) continue;
        byId[id] = Map<String, dynamic>.from(record);
      }
      await _storage.write(key, jsonEncode(byId.values.toList()));
    } catch (_) {
      // ignore secure storage failures
    }
  }

  Future<void> _upsertRecord(
    String key,
    Map<String, dynamic> record,
  ) async {
    final id = '${record['id'] ?? ''}'.trim();
    if (id.isEmpty) return;
    final records = await _loadRecordList(key);
    await _saveRecordList(key, [
      for (final existing in records)
        if ('${existing['id'] ?? ''}'.trim() != id) existing,
      Map<String, dynamic>.from(record),
    ]);
  }

  Future<void> _deleteRecord(String key, String id) async {
    final normalized = id.trim();
    if (normalized.isEmpty) return;
    final records = await _loadRecordList(key);
    await _saveRecordList(
      key,
      records
          .where((record) => '${record['id'] ?? ''}'.trim() != normalized)
          .toList(),
    );
  }
}

List<ModelFavoriteConfig> _dedupeModelFavorites(
  Iterable<ModelFavoriteConfig> favorites,
) {
  final byKey = <String, ModelFavoriteConfig>{};
  for (final favorite in favorites) {
    final key = favorite.key.trim();
    if (key.isEmpty) continue;
    byKey[key] = favorite;
  }
  final list = byKey.values.toList()
    ..sort((a, b) {
      final source = a.source.compareTo(b.source);
      if (source != 0) return source;
      return a.effectiveLabel
          .toLowerCase()
          .compareTo(b.effectiveLabel.toLowerCase());
    });
  return list;
}
