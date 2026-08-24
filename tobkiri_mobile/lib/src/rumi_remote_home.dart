import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import 'authority_approval_screen.dart';
import 'models.dart';
import 'rumi_api_client.dart';
import 'secure_settings_store.dart';

part 'settings_recovery_widgets.dart';

class RumiRemoteHome extends StatefulWidget {
  const RumiRemoteHome({
    super.key,
    this.settingsRepository,
    this.refreshOnLoad = true,
  });

  final SettingsRepository? settingsRepository;
  final bool refreshOnLoad;

  @override
  State<RumiRemoteHome> createState() => _RumiRemoteHomeState();
}

class _RumiRemoteHomeState extends State<RumiRemoteHome> {
  late final SettingsRepository _settingsStore;
  final _serverController = TextEditingController();
  final _tokenController = TextEditingController();

  Timer? _refreshTimer;
  RumiRemoteSettings _settings = RumiRemoteSettings.defaults;
  SettingsLoadResult? _settingsLoad;
  RumiHealth? _health;
  ModuleCatalog? _catalog;
  RumiModule? _selectedModule;
  MigrationStatus? _migration;
  List<PackRequest> _packRequests = const [];
  String? _error;
  bool _loading = true;
  bool _busy = false;
  bool _settingsRecoveryAcknowledged = false;
  int _settingsLoadGeneration = 0;

  RumiApiClient get _client =>
      RumiApiClient(baseUrl: _settings.baseUrl, bearerToken: _settings.token);

  @override
  void initState() {
    super.initState();
    _settingsStore = widget.settingsRepository ?? SecureSettingsStore();
    _loadSettings();
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _serverController.dispose();
    _tokenController.dispose();
    super.dispose();
  }

  Future<void> _loadSettings() async {
    final generation = ++_settingsLoadGeneration;
    if (mounted) {
      setState(() {
        _loading = true;
        _settingsRecoveryAcknowledged = false;
      });
    }
    SettingsLoadResult result;
    try {
      result = await _settingsStore.loadAll();
    } catch (_) {
      result = const SettingsLoadResult(
        apiSettings: null,
        pairedDevice: null,
        notifications: null,
        deviceIdentity: null,
        failures: [
          SettingsLoadFailure(
            source: SettingsDataSource.apiConfiguration,
            code: 'read-unavailable',
          ),
        ],
      );
    }
    if (!mounted || generation != _settingsLoadGeneration) {
      return;
    }
    final settings = result.apiSettings;
    setState(() {
      _settingsLoad = result;
      if (settings != null) {
        _settings = settings;
        _syncControllers(settings);
      }
      _loading = false;
    });
    if (settings == null) {
      _refreshTimer?.cancel();
      return;
    }
    _configureTimer(settings);
    if (widget.refreshOnLoad) await _refresh();
  }

  void _syncControllers(RumiRemoteSettings settings) {
    _serverController.text = settings.baseUrl;
    _tokenController.text = settings.token;
  }

  void _configureTimer(RumiRemoteSettings settings) {
    _refreshTimer?.cancel();
    if (!settings.autoRefresh) {
      return;
    }
    _refreshTimer = Timer.periodic(
      const Duration(seconds: 30),
      (_) => _refresh(silent: true),
    );
  }

  Future<void> _saveSettings(
    bool autoRefresh,
    bool notificationsEnabled,
  ) async {
    final load = _settingsLoad;
    if (load == null || load.apiSettings == null) return;
    if (load.hasFailures && !_settingsRecoveryAcknowledged) {
      _showSnack('Retry loading or choose safe recovery before saving.');
      return;
    }
    final settings = RumiRemoteSettings(
      baseUrl: _serverController.text.trim(),
      token: _tokenController.text.trim(),
      autoRefresh: autoRefresh,
    );
    try {
      await _settingsStore.saveApi(settings);
      if (!load.failed(SettingsDataSource.notifications) &&
          load.notifications != null) {
        await _settingsStore.saveNotifications(
          MobileNotificationSettings(enabled: notificationsEnabled),
        );
      }
    } catch (_) {
      _showSnack('Settings could not be saved. Retry after storage recovers.');
      return;
    }
    if (!mounted) {
      return;
    }
    setState(() {
      _settings = settings;
      _health = null;
      _catalog = null;
      _migration = null;
      _packRequests = const [];
      _selectedModule = null;
      _error = null;
      _settingsLoad = SettingsLoadResult(
        apiSettings: settings,
        pairedDevice: load.pairedDevice,
        notifications: load.failed(SettingsDataSource.notifications)
            ? null
            : MobileNotificationSettings(enabled: notificationsEnabled),
        deviceIdentity: load.deviceIdentity,
        failures: load.failures,
      );
    });
    _configureTimer(settings);
    Navigator.of(context).maybePop();
    if (widget.refreshOnLoad) await _refresh();
  }

  Future<void> _refresh({bool silent = false}) async {
    if (_busy && silent) {
      return;
    }
    if (!silent) {
      setState(() {
        _busy = true;
        _error = null;
      });
    }
    final client = _client;
    try {
      final health = await client.health();
      ModuleCatalog? catalog;
      MigrationStatus? migration;
      List<PackRequest> packRequests = const [];
      RumiModule? selected;
      if (_settings.token.trim().isNotEmpty) {
        catalog = await client.listModules();
        migration = await client.migrationStatus();
        packRequests = await client.listPackRequests();
        selected = _syncSelected(catalog.modules);
      }
      if (!mounted) {
        return;
      }
      setState(() {
        _health = health;
        _catalog = catalog;
        _migration = migration;
        _packRequests = packRequests;
        _selectedModule = selected;
        _error = null;
      });
    } catch (error) {
      if (!silent) {
        _setError(error);
      }
    } finally {
      client.close();
      if (mounted && !silent) {
        setState(() => _busy = false);
      }
    }
  }

  RumiModule? _syncSelected(List<RumiModule> modules) {
    if (modules.isEmpty) {
      return null;
    }
    final selectedId = _selectedModule?.id;
    if (selectedId == null) {
      return modules.first;
    }
    return _firstWhereOrNull(modules, (module) => module.id == selectedId) ??
        modules.first;
  }

  Future<void> _selectModule(RumiModule module) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final client = _client;
    try {
      final detail = await client.getModule(module.id);
      if (!mounted) {
        return;
      }
      setState(() => _selectedModule = detail);
    } catch (error) {
      _setError(error);
    } finally {
      client.close();
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  Future<void> _moduleAction(RumiModule module, ModuleAction action) async {
    if (action.destructive) {
      final confirmed = await _confirmModuleAction(module, action);
      if (!confirmed || !mounted) {
        return;
      }
    }

    setState(() {
      _busy = true;
      _error = null;
    });
    final client = _client;
    try {
      final updated = await client.moduleAction(module.id, action);
      final catalog = await client.listModules();
      if (!mounted) {
        return;
      }
      setState(() {
        _catalog = catalog;
        _selectedModule = updated;
      });
      _showSnack('${module.displayName}: ${action.pathSegment}');
    } catch (error) {
      _setError(error);
    } finally {
      client.close();
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  Future<bool> _confirmModuleAction(
    RumiModule module,
    ModuleAction action,
  ) async {
    final result = await showDialog<bool>(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: Text('${action.label} module?'),
          content: Text(
            'Apply ${action.label.toLowerCase()} to '
            '${module.displayName}? This may interrupt remote workflows.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(context).pop(true),
              child: Text(action.label),
            ),
          ],
        );
      },
    );
    return result == true;
  }

  void _setError(Object error) {
    if (!mounted) {
      return;
    }
    setState(() => _error = _friendlyError(error));
  }

  void _showSnack(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message, maxLines: 2, overflow: TextOverflow.ellipsis),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    final settingsLoad = _settingsLoad;
    if (settingsLoad == null || settingsLoad.apiSettings == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Tobkiri Remote')),
        body: SafeArea(
          child: _SettingsLoadErrorView(
            failures: settingsLoad?.failures ??
                const [
                  SettingsLoadFailure(
                    source: SettingsDataSource.apiConfiguration,
                    code: 'read-unavailable',
                  ),
                ],
            pairedDevice: settingsLoad?.pairedDevice,
            deviceIdentity: settingsLoad?.deviceIdentity,
            onRetry: _loadSettings,
            onReset: _confirmResetSettings,
            onOpenAuthority: () => Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => const AuthorityApprovalScreen(),
              ),
            ),
          ),
        ),
      );
    }

    final modules = _catalog?.modules ?? const <RumiModule>[];
    return Scaffold(
      appBar: AppBar(
        title: const Text('Tobkiri Remote'),
        actions: [
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh),
            onPressed: _busy ? null : () => _refresh(),
          ),
          IconButton(
            tooltip: 'Authority approvals',
            icon: const Icon(Icons.shield_outlined),
            onPressed: _busy
                ? null
                : () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => const AuthorityApprovalScreen(),
                      ),
                    ),
          ),
          IconButton(
            tooltip: 'Settings',
            icon: const Icon(Icons.settings_outlined),
            onPressed: _showSettingsSheet,
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          children: [
            _ConnectionStrip(
              baseUrl: _settings.baseUrl,
              health: _health,
              error: _error,
              busy: _busy,
              tokenConfigured: _settings.token.trim().isNotEmpty,
              onCheck: () => _refresh(),
            ),
            _StatusBand(
              catalog: _catalog,
              migration: _migration,
              packRequests: _packRequests,
            ),
            Expanded(
              child: LayoutBuilder(
                builder: (context, constraints) {
                  final wide = constraints.maxWidth >= 800;
                  if (wide) {
                    return Row(
                      children: [
                        SizedBox(
                          width: 340,
                          child: _ModuleList(
                            modules: modules,
                            selectedId: _selectedModule?.id,
                            busy: _busy,
                            onSelect: _selectModule,
                          ),
                        ),
                        const VerticalDivider(width: 1),
                        Expanded(
                          child: _ModuleDetail(
                            module: _selectedModule,
                            busy: _busy,
                            onAction: _moduleAction,
                          ),
                        ),
                      ],
                    );
                  }
                  return _CompactLayout(
                    modules: modules,
                    selectedId: _selectedModule?.id,
                    busy: _busy,
                    onSelect: _selectModule,
                    detail: _ModuleDetail(
                      module: _selectedModule,
                      busy: _busy,
                      onAction: _moduleAction,
                    ),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _showSettingsSheet() async {
    final load = _settingsLoad;
    if (load == null || load.apiSettings == null) return;
    _syncControllers(_settings);
    var autoRefresh = _settings.autoRefresh;
    var notificationsEnabled = load.notifications?.enabled ??
        MobileNotificationSettings.defaults.enabled;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) {
        final bottom = MediaQuery.viewInsetsOf(context).bottom;
        return StatefulBuilder(
          builder: (context, setSheetState) {
            return Padding(
              padding: EdgeInsets.fromLTRB(16, 0, 16, bottom + 16),
              child: SafeArea(
                top: false,
                child: SingleChildScrollView(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Semantics(
                        header: true,
                        child: Text(
                          'Settings',
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                      ),
                      if (load.hasFailures) ...[
                        const SizedBox(height: 12),
                        _SettingsFailureCard(
                          failures: load.failures,
                          onRetry: () {
                            Navigator.of(context).pop();
                            _loadSettings();
                          },
                          onRecover: _settingsRecoveryAcknowledged
                              ? null
                              : () async {
                                  final recovered =
                                      await _confirmUseLoadedSettings();
                                  if (recovered && mounted) {
                                    setState(() {
                                      _settingsRecoveryAcknowledged = true;
                                    });
                                    setSheetState(() {});
                                  }
                                },
                        ),
                      ],
                      const SizedBox(height: 12),
                      TextField(
                        key: const Key('server-settings-field'),
                        controller: _serverController,
                        keyboardType: TextInputType.url,
                        textInputAction: TextInputAction.next,
                        decoration: const InputDecoration(
                          labelText: 'Kernel API URL',
                          prefixIcon: Icon(Icons.dns_outlined),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        key: const Key('token-settings-field'),
                        controller: _tokenController,
                        obscureText: true,
                        textInputAction: TextInputAction.done,
                        decoration: const InputDecoration(
                          labelText: 'Bearer token',
                          prefixIcon: Icon(Icons.key_outlined),
                        ),
                      ),
                      const SizedBox(height: 8),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Auto refresh'),
                        value: autoRefresh,
                        onChanged: (value) =>
                            setSheetState(() => autoRefresh = value),
                      ),
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        title: const Text('Notifications'),
                        subtitle: load.failed(SettingsDataSource.notifications)
                            ? const Text('Unavailable until loading succeeds')
                            : null,
                        value: notificationsEnabled,
                        onChanged: load.failed(SettingsDataSource.notifications)
                            ? null
                            : (value) => setSheetState(
                                  () => notificationsEnabled = value,
                                ),
                      ),
                      _SettingsReadOnlySummary(
                        pairedDevice: load.pairedDevice,
                        deviceIdentity: load.deviceIdentity,
                        pairedDeviceFailed: load.failed(
                          SettingsDataSource.pairedDevice,
                        ),
                        deviceIdentityFailed: load.failed(
                          SettingsDataSource.deviceIdentity,
                        ),
                      ),
                      const SizedBox(height: 12),
                      Row(
                        children: [
                          Expanded(
                            child: OutlinedButton.icon(
                              icon: const Icon(Icons.close),
                              label: const Text('Close'),
                              onPressed: () => Navigator.of(context).maybePop(),
                            ),
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: FilledButton.icon(
                              key: const Key('save-settings-button'),
                              icon: const Icon(Icons.save_outlined),
                              label: const Text('Save'),
                              onPressed: load.hasFailures &&
                                      !_settingsRecoveryAcknowledged
                                  ? null
                                  : () => _saveSettings(
                                        autoRefresh,
                                        notificationsEnabled,
                                      ),
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      TextButton.icon(
                        key: const Key('reset-settings-button'),
                        icon: const Icon(Icons.restart_alt),
                        label: const Text('Reset editable settings'),
                        onPressed: () async {
                          final reset = await _confirmResetSettings();
                          if (reset && context.mounted) {
                            Navigator.of(context).pop();
                          }
                        },
                      ),
                    ],
                  ),
                ),
              ),
            );
          },
        );
      },
    );
  }

  Future<bool> _confirmUseLoadedSettings() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Use recovered sections?'),
        content: const Text(
          'Only sections that loaded successfully can be saved. Unreadable '
          'pairing, notification, or identity data will not be overwritten.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            key: const Key('confirm-safe-recovery-button'),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Use loaded sections'),
          ),
        ],
      ),
    );
    return confirmed == true;
  }

  Future<bool> _confirmResetSettings() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Reset editable settings?'),
        content: const Text(
          'This clears the Kernel API URL, bearer token, refresh, and '
          'notification preferences. Pairing and device identity are kept.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            key: const Key('confirm-reset-settings-button'),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('Reset'),
          ),
        ],
      ),
    );
    if (confirmed != true) return false;
    try {
      await _settingsStore.resetEditableSettings();
    } catch (_) {
      _showSnack(
        'Reset did not complete. Retry after secure storage is available.',
      );
      return false;
    }
    if (mounted) await _loadSettings();
    return true;
  }
}

class _ConnectionStrip extends StatelessWidget {
  const _ConnectionStrip({
    required this.baseUrl,
    required this.health,
    required this.error,
    required this.busy,
    required this.tokenConfigured,
    required this.onCheck,
  });

  final String baseUrl;
  final RumiHealth? health;
  final String? error;
  final bool busy;
  final bool tokenConfigured;
  final VoidCallback onCheck;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final healthy = health?.isHealthy == true;
    final color = error != null
        ? scheme.errorContainer
        : healthy
            ? const Color(0xFFDFF3E8)
            : scheme.secondaryContainer;
    final foreground = error != null
        ? scheme.onErrorContainer
        : healthy
            ? const Color(0xFF174E36)
            : scheme.onSecondaryContainer;
    final label = error ??
        (tokenConfigured
            ? '${health?.status ?? 'Not checked'} - $baseUrl'
            : 'Token required for defaultspack APIs - $baseUrl');

    return Material(
      color: color,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        child: Row(
          children: [
            Icon(
              error != null
                  ? Icons.error_outline
                  : healthy
                      ? Icons.check_circle_outline
                      : Icons.radio_button_unchecked,
              color: foreground,
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: foreground),
              ),
            ),
            IconButton(
              tooltip: 'Check',
              color: foreground,
              icon: busy
                  ? const SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.wifi_tethering),
              onPressed: busy ? null : onCheck,
            ),
          ],
        ),
      ),
    );
  }
}

class _StatusBand extends StatelessWidget {
  const _StatusBand({
    required this.catalog,
    required this.migration,
    required this.packRequests,
  });

  final ModuleCatalog? catalog;
  final MigrationStatus? migration;
  final List<PackRequest> packRequests;

  @override
  Widget build(BuildContext context) {
    final modules = catalog?.modules ?? const <RumiModule>[];
    final enabled = modules.where((module) => module.enabled).length;
    final degraded = modules.where((module) => module.degraded).length;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 6),
      child: Row(
        children: [
          Expanded(
            child: _MetricTile(
              icon: Icons.extension_outlined,
              label: 'Modules',
              value: modules.isEmpty ? '-' : '$enabled/${modules.length}',
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: _MetricTile(
              icon: Icons.warning_amber_outlined,
              label: 'Needs care',
              value: '$degraded',
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: _MetricTile(
              icon: Icons.move_up_outlined,
              label: 'Migration',
              value: migration?.summary ?? '-',
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: _MetricTile(
              icon: Icons.inbox_outlined,
              label: 'Requests',
              value: '${packRequests.length}',
            ),
          ),
        ],
      ),
    );
  }
}

class _MetricTile extends StatelessWidget {
  const _MetricTile({
    required this.icon,
    required this.label,
    required this.value,
  });

  final IconData icon;
  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Row(
          children: [
            Icon(icon, color: scheme.primary),
            const SizedBox(width: 8),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.labelSmall,
                  ),
                  Text(
                    value,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w700),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _CompactLayout extends StatefulWidget {
  const _CompactLayout({
    required this.modules,
    required this.selectedId,
    required this.busy,
    required this.onSelect,
    required this.detail,
  });

  final List<RumiModule> modules;
  final String? selectedId;
  final bool busy;
  final ValueChanged<RumiModule> onSelect;
  final Widget detail;

  @override
  State<_CompactLayout> createState() => _CompactLayoutState();
}

class _CompactLayoutState extends State<_CompactLayout> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Expanded(
          child: IndexedStack(
            index: _index,
            children: [
              _ModuleList(
                modules: widget.modules,
                selectedId: widget.selectedId,
                busy: widget.busy,
                onSelect: (module) {
                  widget.onSelect(module);
                  setState(() => _index = 1);
                },
              ),
              widget.detail,
            ],
          ),
        ),
        NavigationBar(
          selectedIndex: _index,
          onDestinationSelected: (index) => setState(() => _index = index),
          destinations: const [
            NavigationDestination(
              icon: Icon(Icons.view_list_outlined),
              selectedIcon: Icon(Icons.view_list),
              label: 'Modules',
            ),
            NavigationDestination(
              icon: Icon(Icons.tune_outlined),
              selectedIcon: Icon(Icons.tune),
              label: 'Control',
            ),
          ],
        ),
      ],
    );
  }
}

class _ModuleList extends StatelessWidget {
  const _ModuleList({
    required this.modules,
    required this.selectedId,
    required this.busy,
    required this.onSelect,
  });

  final List<RumiModule> modules;
  final String? selectedId;
  final bool busy;
  final ValueChanged<RumiModule> onSelect;

  @override
  Widget build(BuildContext context) {
    if (modules.isEmpty) {
      return const _EmptyState(
        icon: Icons.extension_off_outlined,
        label: 'No module data',
      );
    }
    return ListView.separated(
      padding: const EdgeInsets.all(12),
      itemCount: modules.length,
      separatorBuilder: (_, __) => const SizedBox(height: 6),
      itemBuilder: (context, index) {
        final module = modules[index];
        return Card(
          child: ListTile(
            selected: module.id == selectedId,
            leading: _StateDot(module: module),
            title: Text(
              module.displayName,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            subtitle: Text(
              '${module.kind} - ${module.state}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            trailing: const Icon(Icons.chevron_right),
            onTap: busy ? null : () => onSelect(module),
          ),
        );
      },
    );
  }
}

class _ModuleDetail extends StatelessWidget {
  const _ModuleDetail({
    required this.module,
    required this.busy,
    required this.onAction,
  });

  final RumiModule? module;
  final bool busy;
  final void Function(RumiModule module, ModuleAction action) onAction;

  @override
  Widget build(BuildContext context) {
    final current = module;
    if (current == null) {
      return const _EmptyState(
        icon: Icons.tune_outlined,
        label: 'Select a module',
      );
    }

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Row(
          children: [
            _StateDot(module: current, large: true),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    current.displayName,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                  Text(current.id, overflow: TextOverflow.ellipsis),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 16),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            _ActionButton(
              icon: Icons.power_settings_new,
              label: ModuleAction.enable.label,
              busy: busy,
              onPressed: () => onAction(current, ModuleAction.enable),
            ),
            _ActionButton(
              icon: Icons.power_off_outlined,
              label: ModuleAction.disable.label,
              busy: busy,
              onPressed: () => onAction(current, ModuleAction.disable),
            ),
            _ActionButton(
              icon: Icons.restart_alt,
              label: ModuleAction.reload.label,
              busy: busy,
              onPressed: () => onAction(current, ModuleAction.reload),
            ),
            _ActionButton(
              icon: Icons.undo,
              label: ModuleAction.rollback.label,
              busy: busy,
              onPressed: () => onAction(current, ModuleAction.rollback),
            ),
          ],
        ),
        const SizedBox(height: 16),
        _InfoSection(
          title: 'State',
          rows: {
            'Kind': current.kind,
            'State': current.state,
            'Experimental': current.experimental ? 'yes' : 'no',
            'Updated': current.updatedAt?.toLocal().toString() ?? '-',
            'Last error': current.lastError ?? '-',
          },
        ),
        const SizedBox(height: 12),
        _InfoSection(
          title: 'Description',
          rows: {
            'Summary': current.description.isEmpty ? '-' : current.description,
            'Dependencies': current.dependencies.isEmpty
                ? '-'
                : current.dependencies.join(', '),
          },
        ),
        const SizedBox(height: 12),
        _JsonPanel(data: current.raw),
      ],
    );
  }
}

class _ActionButton extends StatelessWidget {
  const _ActionButton({
    required this.icon,
    required this.label,
    required this.busy,
    required this.onPressed,
  });

  final IconData icon;
  final String label;
  final bool busy;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return FilledButton.tonalIcon(
      icon: Icon(icon),
      label: Text(label),
      onPressed: busy ? null : onPressed,
    );
  }
}

class _InfoSection extends StatelessWidget {
  const _InfoSection({required this.title, required this.rows});

  final String title;
  final Map<String, String> rows;

  @override
  Widget build(BuildContext context) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
            const SizedBox(height: 8),
            for (final entry in rows.entries)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 3),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SizedBox(
                      width: 110,
                      child: Text(
                        entry.key,
                        style: Theme.of(context).textTheme.labelMedium,
                      ),
                    ),
                    Expanded(child: SelectableText(entry.value)),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _JsonPanel extends StatelessWidget {
  const _JsonPanel({required this.data});

  final Map<String, dynamic> data;

  @override
  Widget build(BuildContext context) {
    const encoder = JsonEncoder.withIndent('  ');
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Raw', style: TextStyle(fontWeight: FontWeight.w700)),
            const SizedBox(height: 8),
            SelectableText(
              encoder.convert(data),
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(fontFamily: 'monospace'),
            ),
          ],
        ),
      ),
    );
  }
}

class _StateDot extends StatelessWidget {
  const _StateDot({required this.module, this.large = false});

  final RumiModule module;
  final bool large;

  @override
  Widget build(BuildContext context) {
    final color = switch (module.state) {
      'enabled' => const Color(0xFF2E7D32),
      'experimental' => const Color(0xFF7B5E00),
      'degraded' => const Color(0xFFB26A00),
      'error_disabled' => Theme.of(context).colorScheme.error,
      'disabled' => Theme.of(context).colorScheme.outline,
      _ => Theme.of(context).colorScheme.outline,
    };
    final size = large ? 18.0 : 12.0;
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.icon, required this.label});

  final IconData icon;
  final String label;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 42, color: scheme.outline),
          const SizedBox(height: 8),
          Text(label, style: TextStyle(color: scheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

T? _firstWhereOrNull<T>(Iterable<T> values, bool Function(T value) test) {
  for (final value in values) {
    if (test(value)) {
      return value;
    }
  }
  return null;
}

String _friendlyError(Object error) {
  if (error is RumiApiException) {
    return error.message;
  }
  return '$error';
}
