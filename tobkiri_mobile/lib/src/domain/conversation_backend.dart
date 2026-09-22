import '../chat/chat_models.dart';
import 'chat_event.dart';
import 'conversation_locator.dart';

class ConversationSummary {
  const ConversationSummary({
    required this.id,
    required this.title,
    required this.authority,
    required this.messageCount,
    required this.updatedAt,
    required this.pinned,
    required this.revision,
  });

  final String id;
  final String title;
  final ConversationAuthorityKind authority;
  final int messageCount;
  final DateTime updatedAt;
  final bool pinned;
  final int revision;

  factory ConversationSummary.from(Conversation convo,
      {ConversationAuthorityKind authority = ConversationAuthorityKind.local}) {
    return ConversationSummary(
      id: convo.id,
      title: convo.title,
      authority: authority,
      messageCount: convo.messages.length,
      updatedAt: convo.updatedAt,
      pinned: convo.pinned,
      revision: convo.revision,
    );
  }
}

class ConversationSnapshot {
  const ConversationSnapshot({
    required this.locator,
    required this.conversation,
    required this.revision,
  });

  final ConversationLocator locator;
  final Conversation conversation;
  final int revision;
}

class CreateConversationRequest {
  const CreateConversationRequest({
    this.title,
    required this.authority,
    this.deviceId,
  });

  final String? title;
  final ConversationAuthorityKind authority;
  final String? deviceId;
}

abstract interface class ConversationBackend {
  ConversationAuthorityKind get authority;

  bool get isConfigured;

  Future<List<ConversationSummary>> listConversations();

  Future<ConversationSnapshot> getConversation(ConversationLocator locator);

  Future<ConversationLocator> createConversation(
      CreateConversationRequest request);

  Stream<ChatEvent> sendMessage({
    required ConversationLocator locator,
    required String text,
    required String clientMessageId,
    required int expectedRevision,
    String? model,
    String? profileId,
    Map<String, dynamic>? params,
  });

  Future<void> stop(String conversationId);
}
