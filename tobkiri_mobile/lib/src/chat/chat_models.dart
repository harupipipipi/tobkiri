import '../domain/conversation_locator.dart';

class ToolActivitySnapshot {
  const ToolActivitySnapshot({
    required this.toolId,
    required this.toolName,
    required this.status,
    this.arguments = const {},
    this.summary,
    this.output,
    this.error,
    this.startedAt,
    this.endedAt,
    this.duration,
  });

  final String toolId;
  final String toolName;
  final String status;
  final Map<String, dynamic> arguments;
  final String? summary;
  final String? output;
  final String? error;
  final DateTime? startedAt;
  final DateTime? endedAt;
  final Duration? duration;

  Map<String, dynamic> toJson() => {
        'toolId': toolId,
        'toolName': toolName,
        'status': status,
        'arguments': arguments,
        'summary': summary,
        'output': output,
        'error': error,
        'startedAt': startedAt?.toIso8601String(),
        'endedAt': endedAt?.toIso8601String(),
        'durationMs': duration?.inMilliseconds,
      };

  factory ToolActivitySnapshot.fromJson(Map<String, dynamic> json) {
    final rawArguments = json['arguments'];
    final durationMs = (json['durationMs'] as num?)?.toInt();
    return ToolActivitySnapshot(
      toolId: json['toolId'] as String? ?? '',
      toolName: json['toolName'] as String? ?? '',
      status: json['status'] as String? ?? 'unknown',
      arguments: rawArguments is Map
          ? Map<String, dynamic>.from(rawArguments)
          : const {},
      summary: json['summary'] as String?,
      output: json['output'] as String?,
      error: json['error'] as String?,
      startedAt: DateTime.tryParse(json['startedAt'] as String? ?? ''),
      endedAt: DateTime.tryParse(json['endedAt'] as String? ?? ''),
      duration: durationMs == null ? null : Duration(milliseconds: durationMs),
    );
  }
}

class ChatRole {
  const ChatRole._(this.value);
  final String value;

  static const system = ChatRole._('system');
  static const user = ChatRole._('user');
  static const assistant = ChatRole._('assistant');

  static ChatRole fromString(String value) {
    switch (value) {
      case 'system':
        return system;
      case 'user':
        return user;
      case 'assistant':
        return assistant;
      default:
        return assistant;
    }
  }

  @override
  bool operator ==(Object other) => other is ChatRole && other.value == value;
  @override
  int get hashCode => value.hashCode;
  @override
  String toString() => value;
}

class ChatMessage {
  ChatMessage({
    required this.id,
    required this.role,
    required this.content,
    this.createdAt,
    this.pending = false,
    this.error = false,
    List<ToolActivitySnapshot>? toolActivities,
  }) : toolActivities = toolActivities ?? <ToolActivitySnapshot>[];

  final String id;
  final ChatRole role;
  String content;
  final DateTime? createdAt;
  bool pending;
  bool error;
  final List<ToolActivitySnapshot> toolActivities;

  Map<String, dynamic> toJson() => {
        'id': id,
        'role': role.value,
        'content': content,
        'createdAt': createdAt?.toIso8601String(),
        'pending': pending,
        'error': error,
        'toolActivities': toolActivities.map((item) => item.toJson()).toList(),
      };

  factory ChatMessage.fromJson(Map<String, dynamic> json) {
    return ChatMessage(
      id: json['id'] as String,
      role: ChatRole.fromString(json['role'] as String? ?? 'assistant'),
      content: json['content'] as String? ?? '',
      createdAt: json['createdAt'] == null
          ? null
          : DateTime.tryParse(json['createdAt'] as String),
      pending: json['pending'] as bool? ?? false,
      error: json['error'] as bool? ?? false,
      toolActivities: (json['toolActivities'] as List? ?? const [])
          .whereType<Map>()
          .map((item) => ToolActivitySnapshot.fromJson(
                Map<String, dynamic>.from(item),
              ))
          .toList(),
    );
  }

  ChatMessage copy() => ChatMessage(
        id: id,
        role: role,
        content: content,
        createdAt: createdAt,
        pending: pending,
        error: error,
        toolActivities: List<ToolActivitySnapshot>.from(toolActivities),
      );
}

class Conversation {
  Conversation({
    required this.id,
    required this.title,
    required this.messages,
    required this.createdAt,
    required this.updatedAt,
    this.pinned = false,
    this.revision = 0,
    this.authority = ConversationAuthorityKind.local,
  });

  final String id;
  String title;
  final List<ChatMessage> messages;
  final DateTime createdAt;
  DateTime updatedAt;
  bool pinned;
  int revision;
  ConversationAuthorityKind authority;

  Map<String, dynamic> toJson() => {
        'id': id,
        'title': title,
        'messages': messages.map((m) => m.toJson()).toList(),
        'createdAt': createdAt.toIso8601String(),
        'updatedAt': updatedAt.toIso8601String(),
        'pinned': pinned,
        'revision': revision,
        'authority': authority.name,
      };

  factory Conversation.fromJson(Map<String, dynamic> json) {
    final list = (json['messages'] as List? ?? [])
        .map((m) => ChatMessage.fromJson(m as Map<String, dynamic>))
        .toList();
    return Conversation(
      id: json['id'] as String,
      title: json['title'] as String? ?? '新しいチャット',
      messages: list,
      createdAt: DateTime.tryParse(json['createdAt'] as String? ?? '') ??
          DateTime.now(),
      updatedAt: DateTime.tryParse(json['updatedAt'] as String? ?? '') ??
          DateTime.now(),
      pinned: json['pinned'] as bool? ?? false,
      revision: (json['revision'] as num?)?.toInt() ?? 0,
      authority: ConversationAuthorityKind.values.firstWhere(
        (e) => e.name == json['authority'],
        orElse: () => ConversationAuthorityKind.local,
      ),
    );
  }

  String get preview {
    for (final m in messages) {
      if (m.content.trim().isNotEmpty) {
        return m.content.trim().replaceAll('\n', ' ');
      }
    }
    return 'メッセージなし';
  }
}
