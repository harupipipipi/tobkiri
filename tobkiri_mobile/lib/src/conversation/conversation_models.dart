class MobileConversationConnection {
  const MobileConversationConnection({
    required this.id,
    required this.label,
    required this.baseUrl,
    required this.deviceId,
    required this.token,
    required this.scopes,
  });

  static const requiredScopes = <String>{'chat.read', 'chat.write'};

  final String id;
  final String label;
  final String baseUrl;
  final String deviceId;
  final String token;
  final Set<String> scopes;

  bool get isValid {
    final uri = Uri.tryParse(baseUrl.trim());
    return id.trim().isNotEmpty &&
        label.trim().isNotEmpty &&
        uri != null &&
        const {'http', 'https'}.contains(uri.scheme) &&
        uri.host.isNotEmpty &&
        uri.userInfo.isEmpty &&
        !uri.hasQuery &&
        !uri.hasFragment &&
        deviceId.trim().isNotEmpty &&
        token.trim().isNotEmpty &&
        scopes.containsAll(requiredScopes) &&
        scopes.every(requiredScopes.contains);
  }

  Map<String, Object> toJson() => {
        'id': id,
        'label': label,
        'base_url': baseUrl,
        'device_id': deviceId,
        'token': token,
        'scopes': scopes.toList()..sort(),
      };

  factory MobileConversationConnection.fromJson(Map<String, dynamic> json) {
    return MobileConversationConnection(
      id: json['id'] as String? ?? '',
      label: json['label'] as String? ?? '',
      baseUrl: json['base_url'] as String? ?? '',
      deviceId: json['device_id'] as String? ?? '',
      token: json['token'] as String? ?? '',
      scopes: (json['scopes'] as List? ?? const [])
          .map((scope) => scope.toString())
          .toSet(),
    );
  }
}

class ConversationSummary {
  const ConversationSummary({
    required this.id,
    required this.title,
    required this.preview,
    required this.messageCount,
    required this.updatedAt,
    required this.pinned,
  });

  final String id;
  final String title;
  final String preview;
  final int messageCount;
  final DateTime? updatedAt;
  final bool pinned;

  String get displayTitle =>
      title.trim().isEmpty ? 'Untitled conversation' : title;

  factory ConversationSummary.fromJson(Map<String, dynamic> json) {
    final rawCount = json['message_count'];
    return ConversationSummary(
      id: json['id'] as String? ?? '',
      title: json['title'] as String? ?? '',
      preview: json['preview'] as String? ?? '',
      messageCount: rawCount is num ? rawCount.toInt() : 0,
      updatedAt: DateTime.tryParse(json['updated_at'] as String? ?? ''),
      pinned: json['pinned'] == true,
    );
  }
}

class ConversationDetail {
  const ConversationDetail({required this.summary, required this.messages});

  final ConversationSummary summary;
  final List<ConversationMessagePreview> messages;

  factory ConversationDetail.fromJson(Map<String, dynamic> json) {
    final rawMessages = json['messages'] as List? ?? const [];
    return ConversationDetail(
      summary: ConversationSummary.fromJson(json),
      messages: rawMessages
          .whereType<Map>()
          .map(
            (value) => ConversationMessagePreview.fromJson(
              Map<String, dynamic>.from(value),
            ),
          )
          .toList(growable: false),
    );
  }
}

class ConversationMessagePreview {
  const ConversationMessagePreview({required this.role, required this.content});

  final String role;
  final String content;

  factory ConversationMessagePreview.fromJson(Map<String, dynamic> json) {
    return ConversationMessagePreview(
      role: json['role'] as String? ?? 'assistant',
      content: json['content'] as String? ?? '',
    );
  }
}
