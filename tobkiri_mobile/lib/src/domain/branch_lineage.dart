import 'conversation_locator.dart';

enum BranchReason {
  offlineContinue,
  switchToPc,
  switchToLocal,
  editPreviousMessage,
  manualBranch,
}

class BranchLineage {
  const BranchLineage({
    required this.parentConversationId,
    required this.forkedAtMessageId,
    required this.parentAuthority,
    this.parentDeviceId,
    required this.reason,
  });

  final String parentConversationId;
  final String forkedAtMessageId;
  final ConversationAuthorityKind parentAuthority;
  final String? parentDeviceId;
  final BranchReason reason;

  Map<String, dynamic> toJson() => {
        'parentConversationId': parentConversationId,
        'forkedAtMessageId': forkedAtMessageId,
        'parentAuthority': parentAuthority.name,
        'parentDeviceId': parentDeviceId,
        'reason': reason.name,
      };

  factory BranchLineage.fromJson(Map<String, dynamic> json) {
    return BranchLineage(
      parentConversationId: json['parentConversationId'] as String,
      forkedAtMessageId: json['forkedAtMessageId'] as String,
      parentAuthority: ConversationAuthorityKind.values.firstWhere(
        (e) => e.name == json['parentAuthority'],
        orElse: () => ConversationAuthorityKind.local,
      ),
      parentDeviceId: json['parentDeviceId'] as String?,
      reason: BranchReason.values.firstWhere(
        (e) => e.name == json['reason'],
        orElse: () => BranchReason.manualBranch,
      ),
    );
  }
}
