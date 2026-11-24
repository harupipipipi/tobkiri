// Generated from command-protocol-v1.schema.json; do not edit.
export type CommandMode = "chat" | "coding" | "agent";
export interface CommandInvocationRequest {
  command_ref: string;
  args: Record<string, unknown>;
  invocation_id: string;
  mode: CommandMode;
  conversation_id?: string;
  profile_id?: string;
  catalog_revision?: string;
  expected_revision?: number;
  idempotency_key?: string;
  client_sequence?: number;
  approval_token?: string;
  authority_request_id?: string;
  authority_approval_token?: string;
}

export interface CommandProtocolError {
  code: string;
  message: string;
}

export interface CommandInvocationResult {
  api_version: "tobkiri.commands/v1";
  operation_id: string;
  status: "succeeded" | "failed" | "approval_required" | "cancelled";
  command_ref: string;
  state_changes: Array<Record<string, unknown>>;
  error?: CommandProtocolError;
}
