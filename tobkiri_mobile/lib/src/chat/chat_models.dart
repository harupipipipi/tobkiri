import '../domain/conversation_locator.dart';

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
  });

  final String id;
  final ChatRole role;
  String content;
  final DateTime? createdAt;
  bool pending;
  bool error;

  Map<String, dynamic> toJson() => {
        'id': id,
        'role': role.value,
        'content': content,
        'createdAt': createdAt?.toIso8601String(),
        'pending': pending,
        'error': error,
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
    );
  }

  ChatMessage copy() => ChatMessage(
        id: id,
        role: role,
        content: content,
        createdAt: createdAt,
        pending: pending,
        error: error,
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
