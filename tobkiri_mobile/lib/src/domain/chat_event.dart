import 'conversation_locator.dart';

sealed class ChatEvent {
  final ConversationLocator locator;
  final String? runId;
  const ChatEvent({required this.locator, this.runId});
}

class ChatRunStarted extends ChatEvent {
  final String assistantMessageId;
  const ChatRunStarted({
    required super.locator,
    required String runId,
    required this.assistantMessageId,
  }) : super(runId: runId);
}

class ChatDelta extends ChatEvent {
  final String assistantMessageId;
  final String delta;
  final String accumulatedContent;
  const ChatDelta({
    required super.locator,
    required super.runId,
    required this.assistantMessageId,
    required this.delta,
    required this.accumulatedContent,
  });
}

class ChatMessageCommitted extends ChatEvent {
  final String messageId;
  final String content;
  final bool error;
  const ChatMessageCommitted({
    required super.locator,
    required super.runId,
    required this.messageId,
    required this.content,
    required this.error,
  });
}

class ChatRunCompleted extends ChatEvent {
  const ChatRunCompleted({required super.locator, required super.runId});
}

class ChatRunStopped extends ChatEvent {
  const ChatRunStopped({required super.locator, required super.runId});
}

class ChatErrorEvent extends ChatEvent {
  final String message;
  final String? assistantMessageId;
  const ChatErrorEvent({
    required super.locator,
    required super.runId,
    required this.message,
    this.assistantMessageId,
  });
}

class ChatStatusEvent extends ChatEvent {
  final String message;
  final String phase;

  const ChatStatusEvent({
    required super.locator,
    required super.runId,
    required this.message,
    this.phase = '',
  });
}

class ToolCallEvent extends ChatEvent {
  final String toolId;
  final String toolName;
  final String status;
  final Map<String, dynamic> arguments;
  final String? summary;
  final String? output;

  const ToolCallEvent({
    required super.locator,
    required super.runId,
    required this.toolId,
    required this.toolName,
    required this.status,
    this.arguments = const {},
    this.summary,
    this.output,
  });
}

class ApprovalEvent extends ChatEvent {
  final String approvalId;
  final String toolName;
  final String prompt;
  final Map<String, dynamic> arguments;
  final bool approved;
  final bool pending;

  const ApprovalEvent({
    required super.locator,
    required super.runId,
    required this.approvalId,
    required this.toolName,
    required this.prompt,
    this.arguments = const {},
    this.approved = false,
    this.pending = true,
  });
}
