class ProviderEntry {
  const ProviderEntry({
    required this.providerId,
    required this.displayName,
    required this.kind,
    required this.configured,
    required this.openaiCompatible,
    required this.local,
    required this.catalogOnly,
    required this.defaultModel,
    required this.defaultBaseUrl,
    required this.defaultModelFor,
    required this.capabilities,
    required this.envVars,
    required this.baseUrlEnvs,
    required this.configuredApiCount,
  });

  final String providerId;
  final String displayName;
  final String kind;
  final bool configured;
  final bool openaiCompatible;
  final bool local;
  final bool catalogOnly;
  final String defaultModel;
  final String defaultBaseUrl;
  final Map<String, String> defaultModelFor;
  final List<String> capabilities;
  final List<String> envVars;
  final List<String> baseUrlEnvs;
  final int configuredApiCount;

  bool get isCloud => !local && !catalogOnly;

  factory ProviderEntry.fromJson(Map<String, dynamic> json) {
    return ProviderEntry(
      providerId: json['provider_id'] as String? ?? '',
      displayName: json['display_name'] as String? ?? '',
      kind: json['kind'] as String? ?? '',
      configured: json['configured'] as bool? ?? false,
      openaiCompatible: json['openai_compatible'] as bool? ?? false,
      local: json['local'] as bool? ?? false,
      catalogOnly: json['catalog_only'] as bool? ?? false,
      defaultModel: json['default_model'] as String? ?? '',
      defaultBaseUrl: json['default_base_url'] as String? ?? '',
      defaultModelFor: (json['default_model_for'] as Map? ?? const {}).map(
        (key, value) => MapEntry(key.toString(), value.toString()),
      ),
      capabilities: (json['capabilities'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
      envVars:
          (json['env_vars'] as List? ?? []).map((e) => e.toString()).toList(),
      baseUrlEnvs: (json['base_url_envs'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
      configuredApiCount: (json['configured_api_count'] as num?)?.toInt() ?? 0,
    );
  }

  Map<String, dynamic> toJson() => {
        'provider_id': providerId,
        'display_name': displayName,
        'kind': kind,
        'configured': configured,
        'openai_compatible': openaiCompatible,
        'local': local,
        'catalog_only': catalogOnly,
        'default_model': defaultModel,
        'default_base_url': defaultBaseUrl,
        'default_model_for': defaultModelFor,
        'capabilities': capabilities,
        'env_vars': envVars,
        'base_url_envs': baseUrlEnvs,
        'configured_api_count': configuredApiCount,
      };
}

class ModelEntry {
  const ModelEntry({
    required this.id,
    required this.providerId,
    required this.modelId,
    required this.displayName,
    required this.type,
    required this.enabled,
    required this.maxContext,
    required this.supportsThinking,
    required this.supportsVision,
    required this.supportsToolCalling,
    required this.thinkingLevels,
    required this.defaultThinkingLevel,
    required this.speedTier,
    required this.costTier,
    required this.capabilityTags,
  });

  final String id;
  final String providerId;
  final String modelId;
  final String displayName;
  final String type;
  final bool enabled;
  final int maxContext;
  final bool supportsThinking;
  final bool supportsVision;
  final bool supportsToolCalling;
  final List<String> thinkingLevels;
  final String? defaultThinkingLevel;
  final String speedTier;
  final String costTier;
  final List<String> capabilityTags;

  factory ModelEntry.fromJson(Map<String, dynamic> json) {
    return ModelEntry(
      id: json['id'] as String? ?? '',
      providerId: json['provider_id'] as String? ?? '',
      modelId: json['model_id'] as String? ?? '',
      displayName: json['display_name'] as String? ?? '',
      type: json['type'] as String? ?? 'chat',
      enabled: json['enabled'] as bool? ?? false,
      maxContext: (json['max_context'] as num?)?.toInt() ?? -1,
      supportsThinking: json['supports_thinking'] as bool? ?? false,
      supportsVision: json['supports_vision'] as bool? ?? false,
      supportsToolCalling: json['supports_tool_calling'] as bool? ?? false,
      thinkingLevels: (json['thinking_levels'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
      defaultThinkingLevel: json['default_thinking_level'] as String?,
      speedTier: json['speed_tier'] as String? ?? 'balanced',
      costTier: json['cost_tier'] as String? ?? 'unknown',
      capabilityTags: (json['capability_tags'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
    );
  }
}

class ProfileEntry {
  const ProfileEntry({
    required this.profileId,
    required this.providerId,
    required this.modelId,
    required this.displayName,
    required this.qualifiedModelId,
    required this.providerDisplayName,
    required this.label,
    required this.type,
    required this.configured,
    required this.local,
    required this.requiresApiKey,
    required this.favorite,
    required this.maxContext,
    required this.supportsThinking,
    required this.supportsVision,
    required this.supportsToolCalling,
    required this.thinkingLevels,
    required this.defaultThinkingLevel,
    required this.speedTier,
    required this.costTier,
    required this.capabilityTags,
  });

  final String profileId;
  final String providerId;
  final String modelId;
  final String displayName;
  final String qualifiedModelId;
  final String providerDisplayName;
  final String label;
  final String type;
  final bool configured;
  final bool local;
  final bool requiresApiKey;
  final bool favorite;
  final int maxContext;
  final bool supportsThinking;
  final bool supportsVision;
  final bool supportsToolCalling;
  final List<String> thinkingLevels;
  final String? defaultThinkingLevel;
  final String speedTier;
  final String costTier;
  final List<String> capabilityTags;

  String get effectiveProfileId =>
      profileId.isNotEmpty ? profileId : qualifiedModelId;

  String get displayLabel {
    if (label.isNotEmpty) return label;
    if (displayName.isNotEmpty) return displayName;
    if (modelId.isNotEmpty) return modelId;
    return effectiveProfileId;
  }

  factory ProfileEntry.fromJson(Map<String, dynamic> json) {
    return ProfileEntry(
      profileId: json['profile_id'] as String? ?? json['id'] as String? ?? '',
      providerId: json['provider_id'] as String? ?? '',
      modelId: json['model_id'] as String? ?? '',
      displayName: json['display_name'] as String? ?? '',
      qualifiedModelId: json['qualified_model_id'] as String? ?? '',
      providerDisplayName: json['provider_display_name'] as String? ?? '',
      label: json['label'] as String? ?? '',
      type: json['type'] as String? ?? 'chat',
      configured: json['configured'] as bool? ?? false,
      local: json['local'] as bool? ?? false,
      requiresApiKey: json['requires_api_key'] as bool? ?? false,
      favorite: json['favorite'] as bool? ?? false,
      maxContext: (json['max_context'] as num?)?.toInt() ?? -1,
      supportsThinking: json['supports_thinking'] as bool? ?? false,
      supportsVision: json['supports_vision'] as bool? ?? false,
      supportsToolCalling: json['supports_tool_calling'] as bool? ?? false,
      thinkingLevels: (json['thinking_levels'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
      defaultThinkingLevel: json['default_thinking_level'] as String?,
      speedTier: json['speed_tier'] as String? ?? 'balanced',
      costTier: json['cost_tier'] as String? ?? 'unknown',
      capabilityTags: (json['capability_tags'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
    );
  }
}

class TemplateEntry {
  const TemplateEntry({
    required this.entryId,
    required this.name,
    required this.description,
    required this.sourceType,
    required this.tags,
    required this.updatedAt,
  });

  final String entryId;
  final String name;
  final String description;
  final String sourceType;
  final List<String> tags;
  final String updatedAt;

  factory TemplateEntry.fromJson(Map<String, dynamic> json) {
    return TemplateEntry(
      entryId: json['entry_id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      description: json['description'] as String? ?? '',
      sourceType: json['source_type'] as String? ?? '',
      tags: (json['tags'] as List? ?? []).map((e) => e.toString()).toList(),
      updatedAt: json['updated_at'] as String? ?? '',
    );
  }
}

class PcManifestRoute {
  const PcManifestRoute({
    required this.method,
    required this.path,
    required this.feature,
    required this.deviceScope,
    required this.pcEquivalent,
  });

  final String method;
  final String path;
  final String feature;
  final String deviceScope;
  final String pcEquivalent;

  factory PcManifestRoute.fromJson(Map<String, dynamic> json) {
    return PcManifestRoute(
      method: json['method'] as String? ?? '',
      path: json['path'] as String? ?? '',
      feature: json['feature'] as String? ?? '',
      deviceScope: json['device_scope'] as String? ?? '',
      pcEquivalent: json['pc_equivalent'] as String? ?? '',
    );
  }
}

class PcMobileManifest {
  const PcMobileManifest({
    required this.kind,
    required this.version,
    required this.routes,
    required this.authorityRoutes,
  });

  final String kind;
  final int version;
  final List<PcManifestRoute> routes;
  final List<PcManifestRoute> authorityRoutes;

  factory PcMobileManifest.fromJson(Map<String, dynamic> json) {
    List<PcManifestRoute> parseRoutes(Object? value) {
      return (value as List? ?? const [])
          .whereType<Map>()
          .map(
            (item) => PcManifestRoute.fromJson(Map<String, dynamic>.from(item)),
          )
          .where((route) => !route.path.startsWith('/api/authority/'))
          .toList();
    }

    return PcMobileManifest(
      kind: json['kind'] as String? ?? '',
      version: (json['version'] as num?)?.toInt() ?? 0,
      routes: parseRoutes(json['routes']),
      authorityRoutes: parseRoutes(json['authority_routes']),
    );
  }
}

class PcRuntimeSettings {
  const PcRuntimeSettings({
    required this.preferredModel,
    required this.preferredModelGroup,
    required this.thinkingLevel,
    required this.deepthinkEnabled,
    required this.favoriteProfiles,
    required this.autoRouteWithinGroup,
  });

  static const empty = PcRuntimeSettings(
    preferredModel: '',
    preferredModelGroup: 'default',
    thinkingLevel: 'medium',
    deepthinkEnabled: false,
    favoriteProfiles: [],
    autoRouteWithinGroup: true,
  );

  final String preferredModel;
  final String preferredModelGroup;
  final String thinkingLevel;
  final bool deepthinkEnabled;
  final List<String> favoriteProfiles;
  final bool autoRouteWithinGroup;

  factory PcRuntimeSettings.fromJson(Map<String, dynamic> json) {
    return PcRuntimeSettings(
      preferredModel: json['preferred_model'] as String? ?? '',
      preferredModelGroup:
          json['preferred_model_group'] as String? ?? 'default',
      thinkingLevel: json['thinking_level'] as String? ?? 'medium',
      deepthinkEnabled: json['deepthink_enabled'] as bool? ?? false,
      favoriteProfiles: (json['favorite_profiles'] as List? ?? [])
          .map((e) => e.toString())
          .toList(),
      autoRouteWithinGroup: json['auto_route_within_group'] as bool? ?? true,
    );
  }
}

class PcCommandArg {
  const PcCommandArg({
    required this.name,
    required this.type,
    required this.required,
    required this.values,
  });

  final String name;
  final String type;
  final bool required;
  final List<String> values;

  factory PcCommandArg.fromJson(Map<String, dynamic> json) {
    return PcCommandArg(
      name: json['name'] as String? ?? '',
      type: json['type'] as String? ?? 'string',
      required: json['required'] as bool? ?? false,
      values: (json['values'] as List? ?? []).map((e) => e.toString()).toList(),
    );
  }
}

class PcCommandItem {
  const PcCommandItem({
    required this.id,
    required this.name,
    required this.aliases,
    required this.label,
    required this.description,
    required this.category,
    required this.visibility,
    required this.risk,
    required this.modes,
    required this.enabled,
    required this.active,
    required this.args,
    required this.execution,
  });

  final String id;
  final String name;
  final List<String> aliases;
  final String label;
  final String description;
  final String category;
  final String visibility;
  final String risk;
  final List<String> modes;
  final bool enabled;
  final bool active;
  final List<PcCommandArg> args;
  final Map<String, dynamic> execution;

  bool get isModelCommand {
    final names = {id, name, ...aliases}.map((e) => e.toLowerCase()).toSet();
    return names.contains('model') || names.contains('models');
  }

  factory PcCommandItem.fromJson(Map<String, dynamic> json) {
    return PcCommandItem(
      id: json['id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      aliases:
          (json['aliases'] as List? ?? []).map((e) => e.toString()).toList(),
      label: json['label'] as String? ?? json['name'] as String? ?? '',
      description: json['description'] as String? ?? '',
      category: json['category'] as String? ?? 'chat',
      visibility: json['visibility'] as String? ?? 'default',
      risk: json['risk'] as String? ?? 'low',
      modes: (json['modes'] as List? ?? []).map((e) => e.toString()).toList(),
      enabled: json['enabled'] as bool? ?? true,
      active: json['active'] as bool? ?? false,
      args: (json['args'] as List? ?? [])
          .map((e) => PcCommandArg.fromJson(e as Map<String, dynamic>))
          .toList(),
      execution: Map<String, dynamic>.from(
        json['execution'] as Map? ?? const {},
      ),
    );
  }
}

class PcToolEntry {
  const PcToolEntry({
    required this.toolId,
    required this.serviceId,
    required this.name,
    required this.summary,
    required this.tags,
    required this.mobileCompatible,
    required this.mobileAvailable,
    required this.executionLocation,
    required this.mobileUnavailableReason,
  });

  final String toolId;
  final String serviceId;
  final String name;
  final String summary;
  final List<String> tags;
  final bool mobileCompatible;
  final bool mobileAvailable;
  final String executionLocation;
  final String mobileUnavailableReason;

  bool get hasMobileTag => tags.contains('mobile-compatible');

  factory PcToolEntry.fromJson(Map<String, dynamic> json) {
    final mobile =
        Map<String, dynamic>.from(json['mobile'] as Map? ?? const {});
    return PcToolEntry(
      toolId: json['tool_id'] as String? ?? '',
      serviceId: json['service_id'] as String? ?? '',
      name: json['name'] as String? ?? '',
      summary: json['summary'] as String? ?? '',
      tags: (json['tags'] as List? ?? []).map((e) => e.toString()).toList(),
      mobileCompatible: json['mobile_compatible'] as bool? ??
          mobile['compatible'] as bool? ??
          false,
      mobileAvailable: mobile['available'] as bool? ?? false,
      executionLocation: json['execution_location'] as String? ??
          mobile['execution_location'] as String? ??
          '',
      mobileUnavailableReason: json['mobile_unavailable_reason'] as String? ??
          mobile['unavailable_reason'] as String? ??
          '',
    );
  }
}

class PcModelCandidate {
  const PcModelCandidate({
    required this.profileId,
    required this.qualifiedModelId,
    required this.providerId,
    required this.modelId,
    required this.displayName,
    required this.providerDisplayName,
    required this.label,
    required this.configured,
  });

  final String profileId;
  final String qualifiedModelId;
  final String providerId;
  final String modelId;
  final String displayName;
  final String providerDisplayName;
  final String label;
  final bool configured;

  String get effectiveProfileId =>
      profileId.isNotEmpty ? profileId : qualifiedModelId;

  String get displayLabel {
    if (label.isNotEmpty) return label;
    if (displayName.isNotEmpty) return displayName;
    if (modelId.isNotEmpty) return modelId;
    return effectiveProfileId;
  }

  factory PcModelCandidate.fromJson(Map<String, dynamic> json) {
    return PcModelCandidate(
      profileId: json['profile_id'] as String? ?? '',
      qualifiedModelId: json['qualified_model_id'] as String? ?? '',
      providerId: json['provider_id'] as String? ?? '',
      modelId: json['model_id'] as String? ?? '',
      displayName: json['display_name'] as String? ?? '',
      providerDisplayName: json['provider_display_name'] as String? ?? '',
      label: json['label'] as String? ?? '',
      configured: json['configured'] as bool? ??
          json['api_key_configured'] as bool? ??
          false,
    );
  }
}

class PcCommandExecuteResult {
  const PcCommandExecuteResult({
    required this.command,
    required this.executed,
    required this.requiresApproval,
    required this.action,
    required this.args,
    required this.result,
    required this.message,
    required this.candidates,
    required this.selectedModel,
  });

  final PcCommandItem? command;
  final bool executed;
  final bool requiresApproval;
  final String action;
  final Map<String, dynamic> args;
  final Object? result;
  final String message;
  final List<PcModelCandidate> candidates;
  final PcModelCandidate? selectedModel;

  factory PcCommandExecuteResult.fromJson(Map<String, dynamic> json) {
    final selected = json['selected_model'];
    PcModelCandidate? selectedModel;
    if (selected is Map<String, dynamic>) {
      selectedModel = PcModelCandidate.fromJson(selected);
    } else if (selected is String && selected.trim().isNotEmpty) {
      selectedModel = PcModelCandidate(
        profileId: selected.trim(),
        qualifiedModelId: selected.trim(),
        providerId: '',
        modelId: selected.trim(),
        displayName: selected.trim(),
        providerDisplayName: '',
        label: selected.trim(),
        configured: true,
      );
    }
    return PcCommandExecuteResult(
      command: json['command'] is Map<String, dynamic>
          ? PcCommandItem.fromJson(json['command'] as Map<String, dynamic>)
          : null,
      executed: json['executed'] as bool? ?? false,
      requiresApproval: json['requires_approval'] as bool? ?? false,
      action: json['action'] as String? ?? '',
      args: Map<String, dynamic>.from(json['args'] as Map? ?? const {}),
      result: json['result'],
      message: json['message'] as String? ?? '',
      candidates: (json['candidates'] as List? ?? [])
          .map((e) => PcModelCandidate.fromJson(e as Map<String, dynamic>))
          .toList(),
      selectedModel: selectedModel,
    );
  }
}

class PcCatalog {
  const PcCatalog({
    required this.providers,
    required this.models,
    required this.profiles,
    required this.templates,
    required this.tools,
    required this.fetchedAt,
    this.runtime = PcRuntimeSettings.empty,
    this.commands = const [],
    this.commandManifestErrors = const [],
  });

  final List<ProviderEntry> providers;
  final List<ModelEntry> models;
  final List<ProfileEntry> profiles;
  final List<TemplateEntry> templates;
  final List<PcToolEntry> tools;
  final DateTime fetchedAt;
  final PcRuntimeSettings runtime;
  final List<PcCommandItem> commands;
  final List<Map<String, dynamic>> commandManifestErrors;

  List<ModelEntry> modelsForProvider(String providerId) =>
      models.where((m) => m.providerId == providerId).toList();

  List<ProviderEntry> get configuredProviders =>
      providers.where((p) => p.configured).toList();

  List<PcToolEntry> get mobileCompatibleTools =>
      tools.where((tool) => tool.mobileCompatible).toList();

  List<ProfileEntry> get selectableProfiles {
    final list = profiles
        .where(
          (p) =>
              p.effectiveProfileId.isNotEmpty &&
              (p.type == 'chat' ||
                  p.type == 'reasoning' ||
                  p.type == 'vision' ||
                  p.supportsToolCalling),
        )
        .toList();
    list.sort((a, b) {
      final favorite = (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0);
      if (favorite != 0) return favorite;
      final configured = (b.configured ? 1 : 0) - (a.configured ? 1 : 0);
      if (configured != 0) return configured;
      final local = (b.local ? 1 : 0) - (a.local ? 1 : 0);
      if (local != 0) return local;
      return a.displayLabel.compareTo(b.displayLabel);
    });
    return list;
  }

  ProfileEntry? profileById(String id) {
    final needle = id.trim();
    if (needle.isEmpty) return null;
    for (final profile in profiles) {
      if (profile.profileId == needle ||
          profile.qualifiedModelId == needle ||
          '${profile.providerId}/${profile.modelId}' == needle) {
        return profile;
      }
    }
    return null;
  }

  String labelForProfile(String id) {
    final profile = profileById(id);
    return profile?.displayLabel ?? id;
  }

  factory PcCatalog.fromJson(Map<String, dynamic> json) {
    return PcCatalog(
      providers: (json['providers'] as List? ?? [])
          .map((e) => ProviderEntry.fromJson(e as Map<String, dynamic>))
          .toList(),
      models: (json['models'] as List? ?? [])
          .map((e) => ModelEntry.fromJson(e as Map<String, dynamic>))
          .toList(),
      profiles: (json['profiles'] as List? ?? [])
          .map((e) => ProfileEntry.fromJson(e as Map<String, dynamic>))
          .toList(),
      templates: (json['templates'] as List? ?? [])
          .map((e) => TemplateEntry.fromJson(e as Map<String, dynamic>))
          .toList(),
      tools: (json['tools'] as List? ?? [])
          .map((e) => PcToolEntry.fromJson(e as Map<String, dynamic>))
          .toList(),
      fetchedAt: DateTime.now(),
      runtime: PcRuntimeSettings.fromJson(
        json['runtime'] as Map<String, dynamic>? ?? const {},
      ),
      commands: (json['commands'] as List? ?? [])
          .map((e) => PcCommandItem.fromJson(e as Map<String, dynamic>))
          .toList(),
      commandManifestErrors: (json['command_manifest_errors'] as List? ?? [])
          .whereType<Map>()
          .map((e) => Map<String, dynamic>.from(e))
          .toList(),
    );
  }
}

class PcBootstrap {
  const PcBootstrap({
    required this.deviceId,
    required this.label,
    required this.version,
    required this.capabilities,
    required this.cursor,
  });

  final String deviceId;
  final String label;
  final String version;
  final PcBootstrapCapabilities capabilities;
  final String cursor;

  factory PcBootstrap.fromJson(Map<String, dynamic> json) {
    final server = json['server'] as Map<String, dynamic>? ?? const {};
    final caps = json['capabilities'] as Map<String, dynamic>? ?? const {};
    return PcBootstrap(
      deviceId: server['device_id'] as String? ?? '',
      label: server['label'] as String? ?? '',
      version: server['version'] as String? ?? '',
      capabilities: PcBootstrapCapabilities.fromJson(caps),
      cursor: json['cursor'] as String? ?? '',
    );
  }
}

class PcBootstrapCapabilities {
  const PcBootstrapCapabilities({
    required this.chat,
    required this.tools,
    required this.approvals,
    required this.credentialTransfer,
  });

  final bool chat;
  final bool tools;
  final bool approvals;
  final bool credentialTransfer;

  factory PcBootstrapCapabilities.fromJson(Map<String, dynamic> json) {
    return PcBootstrapCapabilities(
      chat: json['chat'] as bool? ?? false,
      tools: json['tools'] as bool? ?? false,
      approvals: json['approvals'] as bool? ?? false,
      credentialTransfer: json['credential_transfer'] as bool? ?? false,
    );
  }
}
