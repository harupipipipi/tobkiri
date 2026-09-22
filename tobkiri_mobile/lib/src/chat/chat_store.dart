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

class ChatStore {
  ChatStore({ChatKeyValueStorage? storage})
      : _storage = storage ?? PlatformChatStorage();

  final _uuid = const Uuid();
  final ChatKeyValueStorage _storage;
  List<Conversation> _conversations = const [];
  String? _activeId;

  List<Conversation> get conversations =>
      List<Conversation>.unmodifiable(_conversations);

  Conversation? get active => _activeId == null
      ? null
      : _firstWhere(_conversations, (c) => c.id == _activeId);

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
    if (_activeId != null && !_conversations.any((c) => c.id == _activeId)) {
      _activeId = _conversations.isEmpty ? null : _conversations.first.id;
    }
    if (_activeId == null && _conversations.isNotEmpty) {
      _activeId = _conversations.first.id;
    }
  }

  Future<void> _persist() async {
    try {
      final raw = jsonEncode(_conversations.map((c) => c.toJson()).toList());
      await _storage.write(_kConversationsKey, raw);
      if (_activeId != null) {
        await _storage.write(_kActiveConversationKey, _activeId!);
      } else {
        await _storage.delete(_kActiveConversationKey);
      }
    } catch (_) {
      // Keep the in-memory conversation usable even if platform storage fails.
    }
  }

  void _bumpRevision(Conversation convo) {
    convo.revision += 1;
    convo.updatedAt = DateTime.now();
  }

  Conversation newConversation() {
    final convo = Conversation(
      id: _uuid.v4(),
      title: '新しいチャット',
      messages: <ChatMessage>[],
      createdAt: DateTime.now(),
      updatedAt: DateTime.now(),
    );
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

  Future<void> delete(String id) async {
    _conversations = _conversations.where((c) => c.id != id).toList();
    if (_activeId == id) {
      _activeId = _conversations.isEmpty ? null : _conversations.first.id;
    }
    await _persist();
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
      String conversationId, String messageId, String content,
      {bool? pending, bool? error}) async {
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
