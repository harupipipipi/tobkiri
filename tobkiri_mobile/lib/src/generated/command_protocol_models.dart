// Generated from command-protocol-v1.schema.json; do not edit.
typedef JsonMap = Map<String, Object?>;

enum CommandMode { chat, coding, agent }

final class CommandInvocationRequest {
  const CommandInvocationRequest({
    required this.commandRef,
    required this.args,
    required this.invocationId,
    required this.mode,
    this.conversationId,
    this.profileId,
    this.catalogRevision,
    this.expectedRevision,
    this.idempotencyKey,
    this.clientSequence,
    this.approvalToken,
    this.authorityRequestId,
    this.authorityApprovalToken,
  });
  final String commandRef;
  final JsonMap args;
  final String invocationId;
  final CommandMode mode;
  final String? conversationId;
  final String? profileId;
  final String? catalogRevision;
  final int? expectedRevision;
  final String? idempotencyKey;
  final int? clientSequence;
  final String? approvalToken;
  final String? authorityRequestId;
  final String? authorityApprovalToken;
  JsonMap toJson() => <String, Object?>{
        "command_ref": commandRef,
        "args": args,
        "invocation_id": invocationId,
        "mode": mode.name,
        if (conversationId != null) "conversation_id": conversationId,
        if (profileId != null) "profile_id": profileId,
        if (catalogRevision != null) "catalog_revision": catalogRevision,
        if (expectedRevision != null) "expected_revision": expectedRevision,
        if (idempotencyKey != null) "idempotency_key": idempotencyKey,
        if (clientSequence != null) "client_sequence": clientSequence,
        if (approvalToken != null) "approval_token": approvalToken,
        if (authorityRequestId != null)
          "authority_request_id": authorityRequestId,
        if (authorityApprovalToken != null)
          "authority_approval_token": authorityApprovalToken,
      };
}

final class CommandProtocolError {
  const CommandProtocolError({required this.code, required this.message});
  final String code;
  final String message;
  JsonMap toJson() => <String, Object?>{"code": code, "message": message};
  factory CommandProtocolError.fromJson(JsonMap json) => CommandProtocolError(
        code: json["code"] as String? ?? '',
        message: json["message"] as String? ?? '',
      );
}

final class CommandInvocationResult {
  const CommandInvocationResult({
    required this.apiVersion,
    required this.operationId,
    required this.status,
    required this.commandRef,
    required this.stateChanges,
    this.error,
  });
  final String apiVersion;
  final String operationId;
  final String status;
  final String commandRef;
  final List<JsonMap> stateChanges;
  final CommandProtocolError? error;
  JsonMap toJson() => <String, Object?>{
        "api_version": apiVersion,
        "operation_id": operationId,
        "status": status,
        "command_ref": commandRef,
        "state_changes": stateChanges,
        if (error != null) "error": error!.toJson(),
      };
  factory CommandInvocationResult.fromJson(JsonMap json) =>
      CommandInvocationResult(
        apiVersion: json["api_version"] as String? ?? '',
        operationId: json["operation_id"] as String? ?? '',
        status: json["status"] as String? ?? '',
        commandRef: json["command_ref"] as String? ?? '',
        stateChanges: (json["state_changes"] as List<Object?>? ?? const [])
            .whereType<JsonMap>()
            .toList(growable: false),
        error: json["error"] is JsonMap
            ? CommandProtocolError.fromJson(json["error"] as JsonMap)
            : null,
      );
}
