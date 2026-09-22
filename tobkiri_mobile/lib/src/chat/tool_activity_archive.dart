import 'dart:convert';

import '../domain/conversation_locator.dart';
import '../platform/platform_services.dart';
import 'chat_models.dart';

const _archiveVersion = 1;
const _maxArchivedMessages = 32;
const _maxActivitiesPerMessage = 8;

abstract interface class ToolActivityArchiveStorage {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
}

class PlatformToolActivityArchiveStorage implements ToolActivityArchiveStorage {
  PlatformToolActivityArchiveStorage({PlatformPreferences? preferences})
    : _preferences = preferences ?? PlatformPreferences();

  final PlatformPreferences _preferences;

  @override
  Future<String?> read(String key) => _preferences.read(key);

  @override
  Future<void> write(String key, String value) =>
      _preferences.write(key, value);
}

class ToolActivityArchive {
  ToolActivityArchive({ToolActivityArchiveStorage? storage})
    : _storage = storage ?? PlatformToolActivityArchiveStorage();

  final ToolActivityArchiveStorage _storage;

  Future<void> save(
    ConversationLocator locator,
    Iterable<ChatMessage> messages,
  ) async {
    final archived = messages
        .where((message) => message.toolActivities.isNotEmpty)
        .toList()
        .reversed
        .take(_maxArchivedMessages)
        .toList()
        .reversed
        .map(
          (message) => <String, dynamic>{
            'messageId': message.id,
            'activities': message.toolActivities
                .take(_maxActivitiesPerMessage)
                .map((activity) => activity.toJson())
                .toList(),
          },
        )
        .toList();
    await _storage.write(
      _key(locator),
      jsonEncode(<String, dynamic>{
        'version': _archiveVersion,
        'messages': archived,
      }),
    );
  }

  Future<void> restore(
    ConversationLocator locator,
    Iterable<ChatMessage> messages,
  ) async {
    final raw = await _storage.read(_key(locator));
    if (raw == null || raw.isEmpty) return;
    final decoded = jsonDecode(raw);
    if (decoded is! Map || decoded['version'] != _archiveVersion) return;
    final records = decoded['messages'];
    if (records is! List) return;
    final byMessageId = <String, List<ToolActivitySnapshot>>{};
    for (final record in records.take(_maxArchivedMessages)) {
      if (record is! Map) continue;
      final messageId = record['messageId'];
      final activities = record['activities'];
      if (messageId is! String || messageId.isEmpty || activities is! List) {
        continue;
      }
      final snapshots = activities
          .take(_maxActivitiesPerMessage)
          .whereType<Map>()
          .map(
            (item) =>
                ToolActivitySnapshot.fromJson(Map<String, dynamic>.from(item)),
          )
          .where((item) => item.toolId.isNotEmpty)
          .toList();
      if (snapshots.isNotEmpty) byMessageId[messageId] = snapshots;
    }
    for (final message in messages) {
      final snapshots = byMessageId[message.id];
      if (snapshots == null) continue;
      message.toolActivities
        ..clear()
        ..addAll(snapshots);
    }
  }

  String _key(ConversationLocator locator) {
    final deviceId = Uri.encodeComponent(locator.deviceId ?? 'unknown');
    final conversationId = Uri.encodeComponent(locator.conversationId);
    return 'rumi.chat.tool_activity.v1.$deviceId.$conversationId';
  }
}
