import '../data/local/local_chat_backend.dart';
import '../data/pc/pc_chat_backend.dart';
import '../domain/conversation_backend.dart';
import '../domain/conversation_locator.dart';

class ConversationRouter {
  ConversationRouter({
    required LocalConversationBackend local,
    PcConversationBackend? pc,
  })  : _local = local,
        _pc = pc;

  final LocalConversationBackend _local;
  PcConversationBackend? _pc;

  LocalConversationBackend get local => _local;
  PcConversationBackend? get pc => _pc;

  void setPc(PcConversationBackend? pc) {
    _pc = pc;
  }

  ConversationBackend backendFor(ConversationLocator locator) {
    return switch (locator.authority) {
      ConversationAuthorityKind.local => _local,
      ConversationAuthorityKind.pc => _pc ?? _unsupportedPc(),
    };
  }

  ConversationBackend _unsupportedPc() {
    throw StateError(
        'No PC backend is configured. Pair a device before using PC conversations.');
  }
}
