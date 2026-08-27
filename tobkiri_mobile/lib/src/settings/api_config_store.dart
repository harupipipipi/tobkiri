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

class SettingsRevision {
  const SettingsRevision({
    required this.revision,
    required this.api,
    required this.pc,
  });

  final int revision;
  final ApiConfig api;
  final PcConnection? pc;
}

enum SettingsCommitStatus { saved, conflict, failed }

class SettingsCommitResult {
  const SettingsCommitResult._({
    required this.status,
    this.snapshot,
    this.message,
  });

  const SettingsCommitResult.saved(SettingsRevision snapshot)
      : this._(status: SettingsCommitStatus.saved, snapshot: snapshot);

  const SettingsCommitResult.conflict(String message)
      : this._(status: SettingsCommitStatus.conflict, message: message);

  const SettingsCommitResult.failed(String message)
      : this._(status: SettingsCommitStatus.failed, message: message);

  final SettingsCommitStatus status;
  final SettingsRevision? snapshot;
  final String? message;
}

class SettingsStorageException implements Exception {
  const SettingsStorageException(this.code, this.message);

  final String code;
  final String message;

  @override
  String toString() => 'SettingsStorageException($code)';
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
  static const settingsRecordKey = 'rumi.settings_revision.v1';
  static const settingsJournalKey = 'rumi.settings_revision.journal.v1';
  static const _settingsSchemaVersion = 1;

  final SecureKeyValueStorage _storage;

  Future<SettingsRevision> loadSettingsRevision() async {
    try {
      await _recoverSettingsCommit();
      final record = await _storage.read(settingsRecordKey);
      if (record != null && record.trim().isNotEmpty) {
        return _decodeSettingsRevision(record);
      }
      return await _loadLegacySettingsRevision();
    } on SettingsStorageException {
      rethrow;
    } catch (_) {
      throw const SettingsStorageException(
        'read_failed',
        '安全な設定を読み出せませんでした。保存済みの値は変更していません。',
      );
    }
  }

  Future<SettingsCommitResult> commitSettings({
    required ApiConfig api,
    required PcConnection? pc,
    required int expectedRevision,
  }) async {
    final normalizedApi = _normalizeApiConfig(api);
    final normalizedPc = _normalizePcConnection(pc);
    if (!_validHttpUrl(normalizedApi.baseUrl) ||
        (normalizedPc != null &&
            (!normalizedPc.isConfigured ||
                !_validHttpUrl(normalizedPc.baseUrl)))) {
      return const SettingsCommitResult.failed(
        '入力内容を確認してください。設定は保存されていません。',
      );
    }

    SettingsRevision current;
    String? previousRecord;
    try {
      current = await loadSettingsRevision();
      if (current.revision != expectedRevision) {
        return const SettingsCommitResult.conflict(
          '設定が別の操作で更新されました。画面を開き直して再試行してください。',
        );
      }
      previousRecord = await _storage.read(settingsRecordKey);
    } catch (_) {
      return const SettingsCommitResult.failed(
        '安全な設定領域を利用できません。編集内容は保存されていません。',
      );
    }

    final next = SettingsRevision(
      revision: current.revision + 1,
      api: normalizedApi,
      pc: normalizedPc,
    );
    final nextRecord = _encodeSettingsRevision(next);
    final journal = jsonEncode({
      'schemaVersion': _settingsSchemaVersion,
      'previousRecord': previousRecord,
      'nextRecord': nextRecord,
    });

    try {
      await _storage.write(settingsJournalKey, journal);
      if (await _storage.read(settingsJournalKey) != journal) {
        await _deleteBestEffort(settingsJournalKey);
        return const SettingsCommitResult.failed(
          '設定の保存を安全に開始できませんでした。',
        );
      }
      await _storage.write(settingsRecordKey, nextRecord);
      if (await _storage.read(settingsRecordKey) != nextRecord) {
        await _restoreSettingsRecord(previousRecord);
        await _deleteBestEffort(settingsJournalKey);
        return const SettingsCommitResult.failed(
          '保存した設定を確認できませんでした。以前の設定を維持しています。',
        );
      }
    } catch (_) {
      try {
        if (await _storage.read(settingsRecordKey) == nextRecord) {
          await _finishSettingsCommit();
          return SettingsCommitResult.saved(next);
        }
        await _restoreSettingsRecord(previousRecord);
        await _deleteBestEffort(settingsJournalKey);
      } catch (_) {
        // Keep the journal so the next read can recover deterministically.
      }
      return const SettingsCommitResult.failed(
        '設定を保存できませんでした。以前の設定を維持しています。',
      );
    }

    await _finishSettingsCommit();
    return SettingsCommitResult.saved(next);
  }

  Future<ApiConfig> loadApi() async {
    try {
      return (await loadSettingsRevision()).api;
    } catch (_) {
      return ApiConfig.defaults;
    }
  }

  Future<SettingsCommitResult> saveApi(ApiConfig config) async {
    try {
      final current = await loadSettingsRevision();
      return await commitSettings(
        api: config,
        pc: current.pc,
        expectedRevision: current.revision,
      );
    } catch (_) {
      return const SettingsCommitResult.failed(
        '安全な設定領域を利用できません。編集内容は保存されていません。',
      );
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
      return (await loadSettingsRevision()).pc;
    } catch (_) {
      return null;
    }
  }

  Future<SettingsCommitResult> savePc(PcConnection? pc) async {
    try {
      final current = await loadSettingsRevision();
      return await commitSettings(
        api: current.api,
        pc: pc,
        expectedRevision: current.revision,
      );
    } catch (_) {
      return const SettingsCommitResult.failed(
        '安全な設定領域を利用できません。編集内容は保存されていません。',
      );
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

  Future<SettingsRevision> _loadLegacySettingsRevision() async {
    final values = await Future.wait([
      _storage.read(_apiKey),
      _storage.read(_pcKey),
    ]);
    try {
      final rawApi = values[0];
      final api = rawApi == null || rawApi.trim().isEmpty
          ? ApiConfig.defaults
          : ApiConfig.fromJson(
              Map<String, dynamic>.from(jsonDecode(rawApi) as Map),
            );
      final rawPc = values[1];
      final pc = rawPc == null || rawPc.trim().isEmpty
          ? null
          : PcConnection.fromJson(
              Map<String, dynamic>.from(jsonDecode(rawPc) as Map),
            );
      final normalizedApi = _normalizeApiConfig(api);
      final normalizedPc = _normalizePcConnection(pc);
      if (!_validHttpUrl(normalizedApi.baseUrl) ||
          (normalizedPc != null &&
              (!normalizedPc.isConfigured ||
                  !_validHttpUrl(normalizedPc.baseUrl)))) {
        throw const FormatException();
      }
      return SettingsRevision(
        revision: 0,
        api: normalizedApi,
        pc: normalizedPc,
      );
    } catch (_) {
      throw const SettingsStorageException(
        'legacy_corrupt',
        '保存済みの設定を確認できません。値は変更していません。',
      );
    }
  }

  SettingsRevision _decodeSettingsRevision(String raw) {
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map<String, dynamic> ||
          decoded['schemaVersion'] != _settingsSchemaVersion ||
          decoded['revision'] is! int ||
          decoded['revision'] as int < 1 ||
          decoded['api'] is! Map ||
          (decoded['pc'] != null && decoded['pc'] is! Map)) {
        throw const FormatException();
      }
      final api = _normalizeApiConfig(
        ApiConfig.fromJson(Map<String, dynamic>.from(decoded['api'] as Map)),
      );
      final pc = decoded['pc'] == null
          ? null
          : _normalizePcConnection(
              PcConnection.fromJson(
                Map<String, dynamic>.from(decoded['pc'] as Map),
              ),
            );
      if (!_validHttpUrl(api.baseUrl) ||
          (pc != null && (!pc.isConfigured || !_validHttpUrl(pc.baseUrl)))) {
        throw const FormatException();
      }
      return SettingsRevision(
        revision: decoded['revision'] as int,
        api: api,
        pc: pc,
      );
    } catch (_) {
      throw const SettingsStorageException(
        'record_corrupt',
        '保存済みの設定リビジョンを確認できません。値は変更していません。',
      );
    }
  }

  String _encodeSettingsRevision(SettingsRevision snapshot) => jsonEncode({
        'schemaVersion': _settingsSchemaVersion,
        'revision': snapshot.revision,
        'api': snapshot.api.toJson(),
        'pc': snapshot.pc?.toJson(),
      });

  Future<void> _recoverSettingsCommit() async {
    final rawJournal = await _storage.read(settingsJournalKey);
    if (rawJournal == null || rawJournal.trim().isEmpty) return;
    try {
      final decoded = jsonDecode(rawJournal);
      if (decoded is! Map<String, dynamic> ||
          decoded['schemaVersion'] != _settingsSchemaVersion ||
          decoded['nextRecord'] is! String) {
        throw const FormatException();
      }
      final previous = decoded['previousRecord'] as String?;
      final next = decoded['nextRecord'] as String;
      final current = await _storage.read(settingsRecordKey);
      if (current == next || current == previous) {
        await _deleteBestEffort(settingsJournalKey);
        return;
      }
      await _restoreSettingsRecord(previous);
      await _deleteBestEffort(settingsJournalKey);
    } catch (_) {
      throw const SettingsStorageException(
        'recovery_failed',
        '中断された設定保存を回復できません。値は変更していません。',
      );
    }
  }

  Future<void> _restoreSettingsRecord(String? previous) async {
    if (previous == null) {
      await _storage.delete(settingsRecordKey);
      return;
    }
    await _storage.write(settingsRecordKey, previous);
    if (await _storage.read(settingsRecordKey) != previous) {
      throw const SettingsStorageException(
        'rollback_failed',
        '以前の設定リビジョンを復元できません。',
      );
    }
  }

  Future<void> _finishSettingsCommit() async {
    await _deleteBestEffort(settingsJournalKey);
    await _deleteBestEffort(_apiKey);
    await _deleteBestEffort(_pcKey);
  }

  Future<void> _deleteBestEffort(String key) async {
    try {
      await _storage.delete(key);
    } catch (_) {
      // A stale compatibility key cannot invalidate a verified revision.
    }
  }
}

ApiConfig _normalizeApiConfig(ApiConfig config) => config.copyWith(
      providerId: config.providerId.trim(),
      baseUrl: config.baseUrl.trim().replaceFirst(RegExp(r'/+$'), ''),
      apiKey: config.apiKey.trim(),
      model: config.model.trim().isEmpty
          ? ApiConfig.defaults.model
          : config.model.trim(),
      label: config.label.trim(),
      systemPrompt: config.systemPrompt.trim(),
      apiCompatibility: config.apiCompatibility.trim(),
    );

PcConnection? _normalizePcConnection(PcConnection? pc) {
  if (pc == null) return null;
  return PcConnection(
    baseUrl: pc.baseUrl.trim().replaceFirst(RegExp(r'/+$'), ''),
    token: pc.token.trim(),
    approvalToken: pc.approvalToken.trim(),
  );
}

bool _validHttpUrl(String raw) {
  final uri = Uri.tryParse(raw.trim());
  return uri != null &&
      uri.host.isNotEmpty &&
      (uri.scheme == 'http' || uri.scheme == 'https');
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
