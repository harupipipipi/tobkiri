import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/chat/chat_store.dart';

class _MemoryChatStorage implements ChatKeyValueStorage {
  final values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

class _ThrowingChatStorage implements ChatKeyValueStorage {
  @override
  Future<String?> read(String key) async => throw StateError('read failed');

  @override
  Future<void> write(String key, String value) async =>
      throw StateError('write failed');

  @override
  Future<void> delete(String key) async => throw StateError('delete failed');
}

class _FailingWriteChatStorage extends _MemoryChatStorage {
  bool failWrites = false;

  @override
  Future<void> write(String key, String value) async {
    if (failWrites) throw StateError('write failed');
    await super.write(key, value);
  }

  @override
  Future<void> delete(String key) async {
    if (failWrites) throw StateError('delete failed');
    await super.delete(key);
  }
}

void main() {
  test('load ignores storage failures and keeps in-memory chat usable',
      () async {
    final store = ChatStore(storage: _ThrowingChatStorage());

    await store.load();
    final conversation = store.newConversation();
    await store.persist();

    expect(store.conversations, hasLength(1));
    expect(store.active?.id, conversation.id);
  });

  test('load drops malformed conversation entries without losing valid ones',
      () async {
    final storage = _MemoryChatStorage()
      ..values['rumi_chat.conversations.v1'] = jsonEncode([
        {
          'id': 'valid-1',
          'title': 'Valid',
          'messages': const [],
          'createdAt': DateTime(2026, 1, 1).toIso8601String(),
          'updatedAt': DateTime(2026, 1, 1).toIso8601String(),
          'deletedAt': 42,
          'deletedReplacementId': false,
        },
        {'title': 'missing id'},
        'not an object',
      ])
      ..values['rumi_chat.active_id.v1'] = 'missing-active';
    final store = ChatStore(storage: storage);

    await store.load();

    expect(store.conversations, hasLength(1));
    expect(store.conversations.single.id, 'valid-1');
    expect(store.active?.id, 'valid-1');
  });

  test('active deletion selects the adjacent conversation and restores order',
      () async {
    final storage = _MemoryChatStorage();
    final store = ChatStore(storage: storage);
    final first = await store.createAndPersist();
    await store.rename(first.id, 'First');
    final second = await store.createAndPersist();
    await store.rename(second.id, 'Second');
    final third = await store.createAndPersist();
    await store.rename(third.id, 'Third');
    await store.select(second.id);

    final result = await store.delete(second.id);

    expect(result.wasActive, isTrue);
    expect(store.conversations.map((item) => item.id), [third.id, first.id]);
    expect(store.active?.id, first.id);
    expect(store.deletedConversations.single.id, second.id);

    await store.restore(second.id);

    expect(store.conversations.map((item) => item.id),
        [third.id, second.id, first.id]);
    expect(store.active?.id, second.id);
    expect(store.deletedConversations, isEmpty);
  });

  test('nonactive deletion preserves the active conversation', () async {
    final store = ChatStore(storage: _MemoryChatStorage());
    final first = await store.createAndPersist();
    final active = await store.createAndPersist();

    final result = await store.delete(first.id);

    expect(result.wasActive, isFalse);
    expect(store.active?.id, active.id);
    expect(store.deletedConversations.single.id, first.id);
  });

  test(
      'last conversation deletion creates a usable replacement and undo removes it',
      () async {
    final store = ChatStore(storage: _MemoryChatStorage());
    final original = await store.createAndPersist();

    final result = await store.delete(original.id);

    expect(result.wasActive, isTrue);
    expect(store.conversations, hasLength(1));
    final replacementId = store.active!.id;
    expect(replacementId, isNot(original.id));

    await store.restore(original.id);

    expect(store.conversations.single.id, original.id);
    expect(store.active?.id, original.id);
  });

  test('delete persistence failure rolls back the row and active context',
      () async {
    final storage = _FailingWriteChatStorage();
    final store = ChatStore(storage: storage);
    final conversation = await store.createAndPersist();
    storage.failWrites = true;

    await expectLater(store.delete(conversation.id), throwsStateError);

    expect(store.conversations.single.id, conversation.id);
    expect(store.active?.id, conversation.id);
    expect(store.deletedConversations, isEmpty);
    storage.failWrites = false;
    final reloaded = ChatStore(storage: storage);
    await reloaded.load();
    expect(reloaded.conversations.single.id, conversation.id);
    expect(reloaded.active?.id, conversation.id);
  });

  test('recently deleted conversations survive restart and remain restorable',
      () async {
    final storage = _MemoryChatStorage();
    final store = ChatStore(storage: storage);
    final conversation = await store.createAndPersist();
    await store.rename(conversation.id, 'Durable trash');
    await store.delete(conversation.id);

    final restarted = ChatStore(storage: storage);
    await restarted.load();

    expect(restarted.deletedConversations.single.title, 'Durable trash');
    expect(restarted.active?.id, isNot(conversation.id));
    await restarted.restore(conversation.id);

    final restored = ChatStore(storage: storage);
    await restored.load();
    expect(restored.deletedConversations, isEmpty);
    expect(restored.active?.id, conversation.id);
  });

  test('restore persistence failure keeps the recoverable trash entry',
      () async {
    final storage = _FailingWriteChatStorage();
    final store = ChatStore(storage: storage);
    final conversation = await store.createAndPersist();
    await store.delete(conversation.id);
    final replacementId = store.active!.id;
    storage.failWrites = true;

    await expectLater(store.restore(conversation.id), throwsStateError);

    expect(store.deletedConversations.single.id, conversation.id);
    expect(store.active?.id, replacementId);
    expect(store.conversations.single.id, replacementId);
  });
}
