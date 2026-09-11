import 'package:flutter/material.dart';

import '../data/pc/pc_catalog.dart';
import '../settings/api_config_store.dart';
import 'model_selection_dialogs.dart';

enum ModelSelectionKind { pcProfile, mobileProvider, localModel, openSettings }

class ModelSelectionResult {
  const ModelSelectionResult._({
    required this.kind,
    this.pcProfileId,
    this.mobileProvider,
    this.localModel,
    this.localProviderId,
  });

  const ModelSelectionResult.pcProfile(String profileId)
      : this._(kind: ModelSelectionKind.pcProfile, pcProfileId: profileId);

  const ModelSelectionResult.mobileProvider(MobileProviderConfig provider)
      : this._(
          kind: ModelSelectionKind.mobileProvider,
          mobileProvider: provider,
        );

  const ModelSelectionResult.localModel({
    required String model,
    required String providerId,
  }) : this._(
          kind: ModelSelectionKind.localModel,
          localModel: model,
          localProviderId: providerId,
        );

  const ModelSelectionResult.openSettings()
      : this._(kind: ModelSelectionKind.openSettings);

  final ModelSelectionKind kind;
  final String? pcProfileId;
  final MobileProviderConfig? mobileProvider;
  final String? localModel;
  final String? localProviderId;
}

typedef PcProfileRefresh = Future<List<ProfileEntry>> Function();
typedef MobileProviderRefresh = Future<List<MobileProviderConfig>> Function();

class ModelSelectionScreen extends StatefulWidget {
  const ModelSelectionScreen.pc({
    super.key,
    required this.profiles,
    required this.activeModelId,
    this.onRefreshPcProfiles,
  })  : providers = const <MobileProviderConfig>[],
        activeProviderId = '',
        onRefreshMobileProviders = null,
        _mode = _ModelSelectionMode.pc,
        allowCustomModel = false;

  const ModelSelectionScreen.local({
    super.key,
    required this.providers,
    required this.activeModelId,
    required this.activeProviderId,
    this.onRefreshMobileProviders,
  })  : profiles = const <ProfileEntry>[],
        onRefreshPcProfiles = null,
        _mode = _ModelSelectionMode.local,
        allowCustomModel = true;

  final _ModelSelectionMode _mode;
  final List<ProfileEntry> profiles;
  final List<MobileProviderConfig> providers;
  final String activeModelId;
  final String activeProviderId;
  final bool allowCustomModel;
  final PcProfileRefresh? onRefreshPcProfiles;
  final MobileProviderRefresh? onRefreshMobileProviders;

  @override
  State<ModelSelectionScreen> createState() => _ModelSelectionScreenState();
}

enum _ModelSelectionMode { pc, local }

class _ModelSelectionScreenState extends State<ModelSelectionScreen> {
  final _searchController = TextEditingController();
  late List<ProfileEntry> _profiles;
  late List<MobileProviderConfig> _providers;
  String _query = '';
  String? _refreshError;
  bool _refreshing = false;

  @override
  void initState() {
    super.initState();
    _profiles = List<ProfileEntry>.of(widget.profiles);
    _providers = List<MobileProviderConfig>.of(widget.providers);
  }

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final options = _filteredOptions();
    return Scaffold(
      appBar: AppBar(title: const Text('モデルを選択')),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 10),
              child: TextField(
                controller: _searchController,
                autofocus: true,
                textInputAction: TextInputAction.search,
                decoration: InputDecoration(
                  labelText: 'モデルを検索',
                  hintText: 'プロバイダー名またはモデルID',
                  prefixIcon: const Icon(Icons.search),
                  suffixIcon: _query.isEmpty
                      ? null
                      : IconButton(
                          tooltip: '検索を消す',
                          icon: const Icon(Icons.close),
                          onPressed: () {
                            _searchController.clear();
                            setState(() => _query = '');
                          },
                        ),
                  border: const OutlineInputBorder(),
                ),
                onChanged: (value) => setState(() => _query = value),
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Semantics(
                      liveRegion: true,
                      child: Text(
                        '${options.length}件のモデル',
                        key: const ValueKey('model-result-count'),
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: scheme.onSurfaceVariant,
                        ),
                      ),
                    ),
                  ),
                  if (_refreshing)
                    const SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                ],
              ),
            ),
            if (_refreshError != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                child: Semantics(
                  liveRegion: true,
                  child: Text(
                    _refreshError!,
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: scheme.error,
                    ),
                  ),
                ),
              ),
            Expanded(
              child: options.isEmpty
                  ? _EmptyModelList(
                      query: _query,
                      local: widget._mode == _ModelSelectionMode.local,
                      refreshing: _refreshing,
                      canRefresh: _canRefresh,
                      onClearSearch:
                          _query.trim().isEmpty ? null : _clearSearch,
                      onRefresh: _canRefresh ? _refreshOptions : null,
                      onOpenSettings: _openSettings,
                    )
                  : ListView.separated(
                      keyboardDismissBehavior:
                          ScrollViewKeyboardDismissBehavior.onDrag,
                      padding: const EdgeInsets.fromLTRB(16, 0, 16, 20),
                      itemBuilder: (context, index) {
                        final option = options[index];
                        return Semantics(
                          key: ValueKey('model-option-${option.id}'),
                          container: true,
                          button: true,
                          selected: option.selected,
                          hint: option.enabled
                              ? option.selectionHint
                              : '利用できない理由と設定方法を表示',
                          child: ListTile(
                            contentPadding: EdgeInsets.zero,
                            selected: option.selected,
                            isThreeLine: true,
                            leading: Icon(option.icon),
                            title: Text(
                              option.title,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                            ),
                            subtitle: Text(
                              option.subtitle,
                              maxLines: 3,
                              overflow: TextOverflow.ellipsis,
                            ),
                            trailing: option.selected
                                ? Icon(
                                    Icons.check_circle,
                                    color: scheme.primary,
                                    semanticLabel: '現在選択中',
                                  )
                                : option.enabled
                                    ? null
                                    : const Icon(
                                        Icons.info_outline,
                                        semanticLabel: '利用できません',
                                      ),
                            onTap: option.enabled
                                ? () => Navigator.of(context).pop(option.result)
                                : () => _explainUnavailable(option),
                          ),
                        );
                      },
                      separatorBuilder: (_, __) =>
                          Divider(height: 1, color: scheme.outlineVariant),
                      itemCount: options.length,
                    ),
            ),
            if (widget.allowCustomModel)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
                child: SizedBox(
                  width: double.infinity,
                  child: OutlinedButton.icon(
                    icon: const Icon(Icons.edit_outlined),
                    label: const Text('モデル名を直接入力'),
                    onPressed: _openCustomModelDialog,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  List<_ModelOption> _filteredOptions() {
    final query = _normalize(_query);
    final options = widget._mode == _ModelSelectionMode.pc
        ? _profiles.map(_pcOption).toList()
        : _providers.map(_localOption).toList();
    options.sort((a, b) {
      final title = _normalize(a.title).compareTo(_normalize(b.title));
      if (title != 0) return title;
      return _normalize(a.subtitle).compareTo(_normalize(b.subtitle));
    });
    if (query.isEmpty) return options;
    return options
        .where((option) => _normalize(option.searchText).contains(query))
        .toList();
  }

  _ModelOption _pcOption(ProfileEntry profile) {
    final provider = profile.providerDisplayName.isNotEmpty
        ? profile.providerDisplayName
        : profile.providerId;
    final context = profile.maxContext > 0
        ? ' · ${(profile.maxContext / 1000).round()}k'
        : '';
    final enabled = profile.configured || profile.local;
    final state = profile.local
        ? 'ローカルで利用可能'
        : profile.configured
            ? '設定済みのリモートモデル'
            : '未設定';
    final capabilities = <String>[
      if (profile.supportsVision) '画像',
      if (profile.supportsToolCalling) 'ツール',
      if (profile.supportsThinking) '思考',
      if (profile.type.trim().isNotEmpty && profile.type != 'chat')
        profile.type,
    ];
    final capabilityText =
        capabilities.isEmpty ? '追加機能は未確認' : capabilities.join('・');
    final unavailableReason = enabled
        ? null
        : profile.requiresApiKey
            ? '$provider のAPI設定が必要です'
            : '$provider の設定またはPCへの再接続が必要です';
    return _ModelOption(
      id: profile.effectiveProfileId,
      title: profile.displayLabel,
      subtitle: [
        [provider, profile.modelId]
            .where((part) => part.isNotEmpty)
            .join(' · '),
        '$state · $capabilityText$context',
        if (unavailableReason != null) unavailableReason,
      ].where((part) => part.isNotEmpty).join(' · '),
      searchText:
          '${profile.displayLabel} ${profile.modelId} ${profile.providerId} $provider',
      icon: profile.supportsVision
          ? Icons.visibility_outlined
          : Icons.smart_toy_outlined,
      selected: profile.effectiveProfileId == widget.activeModelId,
      enabled: enabled,
      unavailableReason: unavailableReason,
      selectionHint: 'PCへモデル変更をリクエスト',
      result: ModelSelectionResult.pcProfile(profile.effectiveProfileId),
    );
  }

  _ModelOption _localOption(MobileProviderConfig provider) {
    final label = provider.effectiveLabel;
    final subtitle = [
      provider.displayName,
      provider.model,
    ].where((part) => part.trim().isNotEmpty).join(' · ');
    return _ModelOption(
      id: '${provider.providerId}:${provider.model}',
      title: label,
      subtitle: '$subtitle · '
          '${provider.local ? "ローカル" : "リモート"} · 設定済み',
      searchText:
          '$label ${provider.displayName} ${provider.providerId} ${provider.model}',
      icon: provider.providerId == 'anthropic'
          ? Icons.auto_awesome_outlined
          : Icons.cloud_outlined,
      selected: provider.providerId == widget.activeProviderId &&
          provider.model == widget.activeModelId,
      enabled: true,
      unavailableReason: null,
      selectionHint: 'このスマホのモデルとして選択',
      result: ModelSelectionResult.mobileProvider(provider),
    );
  }

  bool get _canRefresh => widget._mode == _ModelSelectionMode.pc
      ? widget.onRefreshPcProfiles != null
      : widget.onRefreshMobileProviders != null;

  void _clearSearch() {
    _searchController.clear();
    setState(() => _query = '');
  }

  Future<void> _refreshOptions() async {
    if (_refreshing || !_canRefresh) return;
    setState(() {
      _refreshing = true;
      _refreshError = null;
    });
    try {
      if (widget._mode == _ModelSelectionMode.pc) {
        _profiles = List<ProfileEntry>.of(
          await widget.onRefreshPcProfiles!.call(),
        );
      } else {
        _providers = List<MobileProviderConfig>.of(
          await widget.onRefreshMobileProviders!.call(),
        );
      }
    } catch (_) {
      _refreshError = widget._mode == _ModelSelectionMode.pc
          ? 'PCのモデル一覧を再取得できませんでした。接続を確認してください。'
          : '設定済みモデルを再取得できませんでした。設定を確認してください。';
    } finally {
      if (mounted) setState(() => _refreshing = false);
    }
  }

  Future<void> _explainUnavailable(_ModelOption option) async {
    final action = await showUnavailableModelDialog(
      context,
      modelLabel: option.title,
      reason: option.unavailableReason ?? 'このモデルを利用するための設定を確認できませんでした。',
      canRefresh: _canRefresh,
    );
    if (!mounted) return;
    if (action == UnavailableModelAction.refresh) {
      await _refreshOptions();
    } else if (action == UnavailableModelAction.settings) {
      _openSettings();
    }
  }

  void _openSettings() {
    Navigator.of(context).pop(const ModelSelectionResult.openSettings());
  }

  Future<void> _openCustomModelDialog() async {
    final provider = _activeProvider();
    if (provider == null) {
      final openSettings = await showMissingModelProviderDialog(context);
      if (mounted && openSettings == true) _openSettings();
      return;
    }
    final selected = await showProviderBoundCustomModelDialog(
      context,
      providerLabel: provider.effectiveLabel,
      initialModelId: widget.activeModelId,
    );
    if (!mounted || selected == null || selected.trim().isEmpty) return;
    Navigator.of(context).pop(
      ModelSelectionResult.localModel(
        model: selected.trim(),
        providerId: provider.providerId,
      ),
    );
  }

  MobileProviderConfig? _activeProvider() {
    for (final provider in _providers) {
      if (provider.providerId == widget.activeProviderId) return provider;
    }
    return null;
  }

  String _normalize(String value) => value.trim().toLowerCase();
}

class _ModelOption {
  const _ModelOption({
    required this.id,
    required this.title,
    required this.subtitle,
    required this.searchText,
    required this.icon,
    required this.selected,
    required this.enabled,
    required this.unavailableReason,
    required this.selectionHint,
    required this.result,
  });

  final String id;
  final String title;
  final String subtitle;
  final String searchText;
  final IconData icon;
  final bool selected;
  final bool enabled;
  final String? unavailableReason;
  final String selectionHint;
  final ModelSelectionResult result;
}

class _EmptyModelList extends StatelessWidget {
  const _EmptyModelList({
    required this.query,
    required this.local,
    required this.refreshing,
    required this.canRefresh,
    required this.onClearSearch,
    required this.onRefresh,
    required this.onOpenSettings,
  });

  final String query;
  final bool local;
  final bool refreshing;
  final bool canRefresh;
  final VoidCallback? onClearSearch;
  final VoidCallback? onRefresh;
  final VoidCallback onOpenSettings;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final message = query.trim().isNotEmpty
        ? '一致するモデルがありません'
        : local
            ? '設定済みAPIがありません'
            : '選べるPCモデルがありません';
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Semantics(
              liveRegion: true,
              child: Text(
                message,
                textAlign: TextAlign.center,
                style: TextStyle(color: scheme.onSurfaceVariant),
              ),
            ),
            const SizedBox(height: 12),
            Wrap(
              alignment: WrapAlignment.center,
              spacing: 8,
              runSpacing: 8,
              children: [
                if (onClearSearch != null)
                  OutlinedButton(
                    onPressed: onClearSearch,
                    child: const Text('検索を消す'),
                  ),
                if (canRefresh)
                  OutlinedButton.icon(
                    onPressed: refreshing ? null : onRefresh,
                    icon: const Icon(Icons.refresh),
                    label: const Text('再取得'),
                  ),
                if (query.trim().isEmpty)
                  FilledButton(
                    onPressed: onOpenSettings,
                    child: Text(local ? 'API設定を開く' : '接続設定を開く'),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
