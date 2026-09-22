import 'package:flutter/material.dart';

import '../data/pc/pc_catalog.dart';
import '../settings/api_config_store.dart';

enum ModelSelectionKind { pcProfile, mobileProvider, localModel }

class ModelSelectionResult {
  const ModelSelectionResult._({
    required this.kind,
    this.pcProfileId,
    this.mobileProvider,
    this.localModel,
  });

  const ModelSelectionResult.pcProfile(String profileId)
      : this._(kind: ModelSelectionKind.pcProfile, pcProfileId: profileId);

  const ModelSelectionResult.mobileProvider(MobileProviderConfig provider)
      : this._(
          kind: ModelSelectionKind.mobileProvider,
          mobileProvider: provider,
        );

  const ModelSelectionResult.localModel(String model)
      : this._(kind: ModelSelectionKind.localModel, localModel: model);

  final ModelSelectionKind kind;
  final String? pcProfileId;
  final MobileProviderConfig? mobileProvider;
  final String? localModel;
}

class ModelSelectionScreen extends StatefulWidget {
  const ModelSelectionScreen.pc({
    super.key,
    required this.profiles,
    required this.activeModelId,
  })  : providers = const <MobileProviderConfig>[],
        activeProviderId = '',
        _mode = _ModelSelectionMode.pc,
        allowCustomModel = false;

  const ModelSelectionScreen.local({
    super.key,
    required this.providers,
    required this.activeModelId,
    required this.activeProviderId,
  })  : profiles = const <ProfileEntry>[],
        _mode = _ModelSelectionMode.local,
        allowCustomModel = true;

  final _ModelSelectionMode _mode;
  final List<ProfileEntry> profiles;
  final List<MobileProviderConfig> providers;
  final String activeModelId;
  final String activeProviderId;
  final bool allowCustomModel;

  @override
  State<ModelSelectionScreen> createState() => _ModelSelectionScreenState();
}

enum _ModelSelectionMode { pc, local }

class _ModelSelectionScreenState extends State<ModelSelectionScreen> {
  final _searchController = TextEditingController();
  String _query = '';

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
                  hintText: 'モデルを検索',
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
            Expanded(
              child: options.isEmpty
                  ? _EmptyModelList(
                      query: _query,
                      local: widget._mode == _ModelSelectionMode.local,
                    )
                  : ListView.separated(
                      keyboardDismissBehavior:
                          ScrollViewKeyboardDismissBehavior.onDrag,
                      padding: const EdgeInsets.fromLTRB(16, 0, 16, 20),
                      itemBuilder: (context, index) {
                        final option = options[index];
                        return ListTile(
                          contentPadding: EdgeInsets.zero,
                          enabled: option.enabled,
                          leading: Icon(option.icon),
                          title: Text(
                            option.title,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                          subtitle: Text(
                            option.subtitle,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                          ),
                          trailing: option.selected
                              ? Icon(Icons.check, color: scheme.primary)
                              : null,
                          onTap: option.enabled
                              ? () => Navigator.of(context).pop(option.result)
                              : null,
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
            if (widget._mode == _ModelSelectionMode.pc &&
                widget.profiles.isEmpty)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
                child: Text(
                  'PCからモデル一覧を取得できませんでした。',
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: scheme.onSurfaceVariant,
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
        ? widget.profiles.map(_pcOption).toList()
        : widget.providers.map(_localOption).toList();
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
    return _ModelOption(
      title: profile.displayLabel,
      subtitle: [
            provider,
            profile.modelId,
          ].where((part) => part.isNotEmpty).join(' · ') +
          context,
      searchText:
          '${profile.displayLabel} ${profile.modelId} ${profile.providerId} $provider',
      icon: profile.supportsVision
          ? Icons.visibility_outlined
          : Icons.smart_toy_outlined,
      selected: profile.effectiveProfileId == widget.activeModelId,
      enabled: enabled,
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
      title: label,
      subtitle: subtitle,
      searchText:
          '$label ${provider.displayName} ${provider.providerId} ${provider.model}',
      icon: provider.providerId == 'anthropic'
          ? Icons.auto_awesome_outlined
          : Icons.cloud_outlined,
      selected: provider.providerId == widget.activeProviderId &&
          provider.model == widget.activeModelId,
      enabled: true,
      result: ModelSelectionResult.mobileProvider(provider),
    );
  }

  Future<void> _openCustomModelDialog() async {
    final controller = TextEditingController(text: widget.activeModelId);
    final selected = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('モデル名を直接入力'),
        content: TextField(
          controller: controller,
          autofocus: true,
          textInputAction: TextInputAction.done,
          decoration: const InputDecoration(
            labelText: 'model',
            hintText: 'gpt-4o-mini',
          ),
          onSubmitted: (value) => Navigator.pop(context, value.trim()),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('キャンセル'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text.trim()),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (!mounted || selected == null || selected.trim().isEmpty) return;
    Navigator.of(context).pop(ModelSelectionResult.localModel(selected.trim()));
  }

  String _normalize(String value) => value.trim().toLowerCase();
}

class _ModelOption {
  const _ModelOption({
    required this.title,
    required this.subtitle,
    required this.searchText,
    required this.icon,
    required this.selected,
    required this.enabled,
    required this.result,
  });

  final String title;
  final String subtitle;
  final String searchText;
  final IconData icon;
  final bool selected;
  final bool enabled;
  final ModelSelectionResult result;
}

class _EmptyModelList extends StatelessWidget {
  const _EmptyModelList({required this.query, required this.local});

  final String query;
  final bool local;

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
        child: Text(
          message,
          textAlign: TextAlign.center,
          style: TextStyle(color: scheme.onSurfaceVariant),
        ),
      ),
    );
  }
}
