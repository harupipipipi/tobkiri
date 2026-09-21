enum ToolExecutionLocation { pc, mobile, cloud, either }

class ToolCapability {
  const ToolCapability({
    required this.id,
    required this.displayName,
    required this.location,
    required this.riskLevel,
    required this.available,
    required this.requiresApproval,
    this.unavailableReason,
  });

  final String id;
  final String displayName;
  final ToolExecutionLocation location;
  final String riskLevel;
  final bool available;
  final bool requiresApproval;
  final String? unavailableReason;
}
