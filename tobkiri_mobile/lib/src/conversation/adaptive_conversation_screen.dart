import 'dart:async';
import 'dart:math' as math;
import 'dart:ui' show DisplayFeature;

import 'package:flutter/material.dart';

import 'conversation_client.dart';
import 'conversation_models.dart';

typedef ConversationNavigationClientFactory = ConversationNavigationClient
    Function(
  MobileConversationConnection connection,
);

enum ConversationNavigationSize { compact, medium, expanded }

class AdaptiveConversationScreen extends StatefulWidget {
  const AdaptiveConversationScreen({
    super.key,
    this.connectionStore,
    this.clientFactory,
    this.initialConnections,
  });

  final ConversationConnectionStore? connectionStore;
  final ConversationNavigationClientFactory? clientFactory;
  final List<MobileConversationConnection>? initialConnections;

  static ConversationNavigationSize sizeForWidth(double width) {
    if (width < 600) return ConversationNavigationSize.compact;
    if (width < 1024) return ConversationNavigationSize.medium;
    return ConversationNavigationSize.expanded;
  }

  @override
  State<AdaptiveConversationScreen> createState() =>
      _AdaptiveConversationScreenState();
}

class _AdaptiveConversationScreenState
    extends State<AdaptiveConversationScreen> {
  late final ConversationConnectionStore _connectionStore;
  late final ConversationNavigationClientFactory _clientFactory;
  List<MobileConversationConnection> _connections = const [];
  Map<String, bool> _online = const {};
  List<ConversationSummary> _conversations = const [];
  ConversationDetail? _activeConversation;
  String? _activeConnectionId;
  bool _loading = true;
  String? _error;

  MobileConversationConnection? get _activeConnection {
    for (final connection in _connections) {
      if (connection.id == _activeConnectionId) return connection;
    }
    return null;
  }

  @override
  void initState() {
    super.initState();
    _connectionStore =
        widget.connectionStore ?? SecureConversationConnectionStore();
    _clientFactory = widget.clientFactory ??
        (connection) =>
            CanonicalConversationNavigationClient(connection: connection);
    unawaited(_initialize());
  }

  Future<void> _initialize() async {
    try {
      final connections =
          widget.initialConnections ?? await _connectionStore.load();
      if (!mounted) return;
      setState(() {
        _connections = connections;
        _online = {for (final connection in connections) connection.id: true};
        _activeConnectionId = connections.firstOrNull?.id;
        _loading = connections.isNotEmpty;
      });
      if (connections.isNotEmpty) await _refreshConversations();
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = 'Conversation spaces could not be loaded safely.';
      });
    }
  }

  Future<T> _withClient<T>(
    MobileConversationConnection connection,
    Future<T> Function(ConversationNavigationClient client) action,
  ) async {
    final client = _clientFactory(connection);
    try {
      return await action(client);
    } finally {
      client.close();
    }
  }

  Future<void> _refreshConversations() async {
    final connection = _activeConnection;
    if (connection == null) {
      if (mounted) setState(() => _loading = false);
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final conversations = await _withClient(
        connection,
        (client) => client.listConversations(),
      );
      if (!mounted || connection.id != _activeConnectionId) return;
      setState(() {
        _conversations = conversations;
        _activeConversation = null;
        _online = {..._online, connection.id: true};
        _loading = false;
      });
    } catch (_) {
      if (!mounted || connection.id != _activeConnectionId) return;
      setState(() {
        _conversations = const [];
        _activeConversation = null;
        _online = {..._online, connection.id: false};
        _loading = false;
        _error =
            'This conversation space is offline. Try again when it reconnects.';
      });
    }
  }

  Future<void> _selectConnection(String connectionId) async {
    if (connectionId == _activeConnectionId) return;
    setState(() {
      _activeConnectionId = connectionId;
      _conversations = const [];
      _activeConversation = null;
    });
    await _refreshConversations();
  }

  Future<void> _selectConversation(String conversationId) async {
    final connection = _activeConnection;
    if (connection == null) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final detail = await _withClient(
        connection,
        (client) => client.getConversation(conversationId),
      );
      if (!mounted || connection.id != _activeConnectionId) return;
      setState(() {
        _activeConversation = detail;
        _online = {..._online, connection.id: true};
        _loading = false;
      });
    } catch (_) {
      if (!mounted || connection.id != _activeConnectionId) return;
      setState(() {
        _online = {..._online, connection.id: false};
        _loading = false;
        _error = 'The conversation could not be opened.';
      });
    }
  }

  Future<void> _createConversation() async {
    final connection = _activeConnection;
    if (connection == null || _loading) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final detail = await _withClient(
        connection,
        (client) => client.createConversation(),
      );
      if (!mounted || connection.id != _activeConnectionId) return;
      setState(() {
        _conversations = [
          detail.summary,
          ..._conversations.where(
            (conversation) => conversation.id != detail.summary.id,
          ),
        ];
        _activeConversation = detail;
        _online = {..._online, connection.id: true};
        _loading = false;
      });
    } catch (_) {
      if (!mounted || connection.id != _activeConnectionId) return;
      setState(() {
        _online = {..._online, connection.id: false};
        _loading = false;
        _error = 'A new conversation could not be created.';
      });
    }
  }

  Future<void> _addConnection() async {
    final connection = await showModalBottomSheet<MobileConversationConnection>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (context) => const _ConnectionSpaceSheet(),
    );
    if (connection == null || !mounted) return;
    final connections = [
      ..._connections.where((item) => item.id != connection.id),
      connection,
    ];
    try {
      await _connectionStore.saveVerified(connections);
      if (!mounted) return;
      setState(() {
        _connections = connections;
        _activeConnectionId = connection.id;
        _online = {..._online, connection.id: true};
      });
      await _refreshConversations();
    } catch (_) {
      if (!mounted) return;
      setState(
        () => _error = 'The conversation space could not be saved safely.',
      );
    }
  }

  void _closeDrawer(BuildContext context) {
    final scaffold = Scaffold.maybeOf(context);
    if (scaffold?.isDrawerOpen == true) Navigator.of(context).maybePop();
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(
      builder: (context, constraints) {
        final verticalFeature = _verticalSeparatingFeature(
          MediaQuery.displayFeaturesOf(context),
          constraints.biggest,
        );
        final usableWidth =
            constraints.maxWidth - (verticalFeature?.bounds.width ?? 0);
        final size = AdaptiveConversationScreen.sizeForWidth(usableWidth);
        final compact = size == ConversationNavigationSize.compact;
        final navigation = _ConversationNavigationPane(
          size: size,
          connections: _connections,
          activeConnectionId: _activeConnectionId,
          online: _online,
          conversations: _conversations,
          activeConversationId: _activeConversation?.summary.id,
          loading: _loading,
          showNewChat: !compact,
          onNewChat: () => unawaited(_createConversation()),
          onSelectConnection: (id) {
            _closeDrawer(context);
            unawaited(_selectConnection(id));
          },
          onSelectConversation: (id) {
            _closeDrawer(context);
            unawaited(_selectConversation(id));
          },
          onAddConnection: () => unawaited(_addConnection()),
        );
        final body = _ConversationBody(
          connection: _activeConnection,
          detail: _activeConversation,
          error: _error,
          loading: _loading,
          onRetry: () => unawaited(_refreshConversations()),
          onAddConnection: () => unawaited(_addConnection()),
        );

        if (compact) {
          final drawerWidth = math.min(
            360.0,
            math.max(240.0, constraints.maxWidth - 48),
          );
          return Scaffold(
            resizeToAvoidBottomInset: true,
            drawer: Drawer(width: drawerWidth, child: navigation),
            appBar: AppBar(
              title: Text(_activeConnection?.label ?? 'Conversations'),
              actions: [
                IconButton(
                  key: const ValueKey('compact-new-conversation'),
                  tooltip: 'New conversation',
                  onPressed: _activeConnection == null || _loading
                      ? null
                      : () => unawaited(_createConversation()),
                  icon: const Icon(Icons.add_comment_outlined),
                ),
              ],
            ),
            body: SafeArea(child: body),
          );
        }

        var navigationWidth =
            size == ConversationNavigationSize.expanded ? 480.0 : 320.0;
        var hingeWidth = 0.0;
        if (verticalFeature != null &&
            verticalFeature.bounds.left >= 280 &&
            constraints.maxWidth - verticalFeature.bounds.right >= 320) {
          navigationWidth = verticalFeature.bounds.left;
          hingeWidth = verticalFeature.bounds.width;
        }
        return Scaffold(
          resizeToAvoidBottomInset: true,
          body: SafeArea(
            child: Row(
              children: [
                SizedBox(width: navigationWidth, child: navigation),
                if (hingeWidth > 0)
                  SizedBox(
                    key: const ValueKey('display-feature-gap'),
                    width: hingeWidth,
                  )
                else
                  const VerticalDivider(width: 1),
                Expanded(child: body),
              ],
            ),
          ),
        );
      },
    );
  }
}

DisplayFeature? _verticalSeparatingFeature(
  List<DisplayFeature> features,
  Size surface,
) {
  for (final feature in features) {
    if (feature.bounds.width > 0 &&
        feature.bounds.height >= surface.height * 0.8) {
      return feature;
    }
  }
  return null;
}

class _ConversationNavigationPane extends StatelessWidget {
  const _ConversationNavigationPane({
    required this.size,
    required this.connections,
    required this.activeConnectionId,
    required this.online,
    required this.conversations,
    required this.activeConversationId,
    required this.loading,
    required this.showNewChat,
    required this.onNewChat,
    required this.onSelectConnection,
    required this.onSelectConversation,
    required this.onAddConnection,
  });

  final ConversationNavigationSize size;
  final List<MobileConversationConnection> connections;
  final String? activeConnectionId;
  final Map<String, bool> online;
  final List<ConversationSummary> conversations;
  final String? activeConversationId;
  final bool loading;
  final bool showNewChat;
  final VoidCallback onNewChat;
  final ValueChanged<String> onSelectConnection;
  final ValueChanged<String> onSelectConversation;
  final VoidCallback onAddConnection;

  @override
  Widget build(BuildContext context) {
    final spaces = _SpaceList(
      connections: connections,
      activeConnectionId: activeConnectionId,
      online: online,
      onSelect: onSelectConnection,
    );
    final list = _ConversationList(
      conversations: conversations,
      activeConversationId: activeConversationId,
      activeSpaceLabel: connections
          .where((connection) => connection.id == activeConnectionId)
          .firstOrNull
          ?.label,
      loading: loading,
      onSelect: onSelectConversation,
    );

    return Material(
      color: Theme.of(context).colorScheme.surfaceContainerLow,
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 12, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      'Tobkiri',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  IconButton(
                    tooltip: 'Add conversation space',
                    onPressed: onAddConnection,
                    icon: const Icon(Icons.add_link),
                  ),
                ],
              ),
            ),
            if (showNewChat)
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
                child: FilledButton.icon(
                  key: const ValueKey('persistent-new-conversation'),
                  onPressed:
                      activeConnectionId == null || loading ? null : onNewChat,
                  icon: const Icon(Icons.add_comment_outlined),
                  label: const Text('New conversation'),
                  style: FilledButton.styleFrom(
                    minimumSize: const Size.fromHeight(48),
                  ),
                ),
              ),
            if (size == ConversationNavigationSize.expanded)
              Expanded(
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    SizedBox(width: 180, child: spaces),
                    const VerticalDivider(width: 1),
                    Expanded(child: list),
                  ],
                ),
              )
            else ...[
              Flexible(flex: 2, child: spaces),
              const Divider(height: 1),
              Expanded(flex: 5, child: list),
            ],
          ],
        ),
      ),
    );
  }
}

class _SpaceList extends StatelessWidget {
  const _SpaceList({
    required this.connections,
    required this.activeConnectionId,
    required this.online,
    required this.onSelect,
  });

  final List<MobileConversationConnection> connections;
  final String? activeConnectionId;
  final Map<String, bool> online;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    return RadioGroup<String>(
      groupValue: activeConnectionId,
      onChanged: (value) {
        if (value != null) onSelect(value);
      },
      child: Semantics(
        container: true,
        label: 'Conversation spaces',
        child: ListView(
          key: const ValueKey('conversation-space-list'),
          padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 4, 8, 6),
              child: Text(
                'Spaces',
                style: Theme.of(context).textTheme.labelLarge,
              ),
            ),
            if (connections.isEmpty)
              const Padding(
                padding: EdgeInsets.all(12),
                child: Text('Add an approved PC connection to begin.'),
              ),
            for (final connection in connections)
              Semantics(
                selected: connection.id == activeConnectionId,
                label: '${connection.label}, '
                    '${online[connection.id] == false ? 'offline' : 'online'}',
                child: RadioListTile<String>(
                  value: connection.id,
                  selected: connection.id == activeConnectionId,
                  secondary: Icon(
                    online[connection.id] == false
                        ? Icons.cloud_off_outlined
                        : Icons.cloud_done_outlined,
                  ),
                  title: Text(
                    connection.label,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                  subtitle: Text(
                    online[connection.id] == false ? 'Offline' : 'Online',
                  ),
                  contentPadding: const EdgeInsets.symmetric(horizontal: 8),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _ConversationList extends StatelessWidget {
  const _ConversationList({
    required this.conversations,
    required this.activeConversationId,
    required this.activeSpaceLabel,
    required this.loading,
    required this.onSelect,
  });

  final List<ConversationSummary> conversations;
  final String? activeConversationId;
  final String? activeSpaceLabel;
  final bool loading;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    if (loading) return const Center(child: CircularProgressIndicator());
    return Semantics(
      container: true,
      label: 'Conversations in ${activeSpaceLabel ?? 'selected space'}',
      child: ListView(
        key: const ValueKey('conversation-list'),
        padding: const EdgeInsets.fromLTRB(8, 4, 8, 12),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(8, 4, 8, 6),
            child: Text(
              activeSpaceLabel == null
                  ? 'Conversations'
                  : 'Conversations · $activeSpaceLabel',
              style: Theme.of(context).textTheme.labelLarge,
            ),
          ),
          if (conversations.isEmpty)
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text('No conversations in this space.'),
            ),
          for (final conversation in conversations)
            Semantics(
              selected: conversation.id == activeConversationId,
              child: ListTile(
                minVerticalPadding: 12,
                selected: conversation.id == activeConversationId,
                leading: Icon(
                  conversation.pinned
                      ? Icons.push_pin_outlined
                      : Icons.chat_bubble_outline,
                ),
                title: Text(
                  conversation.displayTitle,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
                subtitle: Text(
                  '${conversation.messageCount} messages',
                  maxLines: 1,
                ),
                onTap: () => onSelect(conversation.id),
              ),
            ),
        ],
      ),
    );
  }
}

class _ConversationBody extends StatelessWidget {
  const _ConversationBody({
    required this.connection,
    required this.detail,
    required this.error,
    required this.loading,
    required this.onRetry,
    required this.onAddConnection,
  });

  final MobileConversationConnection? connection;
  final ConversationDetail? detail;
  final String? error;
  final bool loading;
  final VoidCallback onRetry;
  final VoidCallback onAddConnection;

  @override
  Widget build(BuildContext context) {
    if (connection == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.devices_outlined, size: 48),
              const SizedBox(height: 16),
              Text(
                'Connect a conversation space',
                style: Theme.of(context).textTheme.headlineSmall,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              const Text(
                'Use an approved device token limited to chat.read and chat.write.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 20),
              FilledButton.icon(
                onPressed: onAddConnection,
                icon: const Icon(Icons.add_link),
                label: const Text('Add conversation space'),
              ),
            ],
          ),
        ),
      );
    }
    if (error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.cloud_off_outlined, size: 48),
              const SizedBox(height: 12),
              Text(error!, textAlign: TextAlign.center),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                onPressed: loading ? null : onRetry,
                icon: const Icon(Icons.refresh),
                label: const Text('Retry'),
              ),
            ],
          ),
        ),
      );
    }
    if (detail == null) {
      return Center(
        child: Text(
          loading
              ? 'Loading conversations…'
              : 'Choose a conversation from ${connection!.label}.',
          textAlign: TextAlign.center,
        ),
      );
    }
    return CustomScrollView(
      key: const ValueKey('conversation-detail'),
      slivers: [
        SliverAppBar(
          automaticallyImplyLeading: false,
          pinned: true,
          title: Text(detail!.summary.displayTitle),
        ),
        if (detail!.messages.isEmpty)
          const SliverFillRemaining(
            hasScrollBody: false,
            child: Center(child: Text('This conversation is empty.')),
          )
        else
          SliverList.builder(
            itemCount: detail!.messages.length,
            itemBuilder: (context, index) {
              final message = detail!.messages[index];
              final author = message.role == 'user' ? 'You' : 'Tobkiri';
              return Semantics(
                container: true,
                label: '$author message',
                child: ListTile(
                  title: Text(author),
                  subtitle: Text(
                    message.content.trim().isEmpty
                        ? 'No text content'
                        : message.content,
                  ),
                ),
              );
            },
          ),
      ],
    );
  }
}

class _ConnectionSpaceSheet extends StatefulWidget {
  const _ConnectionSpaceSheet();

  @override
  State<_ConnectionSpaceSheet> createState() => _ConnectionSpaceSheetState();
}

class _ConnectionSpaceSheetState extends State<_ConnectionSpaceSheet> {
  final _label = TextEditingController();
  final _url = TextEditingController();
  final _deviceId = TextEditingController();
  final _token = TextEditingController();
  String? _error;

  @override
  void dispose() {
    _label.dispose();
    _url.dispose();
    _deviceId.dispose();
    _token.dispose();
    super.dispose();
  }

  void _save() {
    final uri = Uri.tryParse(_url.text.trim());
    final connection = MobileConversationConnection(
      id: '${_deviceId.text.trim()}@${uri?.host ?? ''}:${uri?.port ?? 0}',
      label: _label.text.trim(),
      baseUrl: _url.text.trim(),
      deviceId: _deviceId.text.trim(),
      token: _token.text.trim(),
      scopes: MobileConversationConnection.requiredScopes,
    );
    if (!connection.isValid) {
      setState(() {
        _error = 'Check the label, PC URL, device ID, and approved token.';
      });
      return;
    }
    Navigator.of(context).pop(connection);
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          24,
          8,
          24,
          24 + MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'Add conversation space',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 8),
              const Text(
                'Only use a paired-device token with exactly chat.read and '
                'chat.write. Tokens are stored in platform secure storage.',
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _label,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(labelText: 'Space name'),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _url,
                keyboardType: TextInputType.url,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(
                  labelText: 'Tobkiri PC URL',
                  hintText: 'https://pc.example:8765',
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _deviceId,
                textInputAction: TextInputAction.next,
                decoration: const InputDecoration(labelText: 'Device ID'),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _token,
                obscureText: true,
                autocorrect: false,
                enableSuggestions: false,
                onSubmitted: (_) => _save(),
                decoration: const InputDecoration(labelText: 'Device token'),
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                Text(
                  _error!,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ],
              const SizedBox(height: 16),
              FilledButton.icon(
                onPressed: _save,
                icon: const Icon(Icons.lock_outline),
                label: const Text('Save securely'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
