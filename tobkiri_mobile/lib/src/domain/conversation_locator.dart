enum ConversationAuthorityKind { local, pc }

class ConversationLocator {
  const ConversationLocator({
    required this.authority,
    required this.conversationId,
    this.deviceId,
  });

  final ConversationAuthorityKind authority;
  final String conversationId;
  final String? deviceId;

  bool get isLocal => authority == ConversationAuthorityKind.local;
  bool get isPc => authority == ConversationAuthorityKind.pc;

  static ConversationLocator local(String conversationId) =>
      ConversationLocator(
        authority: ConversationAuthorityKind.local,
        conversationId: conversationId,
      );

  static ConversationLocator pc(String conversationId, {String? deviceId}) =>
      ConversationLocator(
        authority: ConversationAuthorityKind.pc,
        conversationId: conversationId,
        deviceId: deviceId,
      );

  @override
  bool operator ==(Object other) =>
      other is ConversationLocator &&
      other.authority == authority &&
      other.conversationId == conversationId &&
      other.deviceId == deviceId;

  @override
  int get hashCode => Object.hash(authority, conversationId, deviceId);

  @override
  String toString() => 'ConversationLocator(${authority.name}:$conversationId)';
}
