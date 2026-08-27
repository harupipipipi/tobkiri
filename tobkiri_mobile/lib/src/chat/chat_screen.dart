import 'dart:async';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';

import '../application/conversation_router.dart';
import '../data/local/local_chat_backend.dart';
import '../data/local/mobile_tool_runtime.dart';
import '../data/pc/device_store.dart';
import '../data/pc/pc_approval_client.dart';
import '../data/pc/pc_catalog.dart';
import '../data/pc/pc_catalog_client.dart';
import '../data/pc/pc_chat_backend.dart';
import '../domain/chat_event.dart';
import '../domain/conversation_backend.dart';
import '../domain/conversation_locator.dart';
import '../domain/space.dart';
import '../features/tools/approval_card.dart';
import '../features/tools/tool_activity_card.dart';
import '../platform/platform_services.dart';
import '../settings/api_config_store.dart';
import '../settings/settings_screen.dart';
import 'chat_drawer.dart';
import 'defaultspack_action_icon.dart';
import 'chat_models.dart';
import 'chat_store.dart';
import 'composer_bar.dart';
import 'model_selection_screen.dart';
import 'message_view.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({
    super.key,
    required this.store,
    required this.configStore,
    required this.deviceStore,
    this.localBackend,
  });

  final ChatStore store;
  final ApiConfigStore configStore;
  final MobileDeviceStore deviceStore;
  final LocalConversationBackend? localBackend;

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen>
    implements MobileToolApprovalDelegate {
  final _uuid = const Uuid();
  final _scrollController = ScrollController();
  ApiConfig? _apiConfig;
  late final ConversationRouter _router;
  PcConversationBackend? _pcBackend;
  PairedDevice? _pairedDevice;
  bool _busy = false;
  bool _streaming = false;
  late Future<void> _initFuture;

  List<Space> _spaces = [Space.local];
  String _activeSpaceId = Space.local.id;
  List<PcConversationItem> _pcConversations = [];
  ConversationSnapshot? _activePcSnapshot;
  bool _loadingPc = false;
  List<PairedDevice> _pairedDevices = [];
  PcCatalog? _pcCatalog;
  bool _loadingPcCatalog = false;
  String? _selectedPcModel;
  String _pcThinkingLevel = 'medium';
  bool _pcDeepthinkEnabled = false;
  String _pcMode = 'chat';
  bool _pcYoloMode = false;
  bool _pcUltraYoloMode = false;
  final Map<String, String> _assistantMessageByRunId = {};
  final Map<String, List<ChatEvent>> _activityByAssistantMessageId = {};
  MobileNotificationSettings _notificationSettings =
      MobileNotificationSettings.defaults;
  List<ModelFavoriteConfig> _modelFavorites = [];
  String? _localStoreMessage;

  @override
  void initState() {
    super.initState();
    _router = ConversationRouter(
      local:
          widget.localBackend ??
          LocalConversationBackend(
            store: widget.store,
            configStore: widget.configStore,
            mobileToolApprovalDelegate: this,
          ),
    );
    _initFuture = _init();
    _scrollController.addListener(_onScroll);
  }

  @override
  void dispose() {
    _scrollController.removeListener(_onScroll);
    _scrollController.dispose();
    _router.local.dispose();
    _pcBackend?.close();
    super.dispose();
  }

  Future<void> _init() async {
    try {
      final storeStatus = await widget.store.load();
      if (storeStatus.recoveryAvailable) {
        _localStoreMessage = '保存済みバックアップから履歴を読み込みました。確認して復元してください。';
      }
      final active = widget.store.active;
      if (active == null && !storeStatus.recoveryAvailable) {
        await widget.store.createAndPersist();
      }
    } on ChatStoreException catch (error, stack) {
      _localStoreMessage = error.userMessage;
      debugPrint('Tobkiri local chat storage: ${error.code}\n$stack');
    }
    try {
      _apiConfig = await widget.configStore.loadApi();
      _modelFavorites = await widget.configStore.loadModelFavorites();
      _notificationSettings = await widget.configStore
          .loadNotificationSettings();
      await _loadPcConnection();
      await _loadSpaces();
    } catch (error, stack) {
      debugPrint('Rumi init error: $error\n$stack');
    }
    if (mounted) setState(() {});
  }

  Future<T?> _runLocalStore<T>(Future<T> Function() operation) async {
    try {
      final result = await operation();
      if (mounted &&
          _localStoreMessage != null &&
          !widget.store.recoveryAvailable) {
        setState(() => _localStoreMessage = null);
      }
      return result;
    } on ChatStoreException catch (error) {
      if (mounted) {
        setState(() => _localStoreMessage = error.userMessage);
      }
      return null;
    }
  }

  Future<void> _retryLocalStoreLoad() async {
    await _runLocalStore(() async {
      final status = await widget.store.load();
      if (mounted && status.recoveryAvailable) {
        setState(() {
          _localStoreMessage = '保存済みバックアップから履歴を読み込みました。確認して復元してください。';
        });
      }
      return status;
    });
  }

  Future<void> _acceptLocalStoreRecovery() async {
    await _runLocalStore(widget.store.acceptRecoveredData);
  }

  Future<void> _loadSpaces() async {
    _pairedDevices = await widget.deviceStore.loadPairedDevices();
    final spaces = <Space>[Space.local];
    for (final device in _pairedDevices) {
      spaces.add(Space.fromPairedDevice(device));
    }
    _spaces = spaces;
    if (!_spaces.any((s) => s.id == _activeSpaceId)) {
      _activeSpaceId = Space.local.id;
    }
    if (_activeSpaceId != Space.local.id) {
      await _loadPcConversations();
      await _loadPcCatalogForActiveSpace();
    } else {
      _pcCatalog = null;
    }
  }

  Future<void> _loadPcConversations() async {
    final space = _activeSpace();
    if (space == null || !space.isPc) {
      _pcConversations = [];
      return;
    }
    final backend = _ensurePcBackendForSpace(space);
    if (backend == null) {
      _pcConversations = [];
      return;
    }
    setState(() => _loadingPc = true);
    try {
      final summaries = await backend.listConversations();
      if (!mounted) return;
      setState(() {
        _pcConversations = summaries
            .map(
              (s) => PcConversationItem(
                id: s.id,
                title: s.title,
                messageCount: s.messageCount,
                updatedAt: s.updatedAt,
                pinned: s.pinned,
                preview: '',
              ),
            )
            .toList();
        _loadingPc = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _pcConversations = [];
        _loadingPc = false;
      });
      _updateSpaceOffline(_activeSpaceId, true);
    }
  }

  Future<void> _loadPcCatalogForActiveSpace() async {
    final space = _activeSpace();
    final connection = space?.pcConnection;
    if (space == null || !space.isPc || connection == null) {
      _pcCatalog = null;
      return;
    }
    if (!connection.isConfigured) return;
    if (mounted) setState(() => _loadingPcCatalog = true);
    final client = PcCatalogClient();
    try {
      final catalog = await client.fetchCapabilities(connection);
      final selected = _initialPcModelForCatalog(catalog);
      if (!mounted) return;
      setState(() {
        _pcCatalog = catalog;
        _selectedPcModel = selected;
        _pcThinkingLevel = catalog.runtime.thinkingLevel;
        _pcDeepthinkEnabled = catalog.runtime.deepthinkEnabled;
        _loadingPcCatalog = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _pcCatalog = null;
        _loadingPcCatalog = false;
      });
    } finally {
      client.close();
    }
  }

  String? _initialPcModelForCatalog(PcCatalog catalog) {
    final preferred = catalog.runtime.preferredModel.trim();
    final preferredProfile = preferred.isEmpty
        ? null
        : catalog.profileById(preferred);
    if (preferredProfile != null &&
        preferredProfile.effectiveProfileId != 'stub/default' &&
        (preferredProfile.configured || preferredProfile.local)) {
      return preferredProfile.effectiveProfileId;
    }
    final usable = catalog.selectableProfiles
        .where((p) => p.configured || p.local)
        .toList();
    for (final profile in usable) {
      if (profile.effectiveProfileId != 'stub/default' &&
          profile.providerId != 'stub') {
        return profile.effectiveProfileId;
      }
    }
    if (preferredProfile != null) return preferredProfile.effectiveProfileId;
    return usable.isNotEmpty ? usable.first.effectiveProfileId : null;
  }

  void _updateSpaceOffline(String spaceId, bool offline) {
    _spaces = _spaces
        .map((s) => s.id == spaceId ? s.copyWith(online: !offline) : s)
        .toList();
  }

  Future<void> _selectSpace(String spaceId) async {
    if (spaceId == _activeSpaceId) return;
    setState(() {
      _activeSpaceId = spaceId;
      _pcConversations = [];
      _activePcSnapshot = null;
      _pcCatalog = null;
    });
    if (spaceId != Space.local.id) {
      await _loadPcConversations();
      await _loadPcCatalogForActiveSpace();
    }
    if (mounted) setState(() {});
  }

  Future<void> _loadPcConnection() async {
    final paired = await widget.deviceStore.loadPairedDevice();
    if (paired != null && paired.deviceToken.isNotEmpty) {
      final pc = paired.toPcConnection();
      _pcBackend?.close();
      _pcBackend = PcConversationBackend(
        connection: pc,
        deviceId: paired.deviceId,
      );
      _router.setPc(_pcBackend!);
      _pairedDevice = paired;
    } else {
      _pcBackend?.close();
      _pcBackend = null;
      _router.setPc(null);
      _pairedDevice = null;
    }
  }

  Space? _activeSpace() {
    for (final space in _spaces) {
      if (space.id == _activeSpaceId) return space;
    }
    return Space.local;
  }

  bool get _activeSpaceIsPc => _activeSpace()?.isPc ?? false;

  PcConversationBackend? _ensurePcBackendForSpace(Space space) {
    final connection = space.pcConnection;
    if (connection == null || !connection.isConfigured) return null;
    final current = _pcBackend;
    if (current != null &&
        current.connection.baseUrl == connection.baseUrl &&
        current.connection.token == connection.token &&
        current.deviceId == space.deviceId) {
      _router.setPc(current);
      return current;
    }
    _pcBackend?.close();
    final backend = PcConversationBackend(
      connection: connection,
      deviceId: space.deviceId,
    );
    _pcBackend = backend;
    _router.setPc(backend);
    return backend;
  }

  Conversation? _displayConversation() {
    if (_activeSpaceIsPc) return _activePcSnapshot?.conversation;
    return widget.store.active;
  }

  String? _displayActiveId() {
    if (_activeSpaceIsPc) return _activePcSnapshot?.locator.conversationId;
    return widget.store.active?.id;
  }

  void _onScroll() {}

  void _scrollToBottom({bool animate = true}) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scrollController.hasClients) return;
      final max = _scrollController.position.maxScrollExtent;
      if (animate) {
        _scrollController.animateTo(
          max,
          duration: const Duration(milliseconds: 180),
          curve: Curves.easeOut,
        );
      } else {
        _scrollController.jumpTo(max);
      }
    });
  }

  Future<void> _newChat() async {
    if (_activeSpaceIsPc) {
      final space = _activeSpace();
      if (space == null) return;
      final backend = _ensurePcBackendForSpace(space);
      if (backend == null) {
        _promptPcConfigure();
        return;
      }
      try {
        final locator = await backend.createConversation(
          CreateConversationRequest(
            authority: ConversationAuthorityKind.pc,
            deviceId: space.deviceId,
          ),
        );
        final snapshot = await backend.getConversation(locator);
        if (!mounted) return;
        setState(() => _activePcSnapshot = snapshot);
        await _loadPcConversations();
      } catch (e) {
        _updateSpaceOffline(_activeSpaceId, true);
        if (mounted) setState(() {});
      }
      return;
    }
    final created = await _runLocalStore(widget.store.createAndPersist);
    if (mounted && created != null) setState(() {});
  }

  Future<void> _select(String id) async {
    if (_activeSpaceIsPc) {
      await _selectPcConversation(id);
      return;
    }
    final saved = await _runLocalStore(() => widget.store.select(id));
    if (mounted && saved != null) setState(() {});
  }

  Future<void> _selectPcConversation(String id) async {
    final space = _activeSpace();
    if (space == null) return;
    final backend = _ensurePcBackendForSpace(space);
    if (backend == null) {
      _promptPcConfigure();
      return;
    }
    try {
      final locator = ConversationLocator.pc(id, deviceId: space.deviceId);
      final snapshot = await backend.getConversation(locator);
      if (!mounted) return;
      setState(() => _activePcSnapshot = snapshot);
      _scrollToBottom(animate: false);
    } catch (e) {
      _updateSpaceOffline(_activeSpaceId, true);
      if (mounted) setState(() {});
    }
  }

  Future<void> _reconnectActiveSpace() async {
    _updateSpaceOffline(_activeSpaceId, false);
    if (mounted) setState(() {});
    await _loadPcConversations();
  }

  Future<void> _continuePcConversationLocally() async {
    final snapshot = _activePcSnapshot;
    if (snapshot == null) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('コピーできるPC会話がありません')));
      return;
    }
    final local = await _runLocalStore(() async {
      final created = await widget.store.createAndPersist();
      await widget.store.rename(created.id, snapshot.conversation.title);
      for (final message in snapshot.conversation.messages) {
        await widget.store.addMessage(created.id, message.copy());
      }
      return created;
    });
    if (local == null) return;
    setState(() {
      _activeSpaceId = Space.local.id;
      _activePcSnapshot = null;
    });
  }

  Future<void> _delete(String id) async {
    final saved = await _runLocalStore(() => widget.store.delete(id));
    if (mounted && saved != null) setState(() {});
  }

  Future<void> _pin(String id) async {
    final saved = await _runLocalStore(() => widget.store.togglePin(id));
    if (mounted && saved != null) setState(() {});
  }

  Future<void> _rename(String id) async {
    final convo = widget.store.conversations.firstWhere((c) => c.id == id);
    final controller = TextEditingController(text: convo.title);
    final result = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('名前を変更'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'チャット名'),
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
    if (result != null) {
      final saved = await _runLocalStore(() => widget.store.rename(id, result));
      if (mounted && saved != null) setState(() {});
    }
  }

  void _openSettings() async {
    await Navigator.of(context).push(
      MaterialPageRoute(
        builder: (_) => SettingsScreen(
          configStore: widget.configStore,
          deviceStore: widget.deviceStore,
          onApiChanged: (next) {
            setState(() => _apiConfig = next);
          },
          onDevicePaired: (device) async {
            await _loadPcConnection();
            _activeSpaceId = device == null
                ? Space.local.id
                : 'pc:${device.connectionId}';
            await _loadSpaces();
            if (mounted) setState(() {});
          },
        ),
      ),
    );
    final refreshed = await widget.configStore.loadApi();
    final modelFavorites = await widget.configStore.loadModelFavorites();
    final notificationSettings = await widget.configStore
        .loadNotificationSettings();
    await _loadPcConnection();
    await _loadSpaces();
    if (mounted) {
      setState(() {
        _apiConfig = refreshed;
        _modelFavorites = modelFavorites;
        _notificationSettings = notificationSettings;
      });
    }
  }

  Future<void> _send(String text) async {
    if (text.trim().isEmpty) return;
    if (await _tryExecuteSlashCommand(text)) return;
    if (_activeSpaceIsPc) {
      await _sendToPc(text);
      return;
    }
    final config = _apiConfig;
    if (config == null || !config.isConfigured) {
      _promptConfigure();
      return;
    }

    var convo = widget.store.active;
    if (convo == null) {
      convo = await _runLocalStore(widget.store.createAndPersist);
      if (convo == null) return;
    }
    final locator = ConversationLocator.local(convo.id);
    final clientMessageId = _uuid.v4();
    final expectedRevision = convo.revision;

    setState(() {
      _busy = true;
      _streaming = true;
    });
    _scrollToBottom();

    final backend = _router.backendFor(locator);
    try {
      await for (final event in backend.sendMessage(
        locator: locator,
        text: text,
        clientMessageId: clientMessageId,
        expectedRevision: expectedRevision,
        model: _activeModelId(),
      )) {
        if (!mounted) break;
        switch (event) {
          case ChatRunStarted():
            _rememberRunStarted(event);
            setState(() {});
            _scrollToBottom(animate: false);
            break;
          case ChatDelta():
            setState(() {});
            _scrollToBottom(animate: false);
            break;
          case ChatStatusEvent():
          case ToolCallEvent():
          case ApprovalEvent():
            _recordActivityEvent(event);
            _scrollToBottom(animate: false);
            break;
          case ChatErrorEvent():
            _clearTransientActivityForRun(event);
            break;
          case ChatMessageCommitted():
            _clearTransientActivityForRun(event);
            break;
          case ChatRunCompleted():
          case ChatRunStopped():
            _clearTransientActivityForRun(event);
            break;
        }
      }
    } on ChatStoreException catch (error) {
      if (mounted) {
        setState(() => _localStoreMessage = error.userMessage);
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _streaming = false;
        });
      }
    }
    if (mounted) setState(() {});
  }

  @override
  Future<bool> approve(MobileToolApprovalRequest request) async {
    if (!mounted) return false;
    final result = await showDialog<bool>(
      context: context,
      barrierDismissible: false,
      builder: (dialogContext) => AlertDialog(
        title: const Text('スマホtoolの許可'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(request.prompt),
              const SizedBox(height: 12),
              Text(
                request.toolName,
                style: Theme.of(dialogContext).textTheme.labelLarge,
              ),
              const SizedBox(height: 8),
              Text(
                _mobileToolApprovalDetails(request),
                style: Theme.of(dialogContext).textTheme.bodySmall,
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('拒否'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('許可'),
          ),
        ],
      ),
    );
    return result == true;
  }

  String _mobileToolApprovalDetails(MobileToolApprovalRequest request) {
    final lines = <String>['risk: ${request.risk}'];
    final reason = '${request.arguments['reason'] ?? ''}'.trim();
    if (reason.isNotEmpty) lines.add('reason: $reason');
    if (request.arguments['length'] != null) {
      lines.add('length: ${request.arguments['length']}');
    }
    final preview = '${request.arguments['preview'] ?? ''}'.trim();
    if (preview.isNotEmpty) {
      lines.add('preview: ${_compactApprovalValue(preview)}');
    }
    for (final entry in request.arguments.entries) {
      if (lines.length >= 6) break;
      final key = entry.key;
      if (const {
        'approved',
        'content',
        'input',
        'length',
        'preview',
        'reason',
        'text',
      }.contains(key)) {
        continue;
      }
      lines.add('$key: ${_compactApprovalValue(entry.value)}');
    }
    return lines.join('\n');
  }

  String _compactApprovalValue(Object? value) {
    final text = '$value'.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (text.length <= 160) return text;
    return '${text.substring(0, 160)}...';
  }

  Future<void> _sendToPc(String text) async {
    final space = _activeSpace();
    if (space == null) return;
    final backend = _ensurePcBackendForSpace(space);
    if (backend == null) {
      _promptPcConfigure();
      return;
    }

    var snapshot = _activePcSnapshot;
    if (snapshot == null) {
      final locator = await backend.createConversation(
        CreateConversationRequest(
          authority: ConversationAuthorityKind.pc,
          deviceId: space.deviceId,
        ),
      );
      snapshot = await backend.getConversation(locator);
      if (!mounted) return;
      setState(() => _activePcSnapshot = snapshot);
    }
    final locator = snapshot.locator;
    final clientMessageId = _uuid.v4();
    final expectedRevision = snapshot.revision;
    _appendPcMessage(
      ChatMessage(
        id: clientMessageId,
        role: ChatRole.user,
        content: text,
        createdAt: DateTime.now(),
      ),
    );
    final pendingAssistantId = 'pending:$clientMessageId';
    _appendPcMessage(
      ChatMessage(
        id: pendingAssistantId,
        role: ChatRole.assistant,
        content: '',
        createdAt: DateTime.now(),
        pending: true,
      ),
    );

    setState(() {
      _busy = true;
      _streaming = true;
    });
    _scrollToBottom();

    var completed = false;
    var finalAssistantText = '';
    try {
      await for (final event in backend.sendMessage(
        locator: locator,
        text: text,
        clientMessageId: clientMessageId,
        expectedRevision: expectedRevision,
        model: _activeModelId(),
        profileId: _activeModelId(),
        params: _pcRequestParams(),
      )) {
        if (!mounted) break;
        _applyPcEvent(event);
        if (event is ChatMessageCommitted && !event.error) {
          finalAssistantText = event.content;
        } else if (event is ChatRunCompleted) {
          completed = true;
        }
        _scrollToBottom(animate: false);
      }
      try {
        final refreshed = await backend.getConversation(locator);
        if (mounted) setState(() => _activePcSnapshot = refreshed);
        await _loadPcConversations();
      } catch (_) {
        // Keep optimistic snapshot if refresh fails.
      }
      if (completed) {
        unawaited(_notifyPcTaskFinished(finalAssistantText));
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _streaming = false;
        });
      }
    }
  }

  Future<void> _notifyPcTaskFinished(String assistantText) async {
    if (!_notificationSettings.pcTaskFinishedEnabled) return;
    final space = _activeSpace();
    final pcLabel = space?.label ?? _pairedDevice?.displayPcLabel ?? 'PC';
    final preview = _compactNotificationPreview(assistantText);
    final body = preview.isEmpty ? '$pcLabel のタスクが完了しました' : preview;
    await const PlatformNotifications().showPcTaskFinished(
      title: 'PCタスクが完了しました',
      body: body,
    );
  }

  String _compactNotificationPreview(String value) {
    final normalized = value.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (normalized.length <= 96) return normalized;
    return '${normalized.substring(0, 96)}...';
  }

  void _appendPcMessage(ChatMessage message) {
    final snapshot = _activePcSnapshot;
    if (snapshot == null) return;
    final messages = snapshot.conversation.messages;
    if (messages.any((m) => m.id == message.id)) return;
    setState(() {
      messages.add(message);
      snapshot.conversation.revision += 1;
      snapshot.conversation.updatedAt = DateTime.now();
    });
  }

  void _updatePcMessage(
    String id,
    String content, {
    bool? pending,
    bool? error,
  }) {
    final snapshot = _activePcSnapshot;
    if (snapshot == null) return;
    final messages = snapshot.conversation.messages;
    final matches = messages.where((m) => m.id == id);
    if (matches.isEmpty) return;
    final message = matches.first;
    setState(() {
      message.content = content;
      if (pending != null) message.pending = pending;
      if (error != null) message.error = error;
      snapshot.conversation.updatedAt = DateTime.now();
    });
  }

  void _applyPcEvent(ChatEvent event) {
    switch (event) {
      case ChatRunStarted():
        _rememberRunStarted(event);
        _removePendingPcPlaceholders();
        _appendPcMessage(
          ChatMessage(
            id: event.assistantMessageId,
            role: ChatRole.assistant,
            content: '',
            createdAt: DateTime.now(),
            pending: true,
          ),
        );
        break;
      case ChatDelta():
        _updatePcMessage(
          event.assistantMessageId,
          event.accumulatedContent,
          pending: true,
        );
        break;
      case ChatMessageCommitted():
        _updatePcMessage(
          event.messageId,
          event.content,
          pending: false,
          error: event.error,
        );
        _clearTransientActivityForRun(event);
        break;
      case ChatErrorEvent():
        final assistantId = event.assistantMessageId;
        if (assistantId != null) {
          _updatePcMessage(
            assistantId,
            event.message,
            pending: false,
            error: true,
          );
        }
        _clearTransientActivityForRun(event);
        break;
      case ChatStatusEvent():
        _recordActivityEvent(event);
        break;
      case ChatRunCompleted():
      case ChatRunStopped():
        _clearTransientActivityForRun(event);
        break;
      case ToolCallEvent():
        _recordActivityEvent(event);
        break;
      case ApprovalEvent():
        _recordActivityEvent(event);
        break;
    }
  }

  void _rememberRunStarted(ChatRunStarted event) {
    _assistantMessageByRunId[event.runId ?? ''] = event.assistantMessageId;
    _activityByAssistantMessageId.putIfAbsent(
      event.assistantMessageId,
      () => <ChatEvent>[],
    );
  }

  void _recordActivityEvent(ChatEvent event) {
    final runId = event.runId ?? '';
    final assistantId = _assistantMessageByRunId[runId];
    if (assistantId == null || assistantId.isEmpty) return;
    setState(() {
      final list = _activityByAssistantMessageId.putIfAbsent(
        assistantId,
        () => <ChatEvent>[],
      );
      final replaceIndex = _matchingActivityIndex(list, event);
      if (replaceIndex >= 0) {
        list[replaceIndex] = event;
      } else {
        list.add(event);
      }
      if (list.length > 8) {
        list.removeRange(0, list.length - 8);
      }
    });
  }

  int _matchingActivityIndex(List<ChatEvent> events, ChatEvent next) {
    for (var i = events.length - 1; i >= 0; i--) {
      final current = events[i];
      if (current is ToolCallEvent && next is ToolCallEvent) {
        if (current.toolId.isNotEmpty && current.toolId == next.toolId) {
          return i;
        }
      }
      if (current is ApprovalEvent && next is ApprovalEvent) {
        if (current.approvalId.isNotEmpty &&
            current.approvalId == next.approvalId) {
          return i;
        }
      }
      if (current is ChatStatusEvent && next is ChatStatusEvent) {
        if (current.phase == next.phase) return i;
      }
    }
    return -1;
  }

  List<ChatEvent> _activityForMessage(String messageId) {
    return List<ChatEvent>.unmodifiable(
      _activityByAssistantMessageId[messageId] ?? const <ChatEvent>[],
    );
  }

  void _clearTransientActivityForRun(ChatEvent event) {
    final runId = event.runId ?? '';
    final assistantId = _assistantMessageByRunId[runId];
    if (assistantId == null || assistantId.isEmpty) return;
    final list = _activityByAssistantMessageId[assistantId];
    if (list == null) return;
    final hasTransient = list.any(
      (item) =>
          item is ChatStatusEvent ||
          item is ToolCallEvent ||
          (item is ApprovalEvent && !item.pending),
    );
    if (!hasTransient) return;
    setState(() {
      list.removeWhere(
        (item) =>
            item is ChatStatusEvent ||
            item is ToolCallEvent ||
            (item is ApprovalEvent && !item.pending),
      );
      if (list.isEmpty) {
        _activityByAssistantMessageId.remove(assistantId);
      }
    });
  }

  void _removePendingPcPlaceholders() {
    final snapshot = _activePcSnapshot;
    if (snapshot == null) return;
    final messages = snapshot.conversation.messages;
    setState(() {
      messages.removeWhere(
        (m) =>
            m.id.startsWith('pending:') &&
            m.role == ChatRole.assistant &&
            m.pending &&
            m.content.isEmpty,
      );
    });
  }

  void _stop() {
    if (_activeSpaceIsPc) {
      final locator = _activePcSnapshot?.locator;
      if (locator != null) {
        unawaited(_router.backendFor(locator).stop(locator.conversationId));
      }
      setState(() => _streaming = false);
      return;
    }
    final id = widget.store.active?.id;
    if (id != null) {
      unawaited(_router.local.stop(id));
    }
    setState(() => _streaming = false);
  }

  void _promptConfigure() {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text('APIを設定してください'),
        action: SnackBarAction(label: '設定', onPressed: _openSettings),
      ),
    );
  }

  void _promptPcConfigure() {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: const Text('PCとペアリングしてください'),
        action: SnackBarAction(label: '設定', onPressed: _openSettings),
      ),
    );
  }

  Map<String, dynamic> _pcRequestParams() {
    final profile = _activePcProfile();
    final toolPolicy = <String, dynamic>{
      'ai_input_id': 'rumi.composer.default:default_ai_input',
      'template_tool_policy_id': 'rumi.composer.default:default_tools',
    };
    final params = <String, dynamic>{
      'deepthink_enabled': _pcDeepthinkEnabled,
      'tool_selection': const {
        'mode': 'auto',
        'include': <String>[],
        'exclude': <String>[],
        'scope': 'turn',
        'must_use': false,
      },
      'tool_policy': toolPolicy,
      'metadata': {
        'mode': _pcMode,
        'agent_template_id': 'rumi.composer.default',
        'agent_ai_input_id': 'rumi.composer.default:default_ai_input',
      },
    };
    if (profile?.supportsThinking ?? true) {
      params['thinking_level'] = _pcThinkingLevel;
    }
    if (_pcYoloMode || _pcUltraYoloMode) {
      toolPolicy.addAll({
        'yolo_mode': true,
        'allow_shell': true,
        'allow_file_write': true,
        'write_actions_require_approval': false,
      });
    }
    return params;
  }

  Future<bool> _tryExecuteSlashCommand(String text) async {
    final trimmed = text.trim();
    if (!trimmed.startsWith('/') || trimmed.startsWith('//')) return false;
    if (_activeSpaceIsPc) {
      final catalog = await _ensurePcCatalog();
      final parsed = _parsePcSlashCommand(trimmed, catalog?.commands ?? []);
      if (parsed == null) {
        _showSnack('未登録のPC commandです');
        return true;
      }
      await _executePcCommand(parsed.command, args: parsed.args);
      return true;
    }
    final localModelMatch = RegExp(
      r'^/models?(?:\s+(.+))?$',
      caseSensitive: false,
    ).firstMatch(trimmed);
    if (localModelMatch != null) {
      final model = (localModelMatch.group(1) ?? '').trim();
      if (model.isEmpty) {
        await _openModelPicker();
      } else {
        await _setLocalModel(model);
      }
      return true;
    }
    _showSnack('このスマホでは /model に対応しています');
    return true;
  }

  Future<PcCatalog?> _ensurePcCatalog() async {
    if (!_activeSpaceIsPc) return null;
    if (_pcCatalog != null) return _pcCatalog;
    await _loadPcCatalogForActiveSpace();
    return _pcCatalog;
  }

  Future<void> _openComposerOptions() async {
    if (_activeSpaceIsPc) {
      await _ensurePcCatalog();
    }
    if (!mounted) return;
    final catalog = _pcCatalog;
    final commands = _activeSpaceIsPc
        ? (catalog?.commands ?? const <PcCommandItem>[])
              .where((c) => c.enabled && c.visibility != 'hidden')
              .toList()
        : const <PcCommandItem>[];
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      useSafeArea: true,
      builder: (context) {
        final theme = Theme.of(context);
        return ListView(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
          shrinkWrap: true,
          children: [
            Text('オプション', style: theme.textTheme.titleMedium),
            const SizedBox(height: 8),
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.model_training_outlined),
              title: const Text('モデル'),
              subtitle: Text(_activeModelLabel()),
              trailing: const Icon(Icons.chevron_right),
              onTap: () {
                Navigator.of(context).pop();
                unawaited(_openModelPicker());
              },
            ),
            if (_activeSpaceIsPc) ...[
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('DeepThink'),
                subtitle: const Text('PCのDeepThink設定'),
                value: _pcDeepthinkEnabled,
                onChanged: (value) {
                  Navigator.of(context).pop();
                  unawaited(_setPcDeepthink(value));
                },
              ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.psychology_alt_outlined),
                title: const Text('Thinking level'),
                subtitle: Text(_pcThinkingLevel),
              ),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final level in const [
                    'none',
                    'low',
                    'medium',
                    'high',
                    'xhigh',
                  ])
                    ChoiceChip(
                      label: Text(level),
                      selected: _pcThinkingLevel == level,
                      onSelected: (_) {
                        Navigator.of(context).pop();
                        unawaited(_setPcThinkingLevel(level));
                      },
                    ),
                ],
              ),
              const SizedBox(height: 12),
              Text('/ commands', style: theme.textTheme.titleSmall),
              if (_loadingPcCatalog)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 12),
                  child: LinearProgressIndicator(),
                ),
              for (final command in commands)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  dense: true,
                  leading: Icon(_iconForCommand(command)),
                  title: Text(command.label),
                  subtitle: Text(
                    '/${command.name}${command.args.isEmpty ? "" : " ${command.args.map((a) => "<${a.name}>").join(" ")}"}',
                  ),
                  trailing: command.active
                      ? const Icon(Icons.check_circle, size: 18)
                      : null,
                  onTap: () {
                    Navigator.of(context).pop();
                    unawaited(_runPcCommandFromMenu(command));
                  },
                ),
            ] else ...[
              const SizedBox(height: 12),
              Text('/ commands', style: theme.textTheme.titleSmall),
              ListTile(
                contentPadding: EdgeInsets.zero,
                dense: true,
                leading: const Icon(Icons.model_training_outlined),
                title: const Text('Model'),
                subtitle: const Text('/model <name>'),
                onTap: () {
                  Navigator.of(context).pop();
                  unawaited(_openModelPicker());
                },
              ),
            ],
          ],
        );
      },
    );
  }

  Future<void> _openModelPicker() async {
    if (_activeSpaceIsPc) {
      final catalog = await _ensurePcCatalog();
      if (!mounted) return;
      final profiles = _pcProfilesForSelection(catalog);
      final selected = await Navigator.of(context).push<ModelSelectionResult>(
        MaterialPageRoute(
          builder: (_) => ModelSelectionScreen.pc(
            profiles: profiles,
            activeModelId: _activeModelId(),
          ),
        ),
      );
      if (selected?.pcProfileId?.trim().isNotEmpty == true) {
        await _setPcModel(selected!.pcProfileId!.trim());
      }
      return;
    }
    final providers = (await widget.configStore.loadProviderConfigs())
        .where((provider) => provider.isConfigured)
        .toList();
    final selectableProviders = _mobileProvidersForSelection(providers);
    if (!mounted) return;
    final selected = await Navigator.of(context).push<ModelSelectionResult>(
      MaterialPageRoute(
        builder: (_) => ModelSelectionScreen.local(
          providers: selectableProviders,
          activeModelId: _activeModelId(),
          activeProviderId: _apiConfig?.providerId ?? '',
        ),
      ),
    );
    if (selected == null) return;
    switch (selected.kind) {
      case ModelSelectionKind.mobileProvider:
        final provider = selected.mobileProvider;
        if (provider != null) await _setLocalProvider(provider);
        return;
      case ModelSelectionKind.localModel:
        final model = selected.localModel?.trim();
        if (model != null && model.isNotEmpty) await _setLocalModel(model);
        return;
      case ModelSelectionKind.pcProfile:
        return;
    }
  }

  List<ProfileEntry> _pcProfilesForSelection(PcCatalog? catalog) {
    final profiles = catalog?.selectableProfiles ?? const <ProfileEntry>[];
    if (profiles.isEmpty) return profiles;
    final favorites = profiles
        .where(
          (profile) =>
              profile.favorite ||
              _modelFavorites.any(
                (favorite) => favorite.matchesPcProfile(
                  effectiveProfileId: profile.effectiveProfileId,
                  qualifiedModelId: profile.qualifiedModelId,
                  providerId: profile.providerId,
                  modelId: profile.modelId,
                ),
              ),
        )
        .toList();
    return favorites.isNotEmpty ? favorites : profiles;
  }

  List<MobileProviderConfig> _mobileProvidersForSelection(
    List<MobileProviderConfig> providers,
  ) {
    final favorites = providers
        .where(
          (provider) => _modelFavorites.any(
            (favorite) => favorite.matchesMobileProvider(provider),
          ),
        )
        .toList();
    return favorites.isNotEmpty ? favorites : providers;
  }

  Future<void> _setLocalModel(String model) async {
    final current = _apiConfig ?? await widget.configStore.loadApi();
    final next = current.copyWith(model: model.trim());
    await widget.configStore.saveApi(next);
    if (!mounted) return;
    setState(() => _apiConfig = next);
    _showSnack('このスマホのモデルを ${next.model} にしました');
  }

  Future<void> _setLocalProvider(MobileProviderConfig provider) async {
    final current = _apiConfig ?? await widget.configStore.loadApi();
    final next = provider.toApiConfig(
      systemPrompt: current.systemPrompt,
      temperature: current.temperature,
    );
    await widget.configStore.saveApi(next);
    if (!mounted) return;
    setState(() => _apiConfig = next);
    _showSnack('${provider.effectiveLabel} にしました');
  }

  Future<void> _setPcModel(String profileId) async {
    final space = _activeSpace();
    final connection = space?.pcConnection;
    if (connection == null || !connection.isConfigured) {
      _promptPcConfigure();
      return;
    }
    setState(() => _selectedPcModel = profileId);
    final client = PcCatalogClient();
    try {
      final result = await client.executeCommand(
        connection,
        command: 'model',
        args: {'query': profileId},
        conversationId: _activePcSnapshot?.locator.conversationId,
        mode: _pcMode,
      );
      await _handlePcCommandResult(
        PcCommandItem.fromJson(<String, dynamic>{
          'id': 'model',
          'name': 'model',
          'label': 'Model',
          'category': 'model',
          'visibility': 'default',
          'risk': 'low',
          'execution': {
            'type': 'model_command',
            'action': 'select_or_suggest_model',
          },
        }),
        result,
      );
    } catch (e) {
      _showSnack('PCモデル設定に失敗しました: $e');
    } finally {
      client.close();
    }
  }

  Future<void> _setPcThinkingLevel(String level) async {
    setState(() => _pcThinkingLevel = level);
    final command = _pcCatalog?.commands.firstWhere(
      (c) => c.id == 'think' || c.name == 'think',
      orElse: () => PcCommandItem.fromJson(<String, dynamic>{
        'id': 'think',
        'name': 'think',
        'label': 'Thinking Level',
        'category': 'model',
        'visibility': 'default',
        'risk': 'low',
        'args': [
          {'name': 'level', 'type': 'enum'},
        ],
        'execution': {
          'type': 'rumi_function',
          'qualified_name': 'defaultspack:ai_set_thinking_level',
        },
      }),
    );
    if (command != null) {
      await _executePcCommand(
        command,
        args: {
          'level': level,
          'scope': 'profile',
          'profile_id': _activeModelId(),
        },
      );
      if (mounted) setState(() => _pcThinkingLevel = level);
    }
  }

  Future<void> _setPcDeepthink(bool enabled) async {
    setState(() => _pcDeepthinkEnabled = enabled);
    final command = _pcCatalog?.commands.firstWhere(
      (c) => c.id == 'deepthink' || c.name == 'deepthink',
      orElse: () => PcCommandItem.fromJson(<String, dynamic>{
        'id': 'deepthink',
        'name': 'deepthink',
        'label': 'DeepThink',
        'category': 'model',
        'visibility': 'default',
        'risk': 'medium',
        'args': [
          {'name': 'enabled', 'type': 'boolean'},
        ],
        'execution': {
          'type': 'rumi_function',
          'qualified_name': 'defaultspack:ai_set_deepthink_enabled',
        },
      }),
    );
    if (command != null) {
      await _executePcCommand(command, args: {'enabled': enabled});
    }
  }

  Future<void> _runPcCommandFromMenu(PcCommandItem command) async {
    if (command.isModelCommand) {
      await _openModelPicker();
      return;
    }
    final args = await _collectPcCommandArgs(command);
    if (args == null) return;
    await _executePcCommand(command, args: args);
  }

  Future<Map<String, dynamic>?> _collectPcCommandArgs(
    PcCommandItem command,
  ) async {
    if (command.args.isEmpty) return const {};
    if (!mounted) return null;
    if (command.args.length == 1) {
      final arg = command.args.first;
      if (arg.type == 'enum' && arg.values.isNotEmpty) {
        final picked = await showModalBottomSheet<String>(
          context: context,
          showDragHandle: true,
          useSafeArea: true,
          builder: (context) => ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
            children: [
              Text(
                '/${command.name} ${arg.name}',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 8),
              if (!arg.required)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('そのまま実行'),
                  onTap: () => Navigator.pop(context, ''),
                ),
              for (final value in arg.values)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: Text(value),
                  onTap: () => Navigator.pop(context, value),
                ),
            ],
          ),
        );
        if (picked == null) return null;
        return picked.isEmpty ? const {} : {arg.name: picked};
      }
      if (arg.type == 'boolean') {
        final picked = await showModalBottomSheet<String>(
          context: context,
          showDragHandle: true,
          useSafeArea: true,
          builder: (context) => ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
            children: [
              Text(
                '/${command.name} ${arg.name}',
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 8),
              if (!arg.required)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  title: const Text('切り替え'),
                  onTap: () => Navigator.pop(context, 'toggle'),
                ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('ON'),
                onTap: () => Navigator.pop(context, 'true'),
              ),
              ListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('OFF'),
                onTap: () => Navigator.pop(context, 'false'),
              ),
            ],
          ),
        );
        if (picked == null) return null;
        if (picked == 'toggle') return const {};
        return {arg.name: picked == 'true'};
      }
    }

    final controllers = {
      for (final arg in command.args) arg.name: TextEditingController(),
    };
    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('/${command.name}'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final arg in command.args)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: TextField(
                    controller: controllers[arg.name],
                    decoration: InputDecoration(
                      labelText: arg.required ? '${arg.name} *' : arg.name,
                      helperText: arg.values.isEmpty
                          ? null
                          : arg.values.join(', '),
                    ),
                  ),
                ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('キャンセル'),
          ),
          FilledButton(
            onPressed: () {
              final values = <String, dynamic>{};
              for (final arg in command.args) {
                final value = controllers[arg.name]?.text.trim() ?? '';
                if (value.isNotEmpty) values[arg.name] = value;
              }
              Navigator.pop(context, values);
            },
            child: const Text('実行'),
          ),
        ],
      ),
    );
    for (final controller in controllers.values) {
      controller.dispose();
    }
    return result;
  }

  Future<void> _executePcCommand(
    PcCommandItem command, {
    Map<String, dynamic> args = const {},
  }) async {
    final space = _activeSpace();
    final connection = space?.pcConnection;
    if (connection == null || !connection.isConfigured) {
      _promptPcConfigure();
      return;
    }
    final client = PcCatalogClient();
    try {
      final result = await client.executeCommand(
        connection,
        command: command.name.isNotEmpty ? command.name : command.id,
        args: args,
        conversationId: _activePcSnapshot?.locator.conversationId,
        mode: _pcMode,
      );
      await _handlePcCommandResult(command, result, parsedArgs: args);
    } catch (e) {
      _showSnack('PC commandに失敗しました: $e');
    } finally {
      client.close();
    }
  }

  Future<void> _handlePcCommandResult(
    PcCommandItem command,
    PcCommandExecuteResult result, {
    Map<String, dynamic> parsedArgs = const {},
  }) async {
    if (result.requiresApproval) {
      _showSnack(result.message.isNotEmpty ? result.message : '承認が必要です');
      unawaited(_openPcApprovals());
      return;
    }
    if (command.isModelCommand) {
      if (result.action == 'open_model_picker') {
        await _openModelPicker();
        return;
      }
      if (result.action == 'show_model_candidates') {
        await _openModelCandidates(result.candidates);
        return;
      }
      final selected = result.selectedModel?.effectiveProfileId;
      if (selected != null && selected.isNotEmpty) {
        setState(() => _selectedPcModel = selected);
      }
      await _loadPcCatalogForActiveSpace();
    }
    final action = result.action.isNotEmpty
        ? result.action
        : command.execution['action'] as String? ?? '';
    if (action.isNotEmpty) {
      await _runPcFrontendAction(
        action,
        command,
        result.args.isEmpty ? parsedArgs : result.args,
      );
    }
    if (command.execution['type'] == 'rumi_function') {
      await _loadPcCatalogForActiveSpace();
    }
    if (result.message.isNotEmpty) {
      _showSnack(result.message);
    }
  }

  Future<void> _openModelCandidates(List<PcModelCandidate> candidates) async {
    if (!mounted) return;
    if (candidates.isEmpty) {
      _showSnack('一致するモデルがありません');
      return;
    }
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      useSafeArea: true,
      builder: (context) => ListView(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
        children: [
          Text('候補モデル', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          for (final candidate in candidates)
            ListTile(
              contentPadding: EdgeInsets.zero,
              enabled: candidate.configured,
              title: Text(candidate.displayLabel),
              subtitle: Text(
                [
                  candidate.providerDisplayName.isNotEmpty
                      ? candidate.providerDisplayName
                      : candidate.providerId,
                  candidate.modelId,
                ].where((part) => part.isNotEmpty).join(' · '),
              ),
              onTap: candidate.configured
                  ? () {
                      Navigator.of(context).pop();
                      unawaited(_setPcModel(candidate.effectiveProfileId));
                    }
                  : null,
            ),
        ],
      ),
    );
  }

  Future<void> _runPcFrontendAction(
    String action,
    PcCommandItem command,
    Map<String, dynamic> args,
  ) async {
    switch (action) {
      case 'open_command_help':
        await _openComposerOptions();
        return;
      case 'open_model_picker':
        await _openModelPicker();
        return;
      case 'set_fast_mode':
        final candidate = _fastPcProfile();
        if (candidate == null) {
          _showSnack('fast対応モデルが見つかりません');
          return;
        }
        await _setPcModel(candidate.effectiveProfileId);
        if (candidate.supportsThinking) {
          await _setPcThinkingLevel('low');
        }
        return;
      case 'set_price_mode':
        final tier = '${args['tier'] ?? 'low'}'.toLowerCase() == 'high'
            ? 'high'
            : 'low';
        final candidate = _pricePcProfile(tier);
        if (candidate == null) {
          _showSnack('price=$tier の候補が見つかりません');
          return;
        }
        await _setPcModel(candidate.effectiveProfileId);
        return;
      case 'new_conversation':
        await _newChat();
        return;
      case 'clear_composer_state':
        _showSnack('入力をクリアしました');
        return;
      case 'set_mode_coding':
        setState(() => _pcMode = _pcMode == 'coding' ? 'agent' : 'coding');
        _showSnack('PC mode: $_pcMode');
        return;
      case 'set_mode_chat':
        setState(() => _pcMode = 'chat');
        _showSnack('PC mode: chat');
        return;
      case 'set_mode_agent':
        setState(() => _pcMode = 'agent');
        _showSnack('PC mode: agent');
        return;
      case 'toggle_yolo':
        setState(
          () => _pcYoloMode = _parseCommandBool(args['enabled'], !_pcYoloMode),
        );
        _showSnack('Yolo: ${_pcYoloMode ? "on" : "off"}');
        return;
      case 'toggle_ultra_yolo':
        setState(() {
          _pcUltraYoloMode = _parseCommandBool(
            args['enabled'],
            !_pcUltraYoloMode,
          );
          if (_pcUltraYoloMode) _pcYoloMode = true;
        });
        _showSnack('Ultra Yolo: ${_pcUltraYoloMode ? "on" : "off"}');
        return;
      case 'show_status':
        _showSnack(
          'mode=$_pcMode model=${_activeModelLabel()} thinking=$_pcThinkingLevel deepthink=${_pcDeepthinkEnabled ? "on" : "off"}',
        );
        return;
      case 'open_settings':
        _openSettings();
        return;
      default:
        _showSnack('/${command.name} はPC commandとして取得済みです');
    }
  }

  ProfileEntry? _fastPcProfile() {
    final profiles = _pcCatalog?.selectableProfiles ?? const <ProfileEntry>[];
    for (final profile in profiles) {
      if (!profile.configured && !profile.local) continue;
      if (profile.speedTier == 'fast' ||
          profile.capabilityTags.contains('fast')) {
        return profile;
      }
    }
    return profiles.isNotEmpty ? profiles.first : null;
  }

  ProfileEntry? _pricePcProfile(String tier) {
    final profiles = _pcCatalog?.selectableProfiles ?? const <ProfileEntry>[];
    final matches = profiles.where((profile) {
      if (!profile.configured && !profile.local) return false;
      return profile.costTier == tier;
    }).toList();
    if (matches.isNotEmpty) return matches.first;
    return profiles.isNotEmpty ? profiles.first : null;
  }

  bool _parseCommandBool(Object? value, bool fallback) {
    if (value == null || value == '') return fallback;
    if (value is bool) return value;
    if (value is num) return value != 0;
    final normalized = value.toString().trim().toLowerCase();
    if (['false', '0', 'off', 'no', 'disabled'].contains(normalized)) {
      return false;
    }
    if (['true', '1', 'on', 'yes', 'enabled'].contains(normalized)) {
      return true;
    }
    return fallback;
  }

  IconData _iconForCommand(PcCommandItem command) {
    switch (command.category) {
      case 'model':
        return Icons.model_training_outlined;
      case 'mode':
        return Icons.tune_outlined;
      case 'coding':
        return Icons.code_outlined;
      case 'tools':
        return Icons.construction_outlined;
      case 'settings':
        return Icons.settings_outlined;
      default:
        return Icons.bolt_outlined;
    }
  }

  void _showSnack(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), duration: const Duration(seconds: 2)),
    );
  }

  PcConnection? _activeApprovalConnection() {
    final connection = _activeSpace()?.pcConnection;
    if (connection == null || !connection.canApprove) return null;
    return connection;
  }

  Future<void> _openPcApprovals() async {
    final connection = _activeApprovalConnection();
    if (connection == null) {
      _showSnack('PC承認tokenがありません');
      return;
    }
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      useSafeArea: true,
      builder: (sheetContext) => _PcApprovalsSheet(
        connection: connection,
        deviceStore: widget.deviceStore,
        onSnack: _showSnack,
      ),
    );
  }

  String _activeSpaceHeaderLabel() {
    final space = _activeSpace();
    if (space == null || space.isLocal) return 'このスマホ';
    return space.isOffline ? 'PC オフライン' : 'PC';
  }

  String _activeModelId() {
    if (_activeSpaceIsPc) {
      final selected = (_selectedPcModel ?? '').trim();
      if (selected.isNotEmpty) return selected;
      final runtime = _pcCatalog?.runtime.preferredModel.trim() ?? '';
      if (runtime.isNotEmpty) return runtime;
      return '';
    }
    return _apiConfig?.model.trim().isNotEmpty == true
        ? _apiConfig!.model.trim()
        : ApiConfig.defaults.model;
  }

  String _activeModelLabel() {
    final modelId = _activeModelId();
    if (_activeSpaceIsPc) {
      if (modelId.isEmpty) return 'PC既定モデル';
      return _pcCatalog?.labelForProfile(modelId) ?? modelId;
    }
    final label = _apiConfig?.label.trim() ?? '';
    if (label.isNotEmpty && label != modelId) return '$label · $modelId';
    return modelId;
  }

  ProfileEntry? _activePcProfile() {
    final modelId = _activeModelId();
    if (!_activeSpaceIsPc || modelId.isEmpty) return null;
    return _pcCatalog?.profileById(modelId);
  }

  IconData _spaceIcon(Space? space) {
    if (space == null || space.isLocal) return Icons.phone_android;
    return space.isOffline
        ? Icons.desktop_access_disabled
        : Icons.desktop_windows;
  }

  Widget _buildSpaceSwitcherSubtitle(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final child = Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Flexible(
          child: Text(
            _activeSpaceHeaderLabel(),
            style: TextStyle(
              fontSize: 11,
              fontWeight: FontWeight.w500,
              color: scheme.onSurfaceVariant,
            ),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
        ),
        if (_spaces.length > 1) ...[
          const SizedBox(width: 2),
          Icon(Icons.expand_more, size: 14, color: scheme.onSurfaceVariant),
        ],
      ],
    );
    if (_spaces.length <= 1) return child;
    return PopupMenuButton<String>(
      tooltip: '接続先を切り替え',
      initialValue: _activeSpaceId,
      padding: EdgeInsets.zero,
      onSelected: (spaceId) {
        unawaited(_selectSpace(spaceId));
      },
      itemBuilder: (context) => [
        for (final space in _spaces)
          CheckedPopupMenuItem<String>(
            value: space.id,
            checked: space.id == _activeSpaceId,
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(_spaceIcon(space), size: 18),
                const SizedBox(width: 8),
                Text(space.label),
                if (space.isOffline) ...[
                  const SizedBox(width: 8),
                  Text(
                    'オフライン',
                    style: TextStyle(fontSize: 12, color: scheme.error),
                  ),
                ],
              ],
            ),
          ),
      ],
      child: child,
    );
  }

  Widget _buildModelSwitcherSubtitle(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return InkWell(
      borderRadius: BorderRadius.circular(6),
      onTap: () => unawaited(_openModelPicker()),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Flexible(
              child: Text(
                _activeModelLabel(),
                style: TextStyle(
                  fontSize: 17,
                  fontWeight: FontWeight.w700,
                  color: scheme.onSurface,
                ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
              ),
            ),
            const SizedBox(width: 2),
            Icon(Icons.expand_more, size: 16, color: scheme.onSurfaceVariant),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<void>(
      future: _initFuture,
      builder: (context, snapshot) {
        return Scaffold(
          drawer: Drawer(
            width: 320,
            child: ChatDrawer(
              spaces: _spaces,
              activeSpaceId: _activeSpaceId,
              conversations: widget.store.conversations,
              pcConversations: _pcConversations,
              loadingPc: _loadingPc,
              activeId: _displayActiveId(),
              onNewChat: () {
                Navigator.of(context).pop();
                _newChat();
              },
              onSelectSpace: (spaceId) {
                Navigator.of(context).pop();
                _selectSpace(spaceId);
              },
              onSelect: (id) {
                Navigator.of(context).pop();
                _select(id);
              },
              onDelete: _delete,
              onRename: _rename,
              onPin: _pin,
              onReconnectSpace: () {
                Navigator.of(context).pop();
                _reconnectActiveSpace();
              },
              onContinueOffline: () {
                Navigator.of(context).pop();
                _continuePcConversationLocally();
              },
              onOpenSettings: () {
                Navigator.of(context).pop();
                _openSettings();
              },
            ),
          ),
          appBar: AppBar(
            leading: Builder(
              builder: (context) => IconButton(
                tooltip: 'チャット一覧',
                icon: const Icon(Icons.menu),
                onPressed: () => Scaffold.of(context).openDrawer(),
              ),
            ),
            title: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _buildModelSwitcherSubtitle(context),
                const SizedBox(height: 1),
                _buildSpaceSwitcherSubtitle(context),
              ],
            ),
            actions: [
              if (_activeApprovalConnection() != null)
                IconButton(
                  tooltip: 'PC承認',
                  icon: const Icon(Icons.shield_outlined),
                  onPressed: _openPcApprovals,
                ),
              IconButton(
                tooltip: '新規チャット',
                icon: const DefaultspackActionIcon(
                  kind: DefaultspackActionIconKind.newChat,
                ),
                onPressed: _newChat,
              ),
            ],
          ),
          body: _buildBody(snapshot),
        );
      },
    );
  }

  Widget _buildBody(AsyncSnapshot<void> snapshot) {
    final content = _buildBodyContent(snapshot);
    if (_localStoreMessage == null || _activeSpaceIsPc) return content;
    return Column(
      children: [
        _LocalStoreBanner(
          message: _localStoreMessage!,
          recoveryAvailable: widget.store.recoveryAvailable,
          onRetry: _retryLocalStoreLoad,
          onRecover: _acceptLocalStoreRecovery,
        ),
        Expanded(child: content),
      ],
    );
  }

  Widget _buildBodyContent(AsyncSnapshot<void> snapshot) {
    if (snapshot.connectionState != ConnectionState.done) {
      return const Center(child: CircularProgressIndicator());
    }
    final convo = _displayConversation();
    if (convo == null || convo.messages.isEmpty) {
      return _EmptyState(
        onSuggest: _send,
        onAdd: _openComposerOptions,
        busy: _busy,
      );
    }
    return Column(
      children: [
        Expanded(
          child: ListView.builder(
            controller: _scrollController,
            padding: const EdgeInsets.symmetric(vertical: 12),
            itemCount: convo.messages.length,
            itemBuilder: (context, index) {
              final message = convo.messages[index];
              final activity = _activityForMessage(message.id);
              return Column(
                key: ValueKey('message-block:${message.id}'),
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  MessageView(message: message),
                  if (activity.isNotEmpty)
                    _RunActivityList(
                      events: activity,
                      onApprovalAction: _openPcApprovals,
                    ),
                ],
              );
            },
          ),
        ),
        ComposerBar(
          onSend: _send,
          onStop: _stop,
          onAdd: _openComposerOptions,
          busy: _streaming,
        ),
      ],
    );
  }
}

class _LocalStoreBanner extends StatelessWidget {
  const _LocalStoreBanner({
    required this.message,
    required this.recoveryAvailable,
    required this.onRetry,
    required this.onRecover,
  });

  final String message;
  final bool recoveryAvailable;
  final VoidCallback onRetry;
  final VoidCallback onRecover;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      container: true,
      liveRegion: true,
      label: 'チャット保存エラー。$message',
      child: Container(
        width: double.infinity,
        color: scheme.errorContainer,
        padding: const EdgeInsets.fromLTRB(16, 10, 8, 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Icon(Icons.sync_problem, color: scheme.onErrorContainer),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                message,
                style: TextStyle(color: scheme.onErrorContainer),
              ),
            ),
            if (recoveryAvailable)
              TextButton(onPressed: onRecover, child: const Text('復元'))
            else
              TextButton(onPressed: onRetry, child: const Text('再読み込み')),
          ],
        ),
      ),
    );
  }
}

class _RunActivityList extends StatelessWidget {
  const _RunActivityList({
    required this.events,
    required this.onApprovalAction,
  });

  final List<ChatEvent> events;
  final VoidCallback onApprovalAction;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 0, 52, 6),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 520),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (final event in events)
              switch (event) {
                ChatStatusEvent() => _StatusActivityCard(event: event),
                ToolCallEvent() => ToolActivityCard(event: event),
                ApprovalEvent() => ApprovalCard(
                  event: event,
                  onApprove: onApprovalAction,
                  onDeny: onApprovalAction,
                ),
                _ => const SizedBox.shrink(),
              },
          ],
        ),
      ),
    );
  }
}

class _StatusActivityCard extends StatelessWidget {
  const _StatusActivityCard({required this.event});

  final ChatStatusEvent event;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHighest.withValues(alpha: 0.38),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: scheme.outlineVariant.withValues(alpha: 0.6)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(
            width: 14,
            height: 14,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: scheme.onSurfaceVariant,
            ),
          ),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              event.message,
              style: theme.textTheme.bodySmall?.copyWith(
                color: scheme.onSurfaceVariant,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _PcApprovalsSheet extends StatefulWidget {
  const _PcApprovalsSheet({
    required this.connection,
    required this.deviceStore,
    required this.onSnack,
  });

  final PcConnection connection;
  final MobileDeviceStore deviceStore;
  final ValueChanged<String> onSnack;

  @override
  State<_PcApprovalsSheet> createState() => _PcApprovalsSheetState();
}

class _PcApprovalsSheetState extends State<_PcApprovalsSheet> {
  late final PcApprovalClient _client;
  late Future<List<AuthorityRequestItem>> _future;
  String _busyRequestId = '';

  @override
  void initState() {
    super.initState();
    _client = PcApprovalClient(deviceStore: widget.deviceStore);
    _future = _client.listPending(widget.connection);
  }

  @override
  void dispose() {
    _client.close();
    super.dispose();
  }

  void _reload() {
    setState(() => _future = _client.listPending(widget.connection));
  }

  Future<void> _approve(AuthorityRequestItem request) async {
    setState(() => _busyRequestId = request.requestId);
    try {
      await _client.approve(widget.connection, request);
      widget.onSnack('PC承認を送信しました');
      _reload();
    } catch (e) {
      widget.onSnack('PC承認に失敗しました: $e');
    } finally {
      if (mounted) setState(() => _busyRequestId = '');
    }
  }

  Future<void> _deny(AuthorityRequestItem request) async {
    setState(() => _busyRequestId = request.requestId);
    try {
      await _client.deny(
        widget.connection,
        request,
        reason: 'denied from mobile',
      );
      widget.onSnack('PC承認を拒否しました');
      _reload();
    } catch (e) {
      widget.onSnack('PC拒否に失敗しました: $e');
    } finally {
      if (mounted) setState(() => _busyRequestId = '');
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return FutureBuilder<List<AuthorityRequestItem>>(
      future: _future,
      builder: (context, snapshot) {
        final requests = snapshot.data ?? const <AuthorityRequestItem>[];
        return ListView(
          shrinkWrap: true,
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 20),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text('PC承認', style: theme.textTheme.titleMedium),
                ),
                IconButton(
                  tooltip: '再読み込み',
                  onPressed: snapshot.connectionState == ConnectionState.waiting
                      ? null
                      : _reload,
                  icon: const Icon(Icons.refresh_outlined),
                ),
              ],
            ),
            const SizedBox(height: 8),
            if (snapshot.connectionState == ConnectionState.waiting)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 24),
                child: Center(child: CircularProgressIndicator()),
              )
            else if (snapshot.hasError)
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.error_outline),
                title: const Text('承認一覧を取得できませんでした'),
                subtitle: Text('${snapshot.error}'),
              )
            else if (requests.isEmpty)
              const ListTile(
                contentPadding: EdgeInsets.zero,
                leading: Icon(Icons.verified_user_outlined),
                title: Text('保留中の承認はありません'),
              )
            else
              for (final request in requests)
                _AuthorityRequestTile(
                  request: request,
                  busy: _busyRequestId == request.requestId,
                  onApprove: () => unawaited(_approve(request)),
                  onDeny: () => unawaited(_deny(request)),
                ),
          ],
        );
      },
    );
  }
}

class _AuthorityRequestTile extends StatelessWidget {
  const _AuthorityRequestTile({
    required this.request,
    required this.busy,
    required this.onApprove,
    required this.onDeny,
  });

  final AuthorityRequestItem request;
  final bool busy;
  final VoidCallback onApprove;
  final VoidCallback onDeny;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: DecoratedBox(
        decoration: BoxDecoration(
          border: Border.all(color: Theme.of(context).dividerColor),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    Icons.security_outlined,
                    size: 18,
                    color: scheme.primary,
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      request.title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.titleSmall,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              Text(
                request.summary,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodySmall,
              ),
              const SizedBox(height: 10),
              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  TextButton.icon(
                    icon: const Icon(Icons.close, size: 16),
                    label: const Text('拒否'),
                    onPressed: busy ? null : onDeny,
                  ),
                  const SizedBox(width: 8),
                  FilledButton.icon(
                    icon: busy
                        ? const SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.check, size: 16),
                    label: const Text('承認'),
                    onPressed: busy ? null : onApprove,
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ParsedPcCommand {
  const _ParsedPcCommand({required this.command, required this.args});

  final PcCommandItem command;
  final Map<String, dynamic> args;
}

_ParsedPcCommand? _parsePcSlashCommand(
  String input,
  List<PcCommandItem> commands,
) {
  final trimmed = input.trim();
  if (!trimmed.startsWith('/') || trimmed.startsWith('//')) return null;
  final body = trimmed.substring(1).trim();
  if (body.isEmpty) return null;
  final normalizedBody = body.toLowerCase();

  PcCommandItem? matchedCommand;
  var matchedName = '';
  for (final command in commands) {
    for (final name in _pcCommandNames(command)) {
      final candidate = _matchPcCommandName(normalizedBody, name);
      if (candidate == null || candidate.length <= matchedName.length) {
        continue;
      }
      matchedCommand = command;
      matchedName = candidate;
    }
  }
  if (matchedCommand == null) return null;

  final rest = body.substring(matchedName.length).trim();
  final args = <String, dynamic>{};
  final specs = matchedCommand.args;
  if (specs.length == 1 && rest.isNotEmpty) {
    args[specs.first.name] = rest;
  } else if (specs.length > 1 && rest.isNotEmpty) {
    final tokens = rest.split(RegExp(r'\s+'));
    for (var i = 0; i < specs.length; i++) {
      final spec = specs[i];
      if (i == specs.length - 1) {
        final remainder = tokens.skip(i).join(' ');
        if (remainder.isNotEmpty) args[spec.name] = remainder;
      } else if (i < tokens.length && tokens[i].isNotEmpty) {
        args[spec.name] = tokens[i];
      }
    }
  }
  return _ParsedPcCommand(command: matchedCommand, args: args);
}

List<String> _pcCommandNames(PcCommandItem command) {
  final names = <String>{command.id, command.name, ...command.aliases};
  final list = names
      .map((value) => value.trim().toLowerCase())
      .where((value) => value.isNotEmpty)
      .toList();
  list.sort((left, right) => right.length.compareTo(left.length));
  return list;
}

String? _matchPcCommandName(String body, String candidate) {
  final direct = RegExp(
    '^${RegExp.escape(candidate)}(?:\\s+|\$)',
  ).firstMatch(body);
  if (direct != null) return direct.group(0)?.trimRight();

  final parts = candidate.split(RegExp(r'[\s_-]+')).where((p) => p.isNotEmpty);
  if (parts.length < 2) return null;
  final pattern = parts.map(RegExp.escape).join(r'[\s_-]+');
  final flexible = RegExp('^$pattern(?:\\s+|\$)').firstMatch(body);
  return flexible?.group(0)?.trimRight();
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({
    required this.onSuggest,
    required this.onAdd,
    required this.busy,
  });
  final ValueChanged<String> onSuggest;
  final VoidCallback onAdd;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      children: [
        Expanded(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    'ようこそ',
                    textAlign: TextAlign.center,
                    style: theme.textTheme.headlineSmall?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
        ComposerBar(onSend: onSuggest, onStop: () {}, onAdd: onAdd, busy: busy),
      ],
    );
  }
}
