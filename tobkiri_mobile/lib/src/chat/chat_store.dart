import 'dart:convert';

import 'package:uuid/uuid.dart';

import '../platform/platform_services.dart';
import 'chat_models.dart';

const _legacyConversationsKey = 'rumi_chat.conversations.v1';
const _legacyActiveConversationKey = 'rumi_chat.active_id.v1';
const _snapshotKey = 'tobkiri_chat.snapshot.v2';
const _backupKey = 'tobkiri_chat.snapshot.v2.backup';
const _snapshotSchema = 'io.tobkiri.mobile-chat.snapshot.v2';

/// Classification for the most recent local conversation load.
enum ChatStoreLoadKind {
  notLoaded,
  empty,
  loaded,
  migrated,
  recovered,
  unreadable,
  corrupt,
  incompatible,
}

/// Redacted state that callers can safely present or export for diagnostics.
class ChatStoreStatus {
  const ChatStoreStatus({
    required this.kind,
    required this.code,
    this.revision = 0,
    this.recoveryAvailable = false,
  });

  final ChatStoreLoadKind kind;
  final String code;
  final int revision;
  final bool recoveryAvailable;
}

/// Successful durable publication metadata.
class ChatSaveResult {
  const ChatSaveResult({required this.revision});

  final int revision;
}

/// A redacted local conversation storage failure.
class ChatStoreException implements Exception {
  const ChatStoreException({
    required this.code,
    required this.operation,
    required this.userMessage,
  });

  final String code;
  final String operation;
  final String userMessage;

  @override
  String toString() => 'ChatStoreException($code, $operation)';
}

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
  int _snapshotRevision = 0;
  String? _durableRaw;
  String? _recoveryRaw;
  bool _publishing = false;
  ChatStoreStatus _status = const ChatStoreStatus(
    kind: ChatStoreLoadKind.notLoaded,
    code: 'not_loaded',
  );

  List<Conversation> get conversations =>
      List<Conversation>.unmodifiable(_conversations);

  Conversation? get active => _activeId == null
      ? null
      : _firstWhere(
          _conversations,
          (conversation) => conversation.id == _activeId,
        );

  ChatStoreStatus get status => _status;
  bool get recoveryAvailable => _recoveryRaw != null;

  /// Return redacted persistence metadata without conversation content.
  Map<String, Object?> diagnostics() => {
        'status': _status.kind.name,
        'code': _status.code,
        'revision': _snapshotRevision,
        'conversation_count': _conversations.length,
        'recovery_available': recoveryAvailable,
      };

  /// Load the canonical snapshot or a recoverable backup without overwriting it.
  Future<ChatStoreStatus> load() async {
    final raw = await _readForLoad(_snapshotKey, operation: 'load_snapshot');
    if (raw == null || raw.trim().isEmpty) {
      return _loadLegacyOrEmpty();
    }

    try {
      final decoded = _decodeSnapshot(raw);
      _adopt(decoded);
      _durableRaw = raw;
      _recoveryRaw = null;
      _status = ChatStoreStatus(
        kind: ChatStoreLoadKind.loaded,
        code: 'loaded',
        revision: decoded.revision,
      );
      return _status;
    } on _SnapshotDecodeException catch (error) {
      return _recoverSnapshot(error);
    }
  }

  Future<ChatStoreStatus> _loadLegacyOrEmpty() async {
    final rawConversations = await _readForLoad(
      _legacyConversationsKey,
      operation: 'load_legacy_conversations',
    );
    final rawActive = await _readForLoad(
      _legacyActiveConversationKey,
      operation: 'load_legacy_active',
    );
    if (rawConversations == null || rawConversations.trim().isEmpty) {
      _adopt(
        const _DecodedSnapshot(revision: 0, activeId: null, conversations: []),
      );
      _durableRaw = null;
      _recoveryRaw = null;
      _status = const ChatStoreStatus(
        kind: ChatStoreLoadKind.empty,
        code: 'empty',
      );
      return _status;
    }

    final decoded = _decodeLegacy(rawConversations, rawActive);
    _adopt(decoded.snapshot);
    final candidate = _encodeSnapshot(revision: 1);
    if (decoded.droppedEntries > 0) {
      _durableRaw = null;
      _recoveryRaw = candidate;
      _status = const ChatStoreStatus(
        kind: ChatStoreLoadKind.recovered,
        code: 'legacy_partially_recovered',
        recoveryAvailable: true,
      );
      return _status;
    }

    await _publishInitial(candidate, operation: 'migrate_legacy');
    _status = const ChatStoreStatus(
      kind: ChatStoreLoadKind.migrated,
      code: 'legacy_migrated',
      revision: 1,
    );
    return _status;
  }

  Future<ChatStoreStatus> _recoverSnapshot(
    _SnapshotDecodeException primaryError,
  ) async {
    final backup = await _readForLoad(_backupKey, operation: 'load_backup');
    if (backup != null && backup.trim().isNotEmpty) {
      try {
        final decoded = _decodeSnapshot(backup);
        _adopt(decoded);
        _durableRaw = null;
        _recoveryRaw = backup;
        _status = ChatStoreStatus(
          kind: ChatStoreLoadKind.recovered,
          code: 'backup_recovery_available',
          revision: decoded.revision,
          recoveryAvailable: true,
        );
        return _status;
      } on _SnapshotDecodeException {
        // Report the primary classification without exposing either payload.
      }
    }
    final incompatible = primaryError.code == 'incompatible_version';
    _status = ChatStoreStatus(
      kind: incompatible
          ? ChatStoreLoadKind.incompatible
          : ChatStoreLoadKind.corrupt,
      code: primaryError.code,
    );
    throw ChatStoreException(
      code: primaryError.code,
      operation: 'load_snapshot',
      userMessage: incompatible
          ? 'この端末のチャット履歴は新しい形式です。アプリを更新してください。'
          : 'チャット履歴を読み込めません。元データは上書きしていません。',
    );
  }

  /// Explicitly publish data recovered from a backup or partial legacy file.
  Future<ChatSaveResult> acceptRecoveredData() async {
    if (_recoveryRaw == null) {
      throw const ChatStoreException(
        code: 'recovery_unavailable',
        operation: 'accept_recovery',
        userMessage: '復元できるチャット履歴がありません。',
      );
    }
    final nextRevision = _snapshotRevision + 1;
    final candidate = _encodeSnapshot(revision: nextRevision);
    try {
      await _storage.write(_snapshotKey, candidate);
      final verified = await _storage.read(_snapshotKey);
      if (verified != candidate) {
        throw StateError('snapshot verification failed');
      }
    } catch (_) {
      throw const ChatStoreException(
        code: 'recovery_publish_failed',
        operation: 'accept_recovery',
        userMessage: '復元した履歴を保存できませんでした。元データは保持しています。',
      );
    }
    _snapshotRevision = nextRevision;
    _durableRaw = candidate;
    _recoveryRaw = null;
    _status = ChatStoreStatus(
      kind: ChatStoreLoadKind.loaded,
      code: 'recovery_accepted',
      revision: nextRevision,
    );
    return ChatSaveResult(revision: nextRevision);
  }

  Conversation _newConversation() {
    _ensureWritable('new_conversation');
    final conversation = Conversation(
      id: _uuid.v4(),
      title: '新しいチャット',
      messages: <ChatMessage>[],
      createdAt: DateTime.now(),
      updatedAt: DateTime.now(),
    );
    _conversations = [conversation, ..._conversations];
    _activeId = conversation.id;
    return conversation;
  }

  Future<Conversation> createAndPersist() async {
    final conversation = _newConversation();
    await _persistOrRollback('create_conversation');
    return active ?? conversation;
  }

  Future<ChatSaveResult> select(String id) async {
    _ensureWritable('select_conversation');
    if (!_conversations.any((conversation) => conversation.id == id)) {
      throw const ChatStoreException(
        code: 'conversation_not_found',
        operation: 'select_conversation',
        userMessage: '選択したチャットが見つかりません。',
      );
    }
    _activeId = id;
    return _persistOrRollback('select_conversation');
  }

  Future<ChatSaveResult> delete(String id) async {
    _ensureWritable('delete_conversation');
    _conversations =
        _conversations.where((conversation) => conversation.id != id).toList();
    if (_activeId == id) {
      _activeId = _conversations.isEmpty ? null : _conversations.first.id;
    }
    return _persistOrRollback('delete_conversation');
  }

  Future<ChatSaveResult> rename(String id, String title) async {
    _ensureWritable('rename_conversation');
    final conversation = _conversation(id);
    if (conversation == null) {
      return ChatSaveResult(revision: _snapshotRevision);
    }
    conversation.title = title.trim().isEmpty ? '新しいチャット' : title.trim();
    _bumpRevision(conversation);
    return _persistOrRollback('rename_conversation');
  }

  Future<ChatSaveResult> togglePin(String id) async {
    _ensureWritable('pin_conversation');
    final conversation = _conversation(id);
    if (conversation == null) {
      return ChatSaveResult(revision: _snapshotRevision);
    }
    conversation.pinned = !conversation.pinned;
    _bumpRevision(conversation);
    return _persistOrRollback('pin_conversation');
  }

  Future<ChatSaveResult> addMessage(
    String conversationId,
    ChatMessage message,
  ) async {
    _ensureWritable('add_message');
    final conversation = _conversation(conversationId);
    if (conversation == null) {
      return ChatSaveResult(revision: _snapshotRevision);
    }
    conversation.messages.add(message);
    if (conversation.title == '新しいチャット' &&
        message.role == ChatRole.user &&
        message.content.trim().isNotEmpty) {
      conversation.title = _deriveTitle(message.content);
    }
    _bumpRevision(conversation);
    return _persistOrRollback('add_message');
  }

  Future<ChatSaveResult> updateMessage(
    String conversationId,
    String messageId,
    String content, {
    bool? pending,
    bool? error,
  }) async {
    _ensureWritable('update_message');
    final conversation = _conversation(conversationId);
    if (conversation == null) {
      return ChatSaveResult(revision: _snapshotRevision);
    }
    final message = _firstWhere(
      conversation.messages,
      (candidate) => candidate.id == messageId,
    );
    if (message == null) return ChatSaveResult(revision: _snapshotRevision);
    message.content = content;
    if (pending != null) message.pending = pending;
    if (error != null) message.error = error;
    _bumpRevision(conversation);
    return _persistOrRollback('update_message');
  }

  Future<ChatSaveResult> persist() => _persistOrRollback('persist');

  Future<ChatSaveResult> _persistOrRollback(String operation) async {
    _ensureWritable(operation);
    if (_publishing) {
      _restoreDurable();
      throw ChatStoreException(
        code: 'concurrent_write',
        operation: operation,
        userMessage: '別の保存処理が完了していません。変更を元に戻しました。',
      );
    }
    _publishing = true;
    final nextRevision = _snapshotRevision + 1;
    final candidate = _encodeSnapshot(revision: nextRevision);
    try {
      if (_durableRaw != null) {
        await _storage.write(_backupKey, _durableRaw!);
      }
      await _storage.write(_snapshotKey, candidate);
      final verified = await _storage.read(_snapshotKey);
      if (verified != candidate) {
        throw StateError('snapshot verification failed');
      }
      _snapshotRevision = nextRevision;
      _durableRaw = candidate;
      _status = ChatStoreStatus(
        kind: ChatStoreLoadKind.loaded,
        code: 'saved',
        revision: nextRevision,
      );
      return ChatSaveResult(revision: nextRevision);
    } catch (_) {
      final committed = await _readAfterAmbiguousWrite(candidate);
      if (committed) {
        _snapshotRevision = nextRevision;
        _durableRaw = candidate;
        _status = ChatStoreStatus(
          kind: ChatStoreLoadKind.loaded,
          code: 'saved_after_verification',
          revision: nextRevision,
        );
        return ChatSaveResult(revision: nextRevision);
      }
      _restoreDurable();
      throw ChatStoreException(
        code: 'write_failed',
        operation: operation,
        userMessage: 'チャットを保存できませんでした。変更を元に戻しました。',
      );
    } finally {
      _publishing = false;
    }
  }

  Future<bool> _readAfterAmbiguousWrite(String candidate) async {
    try {
      return await _storage.read(_snapshotKey) == candidate;
    } catch (_) {
      return false;
    }
  }

  Future<void> _publishInitial(
    String candidate, {
    required String operation,
  }) async {
    try {
      await _storage.write(_snapshotKey, candidate);
      if (await _storage.read(_snapshotKey) != candidate) {
        throw StateError('snapshot verification failed');
      }
    } catch (_) {
      throw ChatStoreException(
        code: 'write_failed',
        operation: operation,
        userMessage: '既存のチャット履歴を安全な形式へ移行できませんでした。',
      );
    }
    _snapshotRevision = 1;
    _durableRaw = candidate;
    _recoveryRaw = null;
  }

  Future<String?> _readForLoad(String key, {required String operation}) async {
    try {
      return await _storage.read(key);
    } catch (_) {
      _status = const ChatStoreStatus(
        kind: ChatStoreLoadKind.unreadable,
        code: 'read_failed',
      );
      throw ChatStoreException(
        code: 'read_failed',
        operation: operation,
        userMessage: 'チャット履歴を読み込めません。元データは上書きしていません。',
      );
    }
  }

  void _ensureWritable(String operation) {
    if (_recoveryRaw != null) {
      throw ChatStoreException(
        code: 'recovery_required',
        operation: operation,
        userMessage: '復元候補を確認するまでチャット履歴は変更できません。',
      );
    }
    if (_status.kind == ChatStoreLoadKind.unreadable ||
        _status.kind == ChatStoreLoadKind.corrupt ||
        _status.kind == ChatStoreLoadKind.incompatible) {
      throw ChatStoreException(
        code: 'load_recovery_required',
        operation: operation,
        userMessage: '履歴の読み込み問題を解決するまで変更は保存しません。',
      );
    }
  }

  void _restoreDurable() {
    final raw = _durableRaw;
    if (raw == null) {
      _adopt(
        const _DecodedSnapshot(revision: 0, activeId: null, conversations: []),
      );
      return;
    }
    _adopt(_decodeSnapshot(raw));
  }

  _DecodedSnapshot _decodeSnapshot(String raw) {
    Object? value;
    try {
      value = jsonDecode(raw);
    } catch (_) {
      throw const _SnapshotDecodeException('corrupt_json');
    }
    if (value is! Map) {
      throw const _SnapshotDecodeException('corrupt_shape');
    }
    final map = Map<String, dynamic>.from(value);
    if (map['schema'] != _snapshotSchema) {
      throw const _SnapshotDecodeException('incompatible_version');
    }
    final revision = (map['revision'] as num?)?.toInt();
    final rawConversations = map['conversations'];
    if (revision == null || revision < 0 || rawConversations is! List) {
      throw const _SnapshotDecodeException('corrupt_shape');
    }
    final conversations = <Conversation>[];
    for (final entry in rawConversations) {
      final conversation = _conversationFromRaw(entry);
      if (conversation == null) {
        throw const _SnapshotDecodeException('corrupt_conversation');
      }
      conversations.add(conversation);
    }
    final rawActiveId = map['active_id'];
    final activeId = rawActiveId is String &&
            conversations.any((conversation) => conversation.id == rawActiveId)
        ? rawActiveId
        : conversations.isEmpty
            ? null
            : conversations.first.id;
    return _DecodedSnapshot(
      revision: revision,
      activeId: activeId,
      conversations: conversations,
    );
  }

  _LegacyDecode _decodeLegacy(String raw, String? activeId) {
    Object? value;
    try {
      value = jsonDecode(raw);
    } catch (_) {
      _status = const ChatStoreStatus(
        kind: ChatStoreLoadKind.corrupt,
        code: 'legacy_corrupt_json',
      );
      throw const ChatStoreException(
        code: 'legacy_corrupt_json',
        operation: 'load_legacy',
        userMessage: '以前のチャット履歴を読み込めません。元データは保持しています。',
      );
    }
    if (value is! List) {
      _status = const ChatStoreStatus(
        kind: ChatStoreLoadKind.incompatible,
        code: 'legacy_incompatible_shape',
      );
      throw const ChatStoreException(
        code: 'legacy_incompatible_shape',
        operation: 'load_legacy',
        userMessage: '以前のチャット履歴の形式を確認できません。',
      );
    }
    final conversations = <Conversation>[];
    var dropped = 0;
    for (final entry in value) {
      final conversation = _conversationFromRaw(entry);
      if (conversation == null) {
        dropped += 1;
      } else {
        conversations.add(conversation);
      }
    }
    final resolvedActiveId = activeId != null &&
            conversations.any((conversation) => conversation.id == activeId)
        ? activeId
        : conversations.isEmpty
            ? null
            : conversations.first.id;
    return _LegacyDecode(
      snapshot: _DecodedSnapshot(
        revision: 0,
        activeId: resolvedActiveId,
        conversations: conversations,
      ),
      droppedEntries: dropped,
    );
  }

  String _encodeSnapshot({required int revision}) => jsonEncode({
        'schema': _snapshotSchema,
        'revision': revision,
        'active_id': _activeId,
        'conversations': _conversations
            .map((conversation) => conversation.toJson())
            .toList(),
      });

  void _adopt(_DecodedSnapshot snapshot) {
    _snapshotRevision = snapshot.revision;
    _activeId = snapshot.activeId;
    _conversations = snapshot.conversations
        .map((conversation) => Conversation.fromJson(conversation.toJson()))
        .toList();
  }

  Conversation? _conversation(String id) =>
      _firstWhere(_conversations, (conversation) => conversation.id == id);

  void _bumpRevision(Conversation conversation) {
    conversation.revision += 1;
    conversation.updatedAt = DateTime.now();
  }

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

class _DecodedSnapshot {
  const _DecodedSnapshot({
    required this.revision,
    required this.activeId,
    required this.conversations,
  });

  final int revision;
  final String? activeId;
  final List<Conversation> conversations;
}

class _LegacyDecode {
  const _LegacyDecode({required this.snapshot, required this.droppedEntries});

  final _DecodedSnapshot snapshot;
  final int droppedEntries;
}

class _SnapshotDecodeException implements Exception {
  const _SnapshotDecodeException(this.code);

  final String code;
}
