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
}
