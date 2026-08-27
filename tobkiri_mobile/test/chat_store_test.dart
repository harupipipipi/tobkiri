import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/chat_store.dart';

const _snapshotKey = 'tobkiri_chat.snapshot.v2';
const _backupKey = 'tobkiri_chat.snapshot.v2.backup';
const _legacyConversationsKey = 'rumi_chat.conversations.v1';
const _legacyActiveKey = 'rumi_chat.active_id.v1';

class _MemoryChatStorage implements ChatKeyValueStorage {
  final values = <String, String>{};
  final failReadKeys = <String>{};
  final failWriteKeys = <String>{};
  final writeCounts = <String, int>{};
  bool throwAfterSnapshotWrite = false;

  @override
  Future<String?> read(String key) async {
    if (failReadKeys.contains(key)) throw StateError('private read failure');
    return values[key];
  }

  @override
  Future<void> write(String key, String value) async {
    writeCounts[key] = (writeCounts[key] ?? 0) + 1;
    if (throwAfterSnapshotWrite && key == _snapshotKey) {
      values[key] = value;
      throwAfterSnapshotWrite = false;
      throw StateError('ambiguous platform response');
    }
    if (failWriteKeys.contains(key)) throw StateError('private write failure');
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

Map<String, dynamic> _conversation({
  String id = 'conversation-1',
  String title = 'Saved',
  List<Map<String, dynamic>> messages = const [],
}) => {
  'id': id,
  'title': title,
  'messages': messages,
  'createdAt': DateTime(2026, 1, 1).toIso8601String(),
  'updatedAt': DateTime(2026, 1, 1).toIso8601String(),
  'pinned': false,
  'revision': 1,
  'authority': 'local',
};

String _snapshot({
  int revision = 1,
  String? activeId = 'conversation-1',
  List<Map<String, dynamic>>? conversations,
  String schema = 'io.tobkiri.mobile-chat.snapshot.v2',
}) => jsonEncode({
  'schema': schema,
  'revision': revision,
  'active_id': activeId,
  'conversations': conversations ?? [_conversation()],
});

void main() {
  test(
    'empty store publishes one versioned conversations and active snapshot',
    () async {
      final storage = _MemoryChatStorage();
      final store = ChatStore(storage: storage);

      final loaded = await store.load();
      final conversation = await store.createAndPersist();

      expect(loaded.kind, ChatStoreLoadKind.empty);
      expect(store.active?.id, conversation.id);
      final saved = jsonDecode(storage.values[_snapshotKey]!) as Map;
      expect(saved['schema'], 'io.tobkiri.mobile-chat.snapshot.v2');
      expect(saved['active_id'], conversation.id);
      expect(saved['revision'], 1);
      expect(storage.values, isNot(contains(_legacyActiveKey)));
    },
  );

  test('read failure is explicit and blocks overwrite', () async {
    final storage = _MemoryChatStorage()..failReadKeys.add(_snapshotKey);
    final store = ChatStore(storage: storage);

    await expectLater(
      store.load(),
      throwsA(
        isA<ChatStoreException>().having(
          (error) => error.code,
          'code',
          'read_failed',
        ),
      ),
    );
    expect(store.status.kind, ChatStoreLoadKind.unreadable);
    await expectLater(
      store.createAndPersist(),
      throwsA(isA<ChatStoreException>()),
    );
    expect(storage.writeCounts, isEmpty);
    expect(store.diagnostics().toString(), isNot(contains('private')));
  });

  test('valid legacy values migrate to a verified single snapshot', () async {
    final storage = _MemoryChatStorage()
      ..values[_legacyConversationsKey] = jsonEncode([_conversation()])
      ..values[_legacyActiveKey] = 'conversation-1';
    final store = ChatStore(storage: storage);

    final status = await store.load();

    expect(status.kind, ChatStoreLoadKind.migrated);
    expect(store.active?.id, 'conversation-1');
    expect(jsonDecode(storage.values[_snapshotKey]!)['revision'], 1);
    expect(storage.values[_legacyConversationsKey], isNotNull);
  });

  test(
    'partial legacy recovery is read-only until explicitly accepted',
    () async {
      final storage = _MemoryChatStorage()
        ..values[_legacyConversationsKey] = jsonEncode([
          _conversation(),
          {'title': 'missing id'},
        ])
        ..values[_legacyActiveKey] = 'conversation-1';
      final store = ChatStore(storage: storage);

      final status = await store.load();

      expect(status.kind, ChatStoreLoadKind.recovered);
      expect(status.recoveryAvailable, isTrue);
      expect(store.conversations, hasLength(1));
      await expectLater(
        store.rename('conversation-1', 'Blocked'),
        throwsA(
          isA<ChatStoreException>().having(
            (error) => error.code,
            'code',
            'recovery_required',
          ),
        ),
      );
      await store.acceptRecoveredData();
      expect(store.recoveryAvailable, isFalse);
      expect(jsonDecode(storage.values[_snapshotKey]!)['revision'], 1);
    },
  );

  test(
    'corrupt primary offers verified backup without silently publishing it',
    () async {
      final backup = _snapshot(revision: 4);
      final storage = _MemoryChatStorage()
        ..values[_snapshotKey] = '{broken-json'
        ..values[_backupKey] = backup;
      final store = ChatStore(storage: storage);

      final status = await store.load();

      expect(status.kind, ChatStoreLoadKind.recovered);
      expect(store.active?.title, 'Saved');
      expect(storage.values[_snapshotKey], '{broken-json');
      await store.acceptRecoveredData();
      expect(jsonDecode(storage.values[_snapshotKey]!)['revision'], 5);
    },
  );

  test(
    'incompatible primary stays untouched when no backup is available',
    () async {
      final incompatible = _snapshot(schema: 'future.snapshot.v99');
      final storage = _MemoryChatStorage()..values[_snapshotKey] = incompatible;
      final store = ChatStore(storage: storage);

      await expectLater(
        store.load(),
        throwsA(
          isA<ChatStoreException>().having(
            (error) => error.code,
            'code',
            'incompatible_version',
          ),
        ),
      );
      expect(store.status.kind, ChatStoreLoadKind.incompatible);
      expect(storage.values[_snapshotKey], incompatible);
    },
  );

  test(
    'failed primary write rolls mutation back to last durable revision',
    () async {
      final storage = _MemoryChatStorage();
      final store = ChatStore(storage: storage);
      await store.load();
      final conversation = await store.createAndPersist();
      final durable = storage.values[_snapshotKey];
      storage.failWriteKeys.add(_snapshotKey);

      await expectLater(
        store.rename(conversation.id, 'Unsaved secret title'),
        throwsA(
          isA<ChatStoreException>().having(
            (error) => error.code,
            'code',
            'write_failed',
          ),
        ),
      );

      expect(store.active?.title, '新しいチャット');
      expect(storage.values[_snapshotKey], durable);
      expect(store.diagnostics().toString(), isNot(contains('secret title')));
    },
  );

  test(
    'failed backup write prevents publication and rolls mutation back',
    () async {
      final storage = _MemoryChatStorage();
      final store = ChatStore(storage: storage);
      await store.load();
      final conversation = await store.createAndPersist();
      final durable = storage.values[_snapshotKey];
      storage.failWriteKeys.add(_backupKey);

      await expectLater(
        store.togglePin(conversation.id),
        throwsA(isA<ChatStoreException>()),
      );

      expect(store.active?.pinned, isFalse);
      expect(storage.values[_snapshotKey], durable);
    },
  );

  test(
    'ambiguous write response succeeds only after exact read-back',
    () async {
      final storage = _MemoryChatStorage();
      final store = ChatStore(storage: storage);
      await store.load();
      final conversation = await store.createAndPersist();
      storage.throwAfterSnapshotWrite = true;

      final result = await store.rename(conversation.id, 'Verified');

      expect(result.revision, 2);
      expect(store.active?.title, 'Verified');
      expect(store.status.code, 'saved_after_verification');
    },
  );

  test(
    'streamed delta write failure restores the saved message prefix',
    () async {
      final storage = _MemoryChatStorage();
      final store = ChatStore(storage: storage);
      await store.load();
      final conversation = await store.createAndPersist();
      await store.addMessage(
        conversation.id,
        ChatMessage(
          id: 'assistant-1',
          role: ChatRole.assistant,
          content: 'saved prefix',
          pending: true,
        ),
      );
      storage.failWriteKeys.add(_snapshotKey);

      await expectLater(
        store.updateMessage(
          conversation.id,
          'assistant-1',
          'saved prefix plus unsaved delta',
          pending: false,
        ),
        throwsA(isA<ChatStoreException>()),
      );

      final message = store.active!.messages.single;
      expect(message.content, 'saved prefix');
      expect(message.pending, isTrue);
    },
  );

  test('delete failure cannot make a saved conversation disappear', () async {
    final storage = _MemoryChatStorage();
    final store = ChatStore(storage: storage);
    await store.load();
    final conversation = await store.createAndPersist();
    storage.failWriteKeys.add(_snapshotKey);

    await expectLater(
      store.delete(conversation.id),
      throwsA(isA<ChatStoreException>()),
    );

    expect(store.conversations.single.id, conversation.id);
    expect(store.active?.id, conversation.id);
  });
}
