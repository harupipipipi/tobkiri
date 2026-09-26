import 'dart:async';

import 'package:flutter/material.dart';

import 'conversation_timeline.dart';
import 'mobile_chat_api.dart';

/// Canonical mobile chat backed only by Tobkiri's finite mobile API routes.
class MobileChatScreen extends StatefulWidget {
  const MobileChatScreen({
    super.key,
    required this.baseUrl,
    required this.bearerToken,
    this.gateway,
  });

  final String baseUrl;
  final String bearerToken;
  final MobileChatGateway? gateway;

  @override
  State<MobileChatScreen> createState() => _MobileChatScreenState();
}

class _MobileChatScreenState extends State<MobileChatScreen> {
  static const _initialMessageCount = 40;
  static const _olderMessageBatch = 30;

  final _composerController = TextEditingController();
  late final MobileChatGateway _gateway;
  List<MobileConversationSummary> _conversations = const [];
  List<MobileChatMessage> _messages = const [];
  final List<MobileChatActivity> _activity = [];
  String? _conversationId;
  String _title = 'Tobkiri Chat';
  int _conversationRevision = 0;
  int _visibleStart = 0;
  bool _loading = true;
  bool _streaming = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _gateway = widget.gateway ??
        HttpMobileChatGateway(
          baseUrl: widget.baseUrl,
          bearerToken: widget.bearerToken,
        );
    unawaited(_load());
  }

  @override
  void dispose() {
    _composerController.dispose();
    _gateway.close();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final conversations = await _gateway.listConversations();
      if (!mounted) {
        return;
      }
      setState(() {
        _conversations = conversations;
        _loading = false;
      });
      if (conversations.isNotEmpty) {
        await _openConversation(conversations.first.id);
      }
    } on Object catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _error = '$error';
      });
    }
  }

  Future<void> _openConversation(String conversationId) async {
    if (_streaming) {
      return;
    }
    setState(() {
      _loading = true;
      _error = null;
      _activity.clear();
    });
    try {
      final snapshot = await _gateway.getConversation(conversationId);
      if (!mounted) {
        return;
      }
      setState(() {
        _conversationId = snapshot.id;
        _title = snapshot.title;
        _messages = snapshot.messages;
        _conversationRevision = snapshot.revision;
        _visibleStart = (_messages.length - _initialMessageCount).clamp(
          0,
          _messages.length,
        );
        _loading = false;
      });
    } on Object catch (error) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _error = '$error';
      });
    }
  }

  Future<String?> _ensureConversation() async {
    if (_conversationId != null) {
      return _conversationId;
    }
    try {
      final id = await _gateway.createConversation();
      if (!mounted) {
        return null;
      }
      setState(() {
        _conversationId = id;
        _title = '新しいチャット';
        _messages = const [];
        _visibleStart = 0;
        _conversationRevision = 0;
      });
      unawaited(_refreshConversationList());
      return id;
    } on Object catch (error) {
      if (mounted) {
        setState(() => _error = '$error');
      }
      return null;
    }
  }

  Future<void> _newConversation() async {
    if (_streaming) {
      return;
    }
    setState(() {
      _conversationId = null;
      _title = '新しいチャット';
      _messages = const [];
      _activity.clear();
      _visibleStart = 0;
      _conversationRevision = 0;
      _error = null;
    });
  }

  Future<void> _refreshConversationList() async {
    try {
      final conversations = await _gateway.listConversations();
      if (mounted) {
        setState(() => _conversations = conversations);
      }
    } on Object {
      // The active conversation remains usable if the navigation refresh fails.
    }
  }

  void _loadOlderMessages() {
    if (_visibleStart == 0) {
      return;
    }
    setState(() {
      _visibleStart = (_visibleStart - _olderMessageBatch).clamp(
        0,
        _messages.length,
      );
    });
  }

  Future<void> _send() async {
    final text = _composerController.text.trim();
    if (text.isEmpty || _streaming) {
      return;
    }
    final conversationId = await _ensureConversation();
    if (conversationId == null || !mounted) {
      return;
    }
    final stamp = DateTime.now().microsecondsSinceEpoch;
    final clientMessageId = 'mobile-user-$stamp';
    final assistantMessageId = 'mobile-assistant-$stamp';
    _composerController.clear();
    setState(() {
      _streaming = true;
      _error = null;
      _messages = [
        ..._messages,
        MobileChatMessage(id: clientMessageId, role: 'user', content: text),
        MobileChatMessage(
          id: assistantMessageId,
          role: 'assistant',
          content: '',
          pending: true,
        ),
      ];
    });

    try {
      await for (final event in _gateway.streamMessage(
        conversationId: conversationId,
        text: text,
        clientMessageId: clientMessageId,
        expectedRevision: _conversationRevision,
      )) {
        if (!mounted) {
          return;
        }
        _applyStreamEvent(event, assistantMessageId);
      }
    } finally {
      if (mounted) {
        await _synchronizeConversation(conversationId);
      }
      if (mounted) {
        setState(() {
          _streaming = false;
        });
        unawaited(_refreshConversationList());
      }
    }
  }

  Future<void> _synchronizeConversation(String conversationId) async {
    try {
      final snapshot = await _gateway.getConversation(conversationId);
      if (!mounted ||
          snapshot.id != _conversationId ||
          snapshot.revision <= _conversationRevision) {
        return;
      }
      setState(() {
        _messages = snapshot.messages;
        _conversationRevision = snapshot.revision;
      });
    } on Object {
      // Preserve the streamed optimistic state when the follow-up read fails.
    }
  }

  void _applyStreamEvent(
    MobileChatStreamEvent event,
    String assistantMessageId,
  ) {
    setState(() {
      switch (event) {
        case MobileChatDelta(:final content):
          _replaceMessage(
            assistantMessageId,
            (message) => message.copyWith(content: content),
          );
        case MobileChatActivity():
          final existing = _activity.indexWhere((item) => item.id == event.id);
          if (existing < 0) {
            _activity.add(event);
          } else {
            _activity[existing] = event;
          }
        case MobileChatCompleted():
          _replaceMessage(
            assistantMessageId,
            (message) => message.copyWith(pending: false),
          );
          _conversationRevision++;
          _activity.removeWhere((item) => item.pending);
        case MobileChatFailed(:final message):
          _replaceMessage(
            assistantMessageId,
            (current) => current.copyWith(pending: false, error: true),
          );
          _activity.add(
            MobileChatActivity(
              id: 'error-${DateTime.now().microsecondsSinceEpoch}',
              label: message,
              kind: 'error',
            ),
          );
      }
    });
  }

  void _replaceMessage(
    String id,
    MobileChatMessage Function(MobileChatMessage message) replace,
  ) {
    _messages = _messages
        .map((message) => message.id == id ? replace(message) : message)
        .toList(growable: false);
  }

  Future<void> _stop() async {
    final conversationId = _conversationId;
    if (conversationId == null || !_streaming) {
      return;
    }
    await _gateway.stop(conversationId);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text(_title, overflow: TextOverflow.ellipsis),
        actions: [
          IconButton(
            tooltip: 'チャット一覧',
            onPressed: _showConversationList,
            icon: const Icon(Icons.history),
          ),
          IconButton(
            tooltip: '新規チャット',
            onPressed: _streaming ? null : _newConversation,
            icon: const Icon(Icons.add_comment_outlined),
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          children: [
            if (_error != null)
              MaterialBanner(
                content: Text(_error!),
                actions: [
                  TextButton(
                    onPressed: () => setState(() => _error = null),
                    child: const Text('閉じる'),
                  ),
                ],
              ),
            Expanded(child: _buildConversation()),
            _Composer(
              controller: _composerController,
              streaming: _streaming,
              onSend: _send,
              onStop: _stop,
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildConversation() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_messages.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.forum_outlined, size: 42),
              const SizedBox(height: 12),
              const Text('Tobkiriで会話を始めましょう'),
              const SizedBox(height: 8),
              Text(
                '新しいメッセージは画面下部に表示されます。',
                style: Theme.of(context).textTheme.bodySmall,
                textAlign: TextAlign.center,
              ),
            ],
          ),
        ),
      );
    }

    final visibleMessages = _messages.skip(_visibleStart);
    final entries = <ConversationTimelineItem>[
      if (_visibleStart > 0)
        ConversationTimelineItem(
          id: 'load-older',
          child: Center(
            child: TextButton.icon(
              onPressed: _loadOlderMessages,
              icon: const Icon(Icons.expand_less),
              label: Text('以前のメッセージ（$_visibleStart件）'),
            ),
          ),
        ),
      for (final message in visibleMessages)
        ConversationTimelineItem(
          id: 'message:${message.id}',
          revision: Object.hash(
            message.content,
            message.pending,
            message.error,
          ),
          child: _MessageCard(message: message),
        ),
      for (final activity in _activity)
        ConversationTimelineItem(
          id: 'activity:${activity.id}',
          revision: Object.hash(activity.label, activity.pending),
          isActivity: true,
          child: _ActivityCard(activity: activity),
        ),
    ];
    return ConversationTimeline(
      items: entries,
      padding: const EdgeInsets.symmetric(vertical: 12),
    );
  }

  Future<void> _showConversationList() async {
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) {
        return SafeArea(
          child: ListView(
            shrinkWrap: true,
            children: [
              const ListTile(title: Text('チャット一覧')),
              if (_conversations.isEmpty)
                const ListTile(title: Text('会話はまだありません')),
              for (final conversation in _conversations)
                ListTile(
                  selected: conversation.id == _conversationId,
                  title: Text(conversation.title),
                  subtitle: Text('${conversation.messageCount}件のメッセージ'),
                  onTap: () {
                    Navigator.of(sheetContext).pop();
                    unawaited(_openConversation(conversation.id));
                  },
                ),
            ],
          ),
        );
      },
    );
  }
}

class _MessageCard extends StatelessWidget {
  const _MessageCard({required this.message});

  final MobileChatMessage message;

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == 'user';
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      label: isUser ? 'あなたのメッセージ' : 'Tobkiriのメッセージ',
      child: Align(
        alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
        child: Container(
          constraints: const BoxConstraints(maxWidth: 620),
          margin: EdgeInsets.fromLTRB(isUser ? 52 : 12, 6, isUser ? 12 : 52, 6),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: message.error
                ? scheme.errorContainer
                : isUser
                    ? scheme.primaryContainer
                    : scheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(14),
          ),
          child: message.content.isEmpty && message.pending
              ? const Text('処理中…')
              : SelectableText(message.content),
        ),
      ),
    );
  }
}

class _ActivityCard extends StatelessWidget {
  const _ActivityCard({required this.activity});

  final MobileChatActivity activity;

  @override
  Widget build(BuildContext context) {
    final icon = switch (activity.kind) {
      'tool' => Icons.build_outlined,
      'approval' => Icons.approval_outlined,
      'error' => Icons.error_outline,
      _ => Icons.pending_outlined,
    };
    return Semantics(
      liveRegion: true,
      label: 'アクティビティ: ${activity.label}',
      child: ListTile(
        dense: true,
        leading: Icon(icon),
        title: Text(activity.label),
        trailing: activity.pending
            ? const SizedBox.square(
                dimension: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : null,
      ),
    );
  }
}

class _Composer extends StatefulWidget {
  const _Composer({
    required this.controller,
    required this.streaming,
    required this.onSend,
    required this.onStop,
  });

  final TextEditingController controller;
  final bool streaming;
  final Future<void> Function() onSend;
  final Future<void> Function() onStop;

  @override
  State<_Composer> createState() => _ComposerState();
}

class _ComposerState extends State<_Composer> {
  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onChanged);
  }

  @override
  void didUpdateWidget(covariant _Composer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_onChanged);
      widget.controller.addListener(_onChanged);
    }
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onChanged);
    super.dispose();
  }

  void _onChanged() => setState(() {});

  @override
  Widget build(BuildContext context) {
    return Material(
      elevation: 4,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: TextField(
                controller: widget.controller,
                minLines: 1,
                maxLines: 5,
                textInputAction: TextInputAction.newline,
                decoration: const InputDecoration(
                  hintText: 'メッセージを入力',
                  border: OutlineInputBorder(),
                ),
              ),
            ),
            const SizedBox(width: 8),
            IconButton.filled(
              tooltip: widget.streaming ? '生成を停止' : '送信',
              onPressed: widget.streaming
                  ? widget.onStop
                  : widget.controller.text.trim().isEmpty
                      ? null
                      : widget.onSend,
              icon: Icon(
                widget.streaming ? Icons.stop_rounded : Icons.arrow_upward,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
