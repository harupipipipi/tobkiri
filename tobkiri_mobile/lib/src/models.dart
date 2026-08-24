class RumiApiException implements Exception {
  const RumiApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  @override
  String toString() {
    final code = statusCode == null ? '' : ' ($statusCode)';
    return 'RumiApiException$code: $message';
  }
}

class RumiHealth {
  const RumiHealth({
    required this.status,
    required this.raw,
    this.pack,
    this.timestamp,
  });

  final String status;
  final String? pack;
  final DateTime? timestamp;
  final Map<String, dynamic> raw;

  bool get isHealthy {
    final normalized = status.toLowerCase();
    return normalized == 'healthy' || normalized == 'ok';
  }

  factory RumiHealth.fromJson(Object? value) {
    final map = asMap(value);
    return RumiHealth(
      status: asString(map['status'], fallback: 'unknown'),
      pack: blankToNull(asString(map['pack'] ?? map['service'])),
      timestamp: parseDate(map['ts'] ?? map['timestamp']),
      raw: map,
    );
  }
}

class RumiModule {
  const RumiModule({
    required this.id,
    required this.kind,
    required this.state,
    required this.displayName,
    required this.description,
    required this.dependencies,
    required this.experimental,
    required this.raw,
    this.updatedAt,
    this.lastError,
  });

  final String id;
  final String kind;
  final String state;
  final String displayName;
  final String description;
  final List<String> dependencies;
  final bool experimental;
  final DateTime? updatedAt;
  final String? lastError;
  final Map<String, dynamic> raw;

  bool get enabled => state == 'enabled' || state == 'experimental';
  bool get degraded => state == 'degraded' || state == 'error_disabled';

  factory RumiModule.fromJson(Object? value) {
    final map = asMap(value);
    final id = asString(map['module_id'] ?? map['id']);
    return RumiModule(
      id: id,
      kind: asString(map['kind'], fallback: 'backend'),
      state: asString(map['state'], fallback: 'unknown'),
      displayName: asString(
        map['display_name'] ?? map['name'],
        fallback: id.isEmpty ? 'unknown' : id,
      ),
      description: asString(map['description']),
      dependencies: asStringList(map['dependencies']),
      experimental: map['experimental'] == true,
      updatedAt: parseDate(map['updated_at'] ?? map['updatedAt']),
      lastError: blankToNull(asString(map['last_error'])),
      raw: map,
    );
  }
}

class ModuleCatalog {
  const ModuleCatalog({required this.modules, required this.raw});

  final List<RumiModule> modules;
  final Map<String, dynamic> raw;

  int get count => modules.length;

  factory ModuleCatalog.fromJson(Object? value) {
    final map = asMap(value);
    final source = map.containsKey('modules') ? map['modules'] : value;
    final list = asList(source);
    return ModuleCatalog(
      modules: list.map(RumiModule.fromJson).toList(growable: false),
      raw: map,
    );
  }
}

class MigrationStatus {
  const MigrationStatus({required this.summary, required this.raw});

  final String summary;
  final Map<String, dynamic> raw;

  factory MigrationStatus.fromJson(Object? value) {
    final map = asMap(value);
    final status = asString(
      map['status'] ?? map['state'] ?? map['message'],
      fallback: 'unknown',
    );
    final migrated = map['migrated'];
    final total = map['total'];
    final suffix =
        migrated == null || total == null ? '' : ' ($migrated/$total)';
    return MigrationStatus(summary: '$status$suffix', raw: map);
  }
}

class PackRequest {
  const PackRequest({
    required this.id,
    required this.kind,
    required this.status,
    required this.summary,
    required this.raw,
    this.createdAt,
  });

  final String id;
  final String kind;
  final String status;
  final String summary;
  final DateTime? createdAt;
  final Map<String, dynamic> raw;

  factory PackRequest.fromJson(Object? value) {
    final map = asMap(value);
    final id = asString(map['request_id'] ?? map['id']);
    return PackRequest(
      id: id,
      kind: asString(map['kind'] ?? map['type'], fallback: 'request'),
      status: asString(map['status'] ?? map['state'], fallback: 'unknown'),
      summary: asString(
        map['summary'] ?? map['description'] ?? map['reason'],
        fallback: id.isEmpty ? 'Pack request' : id,
      ),
      createdAt: parseDate(map['created_at'] ?? map['createdAt']),
      raw: map,
    );
  }
}

/// A read-only summary returned by the PC mobile conversation contract.
///
/// The summary deliberately keeps only presentation metadata.  In
/// particular, the latest-message preview is treated as untrusted plain text
/// by the drawer; it is never interpreted as markup or as a link.
class PcConversation {
  const PcConversation({
    required this.id,
    required this.title,
    required this.messageCount,
    required this.updatedAt,
    required this.createdAt,
    required this.pinned,
    required this.preview,
    required this.revision,
    required this.raw,
  });

  final String id;
  final String title;
  final int messageCount;
  final DateTime? updatedAt;
  final DateTime? createdAt;
  final bool pinned;
  final String preview;
  final int revision;
  final Map<String, dynamic> raw;

  /// A bounded, plain-text title suitable for a visible row.
  String get displayTitle {
    final value = sanitizeConversationText(title, maxCharacters: 180);
    return value.isEmpty ? 'Untitled conversation' : value;
  }

  /// A bounded, plain-text preview suitable for a visible row.
  ///
  /// Flutter's [Text] widget does not parse HTML, markdown, or URLs.  The
  /// additional control-character filtering here prevents server-provided
  /// metadata from changing terminal-like presentation or adding invisible
  /// characters to accessibility output.
  String get safePreview =>
      sanitizeConversationText(preview, maxCharacters: 160);

  /// Case-insensitive key used to identify duplicate/default titles.
  String get normalizedTitle => displayTitle.trim().toLowerCase();

  factory PcConversation.fromJson(Object? value) {
    final map = asMap(value);
    final id = asString(map['id'] ?? map['conversation_id']);
    final title = asString(map['title'] ?? map['name']);
    final messageValue = map['message_count'] ??
        map['messageCount'] ??
        (map['messages'] is List ? (map['messages'] as List).length : 0);
    return PcConversation(
      id: id,
      title: title,
      messageCount: asInt(messageValue),
      updatedAt: parseDate(
        map['updated_at'] ?? map['updatedAt'] ?? map['last_activity_at'],
      ),
      createdAt: parseDate(map['created_at'] ?? map['createdAt']),
      pinned: asBool(map['is_pinned'] ?? map['isPinned'] ?? map['pinned']),
      preview: _plainText(
        map['last_message_preview'] ??
            map['lastMessagePreview'] ??
            map['preview'],
      ),
      revision: asInt(map['revision']),
      raw: map,
    );
  }

  PcConversation copyWith({
    String? id,
    String? title,
    int? messageCount,
    DateTime? updatedAt,
    DateTime? createdAt,
    bool? pinned,
    String? preview,
    int? revision,
    Map<String, dynamic>? raw,
  }) {
    return PcConversation(
      id: id ?? this.id,
      title: title ?? this.title,
      messageCount: messageCount ?? this.messageCount,
      updatedAt: updatedAt ?? this.updatedAt,
      createdAt: createdAt ?? this.createdAt,
      pinned: pinned ?? this.pinned,
      preview: preview ?? this.preview,
      revision: revision ?? this.revision,
      raw: raw ?? this.raw,
    );
  }
}

/// The response projection for `GET /api/mobile/v1/conversations`.
class PcConversationCatalog {
  const PcConversationCatalog({
    required this.conversations,
    required this.count,
    required this.raw,
  });

  final List<PcConversation> conversations;
  final int count;
  final Map<String, dynamic> raw;

  factory PcConversationCatalog.fromJson(Object? value) {
    final map = asMap(value);
    final source = map.containsKey('conversations')
        ? map['conversations']
        : value is List
            ? value
            : const <Object?>[];
    final conversations = asList(source)
        .map(PcConversation.fromJson)
        .where((item) => item.id.isNotEmpty)
        .toList(growable: false);
    final count = asInt(
      map['count'] ?? map['total'],
      fallback: conversations.length,
    );
    return PcConversationCatalog(
      conversations: conversations,
      count: count,
      raw: map,
    );
  }
}

Map<String, dynamic> asMap(Object? value) {
  if (value is Map<String, dynamic>) {
    return value;
  }
  if (value is Map) {
    return value.map((key, item) => MapEntry('$key', item));
  }
  return {};
}

List<Object?> asList(Object? value) {
  if (value is List) {
    return value;
  }
  return const [];
}

String asString(Object? value, {String fallback = ''}) {
  if (value == null) {
    return fallback;
  }
  final text = '$value'.trim();
  return text.isEmpty ? fallback : text;
}

List<String> asStringList(Object? value) {
  return asList(value)
      .map((item) => asString(item))
      .where((item) => item.isNotEmpty)
      .toList(growable: false);
}

int asInt(Object? value, {int fallback = 0}) {
  if (value is int) {
    return value;
  }
  if (value is num) {
    return value.toInt();
  }
  final parsed = int.tryParse('$value'.trim());
  return parsed ?? fallback;
}

bool asBool(Object? value, {bool fallback = false}) {
  if (value is bool) {
    return value;
  }
  if (value is num) {
    return value != 0;
  }
  final normalized = '$value'.trim().toLowerCase();
  if (normalized == 'true' || normalized == 'yes' || normalized == '1') {
    return true;
  }
  if (normalized == 'false' || normalized == 'no' || normalized == '0') {
    return false;
  }
  return fallback;
}

String? blankToNull(String value) {
  final trimmed = value.trim();
  return trimmed.isEmpty ? null : trimmed;
}

DateTime? parseDate(Object? value) {
  if (value == null) {
    return null;
  }
  if (value is num) {
    final number = value.toDouble();
    // The runtime currently emits milliseconds, while older summaries used
    // Unix seconds.  Supporting both keeps this parser deterministic without
    // guessing from a formatted string.
    final milliseconds =
        number.abs() < 100000000000 ? (number * 1000).round() : number.round();
    return DateTime.fromMillisecondsSinceEpoch(milliseconds, isUtc: true);
  }
  return DateTime.tryParse('$value');
}

String sanitizeConversationText(String value, {int maxCharacters = 2000}) {
  if (maxCharacters <= 0) {
    return '';
  }
  final filtered = value
      .replaceAll(
        RegExp(
          r'[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F'
          r'\u200B-\u200F\u202A-\u202E\u2060\u2066-\u2069\uFEFF]',
        ),
        ' ',
      )
      .replaceAll(RegExp(r'\s+'), ' ')
      .trim();
  if (filtered.length <= maxCharacters) {
    return filtered;
  }
  if (maxCharacters == 1) {
    return '…';
  }
  return '${filtered.substring(0, maxCharacters - 1).trimRight()}…';
}

String _plainText(Object? value) {
  if (value is String) {
    return value;
  }
  if (value is num || value is bool) {
    return '$value';
  }
  return '';
}
