import 'dart:convert';

import 'package:uuid/uuid.dart';

import '../platform/platform_services.dart';
import 'chat_models.dart';

const _kConversationsKey = 'rumi_chat.conversations.v1';
const _kActiveConversationKey = 'rumi_chat.active_id.v1';

abstract class ChatKeyValueStorage {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

class PlatformChatStorage implements ChatKeyValueStorage {
  PlatformChatStorage({PlatformPreferences? preferences})
      : _preferences = preferences ?? PlatformPreferences();

  final PlatformPreferences _preferences;

  @override
  Future<String?> read(String key) => _preferences.read(key);

  @override
  Future<void> write(String key, String value) =>
      _preferences.write(key, value);

  @override
  Future<void> delete(String key) => _preferences.delete(key);
}

class ConversationDeletionResult {
  const ConversationDeletionResult({
    required this.conversation,
    required this.wasActive,
    required this.nextActiveId,
  });

  final Conversation conversation;
  final bool wasActive;
  final String? nextActiveId;
}

class ChatStore {
  ChatStore({ChatKeyValueStorage? storage})
      : _storage = storage ?? PlatformChatStorage();

  final _uuid = const Uuid();
  final ChatKeyValueStorage _storage;
  List<Conversation> _conversations = const [];
  String? _activeId;

  List<Conversation> get conversations => List<Conversation>.unmodifiable(
        _conversations.where((conversation) => conversation.deletedAt == null),
      );

  List<Conversation> get deletedConversations =>
      List<Conversation>.unmodifiable(
        _conversations
            .where((conversation) => conversation.deletedAt != null)
            .toList()
          ..sort((a, b) => b.deletedAt!.compareTo(a.deletedAt!)),
      );

  Conversation? get active => _activeId == null
      ? null
      : _firstWhere(conversations, (c) => c.id == _activeId);

  Future<void> load() async {
    String? raw;
    try {
      raw = await _storage.read(_kConversationsKey);
    } catch (_) {
      raw = null;
    }
    if (raw != null && raw.trim().isNotEmpty) {
      try {
        final decoded = jsonDecode(raw);
        final list = decoded is List ? decoded : const [];
        _conversations =
            list.map(_conversationFromRaw).whereType<Conversation>().toList();
      } catch (_) {
        _conversations = const [];
      }
    }
    try {
      _activeId = await _storage.read(_kActiveConversationKey);
    } catch (_) {
      _activeId = null;
    }
    if (_activeId != null && !conversations.any((c) => c.id == _activeId)) {
      _activeId = conversations.isEmpty ? null : conversations.first.id;
    }
    if (_activeId == null && conversations.isNotEmpty) {
      _activeId = conversations.first.id;
    }
  }

  Future<void> _persistChecked() async {
    final raw = jsonEncode(_conversations.map((c) => c.toJson()).toList());
    await _storage.write(_kConversationsKey, raw);
    if (_activeId != null) {
      await _storage.write(_kActiveConversationKey, _activeId!);
    } else {
      await _storage.delete(_kActiveConversationKey);
    }
  }

  Future<void> _persist() async {
    try {
      await _persistChecked();
    } catch (_) {
      // Keep the in-memory conversation usable even if platform storage fails.
    }
  }

  void _bumpRevision(Conversation convo) {
    convo.revision += 1;
    convo.updatedAt = DateTime.now();
  }

  Conversation _newConversationRecord() => Conversation(
        id: _uuid.v4(),
        title: '新しいチャット',
        messages: <ChatMessage>[],
        createdAt: DateTime.now(),
        updatedAt: DateTime.now(),
      );

  Conversation newConversation() {
    final convo = _newConversationRecord();
    _conversations = [convo, ..._conversations];
    _activeId = convo.id;
    return convo;
  }

  Future<Conversation> createAndPersist() async {
    final convo = newConversation();
    await _persist();
    return convo;
  }

  Future<void> select(String id) async {
    _activeId = id;
    await _persist();
  }

  Future<ConversationDeletionResult> delete(String id) async {
    final visible = conversations;
    final index = visible.indexWhere((conversation) => conversation.id == id);
    if (index < 0) {
      throw StateError('conversation is unavailable for deletion');
    }
    final conversation = visible[index];
    final previousActiveId = _activeId;
    final wasActive = previousActiveId == id;
    Conversation? replacement;

    conversation.deletedAt = DateTime.now();
    conversation.deletedReplacementId = null;
    final remaining = conversations;
    if (wasActive) {
      if (remaining.isEmpty) {
        replacement = _newConversationRecord();
        _conversations = [replacement, ..._conversations];
        conversation.deletedReplacementId = replacement.id;
        _activeId = replacement.id;
      } else {
        final nextIndex =
            index < remaining.length ? index : remaining.length - 1;
        _activeId = remaining[nextIndex].id;
      }
    }

    try {
      await _persistChecked();
    } catch (_) {
      if (replacement != null) {
        _conversations =
            _conversations.where((item) => item.id != replacement!.id).toList();
      }
      conversation.deletedAt = null;
      conversation.deletedReplacementId = null;
      _activeId = previousActiveId;
      await _persist();
      rethrow;
    }
    return ConversationDeletionResult(
      conversation: conversation,
      wasActive: wasActive,
      nextActiveId: _activeId,
    );
  }

  Future<void> restore(String id) async {
    final conversation = _firstWhere(
      _conversations,
      (item) => item.id == id && item.deletedAt != null,
    );
    if (conversation == null) {
      throw StateError('conversation is unavailable for restore');
    }
    final previousActiveId = _activeId;
    final previousDeletedAt = conversation.deletedAt;
    final replacementId = conversation.deletedReplacementId;
    final replacement =
        replacementId == null || replacementId == conversation.id
            ? null
            : _firstWhere(_conversations, (item) => item.id == replacementId);
    final discardReplacement = replacement != null &&
        replacement.messages.isEmpty &&
        replacement.title == '新しいチャット';

    conversation.deletedAt = null;
    conversation.deletedReplacementId = null;
    if (discardReplacement) {
      _conversations =
          _conversations.where((item) => item.id != replacementId).toList();
    }
    _activeId = conversation.id;

    try {
      await _persistChecked();
    } catch (_) {
      conversation.deletedAt = previousDeletedAt;
      conversation.deletedReplacementId = replacementId;
      if (discardReplacement) {
        _conversations = [replacement, ..._conversations];
      }
      _activeId = previousActiveId;
      await _persist();
      rethrow;
    }
  }

  Future<void> rename(String id, String title) async {
    final convo = _firstWhere(_conversations, (c) => c.id == id);
    if (convo == null) return;
    convo.title = title.trim().isEmpty ? '新しいチャット' : title.trim();
    _bumpRevision(convo);
    await _persist();
  }

  Future<void> togglePin(String id) async {
    final convo = _firstWhere(_conversations, (c) => c.id == id);
    if (convo == null) return;
    convo.pinned = !convo.pinned;
    _bumpRevision(convo);
    await _persist();
  }

  Future<void> addMessage(String conversationId, ChatMessage message) async {
    final convo = _firstWhere(_conversations, (c) => c.id == conversationId);
    if (convo == null) return;
    convo.messages.add(message);
    if (convo.title == '新しいチャット' &&
        message.role == ChatRole.user &&
        message.content.trim().isNotEmpty) {
      convo.title = _deriveTitle(message.content);
    }
    _bumpRevision(convo);
    await _persist();
  }

  Future<void> updateMessage(
    String conversationId,
    String messageId,
    String content, {
    bool? pending,
    bool? error,
  }) async {
    final convo = _firstWhere(_conversations, (c) => c.id == conversationId);
    if (convo == null) return;
    final msg = _firstWhere(convo.messages, (m) => m.id == messageId);
    if (msg == null) return;
    msg.content = content;
    if (pending != null) msg.pending = pending;
    if (error != null) msg.error = error;
    _bumpRevision(convo);
    await _persist();
  }

  Future<void> persist() => _persist();

  String _deriveTitle(String content) {
    final single = content.replaceAll('\n', ' ').trim();
    if (single.length <= 32) return single;
    return '${single.substring(0, 30)}…';
  }

  T? _firstWhere<T>(Iterable<T> items, bool Function(T) test) {
    for (final item in items) {
      if (test(item)) return item;
    }
    return null;
  }

  Conversation? _conversationFromRaw(Object? raw) {
    if (raw is! Map) return null;
    try {
      final conversation = Conversation.fromJson(
        Map<String, dynamic>.from(raw),
      );
      return conversation.id.trim().isEmpty ? null : conversation;
    } catch (_) {
      return null;
    }
  }
}
