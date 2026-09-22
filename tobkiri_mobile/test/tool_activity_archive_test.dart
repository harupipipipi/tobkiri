import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/tool_activity_archive.dart';
import 'package:rumi_remote_app/src/domain/conversation_locator.dart';

class _MemoryToolActivityArchiveStorage implements ToolActivityArchiveStorage {
  final values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }
}

ChatMessage _message(
  String id, {
  List<ToolActivitySnapshot> activities = const [],
}) {
  return ChatMessage(
    id: id,
    role: ChatRole.assistant,
    content: 'result',
    toolActivities: activities,
  );
}

void main() {
  test('archives bounded PC activity and restores it after a reload', () async {
    final storage = _MemoryToolActivityArchiveStorage();
    final archive = ToolActivityArchive(storage: storage);
    final locator = ConversationLocator.pc(
      'conversation-1',
      deviceId: 'phone-1',
    );
    final source = _message(
      'assistant-1',
      activities: [
        ToolActivitySnapshot(
          toolId: 'tool-1',
          toolName: 'file_read',
          status: 'completed',
          arguments: const {'path': '/workspace/readme.md'},
          summary: 'Read README',
        ),
      ],
    );

    await archive.save(locator, [source]);

    final reloaded = _message('assistant-1');
    await archive.restore(locator, [reloaded]);

    expect(reloaded.toolActivities, hasLength(1));
    expect(reloaded.toolActivities.single.toolId, 'tool-1');
    expect(reloaded.toolActivities.single.status, 'completed');
  });

  test(
    'does not restore activity for a different PC or conversation',
    () async {
      final storage = _MemoryToolActivityArchiveStorage();
      final archive = ToolActivityArchive(storage: storage);
      await archive.save(
        ConversationLocator.pc('conversation-1', deviceId: 'phone-1'),
        [
          _message(
            'assistant-1',
            activities: [
              const ToolActivitySnapshot(
                toolId: 'tool-1',
                toolName: 'file_read',
                status: 'completed',
              ),
            ],
          ),
        ],
      );

      final other = _message('assistant-1');
      await archive.restore(
        ConversationLocator.pc('conversation-1', deviceId: 'phone-2'),
        [other],
      );

      expect(other.toolActivities, isEmpty);
    },
  );
}
