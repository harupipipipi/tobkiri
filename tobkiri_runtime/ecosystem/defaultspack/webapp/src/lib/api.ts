import type { ToolPreviewItem } from "../components/ToolPreview";
import type { CommandInvocationRequest } from "../generated/commandProtocolModels";
import type { AuthorityApprovalScope } from "./authorityApproval";
import { defaultspackUrlWithLocalAuthToken } from "./defaultspackLocalAuth";

const PANEL_CSRF_STORAGE_KEY = "rumi-panel-csrf";
const DEFAULTSPACK_CSRF_STORAGE_KEY = "rumi-defaultspack-csrf";
const DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY = "rumi-defaultspack-local-auth";
const DEFAULTSPACK_LOCAL_AUTH_FRAGMENT_KEY = "rumi_local_auth";
let defaultspackLocalAuthMemoryToken = "";
let defaultspackLocalAuthBootstrapped = false;

export type ChatContentBlock = {
  type?: string;
  text?: string;
  [key: string]: unknown;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | string;
  content: string | ChatContentBlock[];
  raw_text?: string | null;
  created_at: number;
  conversation_id: string;
  parent_id?: string | null;
  children_ids?: string[];
  sequence_number?: number;
  finish_reason?: string | null;
  usage?: Record<string, number> | null;
  widget?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  events?: ChatActivityEvent[] | null;
  tool_logs?: ToolLogEntry[] | null;
  model?: string | null;
};

export type TokenizerInfo = {
  available?: boolean;
  fallback?: boolean;
  status?: string;
  source?: string;
  warning?: string;
  warning_code?: string;
  tokenizer_id?: string;
  tokenizer_profile_id?: string;
  tokenizer_provider_id?: string;
  tokenizer_model?: string;
  provider_id?: string;
  model_profile_id?: string;
  model?: string;
};

export type PromptUsageSegment = {
  id: string;
  edge_id?: string;
  prompt_id?: string;
  label?: string;
  kind?: string;
  port?: string;
  status?: "active" | "disabled" | "gated" | "budget-dropped" | string;
  enabled?: boolean;
  source?: string;
  source_type?: string;
  source_chain?: Record<string, unknown>[];
  tokens?: number;
  tokenizer?: TokenizerInfo;
  reason?: string;
  allow_disable?: boolean;
  editable?: boolean;
  readonly_reason?: string;
  preview?: string;
  text?: string;
  explanation?: string;
  input_role?: string;
  source_priority?: string;
  activation_detail?: Record<string, unknown>;
  safety_boundary?: Record<string, unknown>;
  tool_signal?: Record<string, unknown>;
  skill_signal?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

export type PromptUsageSummary = {
  trace_id?: string;
  profile_id?: string;
  conversation_id?: string;
  run_id?: string;
  active_count?: number;
  disabled_count?: number;
  token_estimate?: {
    total?: number;
    by_port?: Record<string, number>;
    by_node?: Record<string, number>;
    tokenizer?: TokenizerInfo;
  };
  segments?: PromptUsageSegment[];
  active_segments?: PromptUsageSegment[];
  disabled_segments?: PromptUsageSegment[];
  source_counts?: Record<string, number>;
};

export type PromptStudioPrompt = {
  id: string;
  name: string;
  prompt_id?: string;
  description?: string;
  body?: string;
  content?: string;
  body_hash?: string;
  variables?: Record<string, unknown>[];
  metadata?: Record<string, unknown>;
  source_type?: string;
  source?: string;
  effective_source?: string;
  effective_source_type?: string;
  read_only?: boolean;
  editable?: boolean;
  tokens?: number;
  tokenizer?: TokenizerInfo;
  preview?: string;
  activation_state?: string;
  active_edge_id?: string;
  active_reason?: string;
  allow_disable?: boolean;
  override_allowed?: boolean;
  source_chain?: Record<string, unknown>[];
  validation?: Record<string, unknown>;
  lint?: Record<string, unknown>;
  versions?: PromptVersionRecord[];
  safety?: Record<string, unknown>;
  input_role?: string;
  source_priority?: string;
  activation_detail?: Record<string, unknown>;
  tool_signal?: Record<string, unknown>;
  skill_signal?: Record<string, unknown>;
};

export type PromptVersionRecord = {
  version_id: string;
  profile_id?: string;
  prompt_id?: string;
  scope?: string;
  created_at?: string;
  reason?: string;
  metadata?: Record<string, unknown>;
};

export type PromptStudioData = {
  profile_id: string;
  model_profile_id?: string;
  model?: string;
  tokenizer?: TokenizerInfo;
  profile_workspace?: Record<string, string>;
  prompts: PromptStudioPrompt[];
  selected_prompt?: PromptStudioPrompt | null;
  active_summary?: PromptUsageSummary;
};

export type PromptStudioTestResult = {
  profile_id: string;
  prompt_id?: string;
  conversation_id?: string;
  input?: {
    user_text?: string;
    selected_tools?: string[];
    model_profile_id?: string;
    model?: string;
  };
  model_profile_id?: string;
  model?: string;
  summary?: PromptUsageSummary;
  segments?: PromptUsageSegment[];
  matched_skills?: Record<string, unknown>[];
  skill_instructions?: string;
  selected_tool_records?: Record<string, unknown>[];
  selected_tool_segments?: PromptUsageSegment[];
  candidate_tool_segments?: PromptUsageSegment[];
  tool_candidates?: {
    combined?: Record<string, unknown>[];
    from_prompt?: Record<string, unknown>[];
    from_input?: Record<string, unknown>[];
  };
  prompt_tool_analysis?: Record<string, unknown>;
  template_tool_policy_resolution?: Record<string, unknown>;
  safety_boundary?: Record<string, unknown>;
  verdicts?: Record<string, string>[];
};

export type PromptTraceSummary = {
  trace_id?: string;
  created_at?: number;
  conversation_id?: string;
  run_id?: string;
  profile_id?: string;
  token_estimate?: PromptUsageSummary["token_estimate"];
  provider_payload_summary?: Record<string, unknown>;
  blocked_count?: number;
};

export type PromptTraceDetail = {
  profile_id: string;
  trace: Record<string, unknown>;
  prompt_usage: PromptUsageSummary;
};

export type PromptToggleResponse = {
  profile_id: string;
  edge_id: string;
  enabled: boolean;
  preview?: boolean;
  ai_input?: Record<string, unknown>;
  summary: PromptUsageSummary;
};

export type ChatAttachment = {
  name: string;
  content?: string;
  dataUrl?: string;
  size: number;
  type?: string;
  truncated?: boolean;
  source?: "local_file" | "workspace";
  sourcePath?: string;
};

export class ChatStreamInterruptedError extends Error {
  partialText: string;
  thinkingText: string;
  sawActivity: boolean;

  constructor(
    message: string,
    details: {
      partialText?: string;
      thinkingText?: string;
      sawActivity?: boolean;
    } = {},
  ) {
    super(message);
    this.name = "ChatStreamInterruptedError";
    this.partialText = details.partialText ?? "";
    this.thinkingText = details.thinkingText ?? "";
    this.sawActivity = details.sawActivity === true;
  }
}

export type BrowserScreenshot = {
  id: string;
  run_id: string;
  tool_call_id?: string | null;
  tool_name?: string;
  mime_type?: string;
  data_url: string;
  action?: string;
  image_size?: { width?: number; height?: number };
  click_marker?: { x?: number; y?: number; screen_x?: number; screen_y?: number; coordinate_space?: string };
  marker?: { x?: number; y?: number; screen_x?: number; screen_y?: number; coordinate_space?: string };
  drag_marker?: {
    from?: { x?: number; y?: number; screen_x?: number; screen_y?: number; coordinate_space?: string };
    to?: { x?: number; y?: number; screen_x?: number; screen_y?: number; coordinate_space?: string };
  };
  target_window?: Record<string, unknown>;
};

export type CodingContextEntry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
};

export type CodingGitStatus = {
  branch?: string;
  clean?: boolean;
  staged?: string[];
  modified?: string[];
  untracked?: string[];
  porcelain?: string;
  [key: string]: unknown;
};

export type CodingContextResponse = {
  branch: string | null;
  root_folder: string | null;
  workspace_id?: string | null;
  workspace_root?: string | null;
  directory?: string;
  files: string[];
  entries?: CodingContextEntry[];
  git?: CodingGitStatus | null;
};

export type CodingBranchResponse = {
  branch: string;
  branches: string[];
  remote?: string | null;
  dirty?: boolean;
  switched?: boolean;
  created?: boolean;
  output?: string;
  workspace_id?: string | null;
  workspace_root?: string | null;
};

export type CodingWorkspaceRecord = {
  workspace_id: string;
  label: string;
  root_path: string;
  trusted?: boolean;
  trust_granted_at?: string | null;
  last_used_at?: string | null;
  metadata?: Record<string, unknown>;
};

export type CodingWorkspacesResponse = {
  workspaces: CodingWorkspaceRecord[];
  selected_workspace_id?: string | null;
};

export type DirectorySelectionResponse = {
  path: string | null;
  cancelled?: boolean;
};

export type ChatGroupStorageResponse = {
  root_path: string;
  rumi_data_path: string;
  chat_store_path: string;
};

export type CodingApprovalRequest = {
  request_id: string;
  operation: string;
  risk_level: string;
  args_hash?: string;
  details?: Record<string, unknown>;
  created_at?: number;
  expires_at?: number;
  status: string;
  display_summary?: string;
};

export type McpApprovalReview = {
  executable?: string;
  normalized_executable?: string;
  transport?: string;
  args?: unknown[];
  normalized_args?: unknown[];
  cwd?: string;
  normalized_cwd?: string;
  redacted_env?: Record<string, unknown> | unknown[];
  environment_redacted?: Record<string, unknown> | unknown[];
  env?: Record<string, unknown>;
  server_source?: string;
  source?: string;
  capabilities?: unknown;
  tools?: unknown;
  network?: unknown;
  filesystem?: unknown;
  persistence?: unknown;
  consequences?: unknown;
};

export type McpConnectResponse = {
  server_id?: string;
  server_name?: string;
  workspace_id?: string | null;
  status?: string;
  approval_required?: boolean;
  approval_request_id?: string;
  approval_request?: CodingApprovalRequest;
  tools_added?: number;
  tools?: unknown[];
  [key: string]: unknown;
};

export type CodingApprovalDecision = {
  request_id: string;
  status: string;
  approved: boolean;
  token?: string;
  expires_at?: number | null;
  reason?: string;
};

export type AuthorityApprovalDecision = {
  request_id: string;
  approved: boolean;
  scope: AuthorityApprovalScope;
  token?: string;
  expires_at?: string | null;
  principal_id?: string;
  permission_id?: string;
  config?: Record<string, unknown>;
  resource?: Record<string, unknown>;
  related_approvals?: AuthorityApprovalDecision[];
};

/**
 * The only approval state that the interactive-approval Pack may return to a
 * web surface.  Authority material intentionally is not representable here:
 * the Host consumes the one-shot grant itself when it resumes the effect.
 */
export type InteractiveApprovalRequest = {
  request_id: string;
  request_snapshot_digest: string;
  state: string;
  expires_at: number;
  typed_confirmation_required: boolean;
  typed_confirmation_digest: string | null;
  redacted_metadata: Record<string, string>;
};

/** A redacted list projection from the interactive-approval Pack. */
export type InteractiveApprovalRequestsResponse = {
  approvals: InteractiveApprovalRequest[];
};

/**
 * The redacted client projection from the high-risk command Host adapter.
 *
 * ``invocation_id`` is an opaque client correlation value. In particular,
 * effect identity, prepared plans, authority tokens, and command arguments
 * are intentionally not representable after prepare.
 */
export type HighRiskCommandInvocation = {
  invocation_id: string;
  approval_request_id: string | null;
  state: string;
  expires_at: number | null;
  redacted_metadata: Record<string, string>;
};

export type HighRiskCommandInvocationsResponse = {
  invocations: HighRiskCommandInvocation[];
};

export type AuthorityUiOperator = {
  version: number;
  kind: "ui_operator";
  origin: string;
  window_label: string;
  request_id: string;
  decision?: "approve" | "deny";
  request_snapshot_digest?: string;
  typed_confirmation_digest?: string | null;
  issued_at: number;
  expires_at: number;
  nonce: string;
  signature: string;
};

export type AuthorityApprovalContext = {
  request_id: string;
  ui_operator: AuthorityUiOperator;
};

export type AuthorityBrowserExchangeBinding = {
  request_id: string;
  device_id: string;
  window_id: string;
  nonce: string;
  origin: string;
};

export type AuthorityBrowserExchange = {
  request_id: string;
  exchange_code: string;
  exchange_id: string;
  server_nonce?: string;
  expires_at: number | string;
};

export type AuthorityRequestDisplayMetadata = {
  title?: string;
  summary?: string;
  permission_id?: string;
  permission_label?: string;
  provider_id?: string | null;
  api_id?: string | null;
  model_id?: string | null;
  function_id?: string | null;
  pack_id?: string | null;
  app_display_name?: string | null;
  provider_display_name?: string | null;
  model_display_name?: string | null;
  endpoint_url?: string | null;
  endpoint_host?: string | null;
  endpoint_path?: string | null;
  credential_label?: string | null;
  access_summary?: string | null;
  host_execution_summary?: {
    executable?: string;
    argument_count?: number;
    cwd?: string;
    target_paths?: string[];
    target_urls?: string[];
  } | null;
  risk_level?: string;
  typed_confirmation_required?: boolean;
  confirmation_phrase?: string | null;
  audit_text?: string;
};

export type AuthorityRequest = {
  request_id: string;
  status: "pending" | "approved" | "denied" | "expired" | string;
  principal_id: string;
  permission_id: string;
  resource: Record<string, unknown>;
  reason: string;
  risk_level: string;
  created_at: string;
  expires_at?: string | null;
  conversation_id?: string | null;
  profile_id?: string | null;
  node_id?: string | null;
  graph_id?: string | null;
  display_metadata?: AuthorityRequestDisplayMetadata;
  allowed_scopes?: AuthorityApprovalScope[];
};

export type AuthorityRequestsResponse = {
  requests: AuthorityRequest[];
  pending: AuthorityRequest[];
  count: number;
};

export type CodingCheckpoint = {
  snapshot_id: string;
  path?: string;
  kind?: string;
  files?: string[];
  created_at?: string;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type RumiLogEvent = {
  event_id: string;
  created_at: string;
  kind: string;
  actor_id?: string;
  agent_role?: string;
  session_id?: string;
  status?: string;
  message?: string;
  branch?: string;
  commit_hash?: string;
  remote?: string;
  paths?: string[];
  metadata?: Record<string, unknown>;
};

export type RumiLogSummary = {
  total: number;
  by_kind?: Record<string, number>;
  by_status?: Record<string, number>;
  agent_ids?: string[];
  commit_count?: number;
  push_count?: number;
  plan_count?: number;
  task_count?: number;
  conversation_count?: number;
  mention_count?: number;
  last_event_at?: string | null;
  last_commit_hash?: string | null;
};

export type RumiLogResponse = {
  rumi_dir: string;
  events_path?: string;
  event?: RumiLogEvent;
  events: RumiLogEvent[];
  summary: RumiLogSummary;
  created?: boolean;
  workspace_id?: string | null;
  workspace_root?: string | null;
};

export type CodingDiffResponse = {
  diff: string;
  stat?: string;
  files?: string[];
  files_changed?: number;
  workspace_id?: string | null;
  workspace_root?: string | null;
};

export type CodingTerminalResponse = {
  command: string;
  approval_required?: boolean;
  approval_request_id?: string;
  operation?: string;
  risk_level?: string;
  args_hash?: string;
  expires_at?: number;
  display_summary?: string;
  classification?: string;
  risk_reasons?: string[];
  risk?: Record<string, unknown>;
  exit_code?: number | null;
  stdout?: string;
  stderr?: string;
  workspace_id?: string | null;
  workspace_root?: string | null;
};

export type BrowserArtifact = {
  artifact_id: string;
  session_id: string;
  action: string;
  created_at: string;
  url?: string | null;
  text?: string | null;
  console?: unknown[];
  screenshot?: {
    path?: string;
    data_url?: string;
    mime_type?: string;
    image_size?: { width?: number; height?: number };
  } | null;
  metadata?: Record<string, unknown>;
};

export type McpServerRecord = {
  server_id: string;
  name?: string;
  server_name?: string;
  transport?: string;
  status?: string;
  connected?: boolean;
  tools?: unknown[];
  permissions?: Record<string, unknown>;
  config?: Record<string, unknown>;
  inspect?: {
    server_id?: string;
    name?: string;
    transport?: string;
    status?: string;
    connected?: boolean;
    command?: string | null;
    args?: string[];
    endpoint?: string | null;
    tools?: string[];
    updated_at?: string;
  };
};

export type CodingAgentSession = {
  session_id: string;
  status: string;
  task?: string;
  agents?: Array<Record<string, unknown>>;
  shared_context?: Record<string, unknown>;
  agent_contexts?: Record<string, unknown>;
  [key: string]: unknown;
};

export type CompanyAgent = {
  id?: string;
  agent_id: string;
  role_key?: string;
  agent_name?: string;
  display_name?: string;
  model?: string;
  allowed_tools?: string[];
  context_limit?: number;
  aliases?: string[];
  system_prompt?: string;
  status?: string;
  agent_kind?: "main" | "subagent";
  runtime_kind?: "agent_run" | "utility_model_call" | "human_task" | "remote_agent" | "composite_team";
  subagent_role?: string;
  placement_id?: string;
  placement_revision?: string;
  placement_map_id?: string;
  effective_plan_hash?: string;
  protocol_membership?: string[];
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type CompanyChannel = {
  id: string;
  name?: string;
  description?: string;
  visibility?: string;
  members?: string[];
  mentions?: boolean;
  append_only?: boolean;
  message_count?: number;
  last_message_at?: string | null;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type CompanyMessage = {
  id: string;
  company_id: string;
  channel_id: string;
  sender_id: string;
  content: string;
  mentions?: string[];
  task_ids?: string[];
  metadata?: Record<string, unknown>;
  handoff?: {
    target_agent_id?: string;
    reason?: string;
  };
  attachments?: Array<{
    name?: string;
    path?: string;
    url?: string;
    mime_type?: string;
    size?: number;
  }>;
  created_at?: string;
  updated_at?: string;
};

export type CompanyTask = {
  id: string;
  company_id: string;
  title: string;
  description?: string;
  target_agent_ids?: string[];
  source?: string;
  status?: string;
  dispatches?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type CompanyRunLink = {
  link_id: string;
  company_id: string;
  task_id?: string | null;
  thread_id?: string | null;
  message_id?: string | null;
  agent_id: string;
  run_id: string;
  status: string;
  heartbeat_at?: string | null;
  agent_run?: {
    status?: string | null;
    model?: string | null;
    result_preview?: string;
    error?: string | null;
    conversation?: CompanyRunConversationMessage[];
    updated_at?: string | null;
  };
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type CompanyRunConversationMessage = {
  role: string;
  label?: string;
  content: string;
  is_error?: boolean;
};

export type KanbanBoardScopeType = "conversation" | "workspace" | "company" | "group" | "global";

export type KanbanBoardScope = {
  type: KanbanBoardScopeType;
  id: string;
};

export type KanbanPriority = "low" | "normal" | "high" | "urgent" | string;

export type KanbanChecklistItem = {
  id: string;
  title: string;
  done: boolean;
};

export type KanbanBoard = {
  board_id: string;
  scope_type: KanbanBoardScopeType;
  scope_id: string;
  title: string;
  metadata?: Record<string, unknown>;
  created_at?: number;
  updated_at?: number;
};

export type KanbanColumn = {
  column_id: string;
  board_id: string;
  title: string;
  position: number;
  done?: boolean | number;
  wip_limit?: number | null;
  created_at?: number;
  updated_at?: number;
};

export type KanbanCard = {
  card_id: string;
  board_id: string;
  column_id: string;
  position: number;
  title: string;
  description?: string | null;
  priority?: KanbanPriority;
  assignee?: string | null;
  due_at?: string | null;
  labels?: string[];
  checklist?: KanbanChecklistItem[];
  depends_on?: string[];
  blocked_by?: string[];
  source_type?: string;
  source_id?: string | null;
  conversation_id?: string | null;
  workspace_id?: string | null;
  company_id?: string | null;
  agent_run_id?: string | null;
  agent_session_id?: string | null;
  agent_status?: string | null;
  branch?: string | null;
  pr_url?: string | null;
  metadata?: Record<string, unknown>;
  created_at?: number;
  updated_at?: number;
};

export type KanbanEvent = {
  event_id?: string;
  board_id?: string;
  card_id?: string | null;
  event_type?: string;
  actor_type?: string;
  actor_id?: string | null;
  payload?: Record<string, unknown>;
  created_at?: number;
  [key: string]: unknown;
};

export type KanbanBoardResponse = {
  board: KanbanBoard;
  columns: KanbanColumn[];
  cards: KanbanCard[];
  events?: KanbanEvent[];
  imported?: {
    conversation_id?: string;
    card_ids?: string[];
    conversation?: Record<string, unknown>;
    extraction?: Record<string, unknown>;
  };
};

export type KanbanMovePayload = {
  column_id: string;
  before_card_id?: string | null;
  after_card_id?: string | null;
  position?: number;
};

export type KanbanImportConversationPayload = {
  conversation_id: string;
  column_id?: string | null;
  title?: string;
  model?: string;
  workspace_id?: string | null;
  company_id?: string | null;
  use_ai?: boolean;
};

export type CompanyInboxItem = {
  inbox_id: string;
  company_id: string;
  agent_id: string;
  message_id?: string | null;
  task_id?: string | null;
  run_id?: string | null;
  kind: string;
  status: string;
  priority?: string;
  content: string;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type SubagentTeamFileTreeEntry = {
  id?: string;
  node_id?: string;
  nodeId?: string;
  name?: string;
  label?: string;
  title?: string;
  path?: string;
  file_path?: string;
  relative_path?: string;
  is_dir?: boolean;
  is_directory?: boolean;
  kind?: string;
  type?: string;
  depth?: number;
  size?: number;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SubagentTeamFileTreeResponse = {
  workspace_id?: string;
  workspaceId?: string;
  workspace_hash?: string;
  root?: string;
  directory?: string;
  files?: SubagentTeamFileTreeEntry[];
  file_tree?: SubagentTeamFileTreeEntry[];
  tree?: SubagentTeamFileTreeEntry[];
  nodes?: SubagentTeamFileTreeEntry[];
  items?: SubagentTeamFileTreeEntry[];
  history?: SubagentTeamFileTreeEntry[];
  history_tree?: SubagentTeamFileTreeEntry[];
  events?: SubagentTeamFileTreeEntry[];
  clipped?: boolean;
  status?: Record<string, unknown>;
  policy?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SubagentTeamFileTreeOpenResponse = {
  id?: string;
  node_id?: string;
  nodeId?: string;
  title?: string;
  label?: string;
  name?: string;
  path?: string;
  file_path?: string;
  content?: string;
  file_content?: string;
  text?: string;
  preview?: string;
  body?: string;
  markdown?: string;
  messages?: Array<Record<string, unknown>>;
  history?: Array<Record<string, unknown>>;
  conversation?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SubagentTeamRichSettings = {
  rich_enabled?: boolean;
  enabled?: boolean;
  max_subagents?: number;
  maxSubagents?: number;
  active_subagents?: number;
  activeSubagents?: number;
  current_subagents?: number;
  currentSubagents?: number;
  can_user_toggle?: boolean;
  canUserToggle?: boolean;
  can_creator_enable_rich?: boolean;
  canCreatorEnableRich?: boolean;
  creator_can_enable_rich?: boolean;
  reason?: string;
  status?: string;
  policy?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SubagentTeamRichSettingsResponse = SubagentTeamRichSettings & {
  settings?: SubagentTeamRichSettings;
  rich?: SubagentTeamRichSettings;
};

export type SubagentTeamCreatorSettings = {
  enabled?: boolean;
  model?: string;
  lifecycle_only?: boolean;
  lifecycleOnly?: boolean;
  can_manage_agents?: boolean;
  canManageAgents?: boolean;
  can_enable_rich?: boolean;
  canEnableRich?: boolean;
  rich_gate_message?: string;
  richGateMessage?: string;
  status?: string;
  policy?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type SubagentTeamCreatorSettingsResponse = SubagentTeamCreatorSettings & {
  settings?: SubagentTeamCreatorSettings;
  creator?: SubagentTeamCreatorSettings;
};

export type SubagentTeamCreatorTestResponse = {
  ok?: boolean;
  status?: string;
  message?: string;
  summary?: string;
  result?: Record<string, unknown> | string;
  [key: string]: unknown;
};

export type SubagentTeamDecisionPreviewResponse = {
  task?: Partial<CompanyTask>;
  decision?: Record<string, unknown>;
  approval?: Record<string, unknown>;
  preview?: Record<string, unknown>;
  status?: string;
  title?: string;
  summary?: string;
  target_agent_ids?: string[];
  [key: string]: unknown;
};

export type CompanyInboundRoute = {
  id: string;
  provider?: string;
  source?: string;
  channel_id?: string;
  enabled?: boolean;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type CompanyRecord = {
  id: string;
  name: string;
  description?: string;
  status?: string;
  conversation_group_id?: string;
  settings?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  agents?: Record<string, CompanyAgent> | CompanyAgent[];
  channels?: Record<string, CompanyChannel> | CompanyChannel[];
  messages?: Record<string, CompanyMessage> | CompanyMessage[];
  tasks?: Record<string, CompanyTask> | CompanyTask[];
  inbound_routes?: Record<string, CompanyInboundRoute> | CompanyInboundRoute[];
  agent_count?: number;
  channel_count?: number;
  message_count?: number;
  task_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type CompanyStatusResponse = {
  bootstrapped: boolean;
  company_id: string;
  conversation_id?: string;
  company?: CompanyRecord | null;
  runtime?: Record<string, number>;
  storage_file?: string;
};

export type RemoteTaskCreateRequest = {
  input: string;
  title?: string;
  company_id?: string;
  target_agent_ids?: string[];
  priority?: "low" | "normal" | "high" | string;
  dispatch?: boolean;
  client?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

export type RemoteTaskSnapshot = {
  remote_task_id: string;
  company_id: string;
  task_id: string;
  message_id?: string;
  thread_id?: string;
  state: string;
  task?: Record<string, unknown>;
  routes?: Array<Record<string, unknown>>;
  run_links?: Array<Record<string, unknown>>;
  agent_runs?: Array<Record<string, unknown>>;
  inbox?: Array<Record<string, unknown>>;
  waiting_approvals?: Array<Record<string, unknown>>;
  updated_at?: string;
  next_poll_ms?: number;
};

export type RemoteTaskEvent = {
  cursor: string;
  type: string;
  message?: string;
  task_id?: string;
  run_id?: string;
  agent_id?: string;
  status?: string;
  created_at?: string;
  data?: Record<string, unknown>;
  [key: string]: unknown;
};

export type RemoteTaskEventsResponse = {
  events: RemoteTaskEvent[];
  next_cursor?: string;
  next_poll_ms?: number;
};

export type P2PSettings = {
  enabled?: boolean;
  bind_host?: string;
  bind_port?: number;
  lan_discovery?: boolean;
  internet_relay?: boolean;
  store_path?: string;
  envelope_ttl_seconds?: number;
  replay_ttl_seconds?: number;
  pairing_ttl_seconds?: number;
  [key: string]: unknown;
};

export type P2PIdentity = {
  node_id: string;
  fingerprint?: string;
  label?: string;
  node_secret?: string;
  created_at?: number;
  updated_at?: number;
  [key: string]: unknown;
};

export type P2PPeer = {
  peer_id: string;
  fingerprint?: string;
  hmac_secret?: string;
  status?: "pending" | "approved" | "blocked" | string;
  capabilities?: string[];
  allowed_company_ids?: string[];
  label?: string;
  metadata?: Record<string, unknown>;
  created_at?: number;
  updated_at?: number;
};

export type P2PPairing = {
  pairing_id: string;
  code: string;
  status: "pending" | "claimed" | "approved" | "rejected" | "expired" | string;
  expires_at: number;
  created_at: number;
  peer_id?: string;
  peer_fingerprint?: string;
  peer_label?: string;
  capabilities?: string[];
  allowed_company_ids?: string[];
  accepted_at?: number;
  approved_at?: number;
  rejected_at?: number;
  claimed_device_id?: string;
  claimed_device_label?: string;
  confirmation_code?: string;
  requested_scopes?: string[];
  base_urls?: string[];
  pickup_secret?: string;
  reason?: string;
};

export type MobileDevice = {
  device_id: string;
  label: string;
  profile_id?: string;
  platform?: string;
  scopes?: string[];
  status?: string;
  encryption_key_configured?: boolean;
  last_seen_at?: number;
};

export type MobileDevicesResponse = { devices: MobileDevice[]; count?: number };

export type CredentialTransferStatus =
  | "awaiting_confirmation" | "pending" | "accepted" | "completed"
  | "rejected" | "expired" | "revoked" | "cancelled";

export type CredentialTransfer = {
  transfer_id: string;
  status: CredentialTransferStatus;
  device_id: string;
  device_label: string;
  profile_id: string;
  provider_id: string;
  api_id: string;
  provider_label: string;
  created_at: number;
  expires_at: number;
};

export type P2PStatusResponse = {
  p2p: P2PSettings;
  peer_count: number;
  approved_peer_count: number;
};

export type MobilePairingStatus = {
  pairing_id: string;
  status: string;
  expires_at?: number;
  token_pickup_consumed_at?: number;
};

export type MobilePairingReview = {
  pairing: { pairing_id: string; status: string; expires_at: number; claimed_at?: number };
  claim: {
    device_label: string;
    device_id_preview?: string;
    requested_scopes: string[];
    allowed_scopes: string[];
    verification_code?: string;
  };
  claim_hash: string;
};

export type MobilePairingApprovePayload = { claim_hash: string; scopes?: string[] };

export type ConversationListOptions = {
  tag?: string;
  tags?: string[];
  is_starred?: boolean;
  is_pinned?: boolean;
  is_archived?: boolean;
  company_id?: string;
  workspace_id?: string;
  conversation_kind?: string;
  group_id?: string;
  include_messages?: boolean;
  limit?: number;
  offset?: number;
};

export type CompactConversationOptions = {
  protect_last_messages?: number;
  start_message_id?: string;
  end_message_id?: string;
  reason?: string;
  instruction?: string;
  approved?: boolean;
  approval_token?: string;
};

export type CompactConversationResult = {
  conversation?: Conversation;
  summary_message?: ChatMessage | null;
  deleted_message_ids?: string[];
  deleted_count?: number;
  protect_last_messages?: number;
  message?: string;
  mode?: string;
  compactable?: boolean;
  trim_plan?: Record<string, unknown>;
};

export type OperationsCompanyRole = {
  agent_id: string;
  role_key: string;
  agent_name: string;
  display_name: string;
  model?: string;
  allowed_tools?: string[];
  context_limit?: number;
};

export type OperationsCompanyStatus = {
  profile_id: string;
  bootstrapped: boolean;
  org_id?: string | null;
  conversation_id?: string | null;
  org?: {
    org_id: string;
    name: string;
    status: string;
    members?: Record<string, unknown>;
    member_count?: number;
    recent_messages?: unknown[];
  } | null;
  schedules?: Array<Record<string, unknown>>;
  manifest: {
    name?: string;
    non_stop?: boolean;
    can_run_24_7?: boolean;
    roles?: OperationsCompanyRole[];
    scheduler?: Record<string, unknown>;
    model_self_selection?: { allowlist?: string[] };
    tool_policy?: { allowlist?: string[]; denylist?: string[]; role_overrides?: Record<string, string[]> };
  };
};

export type MimoCodingCompanyStatus = OperationsCompanyStatus & {
  company?: CompanyRecord | null;
};

export type ChatActivityEvent = {
  type: string;
  run_id?: string;
  seq?: number;
  message?: string;
  phase?: string;
  status?: string;
  summary?: string;
  next_action?: string;
  nextAction?: string;
  timestamp?: number | string;
  tool_name?: string;
  tool_call_id?: string;
  provider_attempt?: number | string;
  provider_attempt_generation?: number | string;
  model?: string;
  [key: string]: unknown;
};

export type ToolLogEntry = {
  tool_name?: string;
  tool_call_id?: string;
  provider_attempt?: number | string;
  provider_attempt_generation?: number | string;
  arguments?: Record<string, unknown>;
  result?: unknown;
  timestamp?: number | string;
  [key: string]: unknown;
};

export function conversationArtifactFileUrl(conversationId: string, path: string): string {
  return defaultspackContractUrl(
    defaultspackContractRoute(`api/chat/conversations/${encodeURIComponent(conversationId)}/artifact-file?path=${encodeURIComponent(path)}`),
  );
}

export type ModelProfile = {
  profile_id: string;
  display_name: string;
  provider_id?: string;
  provider_display_name?: string;
  model_id?: string;
  type?: string;
  qualified_model_id?: string;
  max_context?: number;
  max_context_tokens?: number;
  supports_thinking?: boolean;
  supports_vision?: boolean;
  supports_image_input?: boolean;
  supports_audio?: boolean;
  supports_audio_input?: boolean;
  supports_tool_calling?: boolean;
  supports_fast?: boolean;
  thinking_levels?: string[];
  default_thinking_level?: string | null;
  speed_tier?: string;
  quality_tier?: string;
  knowledge_level?: number;
  knowledge_band?: string;
  cost_tier?: string;
  capability_tags?: string[];
  recommended_roles?: string[];
  allowed_roles?: string[];
  availability?: Record<string, unknown>;
  tokenizer?: Record<string, unknown>;
  tokenizer_profile_id?: string;
  tokenizer_model_profile_id?: string;
  metadata?: Record<string, unknown>;
  defaults?: Record<string, unknown>;
  pricing?: Record<string, unknown>;
  name_collision?: boolean;
  provider_count_for_model_name?: number;
  disambiguated_name?: string;
  same_model_across_providers_key?: string;
  local?: boolean;
};

export type ModelCandidate = {
  provider_id: string;
  model_id: string;
  label?: string;
  profile_id?: string;
};

export type ModelAvailabilityAfterKeySave =
  | { status: "saved" }
  | { status: "models_available"; profiles: ModelProfile[]; selected_profile_id: string }
  | { status: "route_required"; provider_id: string; api_id: string; candidate_models: ModelCandidate[]; reason: string };

export type ModelCommandCandidate = {
  profile_id: string;
  display_name: string;
  subtitle?: string;
  provider_id?: string;
  provider_display_name?: string;
  model_id?: string;
  qualified_model_id?: string;
  requires_api_key?: boolean;
  api_key_required?: boolean;
  api_key_configured?: boolean;
  configured?: boolean;
  availability?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ModelSearchItem = ModelCommandCandidate & {
  label?: string;
  supports_vision?: boolean;
  supports_image_input?: boolean;
  supports_tool_calling?: boolean;
  supports_thinking?: boolean;
  supports_fast?: boolean;
  speed_tier?: string;
  quality_tier?: string;
  cost_tier?: string;
  knowledge_level?: number;
  capability_tags?: string[];
  recommended_roles?: string[];
  notes?: string;
  score?: number;
};

export type ModelSearchResponse = {
  models: ModelSearchItem[];
  filters_applied: Record<string, unknown>;
};

export type ConversationSteerItem = {
  id: string;
  prompt: string;
  target_type?: string;
  target_id?: string;
  conversation_id?: string;
  status: string;
  visible?: boolean;
  auto_send?: boolean;
  created_at?: string;
  updated_at?: string;
  error?: string;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ConversationSteerResponse =
  | ConversationSteerItem
  | {
    items?: ConversationSteerItem[];
    processed?: ConversationSteerItem[];
    cancelled?: boolean;
    item?: ConversationSteerItem | null;
  };

export type Conversation = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  model: string;
  system_prompt_id?: string | null;
  agent_id?: string | null;
  parent_conversation_id?: string | null;
  child_conversation_ids?: string[];
  conversation_kind?: string;
  group_id?: string | null;
  metadata?: Record<string, unknown> | null;
  tags: string[];
  is_starred: boolean;
  is_pinned?: boolean;
  pinned_at?: number | string | null;
  pin_scope?: "global" | "group" | "company" | string;
  is_archived: boolean;
  current_node_id?: string | null;
  messages: ChatMessage[];
};

export type ConversationShareBundle = {
  schema_version: number;
  kind: "rumi.defaultspack.conversation_share";
  created_at: number;
  source: { pack_id?: string; conversation_id?: string; title?: string; share_token?: string };
  conversation: { schema_version?: number; updated_at?: number; conversation: Conversation };
  assets: { included?: unknown[]; omitted?: Array<Record<string, unknown>>; missing_policy?: string };
  preview?: { target_type?: string; message_count?: number; role_counts?: Record<string, number>; content_trust?: string };
  provenance?: {
    source_pack?: string;
    source_conversation_id?: string;
    created_at?: number;
    target_type?: string;
    model?: { source_model?: string | null; source_provider?: string | null; policy?: string; import_model?: string };
  };
  security: {
    redacted?: boolean;
    permissions?: Record<string, boolean>;
    expires_at?: string | number | null;
    visibility?: string;
    import_modes?: Array<"read_only" | "continue_copy">;
    copy_policy?: string;
    secret_policy?: string;
    attachment_policy?: string;
    malicious_content_policy?: string;
    tool_policy?: string;
  };
};

export type ConversationShareRecord = {
  token: string;
  target_type: string;
  title?: string;
  visibility?: string;
  expires_at?: string | number | null;
  share_url?: string;
  api_url?: string;
  created_at?: string;
  audit?: Array<{ operation: string; timestamp: string; result: string; mode?: string }>;
  content: ConversationShareBundle;
};

export type ConversationSearchMatch = {
  message_id?: string;
  role?: string;
  created_at?: number;
  snippet: string;
  exact?: boolean;
  score?: number;
};

export type ConversationSearchResult = {
  conversation_id: string;
  title: string;
  created_at?: number;
  updated_at?: number;
  is_starred?: boolean;
  is_archived?: boolean;
  score?: number;
  exact_score?: number;
  semantic_score?: number;
  match_count?: number;
  matches?: ConversationSearchMatch[];
};

export type ConversationSearchOptions = {
  date_filter?: "all" | "today" | "7d" | "30d";
  is_starred?: boolean;
  is_archived?: boolean;
  role?: "all" | "user" | "assistant";
  limit?: number;
  offset?: number;
};

export type SidebarCategory = "activity" | "tool" | "widget" | "system" | "integration" | "capability";

export type SidebarFieldOption = {
  value: string | number | boolean;
  label: string;
  provider_id?: string;
  provider_display_name?: string;
  model_id?: string;
  qualified_model_id?: string;
  requires_api_key?: boolean;
  api_key_required?: boolean;
  api_key_configured?: boolean;
  configured?: boolean;
  local?: boolean;
  supports_vision?: boolean;
  supports_image_input?: boolean;
  supports_tool_calling?: boolean;
  supports_thinking?: boolean;
  supports_fast?: boolean;
  speed_tier?: string;
  quality_tier?: string;
  cost_tier?: string;
  knowledge_level?: number;
  capability_tags?: string[];
  recommended_roles?: string[];
  notes?: string;
};

export type SidebarField = {
  id: string;
  label: string;
  type: "text" | "textarea" | "number" | "toggle" | "select" | "color" | "readonly" | "secret" | "api_keys" | "api_key_setup" | "external_tokens" | "public_url" | "model_api_routes" | "continuity";
  default?: unknown;
  required?: boolean;
  help?: string;
  min?: number;
  max?: number;
  options?: SidebarFieldOption[];
  provider_id?: string;
  configured_field?: string;
  advanced?: boolean;
  control_center_section?: string;
  api_keys?: Array<Record<string, unknown>>;
  selector_schema?: Record<string, unknown>;
};

export type ContinuityNode = {
  node_id: string;
  display_name?: string;
  destination_kind?: string;
  platform?: string;
  architecture?: string;
  online?: boolean;
  last_seen_at?: string;
  app_version?: string;
  runtime_providers?: string[];
  sandbox_capabilities?: string[];
  network_reachability_classes?: string[];
  desktop_capacity?: number;
  [key: string]: unknown;
};

export type ContinuityProviderRoute = {
  route_id: string;
  provider_id: string;
  api_id: string;
  model_id: string;
  qualified_route?: string;
  adapter_id?: string;
  provider_extension_ref?: string | null;
  base_url?: string | null;
  auth_scheme?: string;
  header_profile?: string | null;
  allowed_models?: string[];
  capability_hash?: string;
  endpoint_class?: string;
  credential_ref?: string;
  fallback_routes?: string[];
  portable?: boolean;
  blocked_reason?: string | null;
  [key: string]: unknown;
};

export type ContinuityPreflightResult = {
  ok: boolean;
  route?: ContinuityProviderRoute | Record<string, unknown> | null;
  destination?: ContinuityNode | Record<string, unknown> | null;
  checks?: Array<Record<string, unknown>>;
  errors?: Array<Record<string, unknown>>;
};

export type ContinuityHandoffPlan = {
  plan_id: string;
  mode: string;
  sandbox_id: string;
  destination_node_id: string;
  provider_route_ref: ContinuityProviderRoute | Record<string, unknown>;
  fallback_route_refs?: Array<ContinuityProviderRoute | Record<string, unknown>>;
  credential_delegation?: Record<string, unknown>;
  checkpoint_estimate?: Record<string, unknown>;
  resource_preflight?: ContinuityPreflightResult | Record<string, unknown>;
  cutover?: Record<string, unknown>;
  status: string;
  created_at?: string;
  [key: string]: unknown;
};

export type ContinuityHandoffOperation = {
  operation_id: string;
  status: string;
  mode?: string;
  sandbox_id?: string;
  destination_node_id?: string;
  plan?: ContinuityHandoffPlan | Record<string, unknown>;
  message?: string;
  checkpoint_id?: string;
  credential_envelope_id?: string | null;
  destination_primary?: boolean;
  source_primary?: boolean;
  primary_lease?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
  events?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type ContinuityPairingStartResponse = {
  request_id: string;
  code: string;
  display_name?: string;
  created_at?: string;
};

export type ContinuityHandoffRequest = {
  sandbox_id?: string;
  seat_id?: string;
  destination_node_id?: string;
  node_id?: string;
  route_id?: string;
  provider_id?: string;
  api_id?: string;
  model_id?: string;
  provider_route?: Record<string, unknown>;
  mode?: string;
  credential_ttl_seconds?: number;
  credential_max_requests?: number;
  state?: Record<string, unknown>;
};

export type SidebarAction = {
  id: string;
  label: string;
  icon?: string;
  method?: "GET" | "POST" | "PUT" | "DELETE";
  endpoint?: string;
  payload?: Record<string, unknown>;
  preview_type?: "web" | "code" | "file" | "image";
  requires_approval?: boolean;
};

export type ComposerWidgetKind = "tool_toggle" | "button" | "panel" | "selector";

export type ComposerWidgetAction =
  | { type: "open_panel"; target_item_id?: string }
  | {
      type: "call_endpoint";
      endpoint: string;
      method?: "GET" | "POST" | "PUT" | "DELETE";
      payload?: Record<string, unknown>;
      result_surface?: "preview" | "chat" | "silent";
      requires_approval?: boolean;
    }
  | { type: "select_model"; profile_id?: string }
  | { type: "toggle_tool"; tool_id?: string };

export type ToolUiMetadata = {
  group_id?: string;
  group_label?: string;
  group_icon?: string;
  item_icon?: string;
  drop_capabilities?: string[];
  widget_kind?: ComposerWidgetKind | string | null;
  composer_label?: string;
  composer_description?: string;
  composer_icon?: string;
  composer_action?: ComposerWidgetAction;
};

export type ToolCapabilityRequirements = {
  requires_all?: string[];
  requires_any?: string[];
  forbids?: string[];
};

export type ToolSetupState = {
  status?: "ok" | "missing" | string;
  missing?: string[];
};

export type ToolInfo = {
  requires_approval?: boolean;
  approval_policy?: string;
  attachment_policy?: string;
  supports_attachments?: boolean | null;
  capability_requirements?: ToolCapabilityRequirements;
  requires_model_capabilities?: string[];
  requires_input_modalities?: string[];
  requires_runtime_capabilities?: string[];
  setup_state?: ToolSetupState;
  trusted?: boolean;
  source_pack_id?: string;
};

export type SidebarItem = {
  id: string;
  label: string;
  category: SidebarCategory;
  description?: string;
  badge?: string | null;
  tags?: string[];
  risk?: "low" | "medium" | "high" | string | null;
  ui?: ToolUiMetadata;
  tool_info?: ToolInfo;
  origin?: {
    kind: string;
    path?: string;
  };
  panel?: {
    kind: string;
    title?: string;
    fields?: SidebarField[];
    notes?: string[];
    models?: { id: string; name?: string }[];
    actions?: SidebarAction[];
  };
};

export type SettingsSection = {
  id: string;
  label: string;
  description?: string;
  fields: SidebarField[];
};

export type ComposerCommandCategory = "chat" | "model" | "mode" | "coding" | "tools" | "settings" | "debug";
export type ComposerCommandVisibility = "default" | "advanced" | "hidden";
export type ComposerCommandRisk = "low" | "medium" | "high";
export type ComposerCommandMode = "chat" | "coding" | "agent";

export type ComposerCommandArg = {
  name: string;
  type: "string" | "enum" | "boolean";
  required?: boolean;
  values?: string[];
  greedy?: boolean;
  label?: string;
  placeholder?: string;
};

export type ComposerCommandExecution =
  | { type: "frontend"; action: string }
  | { type: "model_command"; action: string }
  | { type: "settings_patch"; section: string; field: string }
  | { type: "rumi_function"; qualified_name: string }
  | { type: "chat_action"; action: string }
  | { type: "pack_block"; qualified_name: string };

export type ComposerCommandItem = {
  id: string;
  name: string;
  aliases?: string[];
  label: string;
  description?: string;
  category: ComposerCommandCategory;
  visibility: ComposerCommandVisibility;
  modes?: ComposerCommandMode[];
  risk: ComposerCommandRisk;
  enabled?: boolean;
  active?: boolean;
  args?: ComposerCommandArg[];
  execution: ComposerCommandExecution;
  source?: string;
  template_id?: string;
  piece_id?: string;
  canonical_id?: string;
  protocol_presentation?: ResolvedCommandProtocolCommand["presentation"];
  protocol_execution?: ResolvedCommandProtocolCommand["execution"];
  availability?: ResolvedCommandProtocolCommand["availability"];
};

export type ComposerCommandExecuteResult = {
  command: ComposerCommandItem;
  executed?: boolean;
  requires_approval?: boolean;
  action?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  message?: string;
  candidates?: ModelCommandCandidate[];
  selected_model?: string | ModelCommandCandidate | null;
  operation_id?: string;
  operation_status?: "pending" | "succeeded" | "failed";
  client_sequence?: number;
  state_changes?: CommandStateSnapshot[];
  approval_request_id?: string;
  approval_kind?: "authority" | "coding";
};

export type CommandStateSnapshot = {
  state_ref: string;
  value: unknown;
  revision: number;
  freshness?: "authoritative" | "stale";
};

export type ResolvedCommandProtocolCommand = {
  canonical_id: string;
  pack_id: string;
  pack_generation: number;
  command_version: string;
  identity: { id: string; name: string; aliases?: string[] };
  presentation: {
    label: { fallback: string };
    description?: { fallback: string };
    category: string;
    visibility: string;
    icon?: string;
    input: { kind: "search_select" | "select" | "toggle" | "action" | "form"; [key: string]: unknown };
    mounts?: Array<{
      slot_ref: string;
      display: "persistent" | "command";
      order?: number;
      [key: string]: unknown;
    }>;
  };
  execution: {
    kind: "state_mutation" | "host_operation" | "pack_operation";
    [key: string]: unknown;
  };
  authorization: {
    risk: ComposerCommandRisk;
    permissions: string[];
    approval_required: boolean;
    approval_policy: "never" | "required";
    executor_policy_ref: string;
  };
  constraints: { modes: ComposerCommandMode[] };
  availability: { status: "available" | "unavailable"; reason?: string; reason_code?: string };
};

export type ResolvedCommandCatalog = {
  api_version: "tobkiri.commands/v1";
  kind: "ResolvedCommandCatalog";
  catalog_revision: string;
  rollout?: {
    feature_flag: "command_protocol_v1";
    phase: "enforced";
    legacy_execution_enabled: false;
  };
  commands: ResolvedCommandProtocolCommand[];
  states?: Array<Record<string, unknown>>;
  datasources?: Array<Record<string, unknown>>;
  state_snapshots: CommandStateSnapshot[];
  diagnostics?: Array<Record<string, unknown>>;
};

export type CommandProtocolInvocationResult = {
  api_version: "tobkiri.commands/v1";
  operation_id: string;
  status: "succeeded" | "failed" | "approval_required" | "cancelled";
  command_ref: string;
  client_sequence?: number;
  state_changes: CommandStateSnapshot[];
  message?: string;
  approval?: {
    required: boolean;
    kind?: "authority" | "coding";
    request_id?: string;
    expires_at?: number;
    permission_ids?: string[];
    details?: {
      approved_arguments?: Record<string, unknown>;
      args?: Record<string, unknown>;
      conversation_id?: string | null;
      mode?: ComposerCommandMode;
      operation_ref?: string;
      [key: string]: unknown;
    };
  } | null;
  error?: { code?: string; message?: string };
  legacy_result?: ComposerCommandExecuteResult;
};

export type TemplateComposerFieldOption = {
  value: string;
  label?: string;
};

export type TemplateComposerField = {
  id: string;
  type?: "select" | "text" | "textarea";
  label?: string;
  description?: string;
  placeholder?: string;
  default?: string;
  required?: boolean;
  options?: TemplateComposerFieldOption[];
};

export type TemplateComposerPosition = "center" | "bottom" | "inline";

export type TemplateComposerLayout = {
  home?: { position?: TemplateComposerPosition };
  conversation?: { position?: TemplateComposerPosition };
};

export type TemplateComposerInput = {
  id: string;
  label?: string;
  description?: string;
  placeholder?: string;
  help?: string;
  layout?: TemplateComposerLayout;
  accepted_modalities?: string[];
  feature_flags?: Record<string, boolean | string | number | null | undefined>;
  fields?: TemplateComposerField[];
  field_layout?: "popover_above" | "inline";
  modes?: ComposerCommandMode[];
  enabled?: boolean;
  component?: string;
  renderer?: string;
  template_id?: string;
  piece_id?: string;
  origin?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

export type TemplateContextPolicy = {
  id: string;
  label?: string;
  description?: string;
  policy?: Record<string, unknown>;
  modes?: ComposerCommandMode[];
  enabled?: boolean;
  template_id?: string;
  piece_id?: string;
  origin?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

export type TemplateToolPolicy = {
  id: string;
  label?: string;
  description?: string;
  toggleable?: boolean;
  default_enabled_tools?: string[];
  default_disabled_tools?: string[];
  allowed_tools?: string[];
  denied_tools?: string[];
  tool_choice?: "auto" | "none" | "required" | Record<string, unknown>;
  parallel_tool_calls?: boolean;
  params?: Record<string, unknown>;
  policy?: Record<string, unknown>;
  modes?: ComposerCommandMode[];
  enabled?: boolean;
  template_id?: string;
  piece_id?: string;
  origin?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

export type TemplateAiInput = {
  id: string;
  label?: string;
  description?: string;
  composer_input?: string;
  composer_input_id?: string;
  context_policy?: string;
  context_policy_id?: string;
  tool_policy?: string;
  tool_policy_id?: string;
  widgets?: string[];
  params?: Record<string, unknown>;
  modes?: ComposerCommandMode[];
  enabled?: boolean;
  template_id?: string;
  piece_id?: string;
  origin?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
};

export type TemplateCatalogMetadataItem = {
  id?: string;
  label?: string;
  description?: string;
  template_id?: string;
  piece_id?: string;
  origin?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  [key: string]: unknown;
};

export type ShellRegion = {
  id: string;
  part_id?: string;
  renderer?: string;
  slot?: string;
  order?: number;
  enabled?: boolean;
};

export type ShellRenderer = {
  id: string;
  component: string;
  regions?: string[];
  fallback?: string;
  module?: string;
  export?: string;
  trust?: "local";
};

export type SkillCatalogItem = {
  id: string;
  label: string;
  description?: string;
  triggers?: string[];
  applies_to_tools?: string[];
  aliases?: string[];
  metadata?: Record<string, unknown>;
};

export type UICatalog = {
  dynamic_host?: import("../host/frontendContracts").FrontendCatalog | null;
  app?: {
    id: string;
    name: string;
    icon?: string;
    account?: {
      display_name?: string;
      email?: string;
      plan_label?: string;
      avatar_url?: string;
      initial?: string;
      source?: string;
    };
  };
  agent_service?: {
    service_id?: string;
    version?: string;
    local_first?: boolean;
    core_requires_api_key?: boolean;
    default_profile?: string;
    counts?: Record<string, number>;
    capabilities?: Array<Record<string, unknown>>;
    profiles?: Array<Record<string, unknown>>;
    presets?: Array<Record<string, unknown>>;
    policy?: Record<string, unknown>;
  };
  shell?: {
    layout?: {
      id: string;
      regions?: ShellRegion[];
    };
    renderers?: ShellRenderer[];
  };
  parts?: Array<{
    id: string;
    kind: string;
    label?: string;
    uses?: string[];
    contracts?: Record<string, string>;
    schema?: Record<string, unknown>;
  }>;
  component_bindings?: Array<{
    part_id: string;
    component: string;
    requires?: string[];
    optional?: string[];
  }>;
  sidebar: {
    filters: { id: "all" | SidebarCategory; label: string }[];
    items: SidebarItem[];
  };
  settings: {
    sections: SettingsSection[];
    values: Record<string, Record<string, unknown>>;
  };
  chat_rendering: {
    renderers: Array<{
      id: string;
      block_types?: string[];
      widget_types?: string[];
      component: string;
      fallback?: string;
    }>;
  };
  skills?: SkillCatalogItem[];
  commands?: ComposerCommandItem[];
  composer_inputs?: TemplateComposerInput[];
  ai_inputs?: TemplateAiInput[];
  tool_policies?: TemplateToolPolicy[];
  context_policies?: TemplateContextPolicy[];
  composer_widgets?: TemplateCatalogMetadataItem[];
  external_io_templates?: TemplateCatalogMetadataItem[];
  templates?: TemplateCatalogMetadataItem[];
  actions?: TemplateCatalogMetadataItem[];
  data_sources?: TemplateCatalogMetadataItem[];
  api_routes?: TemplateCatalogMetadataItem[];
  permissions?: TemplateCatalogMetadataItem[];
  shell_regions?: ShellRegion[];
  shell_renderers?: ShellRenderer[];
  extension_points: Array<{
    id: string;
    path: string;
    description: string;
  }>;
  diagnostics?: Array<{
    level: "info" | "warning" | "error" | string;
    code: string;
    message: string;
    source: string;
  }>;
};

function normalizedCommandIdentity(value: unknown): string {
  return String(value ?? "").trim().toLowerCase();
}

function commandIdentityKeys(command: ComposerCommandItem): string[] {
  const keys = [
    normalizedCommandIdentity(command.id),
    normalizedCommandIdentity(command.name),
  ].filter(Boolean);
  return [...new Set(keys)];
}

export function mergeComposerCommands(
  backendCommands: ComposerCommandItem[] = [],
  catalogCommands: ComposerCommandItem[] = [],
): ComposerCommandItem[] {
  const merged: ComposerCommandItem[] = [];
  const indexByKey = new Map<string, number>();

  const upsert = (command: ComposerCommandItem, source: "backend" | "catalog") => {
    const keys = commandIdentityKeys(command);
    const existingIndex = keys.map((key) => indexByKey.get(key)).find((index) => index !== undefined);
    if (existingIndex !== undefined) {
      if (source === "catalog") {
        return;
      }
      return;
    }
    merged.push(command);
    const nextIndex = merged.length - 1;
    keys.forEach((key) => indexByKey.set(key, nextIndex));
  };

  backendCommands.forEach((command) => upsert(command, "backend"));
  catalogCommands.forEach((command) => upsert(command, "catalog"));
  return merged;
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function hasNonEmptyString(record: Record<string, unknown>, key: string): boolean {
  const value = record[key];
  return typeof value === "string" && value.trim().length > 0;
}

function isSidebarFieldShape(value: unknown): boolean {
  const record = objectRecord(value);
  return Boolean(
    record
    && hasNonEmptyString(record, "id")
    && hasNonEmptyString(record, "label")
    && hasNonEmptyString(record, "type")
  );
}

function isSettingsSectionShape(value: unknown): boolean {
  const record = objectRecord(value);
  return Boolean(
    record
    && hasNonEmptyString(record, "id")
    && hasNonEmptyString(record, "label")
    && Array.isArray(record.fields)
    && record.fields.every(isSidebarFieldShape)
  );
}

function isRecordOfRecords(value: unknown): boolean {
  const record = objectRecord(value);
  return Boolean(
    record
    && Object.values(record).every((item) => objectRecord(item) !== null)
  );
}

/** Validate the required Pack v4 UI catalog shape before ChatApp consumes it. */
export function isUICatalog(value: unknown): value is UICatalog {
  const record = objectRecord(value);
  if (!record) return false;

  const app = record.app === undefined ? null : objectRecord(record.app);
  const sidebar = objectRecord(record.sidebar);
  const settings = objectRecord(record.settings);
  const chatRendering = objectRecord(record.chat_rendering);
  const extensionPoints = record.extension_points;
  if (
    (record.app !== undefined && (!app || !hasNonEmptyString(app, "id") || !hasNonEmptyString(app, "name")))
    || !sidebar
    || !Array.isArray(sidebar.filters)
    || !sidebar.filters.every((filter) => {
      const item = objectRecord(filter);
      return Boolean(item && hasNonEmptyString(item, "id") && hasNonEmptyString(item, "label"));
    })
    || !Array.isArray(sidebar.items)
    || !sidebar.items.every((item) => {
      const sidebarItem = objectRecord(item);
      return Boolean(
        sidebarItem
        && hasNonEmptyString(sidebarItem, "id")
        && hasNonEmptyString(sidebarItem, "label")
        && hasNonEmptyString(sidebarItem, "category")
      );
    })
    || !settings
    || !Array.isArray(settings.sections)
    || !settings.sections.every(isSettingsSectionShape)
    || !isRecordOfRecords(settings.values)
    || !chatRendering
    || !Array.isArray(chatRendering.renderers)
    || !chatRendering.renderers.every((renderer) => {
      const item = objectRecord(renderer);
      return Boolean(item && hasNonEmptyString(item, "id") && hasNonEmptyString(item, "component"));
    })
    || !Array.isArray(extensionPoints)
    || !extensionPoints.every((extensionPoint) => {
      const item = objectRecord(extensionPoint);
      return Boolean(
        item
        && hasNonEmptyString(item, "id")
        && hasNonEmptyString(item, "path")
        && hasNonEmptyString(item, "description")
      );
    })
  ) {
    return false;
  }

  return true;
}

/** Validate the Pack v4 settings response used during startup. */
export function isUiSettingsResponse(
  value: unknown,
): value is { sections: SettingsSection[]; values: Record<string, Record<string, unknown>> } {
  const record = objectRecord(value);
  return Boolean(
    record
    && Array.isArray(record.sections)
    && record.sections.every(isSettingsSectionShape)
    && isRecordOfRecords(record.values)
  );
}

/** Validate the model profile list before Profile controls receive it. */
export function isModelProfilesResponse(
  value: unknown,
): value is { profiles: ModelProfile[]; count: number } {
  const record = objectRecord(value);
  return Boolean(
    record
    && Array.isArray(record.profiles)
    && record.profiles.every((profile) => {
      const item = objectRecord(profile);
      return Boolean(
        item
        && hasNonEmptyString(item, "profile_id")
        && hasNonEmptyString(item, "display_name")
      );
    })
    && typeof record.count === "number"
    && Number.isInteger(record.count)
    && record.count >= 0
  );
}

function nonEmptyString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function composerCommandResultMessage(result: ComposerCommandExecuteResult): string | null {
  const directMessage = nonEmptyString(result.message);
  if (directMessage) return directMessage;

  const resultPayload = objectRecord(result.result);
  if (!resultPayload) return null;

  const payloadMessage = nonEmptyString(resultPayload.message);
  const path = nonEmptyString(resultPayload.path)
    || nonEmptyString(resultPayload.file_path)
    || nonEmptyString(resultPayload.artifact_path);
  if (payloadMessage && path) return `${payloadMessage}\n${path}`;
  if (payloadMessage) return payloadMessage;
  if (path) return `Command wrote ${path}`;
  return null;
}

export type ComposerCommandFeedbackTone = "success" | "info" | "warning" | "error";

export function composerCommandFeedbackTone(
  result: ComposerCommandExecuteResult,
): ComposerCommandFeedbackTone {
  if (result.operation_status === "failed") return "error";
  if (result.requires_approval) return "warning";

  const deepthinkState = result.state_changes?.find(
    (snapshot) => snapshot.state_ref === "defaultspack:models.deepthink_enabled",
  );
  if (deepthinkState) {
    return deepthinkState.value === true ? "warning" : "success";
  }

  return "success";
}

type ApiOk<T> = {
  status: "ok";
  data: T;
};

type ApiError = {
  status: "error";
  error: {
    code: string;
    message: string;
  };
};

type ApiEnvelope<T> = ApiOk<T> | ApiError;

function isApiOkEnvelope(value: unknown): value is ApiOk<unknown> {
  const record = objectRecord(value);
  return Boolean(
    record
    && record.status === "ok"
    && Object.prototype.hasOwnProperty.call(record, "data")
  );
}

function isApiErrorEnvelope(value: unknown): value is ApiError {
  const record = objectRecord(value);
  const error = record ? objectRecord(record.error) : null;
  return Boolean(
    record
    && record.status === "error"
    && error
    && hasNonEmptyString(error, "code")
    && hasNonEmptyString(error, "message")
  );
}

export type ToolSelectionMode = "auto" | "review" | "manual" | "none";
export type ToolSelectionScope = "turn" | "conversation";
export type ToolSelectionStrategy = "hybrid" | "semantic" | "catalog_ai" | "all_with_hints" | "all_schemas" | "lexical";
export type ToolTarget = { kind: "tool" | "service"; id: string };

export type ToolSelectionRequest = {
  mode?: ToolSelectionMode;
  strategy?: ToolSelectionStrategy | null;
  include?: Array<string | ToolTarget>;
  exclude?: Array<string | ToolTarget>;
  scope?: ToolSelectionScope;
  must_use?: boolean;
  preview_id?: string | null;
};

export type ToolCatalogService = {
  service_id: string;
  label: string;
  summary?: string;
  connection_status?: string;
  tool_count?: number;
  action_classes?: string[];
};

export type ToolCatalogTool = {
  tool_id: string;
  service_id: string;
  service_label: string;
  name: string;
  summary?: string;
  action_class: string;
  risk?: string;
  requires_explicit_intent?: boolean;
  connection_status?: string;
  minimum_permission?: string;
  tags?: string[];
  permission?: Record<string, unknown>;
};

export type ToolCatalogResponse = {
  services: ToolCatalogService[];
  tools: ToolCatalogTool[];
  count: number;
};

export type ToolSelectionPreviewResponse = {
  preview_id: string;
  expires_at: string;
  decision: {
    selected_tools: string[];
    selected_services: ToolCatalogService[];
    recommendations: Array<{ tool_id: string; confidence?: number; reason?: string }>;
    permission_summary: Record<string, number>;
    fallbacks?: Array<Record<string, unknown>>;
    metadata?: Record<string, unknown>;
  };
};

type SendMessageOptions = {
  idempotency_key?: string;
  thinking_level?: string | null;
  deepthink_enabled?: boolean;
  tool_choice?: "auto" | "none" | "required" | Record<string, unknown>;
  parallel_tool_calls?: boolean;
  params?: Record<string, unknown>;
  tool_policy?: Record<string, unknown>;
  tool_selection?: ToolSelectionRequest;
  attachments?: ChatAttachment[];
  tools?: string[];
  metadata?: Record<string, unknown>;
};

type ChatStreamError = string | { code?: string; message?: string };

export type ChatToolStreamEvent = ChatActivityEvent & {
  type:
    | "status"
    | "tool_call"
    | "tool_call_started"
    | "tool_call_delta"
    | "tool_call_completed"
    | "tool_result"
    | "browser_state_invalidated"
    | "browser_state_snapshot"
    | "browser_dom_snapshot"
    | "browser_screenshot"
    | "approval_requested"
    | "ai_retry_scheduled"
    | "tool_selection_started"
    | "tool_selection_completed"
    | "tool_selection_fallback"
    | "tool_selection_reviewed"
    | "task_failed";
};

export type ChatStreamEvent =
  | { type: "delta"; delta: string }
  | { type: "thinking_delta"; delta: string }
  | { type: "message" | "done" | "user_message"; message?: ChatMessage }
  | { type: "error"; error?: ChatStreamError }
  | ChatToolStreamEvent;

type ChatStreamHandlers = {
  onEvent?: (event: ChatStreamEvent) => void;
  onDelta?: (delta: string) => void;
  onThinkingDelta?: (delta: string) => void;
  onMessage?: (message: ChatMessage) => void;
  onUserMessage?: (message: ChatMessage) => void;
  signal?: AbortSignal;
};

function streamRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function streamString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}

function streamMessageValue(record: Record<string, unknown>, data: Record<string, unknown>): ChatMessage | undefined {
  const value = record.message ?? data.message;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as ChatMessage
    : undefined;
}

function streamErrorValue(record: Record<string, unknown>, data: Record<string, unknown>): ChatStreamError | undefined {
  const value = record.error ?? data.error;
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as ChatStreamError;
  }
  const message = record.message ?? data.message;
  return typeof message === "string" && message.trim() ? { message } : undefined;
}

export function normalizeChatStreamEvent(value: unknown): ChatStreamEvent | null {
  const record = streamRecord(value);
  const rawType = streamString(record, "type").trim();
  if (!rawType) return null;
  const data = streamRecord(record.data);
  const delta = streamString(data, "delta") || streamString(record, "delta") || streamString(data, "content") || streamString(record, "content");

  if (rawType === "delta" || rawType === "content_delta") {
    return { type: "delta", delta };
  }
  if (rawType === "thinking_delta") {
    return { type: "thinking_delta", delta };
  }
  if (rawType === "user_message" || rawType === "user_message_committed") {
    return { type: "user_message", message: streamMessageValue(record, data) };
  }
  if (rawType === "message" || rawType === "assistant_message_completed") {
    return { type: "message", message: streamMessageValue(record, data) };
  }
  if (rawType === "done" || rawType === "stream_end") {
    return { type: "done", message: streamMessageValue(record, data) };
  }
  if (rawType === "error" || rawType === "cancelled") {
    return { type: "error", error: rawType === "cancelled" ? "cancelled" : streamErrorValue(record, data) };
  }

  const merged: Record<string, unknown> = { ...record, ...data, type: rawType };
  delete merged.data;
  delete merged.schema_version;
  return merged as ChatStreamEvent;
}

const BROWSER_COMPUTER_APPROVAL_TOOLS = new Set([
  "browser_computer",
  "browser_companion",
  "browser_use",
  "computer_use",
  "browser_open_url",
  "open_browser",
]);

const BROWSER_OPEN_ACTION_ALIASES = new Set([
  "browser_open_url",
  "open_browser",
  "open_url",
]);

const COMPUTER_APPROVAL_ACTION_ALIASES = new Set([
  "context",
  "app_context",
  "state",
  "apps",
  "list_apps",
  "open_apps",
  "applications",
  "windows",
  "list_windows",
  "select_app",
  "show_app",
  "focus_app",
  "activate_app",
  "select_window",
  "screenshot",
  "move",
  "click",
  "drag",
  "type",
  "key",
  "scroll",
  "observe",
  "semantic_action",
  "press",
  "pid_event",
]);

export function usesBrowserComputerApprovalEndpoint(toolName: string): boolean {
  return BROWSER_COMPUTER_APPROVAL_TOOLS.has(String(toolName || "").trim());
}

export function normalizeBrowserComputerApprovalAction(toolName: string, action: string): string {
  const normalizedAction = String(action || "").trim();
  if (usesBrowserComputerApprovalEndpoint(toolName) && BROWSER_OPEN_ACTION_ALIASES.has(normalizedAction)) {
    return "browser.open_url";
  }
  if (
    usesBrowserComputerApprovalEndpoint(toolName)
    && normalizedAction
    && !normalizedAction.includes(".")
    && COMPUTER_APPROVAL_ACTION_ALIASES.has(normalizedAction)
  ) {
    return `computer.${normalizedAction}`;
  }
  return normalizedAction;
}

function isUnsafeHttpMethod(method: string): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
}

function sessionStorageOrNull(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

function generateCsrfToken(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `csrf-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function getDefaultspackCsrfToken(): string {
  const storage = sessionStorageOrNull();
  const panelToken = storage?.getItem(PANEL_CSRF_STORAGE_KEY);
  if (panelToken?.trim()) return panelToken;
  const stored = storage?.getItem(DEFAULTSPACK_CSRF_STORAGE_KEY);
  if (stored) return stored;
  const token = generateCsrfToken();
  try {
    storage?.setItem(DEFAULTSPACK_CSRF_STORAGE_KEY, token);
  } catch {
    // A nonempty per-request token still satisfies the local CSRF guard.
  }
  return token;
}

function consumeDefaultspackLocalAuthFromLocation(): string {
  if (typeof window === "undefined") return "";
  const storage = sessionStorageOrNull();
  try {
    const rawHash = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : window.location.hash;
    if (!rawHash) return defaultspackLocalAuthMemoryToken || storage?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() || "";
    const params = new URLSearchParams(rawHash);
    const token = params.get(DEFAULTSPACK_LOCAL_AUTH_FRAGMENT_KEY)?.trim() ?? "";
    if (!token) return defaultspackLocalAuthMemoryToken || storage?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() || "";
    defaultspackLocalAuthMemoryToken = token;
    storage?.setItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY, token);
    params.delete(DEFAULTSPACK_LOCAL_AUTH_FRAGMENT_KEY);
    const nextHash = params.toString();
    const nextUrl = `${window.location.pathname}${window.location.search}${nextHash ? `#${nextHash}` : ""}`;
    window.history.replaceState(window.history.state, document.title, nextUrl);
    return token;
  } catch {
    return storage?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() ?? "";
  }
}

export function bootstrapDefaultspackLocalAuth(): string {
  const stored = sessionStorageOrNull()?.getItem(DEFAULTSPACK_LOCAL_AUTH_STORAGE_KEY)?.trim() || "";
  if (defaultspackLocalAuthBootstrapped) {
    if (defaultspackLocalAuthMemoryToken || stored) return defaultspackLocalAuthMemoryToken || stored;
  }
  defaultspackLocalAuthBootstrapped = true;
  const consumed = consumeDefaultspackLocalAuthFromLocation();
  if (consumed) {
    defaultspackLocalAuthMemoryToken = consumed;
    return consumed;
  }
  defaultspackLocalAuthMemoryToken = stored;
  return defaultspackLocalAuthMemoryToken;
}

function getDefaultspackLocalAuthToken(): string {
  return bootstrapDefaultspackLocalAuth();
}

bootstrapDefaultspackLocalAuth();

export function defaultspackApiHeaders(method: string, headers?: HeadersInit): Headers {
  const nextHeaders = new Headers(headers);
  if (!nextHeaders.has("Content-Type")) {
    nextHeaders.set("Content-Type", "application/json");
  }
  if (!nextHeaders.has("Authorization")) {
    const token = getDefaultspackLocalAuthToken();
    if (token) nextHeaders.set("Authorization", `Bearer ${token}`);
  }
  const csrfHeader = nextHeaders.get("X-Rumi-CSRF");
  if (isUnsafeHttpMethod(method) && (!csrfHeader || !csrfHeader.trim())) {
    nextHeaders.set("X-Rumi-CSRF", getDefaultspackCsrfToken());
  }
  return nextHeaders;
}

export function defaultspackUrlWithLocalAuth(pathOrUrl: string): string {
  return defaultspackUrlWithLocalAuthToken(pathOrUrl, getDefaultspackLocalAuthToken());
}

/**
 * A Host-owned frontend route operation.  Callers provide an API-map key
 * without a leading slash (for example ``api/ui/catalog``); the browser only
 * ever requests the canonical contract endpoint below.  The Host decodes the
 * opaque operation and applies the ordinary route/authentication boundary.
 */
export type DefaultspackContractRoute = {
  readonly kind: "defaultspack-contract-route";
  readonly apiPath: string;
};

export const DEFAULTSPACK_CONTRACT_ENDPOINT = "/api/contracts/defaultspack/";

export function defaultspackContractRoute(apiPath: string): DefaultspackContractRoute {
  const normalized = apiPath.startsWith("/") ? apiPath : `/${apiPath}`;
  const segments = normalized.split("/");
  if (segments[1] !== "api" || normalized.startsWith(DEFAULTSPACK_CONTRACT_ENDPOINT)) {
    throw new Error("invalid defaultspack contract route");
  }
  return { kind: "defaultspack-contract-route", apiPath: normalized };
}

/** Return a route key for configuration values that are not HTTP calls. */
export function defaultspackCanonicalRouteKey(apiPath: string): string {
  return defaultspackContractRoute(apiPath).apiPath;
}

export function isDefaultspackRouteKey(value: string): boolean {
  const segments = String(value || "").split("/");
  return segments[0] === "" && segments[1] === "api" && segments.length > 2;
}

export function defaultspackContractUrl(
  route: DefaultspackContractRoute,
  method = "GET",
): string {
  const operation = `${method.toUpperCase()} ${route.apiPath}`;
  return `${DEFAULTSPACK_CONTRACT_ENDPOINT}${encodeURIComponent(operation)}`;
}

function isDefaultspackContractRoute(
  input: RequestInfo | URL | DefaultspackContractRoute,
): input is DefaultspackContractRoute {
  return typeof input === "object"
    && input !== null
    && "kind" in input
    && (input as DefaultspackContractRoute).kind === "defaultspack-contract-route";
}

export function defaultspackApiFetch(
  input: RequestInfo | URL | DefaultspackContractRoute,
  init: RequestInit = {},
): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const requestInput = isDefaultspackContractRoute(input)
    ? defaultspackContractUrl(input, method)
    : input;
  const headers = defaultspackApiHeaders(method, init.headers);
  if (isDefaultspackContractRoute(input)) {
    headers.set("X-Tobkiri-Request-ID", crypto.randomUUID());
  }
  return fetch(requestInput, {
    ...init,
    method,
    headers,
  });
}

export async function* streamCommandInvocationEvents(
  invocationId: string,
  options: { afterSequence?: number; signal?: AbortSignal; waitSeconds?: number } = {},
): AsyncGenerator<Record<string, unknown>> {
  let lastSequence = options.afterSequence ?? 0;
  let consecutiveFailures = 0;
  while (!options.signal?.aborted) {
    try {
      const params = new URLSearchParams({
        after_sequence: String(lastSequence),
        wait_seconds: String(options.waitSeconds ?? 30),
      });
      const response = await defaultspackApiFetch(
        defaultspackContractRoute(`api/command-protocol/v1/invocations/${encodeURIComponent(invocationId)}/events?${params}`),
        {
          headers: {
            Accept: "text/event-stream",
            ...(lastSequence > 0 ? { "Last-Event-ID": String(lastSequence) } : {}),
          },
          signal: options.signal,
        },
      );
      if (!response.ok || !response.body) {
        const failure = new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
        if (response.status < 500 && response.status !== 429) {
          throw Object.assign(failure, { retryableCommandStream: false });
        }
        throw Object.assign(failure, { retryableCommandStream: true });
      }
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      try {
        while (true) {
          const { done, value } = await reader.read();
          consecutiveFailures = 0;
          buffer += value ?? "";
          const frames = buffer.replace(/\r\n/g, "\n").split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const data = frame
              .split("\n")
              .filter((line) => line.startsWith("data:"))
              .map((line) => line.slice(5).trimStart())
              .join("\n");
            if (!data) continue;
            const event = JSON.parse(data) as Record<string, unknown>;
            const sequence = Number(event.sequence ?? 0);
            if (sequence && sequence !== lastSequence + 1) {
              throw new Error(`command event sequence gap: expected ${lastSequence + 1}, got ${sequence}`);
            }
            if (sequence) lastSequence = sequence;
            yield event;
            if (["completed", "failed", "cancelled", "conflicted", "expired"].includes(String(event.type))) {
              return;
            }
          }
          if (done) break;
        }
      } finally {
        reader.releaseLock();
      }
    } catch (streamError) {
      if (options.signal?.aborted) return;
      const message = streamError instanceof Error ? streamError.message : "";
      const retryableFlag = streamError instanceof Error
        ? (streamError as Error & { retryableCommandStream?: boolean }).retryableCommandStream
        : undefined;
      const protocolFailure = message.includes("sequence gap")
        || streamError instanceof SyntaxError
        || retryableFlag === false;
      if (protocolFailure) throw streamError;
      consecutiveFailures += 1;
      const delayMs = Math.min(5000, 250 * (2 ** Math.min(consecutiveFailures, 5)));
      await new Promise<void>((resolve, reject) => {
        const timeout = globalThis.setTimeout(resolve, delayMs);
        options.signal?.addEventListener("abort", () => {
          globalThis.clearTimeout(timeout);
          reject(new DOMException("Aborted", "AbortError"));
        }, { once: true });
      }).catch((abortError) => {
        if (options.signal?.aborted) return;
        throw abortError;
      });
    }
  }
}

function truncateApiErrorDetail(value: string, limit = 700): string {
  const text = value.trim().replace(/\s+/g, " ");
  if (text.length <= limit) return text;
  return `${text.slice(0, limit).trim()}...`;
}

function defaultspackApiCodeHint(code: string | undefined): string | null {
  if (code === "AUTHORITY_BROWSER_TEST_DISABLED") {
    return "ブラウザ承認は、このDefaultspack起動では有効化されていません。Tobkiri Launcherの承認ウィンドウから開き直してください。";
  }
  if (code === "AUTHORITY_BROWSER_TOKEN_REQUIRED") {
    return "旧式のブラウザ承認情報は使用できません。承認ページを安全な経路から開き直してください。";
  }
  if (code === "AUTHORITY_BROWSER_TOKEN_INVALID") {
    return "旧式のブラウザ承認情報は無効化されました。承認ページを安全な経路から開き直してください。";
  }
  if (code === "AUTHORITY_UI_OPERATOR_UNAVAILABLE") {
    return "承認操作の署名secretがこのDefaultspack起動にありません。Tobkiri Launcherから起動し直すか、ブラウザQAでは Viewer と同じ RUMI_PANEL_BOOTSTRAP_SECRET を渡してください。";
  }
  return null;
}

function defaultspackApiStatusHint(status: number, code?: string): string {
  const codeHint = defaultspackApiCodeHint(code);
  if (codeHint) return codeHint;
  if (status === 400) return "リクエスト形式、モデル設定、添付ファイル、または選択中の tool が backend と噛み合っていません。";
  if (status === 401) return "認証が必要です。ログイン状態、APIキー、OAuth 接続を確認してください。";
  if (status === 403) return "権限または承認で拒否されました。承認カード、CSRF、APIキーの利用権限、モデルアクセス権を確認してください。";
  if (status === 404) return "対象の会話、モデル、ファイル、または endpoint が見つかりません。";
  if (status === 409) return "同時実行や状態の衝突が起きています。画面を更新して再試行してください。";
  if (status === 429) return "レート制限またはクォータ上限です。少し待つか、別のキー/モデルに切り替えてください。";
  if (status >= 500) return "backend または provider 側の障害です。少し待って再試行してください。";
  return "backend からエラーが返りました。詳細を確認して再試行してください。";
}

export function explainDefaultspackApiError(
  status: number,
  error?: { code?: string; message?: string },
  statusText?: string,
): string {
  const label = status ? `HTTP ${status}${statusText ? ` ${statusText}` : ""}` : "defaultspack API error";
  const code = error?.code ? ` (${error.code})` : "";
  const detail = error?.message ? truncateApiErrorDetail(error.message) : "";
  const recoveryHint = /local auth token (?:required|is not configured)/i.test(error?.message ?? "")
    ? "この画面がTobkiri Launcherのローカル認証を引き継げていません。APIキーの誤りではありません。LauncherからTobkiriを開き直してください。"
    : defaultspackApiStatusHint(status, error?.code);
  return [
    `${label}${code}`,
    recoveryHint,
    detail ? `詳細: ${detail}` : "",
  ].filter(Boolean).join("\n");
}

type DefaultspackApiPath = string | DefaultspackContractRoute;

type ResponseShapeGuard<T> = (value: unknown) => value is T;

function apiPathLabel(path: DefaultspackApiPath): string {
  return typeof path === "string" ? path : path.apiPath;
}

function invalidApiContractResponse(path: DefaultspackApiPath, detail: string): Error {
  return new Error(`invalid Pack v4 response for ${apiPathLabel(path)}: ${detail}`);
}

async function request<T>(
  path: DefaultspackApiPath,
  init?: RequestInit,
  responseShape?: ResponseShapeGuard<T>,
): Promise<T> {
  const response = await defaultspackApiFetch(path, {
    ...init,
  });

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    if (!response.ok) {
      throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
    }
    throw new Error("defaultspack API returned an invalid JSON response");
  }

  if (isApiErrorEnvelope(payload)) {
    throw new Error(explainDefaultspackApiError(
      response.status,
      payload.error,
      response.statusText,
    ));
  }
  if (!isApiOkEnvelope(payload)) {
    if (!response.ok) {
      throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
    }
    throw invalidApiContractResponse(path, "missing status/data envelope");
  }
  if (!response.ok) {
    throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
  }
  if (responseShape && !responseShape(payload.data)) {
    throw invalidApiContractResponse(path, "data does not match the endpoint schema");
  }

  return payload.data as T;
}

function encodeQueryValue(value: unknown): string | null {
  if (value === undefined || value === null || value === "") return null;
  if (Array.isArray(value)) {
    const items = value.map((item) => String(item).trim()).filter(Boolean);
    return items.length ? items.join(",") : null;
  }
  return String(value);
}

function withQuery(path: DefaultspackApiPath, params?: Record<string, unknown>): DefaultspackApiPath {
  if (!params) return path;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    const encoded = encodeQueryValue(value);
    if (encoded !== null) query.set(key, encoded);
  }
  const suffix = query.toString();
  if (!suffix) return path;
  if (typeof path === "string") return `${path}?${suffix}`;
  return defaultspackContractRoute(`${path.apiPath}?${suffix}`);
}

export async function createRemoteTask(input: RemoteTaskCreateRequest): Promise<RemoteTaskSnapshot> {
  return request<RemoteTaskSnapshot>(defaultspackContractRoute("api/remote/tasks"), {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function getRemoteTask(taskId: string): Promise<RemoteTaskSnapshot> {
  return request<RemoteTaskSnapshot>(defaultspackContractRoute(`api/remote/tasks/${encodeURIComponent(taskId)}`), { cache: "no-store" });
}

export async function listRemoteTaskEvents(taskId: string, after?: string): Promise<RemoteTaskEventsResponse> {
  return request<RemoteTaskEventsResponse>(
    withQuery(defaultspackContractRoute(`api/remote/tasks/${encodeURIComponent(taskId)}/events`), { after }),
    { cache: "no-store" },
  );
}

export async function cancelRemoteTask(taskId: string, reason?: string): Promise<RemoteTaskSnapshot> {
  return request<RemoteTaskSnapshot>(defaultspackContractRoute(`api/remote/tasks/${encodeURIComponent(taskId)}/cancel`), {
    method: "POST",
    body: JSON.stringify(reason ? { reason } : {}),
  });
}

export async function getRemoteHostStatus(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(defaultspackContractRoute("api/remote/host/status"), { cache: "no-store" });
}

export function arrayFromRecord<T>(value: Record<string, T> | T[] | undefined | null): T[] {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") return Object.values(value);
  return [];
}

function messageRequestBody(
  text: string,
  options?: SendMessageOptions,
): Record<string, unknown> {
  return {
    idempotency_key: options?.idempotency_key ?? createChatOperationId(),
    message: {
      role: "user",
      content: text,
      attachments: options?.attachments?.length ? options.attachments : undefined,
      metadata: options?.metadata,
    },
    tools: Array.isArray(options?.tools) ? options.tools : undefined,
    params: {
      ...(options?.params ?? {}),
      thinking_level: options?.thinking_level ?? undefined,
      deepthink_enabled: options?.deepthink_enabled ?? undefined,
      tool_choice: options?.tool_choice ?? undefined,
      parallel_tool_calls: options?.parallel_tool_calls ?? undefined,
      tool_policy: options?.tool_policy ?? undefined,
      tool_selection: options?.tool_selection ?? undefined,
    },
  };
}

function createChatOperationId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

async function readStreamEvents(
  response: Response,
  handlers: ChatStreamHandlers = {},
): Promise<ChatMessage | null> {
  if (!response.body) {
    throw new Error("defaultspack API returned an empty stream");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalMessage: ChatMessage | null = null;
  let partialText = "";
  let thinkingText = "";
  let sawActivity = false;

  const streamErrorMessage = (value: ChatStreamError | undefined): string => {
    if (typeof value === "string" && value.trim()) return value;
    if (value && typeof value === "object" && typeof value.message === "string") {
      return value.message;
    }
    return "defaultspack stream failed";
  };

  const consumePacket = (packet: string) => {
    const dataLines = packet
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart());
    if (!dataLines.length) return;
    const raw = dataLines.join("\n");
    if (!raw || raw === "[DONE]") return;
    let event: ChatStreamEvent;
    try {
      const parsed = JSON.parse(raw) as unknown;
      const normalized = normalizeChatStreamEvent(parsed);
      if (!normalized) {
        throw new Error("empty event");
      }
      event = normalized;
    } catch {
      throw new Error("defaultspack stream returned a malformed event");
    }
    handlers.onEvent?.(event);
    if (event.type === "delta") {
      partialText += event.delta;
      handlers.onDelta?.(event.delta);
    } else if (event.type === "thinking_delta") {
      thinkingText += event.delta;
      handlers.onThinkingDelta?.(event.delta);
    } else if (event.type === "user_message" && event.message) {
      handlers.onUserMessage?.(event.message);
    } else if ((event.type === "message" || event.type === "done") && event.message) {
      finalMessage = event.message;
      handlers.onMessage?.(event.message);
    } else if (event.type === "error") {
      throw new Error(streamErrorMessage(event.error));
    } else {
      sawActivity = true;
    }
  };

  const interruptionError = (message: string): Error => {
    if (partialText.trim() || thinkingText.trim() || sawActivity) {
      return new ChatStreamInterruptedError(message, {
        partialText,
        thinkingText,
        sawActivity,
      });
    }
    return new Error(message);
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const packets = buffer.split(/\r?\n\r?\n/);
      buffer = packets.pop() ?? "";
      for (const packet of packets) {
        consumePacket(packet);
      }
      if (done) break;
    }
    if (buffer.trim()) {
      consumePacket(buffer);
    }
  } catch (errorValue) {
    if (errorValue instanceof ChatStreamInterruptedError) throw errorValue;
    throw interruptionError(errorValue instanceof Error ? errorValue.message : "defaultspack stream failed");
  }

  if (!finalMessage) {
    throw interruptionError("defaultspack stream ended before a final response arrived");
  }
  return finalMessage;
}

export type CodexAppServerConfig = {
  transport?: "off" | "stdio" | "unix" | "websocket_loopback" | "websocket_remote";
  enabled?: boolean;
  baseUrl?: string;
  websocketUrl?: string;
  unixSocketPath?: string;
  wsTokenFile?: string;
  sharedSecretFile?: string;
  toolSourceEnabled?: boolean;
  automationEndpointEnabled?: boolean;
};

export type CodexConnectionStatusResponse = {
  provider: Record<string, unknown>;
  app_server: Record<string, unknown>;
};

export type CodexConnectionActionResponse = Partial<CodexConnectionStatusResponse> & {
  provider_id?: string;
  configured?: boolean;
  cleared?: boolean;
  created?: boolean;
  status?: Record<string, unknown>;
  account?: Record<string, unknown>;
  probe?: Record<string, unknown>;
};

async function nativeCodingApprovalOperator(
  requestId: string,
  decision: "approve" | "deny",
): Promise<Record<string, unknown>> {
  const listing = await request<{
    requests?: CodingApprovalRequest[];
    pending?: CodingApprovalRequest[];
  }>(
    withQuery(defaultspackContractRoute("api/coding/approvals"), { include_expired: true, limit: 500 }),
    { cache: "no-store" },
  );
  const approval = [...(listing.requests ?? []), ...(listing.pending ?? [])].find(
    (candidate) => candidate.request_id === requestId,
  );
  const expectedDigest = String(approval?.args_hash ?? "").trim();
  if (!/^[a-f0-9]{64}$/i.test(expectedDigest)) {
    throw new Error("The coding approval snapshot is unavailable.");
  }
  const tauri = (
    globalThis as typeof globalThis & {
      __TAURI__?: {
        core?: {
          invoke?: (
            command: string,
            args?: Record<string, unknown>,
          ) => Promise<Record<string, unknown>>;
        };
      };
    }
  ).__TAURI__;
  if (typeof tauri?.core?.invoke !== "function") {
    throw new Error("Coding approval requires the focused Tobkiri Launcher window.");
  }
  return tauri.core.invoke("coding_approval_operator", {
    requestId,
    expectedDigest,
    decision,
  });
}

export const api = {
  listConversations(options?: ConversationListOptions) {
    return request<{ conversations: Conversation[]; total: number }>(
      withQuery(defaultspackContractRoute("api/chat/conversations"), options),
    );
  },

  searchConversations(query: string, options?: ConversationSearchOptions) {
    return request<{ results: ConversationSearchResult[]; total: number; query: string }>(defaultspackContractRoute("api/chat/search"), {
      method: "POST",
      body: JSON.stringify({
        query,
        mode: "conversations",
        date_filter: options?.date_filter ?? "all",
        is_starred: options?.is_starred,
        is_archived: options?.is_archived,
        role: options?.role ?? "all",
        limit: options?.limit ?? 12,
        offset: options?.offset ?? 0,
      }),
    });
  },

  getConversation(id: string) {
    return request<Conversation>(defaultspackContractRoute(`api/chat/conversations/${id}`));
  },

  createConversation(options?: {
    model?: string;
    system_prompt_id?: string | null;
    agent_id?: string | null;
    tags?: string[];
    parent_conversation_id?: string | null;
    conversation_kind?: string | null;
    group_id?: string | null;
    metadata?: Record<string, unknown>;
  }) {
    return request<Conversation>(defaultspackContractRoute("api/chat/conversations"), {
      method: "POST",
      body: JSON.stringify(options ?? {}),
    });
  },

  updateConversation(id: string, updates: Partial<Conversation>) {
    return request<Conversation>(defaultspackContractRoute(`api/chat/conversations/${id}`), {
      method: "PUT",
      body: JSON.stringify({ updates }),
    });
  },

  deleteConversation(id: string) {
    return request<{ deleted: boolean }>(defaultspackContractRoute(`api/chat/conversations/${id}`), {
      method: "DELETE",
    });
  },

  getPromptActive(params?: { profile_id?: string; conversation_id?: string; include_text?: boolean; model_profile_id?: string; model?: string }) {
    return request<{
      profile_id: string;
      conversation_id?: string;
      summary: PromptUsageSummary;
      segments?: PromptUsageSegment[];
      active_segments?: PromptUsageSegment[];
      disabled_segments?: PromptUsageSegment[];
      token_estimate?: PromptUsageSummary["token_estimate"];
    }>(withQuery(defaultspackContractRoute("api/prompts/active"), params));
  },

  listPromptTraces(params?: { profile_id?: string; conversation_id?: string; limit?: number }) {
    return request<{ profile_id: string; traces: PromptTraceSummary[]; count: number }>(
      withQuery(defaultspackContractRoute("api/prompts/traces"), params),
    );
  },

  getPromptTrace(traceId: string, params?: { profile_id?: string; include_text?: boolean }) {
    return request<PromptTraceDetail>(
      withQuery(defaultspackContractRoute(`api/prompts/traces/${encodeURIComponent(traceId)}`), params),
    );
  },

  getPromptStudio(params?: { profile_id?: string; prompt_id?: string; conversation_id?: string; model_profile_id?: string; model?: string }) {
    return request<PromptStudioData>(withQuery(defaultspackContractRoute("api/prompts/editor"), params));
  },

  testPromptStudio(payload: {
    profile_id?: string;
    prompt_id?: string;
    conversation_id?: string;
    draft?: string;
    user_text?: string;
    selected_tools?: string[];
    model_profile_id?: string;
    model?: string;
    request_context?: Record<string, unknown>;
    template_policy?: Record<string, unknown>;
  }) {
    return request<PromptStudioTestResult>(defaultspackContractRoute("api/prompts/test"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  savePrompt(payload: {
    profile_id?: string;
    prompt_id: string;
    body: string;
    description?: string;
    variables?: Record<string, unknown>[];
    metadata?: Record<string, unknown>;
    create_override?: boolean;
    expected_body_hash?: string;
    expected_exists?: boolean;
    reason?: string;
  }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/prompts/editor/save"), {
      method: "POST",
      body: JSON.stringify({ action: "save", ...payload }),
    });
  },

  createPromptOverride(payload: { profile_id?: string; prompt_id: string; body?: string; expected_body_hash?: string; expected_exists?: boolean; reason?: string }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/prompts/override"), {
      method: "POST",
      body: JSON.stringify({ action: "override", ...payload }),
    });
  },

  togglePromptEdge(payload: { profile_id?: string; edge_id: string; enabled: boolean; conversation_id?: string; model_profile_id?: string; model?: string }) {
    return request<PromptToggleResponse>(defaultspackContractRoute("api/prompts/toggle"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  previewPromptToggle(payload: { profile_id?: string; edge_id: string; enabled: boolean; conversation_id?: string; model_profile_id?: string; model?: string }) {
    return request<PromptToggleResponse>(defaultspackContractRoute("api/prompts/preview-toggle"), {
      method: "POST",
      body: JSON.stringify({ preview: true, ...payload }),
    });
  },

  diffPrompt(payload: { profile_id?: string; prompt_id: string; base?: string; draft?: string }) {
    return request<{ profile_id: string; prompt_id: string; diff: string; changed: boolean }>(defaultspackContractRoute("api/prompts/diff"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  lintPrompt(payload: { prompt?: string; text?: string; body?: string; token_budget?: number }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/prompts/lint"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  compactPrompt(payload: { prompt?: string; text?: string; body?: string; target_chars?: number }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/prompts/compact"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  rollbackPrompt(payload: { profile_id?: string; prompt_id: string; version_id: string; expected_body_hash?: string; expected_exists?: boolean }) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/prompts/${encodeURIComponent(payload.prompt_id)}/rollback`), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  sendMessage(
    conversationId: string,
    text: string,
    options?: SendMessageOptions,
  ) {
    return request<ChatMessage>(
      defaultspackContractRoute(`api/chat/conversations/${conversationId}/messages`),
      {
        method: "POST",
        body: JSON.stringify(messageRequestBody(text, options)),
      },
    );
  },

  async streamMessage(
    conversationId: string,
    text: string,
    options?: SendMessageOptions,
    handlers?: ChatStreamHandlers,
  ) {
    const response = await defaultspackApiFetch(defaultspackContractRoute(`api/chat/conversations/${conversationId}/stream`), {
      method: "POST",
      body: JSON.stringify(messageRequestBody(text, options)),
      signal: handlers?.signal,
    });

    const contentType = response.headers.get("Content-Type") ?? "";
    if (!response.ok || !contentType.includes("text/event-stream")) {
      let payload: ApiEnvelope<ChatMessage>;
      try {
        payload = (await response.json()) as ApiEnvelope<ChatMessage>;
      } catch {
        throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
      }
      if (payload.status === "error") {
        throw new Error(explainDefaultspackApiError(response.status, payload.error, response.statusText));
      }
      if (!response.ok) {
        throw new Error(explainDefaultspackApiError(response.status, undefined, response.statusText));
      }
      handlers?.onMessage?.(payload.data);
      return payload.data;
    }
    return readStreamEvents(response, handlers);
  },

  stopMessage(conversationId: string) {
    return request<{ success: boolean; conversation_id: string; cancelled: boolean }>(
      defaultspackContractRoute(`api/chat/conversations/${conversationId}/stop`),
      {
        method: "POST",
        body: JSON.stringify({}),
      },
    );
  },

  listModelProfiles() {
    return request<{ profiles: ModelProfile[]; count: number }>(defaultspackContractRoute("api/ai/profiles"), {
      cache: "no-store",
    }, isModelProfilesResponse);
  },

  searchModels(filters: Record<string, unknown>) {
    return request<ModelSearchResponse>(defaultspackContractRoute("api/ai/models/search"), {
      method: "POST",
      body: JSON.stringify(filters),
    });
  },

  conversationSteer(payload: Record<string, unknown>) {
    return request<ConversationSteerResponse>(defaultspackContractRoute("api/chat/steer"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  health() {
    return request<{ status: string; pack: string; ts: string }>(defaultspackContractRoute("api/health"));
  },

  uiCatalog() {
    return request<UICatalog>(
      defaultspackContractRoute("api/ui/catalog?include_skills=true"),
      undefined,
      isUICatalog,
    );
  },

  uiSettings(options: { full?: boolean } = {}) {
    const query = options.full ? "?full=true" : "";
    return request<{ sections: SettingsSection[]; values: Record<string, Record<string, unknown>> }>(
      defaultspackContractRoute(`api/ui/settings${query}`),
      { cache: "no-store" },
      isUiSettingsResponse,
    );
  },

  uiCommands() {
    return request<{ commands: ComposerCommandItem[] }>(defaultspackContractRoute("api/ui/commands"));
  },

  commandProtocolCatalog() {
    return request<ResolvedCommandCatalog>(defaultspackContractRoute("api/command-protocol/v1/catalog"), {
      cache: "no-store",
    });
  },

  async resolvedUiCommands() {
    const protocol = await request<ResolvedCommandCatalog>(
      defaultspackContractRoute("api/command-protocol/v1/catalog"),
      { cache: "no-store" },
    );
    return {
      commands: protocol.commands.map((command) => {
        const input = command.presentation.input;
        const fields = Array.isArray(input.fields)
          ? input.fields.filter((item): item is Record<string, unknown> => (
              Boolean(item) && typeof item === "object"
            ))
          : [];
        const argument = typeof input.argument === "string" ? input.argument : null;
        const args: ComposerCommandArg[] = fields.map((field) => ({
          name: String(field.argument ?? ""),
          type: (field.control === "checkbox" ? "boolean" : "string") as ComposerCommandArg["type"],
          required: field.required === true,
        })).filter((field) => field.name);
        if (argument && !args.some((item) => item.name === argument)) {
          args.push({
            name: argument,
            type: input.kind === "toggle" ? "boolean" : "string",
            required: false,
          });
        }
        const operationRef = String(command.execution.operation_ref ?? "");
        const execution: ComposerCommandExecution = command.execution.kind === "host_operation"
          ? { type: "frontend", action: operationRef.replace(/^host:/, "") }
          : command.execution.kind === "state_mutation"
            ? { type: "settings_patch", section: "protocol", field: String(command.execution.state_ref ?? "") }
            : { type: "rumi_function", qualified_name: operationRef };
        return {
          id: command.identity.id,
          name: command.identity.name,
          aliases: command.identity.aliases,
          label: command.presentation.label.fallback,
          description: command.presentation.description?.fallback,
          category: command.presentation.category as ComposerCommandCategory,
          visibility: command.presentation.visibility as ComposerCommandVisibility,
          modes: command.constraints?.modes as ComposerCommandMode[] | undefined,
          risk: command.authorization?.risk as ComposerCommandRisk,
          args,
          execution,
          canonical_id: command.canonical_id,
          protocol_presentation: command.presentation,
          protocol_execution: command.execution,
          availability: command.availability,
        };
      }),
      protocol,
    };
  },

  queryCommandStates(stateRefs: string[]) {
    return request<{ api_version: string; states: CommandStateSnapshot[] }>(
      defaultspackContractRoute("api/command-protocol/v1/states/query"),
      { method: "POST", body: JSON.stringify({ state_refs: stateRefs }) },
    );
  },

  queryCommandDatasource(payload: {
    datasource_ref: string;
    query?: string;
    cursor?: string | null;
    limit?: number;
    selected_values?: string[];
    request_id?: string;
  }, options: { signal?: AbortSignal } = {}) {
    return request<Record<string, unknown>>(
      defaultspackContractRoute("api/command-protocol/v1/datasources/query"),
      { method: "POST", body: JSON.stringify(payload), signal: options.signal },
    );
  },

  toolCatalog() {
    return request<ToolCatalogResponse>(defaultspackContractRoute("api/tools/catalog"), { cache: "no-store" });
  },

  previewToolSelection(payload: {
    conversation_id?: string | null;
    user_text?: string;
    text?: string;
    attachment_metadata?: unknown[];
    tool_selection?: ToolSelectionRequest;
    model?: string | null;
  }) {
    return request<ToolSelectionPreviewResponse>(defaultspackContractRoute("api/tools/selection/preview"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  rebuildToolEmbeddingIndex(payload: { model?: string | null }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/tools/embedding-index/rebuild"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getConversationToolPreferences(conversationId: string) {
    return request<{ conversation_id: string; preferences: Record<string, unknown> }>(
      defaultspackContractRoute(`api/conversations/${encodeURIComponent(conversationId)}/tool-preferences`),
      { cache: "no-store" },
    );
  },

  updateConversationToolPreferences(conversationId: string, preferences: Record<string, unknown>) {
    return request<{ conversation_id: string; preferences: Record<string, unknown> }>(
      defaultspackContractRoute(`api/conversations/${encodeURIComponent(conversationId)}/tool-preferences`),
      { method: "PUT", body: JSON.stringify({ preferences }) },
    );
  },

  async executeResolvedUiCommand(payload: {
    command: string;
    args?: Record<string, unknown>;
    conversation_id?: string | null;
    mode?: ComposerCommandMode;
    invocation_id?: string;
    idempotency_key?: string;
    client_sequence?: number;
    expected_revision?: number;
  } & Partial<Omit<CommandInvocationRequest, "command_ref" | "args" | "conversation_id">>): Promise<ComposerCommandExecuteResult> {
    const result = await request<CommandProtocolInvocationResult>(
      defaultspackContractRoute("api/command-protocol/v1/invoke"),
      {
        method: "POST",
        body: JSON.stringify({ ...payload, command_ref: payload.command }),
      },
    );
    if (result.status === "failed") {
      throw new Error(result.error?.message || "command protocol invocation failed");
    }
    const legacy = result.legacy_result ?? {
      command: {
        id: payload.command,
        name: payload.command,
        label: payload.command,
      } as ComposerCommandItem,
      executed: false,
      requires_approval: result.status === "approval_required",
    };
    return {
      ...legacy,
      operation_id: result.operation_id,
      operation_status: result.status === "succeeded" ? "succeeded" : "pending",
      client_sequence: result.client_sequence,
      state_changes: result.state_changes,
      requires_approval: result.status === "approval_required" || legacy.requires_approval,
      approval_request_id: result.approval?.request_id ?? legacy.approval_request_id,
      approval_kind: result.approval?.kind ?? "coding",
      message: result.message ?? legacy.message,
    };
  },

  prepareHighRiskCommand(payload: {
    invocation_id: string;
    command_ref: "terminal" | "commit" | "push" | "patch" | "restore";
    arguments: Record<string, unknown>;
    presentation: { title: string; summary: string };
  }) {
    return request<HighRiskCommandInvocation>(
      defaultspackContractRoute("api/command-protocol/v1/high-risk"),
      {
        method: "POST",
        body: JSON.stringify({ phase: "prepare", ...payload }),
      },
    );
  },

  listHighRiskCommands() {
    return request<HighRiskCommandInvocationsResponse>(
      defaultspackContractRoute("api/command-protocol/v1/high-risk"),
      {
        method: "POST",
        body: JSON.stringify({ phase: "list_pending" }),
        cache: "no-store",
      },
    );
  },

  highRiskCommandStatus(invocationId: string) {
    return request<HighRiskCommandInvocation>(
      defaultspackContractRoute("api/command-protocol/v1/high-risk"),
      {
        method: "POST",
        body: JSON.stringify({ phase: "status", invocation_id: invocationId }),
        cache: "no-store",
      },
    );
  },

  resumeHighRiskCommand(invocationId: string) {
    return request<HighRiskCommandInvocation>(
      defaultspackContractRoute("api/command-protocol/v1/high-risk"),
      {
        method: "POST",
        body: JSON.stringify({ phase: "resume", invocation_id: invocationId }),
      },
    );
  },

  cancelHighRiskCommand(invocationId: string) {
    return request<HighRiskCommandInvocation>(
      defaultspackContractRoute("api/command-protocol/v1/high-risk"),
      {
        method: "POST",
        body: JSON.stringify({ phase: "cancel", invocation_id: invocationId }),
      },
    );
  },

  resumeResolvedUiCommand(payload: {
    command: string;
    approval_token?: string;
    args?: Record<string, unknown>;
    conversation_id?: string | null;
    mode?: ComposerCommandMode;
    invocation_id?: string;
  } & Partial<Omit<CommandInvocationRequest, "command_ref" | "args" | "approval_token" | "conversation_id">>) {
    return request<CommandProtocolInvocationResult>(
      defaultspackContractRoute("api/command-protocol/v1/resume"),
      {
        method: "POST",
        body: JSON.stringify({ ...payload, command_ref: payload.command }),
      },
    );
  },

  cancelResolvedUiCommand(payload: {
    invocation_id: string;
    command_ref: string;
    conversation_id?: string | null;
    mode?: ComposerCommandMode;
    action: "deny" | "cancel" | "expire";
    reason?: string;
  }) {
    return request<CommandProtocolInvocationResult>(
      defaultspackContractRoute("api/command-protocol/v1/resume"),
      { method: "POST", body: JSON.stringify(payload) },
    );
  },

  queryCommandInvocationEvents(payload: {
    invocation_id: string;
    after_sequence?: number;
    limit?: number;
  }) {
    return request<{
      api_version: string;
      events: Array<Record<string, unknown>>;
      snapshot: Record<string, unknown>;
    }>(defaultspackContractRoute("api/command-protocol/v1/invocations/events/query"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  pendingCommandApprovals() {
    return request<{
      api_version: string;
      pending_approvals: Array<{
        invocation_id: string;
        approval_request_id: string;
        result?: CommandProtocolInvocationResult | null;
      }>;
    }>(defaultspackContractRoute("api/command-protocol/v1/invocations/events/query"), {
      method: "POST",
      body: JSON.stringify({ action: "pending_approvals" }),
    });
  },

  streamCommandInvocationEvents,

  commandOfflineQueue(payload: {
    action: "enqueue" | "pending" | "replay";
    command_ref?: string;
    args?: Record<string, unknown>;
    idempotency_key?: string;
    expected_revision?: number;
    limit?: number;
  }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/command-protocol/v1/offline"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  updateUiSettings(values: Record<string, Record<string, unknown>>) {
    return request<{ values: Record<string, Record<string, unknown>> }>(defaultspackContractRoute("api/ui/settings"), {
      method: "PUT",
      body: JSON.stringify({ values }),
    });
  },

  updateUiSettingsPatches(patches: Array<{ section: string; field: string; value: unknown }>) {
    return request<{ values: Record<string, Record<string, unknown>> }>(defaultspackContractRoute("api/ui/settings"), {
      method: "PUT",
      body: JSON.stringify({ patches }),
    });
  },

  listContinuityNodes() {
    return request<{ nodes: ContinuityNode[]; local_node: ContinuityNode }>(defaultspackContractRoute("api/continuity/nodes"), { cache: "no-store" });
  },

  startContinuityPairing(payload?: { display_name?: string }) {
    return request<ContinuityPairingStartResponse>(defaultspackContractRoute("api/continuity/pairing/start"), {
      method: "POST",
      body: JSON.stringify(payload ?? {}),
    });
  },

  acceptContinuityPairing(payload: {
    request_id: string;
    code: string;
    display_name?: string;
    descriptor?: Record<string, unknown>;
    simulate_local_destination?: boolean;
    destination_kind?: string;
  }) {
    return request<{ node: ContinuityNode }>(defaultspackContractRoute("api/continuity/pairing/accept"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  removeContinuityNode(nodeId: string) {
    return request<{ removed: boolean; node_id: string }>(defaultspackContractRoute(`api/continuity/nodes/${encodeURIComponent(nodeId)}`), {
      method: "DELETE",
    });
  },

  probeContinuityNode(nodeId: string, payload?: Record<string, unknown>) {
    return request<{ node: ContinuityNode; checks: Array<Record<string, unknown>>; ok: boolean }>(
      defaultspackContractRoute(`api/continuity/nodes/${encodeURIComponent(nodeId)}/probe`),
      {
        method: "POST",
        body: JSON.stringify(payload ?? {}),
      },
    );
  },

  listContinuityProviderRoutes() {
    return request<{ routes: ContinuityProviderRoute[] }>(defaultspackContractRoute("api/continuity/provider-routes"), { cache: "no-store" });
  },

  probeContinuityProviderRoute(routeId: string, payload?: { destination_node_id?: string; node_id?: string }) {
    return request<ContinuityPreflightResult>(defaultspackContractRoute(`api/continuity/provider-routes/${encodeURIComponent(routeId)}/probe`), {
      method: "POST",
      body: JSON.stringify({ ...(payload ?? {}), route_id: routeId }),
    });
  },

  setContinuityProviderFallbacks(routeId: string, fallbackRouteIds: string[]) {
    return request<{ route_id: string; fallback_route_ids: string[] }>(
      defaultspackContractRoute(`api/continuity/provider-routes/${encodeURIComponent(routeId)}/set-fallbacks`),
      {
        method: "POST",
        body: JSON.stringify({ route_id: routeId, fallback_route_ids: fallbackRouteIds }),
      },
    );
  },

  listContinuityProviderExtensions() {
    return request<{ extensions: Array<Record<string, unknown>> }>(defaultspackContractRoute("api/continuity/provider-extensions"), { cache: "no-store" });
  },

  planContinuityHandoff(payload: ContinuityHandoffRequest) {
    return request<{ plan: ContinuityHandoffPlan }>(defaultspackContractRoute("api/continuity/plans"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  listContinuityHandoffs() {
    return request<{ operations: ContinuityHandoffOperation[] }>(defaultspackContractRoute("api/continuity/handoffs"), { cache: "no-store" });
  },

  getContinuityHandoff(operationId: string) {
    return request<{ operation: ContinuityHandoffOperation }>(defaultspackContractRoute(`api/continuity/handoffs/${encodeURIComponent(operationId)}`), {
      cache: "no-store",
    });
  },

  cancelContinuityHandoff(operationId: string) {
    return request<{ operation: ContinuityHandoffOperation }>(defaultspackContractRoute(`api/continuity/handoffs/${encodeURIComponent(operationId)}/cancel`), {
      method: "POST",
    });
  },

  createContinuityCheckpoint(payload: ContinuityHandoffRequest) {
    return request<{ operation: ContinuityHandoffOperation; checkpoint: Record<string, unknown> }>(defaultspackContractRoute("api/continuity/checkpoints"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  writeClipboard(content: string) {
    return request<{ written: boolean }>(defaultspackContractRoute("api/ui/clipboard"), {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },

  reportClientEvent(payload: {
    source?: string;
    category?: string;
    level?: string;
    message: string;
    fingerprint?: string;
    conversation_id?: string;
    detail?: unknown;
  }) {
    return request<{ recorded: boolean; diagnostic_id?: string }>(defaultspackContractRoute("api/ui/client-events"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  saveProviderApiKey(providerId: string, value: string, options?: {
    apiId?: string;
    name?: string;
    baseUrl?: string;
    allowedModels?: string[];
    defaultModel?: string;
    notes?: string;
    quotaLabel?: string;
    kind?: string;
  }) {
    return request<{ provider_id: string; api_id?: string; name?: string; configured: boolean; kind?: string; model_availability?: ModelAvailabilityAfterKeySave }>(defaultspackContractRoute("api/ai/provider-key"), {
      method: "POST",
      body: JSON.stringify({
        provider_id: providerId,
        value,
        api_id: options?.apiId,
        name: options?.name,
        base_url: options?.baseUrl,
        allowed_models: options?.allowedModels,
        default_model: options?.defaultModel,
        notes: options?.notes,
        quota_label: options?.quotaLabel,
        kind: options?.kind,
      }),
    });
  },

  registerCustomProvider(providerId: string, options?: { label?: string; kind?: string }) {
    return request<{ provider_id: string; label?: string; kind?: string; builtin?: boolean }>(defaultspackContractRoute("api/ai/provider-key"), {
      method: "POST",
      body: JSON.stringify({
        action: "register_provider",
        provider_id: providerId,
        label: options?.label,
        kind: options?.kind,
      }),
    });
  },

  deleteCustomProvider(providerId: string) {
    return request<{ provider_id: string; deleted?: boolean }>(defaultspackContractRoute("api/ai/provider-key"), {
      method: "POST",
      body: JSON.stringify({
        action: "delete_provider",
        provider_id: providerId,
      }),
    });
  },

  renameProviderApiKey(providerId: string, apiId: string, name: string) {
    return request<{ provider_id: string; api_id?: string; name?: string; configured: boolean }>(defaultspackContractRoute("api/ai/provider-key"), {
      method: "POST",
      body: JSON.stringify({
        action: "rename",
        provider_id: providerId,
        api_id: apiId,
        name,
        new_api_id: name,
      }),
    });
  },

  deleteProviderApiKey(providerId: string, apiId: string) {
    return request<{ provider_id: string; api_id?: string; configured: boolean; cleared?: boolean }>(defaultspackContractRoute("api/ai/provider-key"), {
      method: "POST",
      body: JSON.stringify({
        action: "delete",
        provider_id: providerId,
        api_id: apiId,
      }),
    });
  },

  providerOAuthStatus(providerId?: string, options: { activeDiagnostics?: boolean } = {}) {
    const params = new URLSearchParams();
    if (providerId) params.set("provider_id", providerId);
    if (options.activeDiagnostics) params.set("active_diagnostics", "true");
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return request<{ provider?: Record<string, unknown>; providers?: Record<string, Record<string, unknown>> }>(defaultspackContractRoute(`api/ai/oauth${suffix}`), { cache: "no-store" });
  },

  runProviderOAuthDiagnostics(providerId: string) {
    return request<{ provider_id: string; provider?: Record<string, unknown> }>(defaultspackContractRoute("api/ai/oauth"), {
      method: "POST",
      body: JSON.stringify({
        action: "cloudflare_diagnostics",
        provider_id: providerId,
      }),
    });
  },

  saveProviderOAuthClientConfig(providerId: string, clientConfig: string) {
    return request<{ provider_id: string; client_configured: boolean; client_label?: string }>(defaultspackContractRoute("api/ai/oauth"), {
      method: "POST",
      body: JSON.stringify({
        action: "save_client",
        provider_id: providerId,
        client_config: clientConfig,
      }),
    });
  },

  importConnectionBundle(credentialBundle: string | Record<string, unknown>, providerId?: string) {
    return request<{
      provider_id: string;
      connection_id: string;
      credential_ref?: Record<string, string>;
      scopes?: string[];
      capabilities?: string[];
      approval_required_capabilities?: string[];
      rejected_capabilities?: string[];
      expires_at?: string;
      status?: string;
    }>(defaultspackContractRoute("api/connections/import"), {
      method: "POST",
      body: JSON.stringify({
        provider_id: providerId,
        credential_bundle: credentialBundle,
      }),
    });
  },

  importProviderConnection(providerId: string, credentialBundle: string) {
    return request<{
      provider_id: string;
      connection_id: string;
      credential_ref?: Record<string, string>;
      scopes?: string[];
      capabilities?: string[];
      approval_required_capabilities?: string[];
      rejected_capabilities?: string[];
      expires_at?: string;
      status?: string;
    }>(defaultspackContractRoute("api/connections/import"), {
      method: "POST",
      body: JSON.stringify({
        provider_id: providerId,
        credential_bundle: credentialBundle,
      }),
    });
  },

  startProviderOAuth(providerId: string, options: { scopeMode?: string; services?: string[] } = {}) {
    return request<{ provider_id: string; authorize_url: string; redirect_uri: string; scope_mode?: string; services?: string[]; scopes: string[] }>(defaultspackContractRoute("api/ai/oauth"), {
      method: "POST",
      body: JSON.stringify({
        action: "start",
        provider_id: providerId,
        scope_mode: options.scopeMode,
        services: options.services,
      }),
    });
  },

  disconnectProviderOAuth(providerId: string) {
    return request<{ provider_id: string; connected: boolean }>(defaultspackContractRoute("api/ai/oauth"), {
      method: "POST",
      body: JSON.stringify({
        action: "disconnect",
        provider_id: providerId,
      }),
    });
  },

  clearProviderOAuthClientConfig(providerId: string) {
    return request<{ provider_id: string; client_configured: boolean; connected: boolean }>(defaultspackContractRoute("api/ai/oauth"), {
      method: "POST",
      body: JSON.stringify({
        action: "clear_client",
        provider_id: providerId,
      }),
    });
  },

  getCodexConnectionStatus() {
    return request<CodexConnectionStatusResponse>(defaultspackContractRoute("api/connections/codex"), { cache: "no-store" });
  },

  saveCodexAccessToken(accessToken: string) {
    return request<CodexConnectionActionResponse>(defaultspackContractRoute("api/connections/codex"), {
      method: "POST",
      body: JSON.stringify({
        action: "save_token",
        access_token: accessToken,
      }),
    });
  },

  clearCodexAccessToken() {
    return request<CodexConnectionActionResponse>(defaultspackContractRoute("api/connections/codex"), {
      method: "POST",
      body: JSON.stringify({
        action: "clear_token",
      }),
    });
  },

  saveCodexAppServerConfig(config: CodexAppServerConfig) {
    return request<CodexConnectionActionResponse>(defaultspackContractRoute("api/connections/codex"), {
      method: "POST",
      body: JSON.stringify({
        action: "save_app_server",
        app_server: {
          transport: config.transport,
          enabled: config.enabled,
          base_url: config.baseUrl,
          websocket_url: config.websocketUrl,
          unix_socket_path: config.unixSocketPath,
          ws_token_file: config.wsTokenFile,
          shared_secret_file: config.sharedSecretFile,
          tool_source_enabled: config.toolSourceEnabled,
          automation_endpoint_enabled: config.automationEndpointEnabled,
        },
      }),
    });
  },

  clearCodexAppServerConfig() {
    return request<CodexConnectionActionResponse>(defaultspackContractRoute("api/connections/codex"), {
      method: "POST",
      body: JSON.stringify({
        action: "clear_app_server",
      }),
    });
  },

  probeCodexAppServer() {
    return request<CodexConnectionActionResponse>(defaultspackContractRoute("api/connections/codex"), {
      method: "POST",
      body: JSON.stringify({
        action: "probe_app_server",
      }),
    });
  },

  saveExternalToken(providerId: string, value: string, options?: { tokenId?: string; name?: string; kind?: string }) {
    return request<{ provider_id: string; token_id?: string; name?: string; kind?: string; configured: boolean }>(defaultspackContractRoute("api/external/tokens"), {
      method: "POST",
      body: JSON.stringify({
        provider_id: providerId,
        token_id: options?.tokenId,
        name: options?.name,
        kind: options?.kind,
        value,
      }),
    });
  },

  renameExternalToken(providerId: string, tokenId: string, name: string) {
    return request<{ provider_id: string; token_id?: string; name?: string; configured: boolean }>(defaultspackContractRoute("api/external/tokens"), {
      method: "POST",
      body: JSON.stringify({
        action: "rename",
        provider_id: providerId,
        token_id: tokenId,
        name,
        new_token_id: name,
      }),
    });
  },

  deleteExternalToken(providerId: string, tokenId: string) {
    return request<{ provider_id: string; token_id?: string; configured: boolean; cleared?: boolean }>(defaultspackContractRoute("api/external/tokens"), {
      method: "POST",
      body: JSON.stringify({
        action: "delete",
        provider_id: providerId,
        token_id: tokenId,
      }),
    });
  },

  listPublicUrlProviders() {
    return request<{ providers: Array<Record<string, unknown>>; default_local_url?: string }>(defaultspackContractRoute("api/webhooks/public-urls"));
  },

  createPublicUrl(payload: { provider_id?: string; provider?: string; local_url?: string; route_path?: string; ttl_seconds?: number }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/webhooks/public-urls"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  closePublicUrl(urlId: string) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/webhooks/public-urls/${encodeURIComponent(urlId)}`), {
      method: "DELETE",
    });
  },

  conversationPreview(conversationId: string) {
    return request<{ conversation_id: string; previews: ToolPreviewItem[]; summary: Record<string, number> }>(
      defaultspackContractRoute(`api/ui/conversations/${conversationId}/preview`),
    );
  },

  exportConversation(conversationId: string, format = "markdown") {
    return request<{ conversation_id: string; content: string; format: "markdown" | "json" }>(
      defaultspackContractRoute(`api/chat/conversations/${conversationId}/export`),
      {
        method: "POST",
        body: JSON.stringify({ format }),
      },
    );
  },

  listArtifacts() {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/artifacts"));
  },

  webSearch(query: string, allowNetwork = false) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/research/web-search"), {
      method: "POST",
      body: JSON.stringify({ query, allow_network: allowNetwork, limit: 5 }),
    });
  },

  redditSearch(query: string, allowNetwork = false) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/research/reddit-search"), {
      method: "POST",
      body: JSON.stringify({ query, allow_network: allowNetwork, limit: 5 }),
    });
  },

  browserComputer(action: string, payload?: Record<string, unknown>) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/tools/browser-computer"), {
      method: "POST",
      body: JSON.stringify({ action, payload: payload ?? {} }),
    });
  },

  approveBrowserComputerAction(toolName: string, action: string, payload?: Record<string, unknown>) {
    const normalizedAction = normalizeBrowserComputerApprovalAction(toolName, action);
    if (usesBrowserComputerApprovalEndpoint(toolName)) {
      return request<Record<string, unknown>>(defaultspackContractRoute("api/tools/browser-computer"), {
        method: "POST",
        body: JSON.stringify({ action: normalizedAction, payload: payload ?? {} }),
      });
    }
    return request<Record<string, unknown>>(defaultspackContractRoute("api/tools/invoke"), {
      method: "POST",
      body: JSON.stringify({
        tool_name: toolName,
        arguments: { ...(payload ?? {}), action: normalizedAction },
      }),
    });
  },

  invokeTool(toolName: string, argumentsPayload?: Record<string, unknown>, context?: Record<string, unknown>) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/tools/invoke"), {
      method: "POST",
      body: JSON.stringify({
        tool_name: toolName,
        arguments: argumentsPayload ?? {},
        ...(context ? { context } : {}),
      }),
    });
  },

  getBrowserScreenshots(conversationId: string, runId: string) {
    return request<{ screenshots: BrowserScreenshot[]; omitted_count?: number }>(
      defaultspackContractRoute(`api/chat/conversations/${conversationId}/run-results/${runId}/browser-screenshots`),
    );
  },

  listSchedules() {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/agent/schedules"));
  },

  createSchedule(payload: Record<string, unknown>) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/agent/schedules"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  updateSchedule(scheduleId: string, payload: Record<string, unknown>) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/agent/schedules/${encodeURIComponent(scheduleId)}`), {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  deleteSchedule(scheduleId: string) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/agent/schedules/${encodeURIComponent(scheduleId)}`), {
      method: "DELETE",
    });
  },

  getScheduleHistory(scheduleId: string) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/agent/schedules/${encodeURIComponent(scheduleId)}/history`), {
      cache: "no-store",
    });
  },

  getOperationsCompanyStatus() {
    return request<OperationsCompanyStatus>(defaultspackContractRoute("api/agent/company/status"));
  },

  bootstrapOperationsCompany(options?: {
    start_nonstop?: boolean;
    heartbeat_minutes?: number;
    model?: string;
  }) {
    return request<OperationsCompanyStatus>(defaultspackContractRoute("api/agent/company/bootstrap"), {
      method: "POST",
      body: JSON.stringify(options ?? {}),
    });
  },

  getMimoCodingCompanyStatus() {
    return request<MimoCodingCompanyStatus>(defaultspackContractRoute("api/agent/mimo-company/status"));
  },

  bootstrapMimoCodingCompany(options?: {
    start_nonstop?: boolean;
    heartbeat_minutes?: number;
    review_interval_minutes?: number;
    qa_interval_minutes?: number;
    max_tool_calls?: number;
    model?: string;
    vision_model?: string;
    fast_model?: string;
    qa_targets?: string[];
    docker_worker_count?: number;
    docker_personas?: string[];
    workspace_id?: string | null;
    workspace_label?: string | null;
    workspace_root?: string | null;
    run_initial_review_now?: boolean;
    seed_tasks?: boolean;
    seed_knowledge?: boolean;
  }) {
    return request<MimoCodingCompanyStatus>(defaultspackContractRoute("api/agent/mimo-company/bootstrap"), {
      method: "POST",
      body: JSON.stringify(options ?? {}),
    });
  },

  listCompanies(options?: { limit?: number; offset?: number }) {
    return request<{ companies: CompanyRecord[]; total: number }>(
      withQuery(defaultspackContractRoute("api/company"), options),
      { cache: "no-store" },
    );
  },

  getCompany(companyId: string) {
    return request<CompanyRecord>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}`), { cache: "no-store" });
  },

  createCompany(payload: {
    id?: string;
    company_id?: string;
    name: string;
    description?: string;
    settings?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
  }) {
    return request<CompanyRecord>(defaultspackContractRoute("api/company"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  updateCompany(companyId: string, updates: Partial<CompanyRecord>) {
    return request<CompanyRecord>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}`), {
      method: "PUT",
      body: JSON.stringify({ company_id: companyId, updates }),
    });
  },

  deleteCompany(companyId: string) {
    return request<{ deleted: boolean; company_id: string }>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}`), {
      method: "DELETE",
    });
  },

  getCompanyStatus(options?: string | { companyId?: string | null; conversationId?: string | null; bootstrap?: boolean }) {
    const query = typeof options === "string"
      ? { company_id: options }
      : {
          company_id: options?.companyId,
          conversation_id: options?.conversationId,
          bootstrap: options?.bootstrap,
        };
    return request<CompanyStatusResponse>(
      withQuery(defaultspackContractRoute("api/company/status"), query),
      { cache: "no-store" },
    );
  },

  createRemoteTask,

  getRemoteTask,

  listRemoteTaskEvents,

  cancelRemoteTask,

  getRemoteHostStatus,

  bootstrapCompanyWorkspace(metadata?: Record<string, unknown>, options?: { conversationId?: string | null; scope?: "conversation" | "default" }) {
    return request<{ bootstrapped: boolean; company: CompanyRecord }>(defaultspackContractRoute("api/company/bootstrap"), {
      method: "POST",
      body: JSON.stringify({
        ...(metadata ? { metadata } : {}),
        ...(options?.conversationId ? { conversation_id: options.conversationId } : {}),
        ...(options?.scope ? { scope: options.scope } : {}),
      }),
    });
  },

  getCompanySettings(companyId: string) {
    return request<{ settings: Record<string, unknown> }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/settings`), { company_id: companyId }),
      { cache: "no-store" },
    );
  },

  updateCompanySettings(companyId: string, settings: Record<string, unknown>, replace = false) {
    return request<{ settings: Record<string, unknown> }>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/settings`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "update", settings, replace }),
    });
  },

  listCompanyAgents(companyId: string) {
    return request<{ agents: CompanyAgent[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/agents`), { company_id: companyId }),
      { cache: "no-store" },
    );
  },

  upsertCompanyAgent(companyId: string, agent: Partial<CompanyAgent>) {
    return request<CompanyAgent>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/agents`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "upsert", agent }),
    });
  },

  listCompanyChannels(companyId: string) {
    return request<{ channels: CompanyChannel[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/channels`), { company_id: companyId }),
      { cache: "no-store" },
    );
  },

  upsertCompanyChannel(companyId: string, channel: Partial<CompanyChannel>) {
    return request<CompanyChannel>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/channels`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "upsert", channel }),
    });
  },

  listCompanyMessages(companyId: string, options?: {
    channel_id?: string;
    thread_id?: string;
    limit?: number;
    offset?: number;
    tail?: boolean;
    latest?: boolean;
    order?: "asc" | "desc" | string;
  }) {
    return request<{ messages: CompanyMessage[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/messages`), { company_id: companyId, ...options }),
      { cache: "no-store" },
    );
  },

  sendCompanyMessage(companyId: string, payload: {
    content: string;
    channel_id?: string;
    sender_id?: string;
    mentions?: string[];
    task_ids?: string[];
    metadata?: Record<string, unknown>;
  }) {
    return request<CompanyMessage>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/messages`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "create", ...payload }),
    });
  },

  listCompanyTasks(companyId: string, options?: { status?: string; target_agent_id?: string; limit?: number; offset?: number }) {
    return request<{ tasks: CompanyTask[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/tasks`), { company_id: companyId, ...options }),
      { cache: "no-store" },
    );
  },

  createCompanyTask(companyId: string, payload: {
    title: string;
    description?: string;
    target_agent_ids?: string[];
    source?: string;
    metadata?: Record<string, unknown>;
  }) {
    return request<CompanyTask>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/tasks`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "create", ...payload }),
    });
  },

  updateCompanyTask(companyId: string, taskId: string, updates: Partial<CompanyTask>) {
    return request<CompanyTask>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/tasks`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "update", task_id: taskId, updates }),
    });
  },

  deleteCompanyTask(companyId: string, taskId: string) {
    return request<{ deleted: boolean; task_id: string }>(
      defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/tasks/${encodeURIComponent(taskId)}`),
      { method: "DELETE" },
    );
  },

  dispatchCompanyTask(companyId: string, taskId: string, policy?: Record<string, unknown>) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/dispatch`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, task_id: taskId, policy }),
    });
  },

  listCompanyRuns(companyId: string, options?: { agent_id?: string; task_id?: string; status?: string; limit?: number; offset?: number }) {
    return request<{ runs: CompanyRunLink[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/runs`), { company_id: companyId, ...options }),
      { cache: "no-store" },
    );
  },

  listCompanyAgentInbox(companyId: string, agentId: string, options?: { status?: string; kind?: string; limit?: number }) {
    return request<{ inbox: CompanyInboxItem[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/agents/${encodeURIComponent(agentId)}/inbox`), {
        company_id: companyId,
        agent_id: agentId,
        ...options,
      }),
      { cache: "no-store" },
    );
  },

  listCompanyInboundRoutes(companyId: string) {
    return request<{ routes: CompanyInboundRoute[]; total: number }>(
      withQuery(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/inbound-routes`), { company_id: companyId }),
      { cache: "no-store" },
    );
  },

  bootstrapSubagentTeamWorkspace(metadata?: Record<string, unknown>, options?: { conversationId?: string | null; scope?: "conversation" | "default" }) {
    return request<{ bootstrapped: boolean; company: CompanyRecord }>(defaultspackContractRoute("api/subagent-team/bootstrap"), {
      method: "POST",
      body: JSON.stringify({
        ...(metadata ? { metadata } : {}),
        ...(options?.conversationId ? { conversation_id: options.conversationId } : {}),
        ...(options?.scope ? { scope: options.scope } : {}),
      }),
    });
  },

  updateSubagentTeamWorkspaceMetadata(options: {
    companyId?: string | null;
    conversationId?: string | null;
    metadata: Record<string, unknown>;
  }) {
    return request<CompanyRecord>(defaultspackContractRoute("api/subagent-team/workspace/metadata"), {
      method: "POST",
      body: JSON.stringify({
        company_id: options.companyId,
        conversation_id: options.conversationId,
        metadata: options.metadata,
      }),
    });
  },

  sendSubagentTeamMessage(payload: {
    companyId?: string | null;
    conversationId?: string | null;
    content: string;
    channel_id?: string;
    sender_id?: string;
    mentions?: string[];
    task_ids?: string[];
    client_message_id?: string;
    metadata?: Record<string, unknown>;
  }) {
    return request<CompanyMessage>(defaultspackContractRoute("api/subagent-team/messages"), {
      method: "POST",
      body: JSON.stringify({
        company_id: payload.companyId,
        conversation_id: payload.conversationId,
        content: payload.content,
        channel_id: payload.channel_id,
        sender_id: payload.sender_id,
        mentions: payload.mentions,
        task_ids: payload.task_ids,
        client_message_id: payload.client_message_id,
        metadata: payload.metadata,
      }),
    });
  },

  getSubagentTeamRichSettings(options?: { companyId?: string | null; conversationId?: string | null }) {
    return request<SubagentTeamRichSettingsResponse>(
      withQuery(defaultspackContractRoute("api/subagent-team/rich"), {
        company_id: options?.companyId,
        conversation_id: options?.conversationId,
      }),
      { cache: "no-store" },
    );
  },

  updateSubagentTeamRichSettings(payload: Partial<SubagentTeamRichSettings> & {
    companyId?: string | null;
    conversationId?: string | null;
  }) {
    const { companyId, conversationId, ...settings } = payload;
    return request<SubagentTeamRichSettingsResponse>(defaultspackContractRoute("api/subagent-team/rich"), {
      method: "POST",
      body: JSON.stringify({
        company_id: companyId,
        conversation_id: conversationId,
        ...settings,
      }),
    });
  },

  getSubagentTeamCreatorSettings(options?: { companyId?: string | null; conversationId?: string | null }) {
    return request<SubagentTeamCreatorSettingsResponse>(
      withQuery(defaultspackContractRoute("api/subagent-team/creator/settings"), {
        company_id: options?.companyId,
        conversation_id: options?.conversationId,
      }),
      { cache: "no-store" },
    );
  },

  updateSubagentTeamCreatorSettings(payload: Partial<SubagentTeamCreatorSettings> & {
    companyId?: string | null;
    conversationId?: string | null;
  }) {
    const { companyId, conversationId, ...settings } = payload;
    return request<SubagentTeamCreatorSettingsResponse>(defaultspackContractRoute("api/subagent-team/creator/settings"), {
      method: "PATCH",
      body: JSON.stringify({
        company_id: companyId,
        conversation_id: conversationId,
        settings,
      }),
    });
  },

  testSubagentTeamCreator(payload?: {
    companyId?: string | null;
    conversationId?: string | null;
    prompt?: string;
    channel_id?: string;
    agent_id?: string;
    metadata?: Record<string, unknown>;
  }) {
    return request<SubagentTeamCreatorTestResponse>(defaultspackContractRoute("api/subagent-team/creator/test"), {
      method: "POST",
      body: JSON.stringify({
        company_id: payload?.companyId,
        conversation_id: payload?.conversationId,
        prompt: payload?.prompt,
        channel_id: payload?.channel_id,
        agent_id: payload?.agent_id,
        metadata: payload?.metadata,
      }),
    });
  },

  getSubagentTeamCreatorDecisionPreview(options?: { companyId?: string | null; conversationId?: string | null; channelId?: string | null }) {
    return request<SubagentTeamDecisionPreviewResponse>(
      withQuery(defaultspackContractRoute("api/subagent-team/creator/decision-preview"), {
        company_id: options?.companyId,
        conversation_id: options?.conversationId,
        channel_id: options?.channelId,
      }),
      { cache: "no-store" },
    );
  },

  getSubagentTeamFileTree(options?: {
    companyId?: string | null;
    conversationId?: string | null;
    directory?: string;
    limit?: number;
    includeGit?: boolean;
  }) {
    return request<SubagentTeamFileTreeResponse>(
      withQuery(defaultspackContractRoute("api/subagent-team/file-tree"), {
        company_id: options?.companyId,
        conversation_id: options?.conversationId,
        directory: options?.directory,
        limit: options?.limit,
        include_git: options?.includeGit,
      }),
      { cache: "no-store" },
    );
  },

  openSubagentTeamFileTreeNode(options: {
    nodeId: string;
    companyId?: string | null;
    conversationId?: string | null;
  }) {
    return request<SubagentTeamFileTreeOpenResponse>(
      withQuery(defaultspackContractRoute("api/subagent-team/file-tree/open"), {
        node_id: options.nodeId,
        company_id: options.companyId,
        conversation_id: options.conversationId,
      }),
      { cache: "no-store" },
    );
  },

  upsertCompanyInboundRoute(companyId: string, route: Partial<CompanyInboundRoute>) {
    return request<CompanyInboundRoute>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/inbound-routes`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "upsert", route }),
    });
  },

  deleteCompanyInboundRoute(companyId: string, routeId: string) {
    return request<{ deleted: boolean; route_id: string }>(defaultspackContractRoute(`api/company/${encodeURIComponent(companyId)}/inbound-routes`), {
      method: "POST",
      body: JSON.stringify({ company_id: companyId, action: "delete", route_id: routeId }),
    });
  },

  triggerSchedule(scheduleId: string) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/agent/schedules/${scheduleId}/trigger`), {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  pauseSchedule(scheduleId: string) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/agent/schedules/${encodeURIComponent(scheduleId)}/pause`), {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  resumeSchedule(scheduleId: string) {
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/agent/schedules/${encodeURIComponent(scheduleId)}/resume`), {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  listChannels() {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/chat/channels"));
  },

  getP2PStatus() {
    return request<P2PStatusResponse>(defaultspackContractRoute("api/p2p/status"), { cache: "no-store" });
  },

  getP2PIdentity(label?: string) {
    return request<{ identity: P2PIdentity; p2p: P2PSettings }>(
      withQuery(defaultspackContractRoute("api/p2p/identity"), { label }),
      { cache: "no-store" },
    );
  },

  listP2PPeers() {
    return request<{ peers: P2PPeer[] }>(defaultspackContractRoute("api/p2p/peers"), { cache: "no-store" });
  },

  approveP2PPeer(payload: {
    peer_id: string;
    fingerprint?: string;
    hmac_secret?: string;
    shared_secret?: string;
    capabilities?: string[];
    allowed_company_ids?: string[];
    label?: string;
    metadata?: Record<string, unknown>;
  }) {
    return request<{ peer: P2PPeer }>(defaultspackContractRoute("api/p2p/peers"), {
      method: "POST",
      body: JSON.stringify({ action: "approve", ...payload }),
    });
  },

  blockP2PPeer(peerId: string, reason?: string) {
    return request<{ peer: P2PPeer }>(defaultspackContractRoute("api/p2p/peers"), {
      method: "POST",
      body: JSON.stringify({ action: "block", peer_id: peerId, reason }),
    });
  },

  startP2PPairing(payload?: {
    peer_id?: string;
    peer_fingerprint?: string;
    peer_label?: string;
    ttl_seconds?: number;
    capabilities?: string[];
    allowed_company_ids?: string[];
  }) {
    return request<{ pairing: P2PPairing }>(defaultspackContractRoute("api/p2p/pairing/start"), {
      method: "POST",
      body: JSON.stringify(payload ?? {}),
    });
  },

  acceptP2PPairing(payload: {
    code: string;
    peer_id?: string;
    peer_fingerprint?: string;
    peer_label?: string;
    hmac_secret?: string;
    shared_secret?: string;
    capabilities?: string[];
    allowed_company_ids?: string[];
  }) {
    return request<{ ok: boolean; pairing: P2PPairing; peer?: P2PPeer; hmac_secret?: string }>(defaultspackContractRoute("api/p2p/pairing/accept"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  rejectP2PPairing(code: string, reason?: string) {
    return request<{ ok: boolean; pairing: P2PPairing }>(defaultspackContractRoute("api/p2p/pairing/reject"), {
      method: "POST",
      body: JSON.stringify({ code, reason }),
    });
  },

  listMobileDevices() {
    return request<MobileDevicesResponse>(defaultspackContractRoute("api/mobile/v1/devices"), { cache: "no-store" });
  },

  createCredentialTransfer(payload: { device_id: string; provider_id: string; api_id: string; provider_label?: string }) {
    return request<{ transfer: CredentialTransfer }>(defaultspackContractRoute("api/mobile/v1/credential-transfers"), {
      method: "POST", body: JSON.stringify(payload),
    });
  },

  confirmCredentialTransfer(transferId: string, payload: { device_id: string; provider_id: string; api_id: string; user_confirmed: true }) {
    return request<{ transfer: CredentialTransfer }>(defaultspackContractRoute(`api/mobile/v1/credential-transfers/${encodeURIComponent(transferId)}/confirm`), {
      method: "POST", body: JSON.stringify(payload),
    });
  },

  getCredentialTransferStatus(transferId: string) {
    return request<{ transfer: CredentialTransfer }>(defaultspackContractRoute(`api/mobile/v1/credential-transfers/${encodeURIComponent(transferId)}/status`), { cache: "no-store" });
  },

  cancelCredentialTransfer(transferId: string) {
    return request<{ transfer: CredentialTransfer }>(defaultspackContractRoute(`api/mobile/v1/credential-transfers/${encodeURIComponent(transferId)}/cancel`), {
      method: "POST", body: JSON.stringify({ reason: "cancelled by PC user" }),
    });
  },

  revokeCredentialTransfer(transferId: string) {
    return request<{ transfer: CredentialTransfer }>(defaultspackContractRoute(`api/mobile/v1/credential-transfers/${encodeURIComponent(transferId)}/revoke`), {
      method: "POST", body: JSON.stringify({ reason: "revoked by PC user" }),
    });
  },

  getMobilePairingStatus(pairingId: string) {
    return request<MobilePairingStatus>(
      defaultspackContractRoute(`api/mobile/v1/pairings/${encodeURIComponent(pairingId)}/status`),
      { cache: "no-store" },
    );
  },

  getMobilePairingReview(pairingId: string) {
    return request<MobilePairingReview>(
      defaultspackContractRoute(`api/mobile/v1/pairings/${encodeURIComponent(pairingId)}/review`),
      { cache: "no-store" },
    );
  },

  approveMobilePairing(pairingId: string, payload: MobilePairingApprovePayload) {
    return request<{ pairing?: MobilePairingStatus }>(
      defaultspackContractRoute(`api/mobile/v1/pairings/${encodeURIComponent(pairingId)}/approve`),
      { method: "POST", body: JSON.stringify(payload) },
    );
  },

  rejectMobilePairing(pairingId: string, reason?: string) {
    return request<{ pairing?: MobilePairingStatus }>(
      defaultspackContractRoute(`api/mobile/v1/pairings/${encodeURIComponent(pairingId)}/reject`),
      { method: "POST", body: JSON.stringify({ reason }) },
    );
  },

  sendP2PMessage(peerId: string, payload: {
    text?: string;
    message?: string;
    body?: Record<string, unknown>;
    type?: string;
    metadata?: Record<string, unknown>;
    ttl_seconds?: number;
  }) {
    return request<{ envelope: Record<string, unknown>; peer: P2PPeer }>(defaultspackContractRoute("api/p2p/messages/send"), {
      method: "POST",
      body: JSON.stringify({ peer_id: peerId, ...payload }),
    });
  },

  createShare(payload: Record<string, unknown>) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/share"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getShare(token: string) {
    return request<ConversationShareRecord>(defaultspackContractRoute(`api/share/${encodeURIComponent(token)}`), { cache: "no-store" });
  },

  importShare(token: string, sourceUrl?: string, importMode: "read_only" | "continue_copy" = "continue_copy") {
    return request<{ conversation: Conversation; conversation_id: string; import_mode: string; audit?: Record<string, unknown> }>(
      defaultspackContractRoute(`api/share/${encodeURIComponent(token)}/import`),
      { method: "POST", body: JSON.stringify({ source_url: sourceUrl, import_mode: importMode }) },
    );
  },

  revokeShare(token: string) {
    return request<{ revoked: boolean }>(defaultspackContractRoute(`api/share/${encodeURIComponent(token)}`), { method: "DELETE" });
  },

  exportShare(token: string) {
    return request<{ conversation: ConversationShareBundle["conversation"]; audit?: Record<string, unknown> }>(
      defaultspackContractRoute(`api/share/${encodeURIComponent(token)}/export`), { method: "POST", body: "{}" },
    );
  },

  importConversationBundle(bundle: Record<string, unknown>, sourceUrl?: string) {
    return request<{ conversation: Conversation; conversation_id: string }>(
      defaultspackContractRoute("api/packs/defaultspack/chat/conversations/import"),
      { method: "POST", body: JSON.stringify({ bundle, source_url: sourceUrl }) },
    );
  },

  compactConversation(conversationId: string, options?: CompactConversationOptions) {
    return request<CompactConversationResult>(
      defaultspackContractRoute(`api/chat/conversations/${encodeURIComponent(conversationId)}/compact`),
      {
        method: "POST",
        body: JSON.stringify({ conversation_id: conversationId, ...(options ?? {}) }),
      },
    );
  },

  autoCompactConversation(conversationId: string, options?: CompactConversationOptions & { mode?: "suggest" | "apply" }) {
    return request<CompactConversationResult>(
      defaultspackContractRoute(`api/chat/conversations/${encodeURIComponent(conversationId)}/auto-compact`),
      {
        method: "POST",
        body: JSON.stringify({ conversation_id: conversationId, mode: options?.mode ?? "suggest", ...(options ?? {}) }),
      },
    );
  },

  getCodingContext(options?: { directory?: string; workspace_id?: string | null }) {
    return request<CodingContextResponse>(
      withQuery(defaultspackContractRoute("api/coding/context"), { directory: options?.directory, workspace_id: options?.workspace_id }),
    );
  },

  listWorkspaceFiles(directory?: string, options?: { workspace_id?: string | null }) {
    return request<{ files: CodingContextEntry[] }>(
      withQuery(defaultspackContractRoute("api/coding/files"), { directory, workspace_id: options?.workspace_id }),
    );
  },

  readWorkspaceFile(path: string, options?: { workspace_id?: string | null }) {
    return request<{
      path: string;
      content: string;
      size?: number;
      encoding?: string;
      workspace_id?: string | null;
      workspace_root?: string | null;
    }>(defaultspackContractRoute("api/coding/files/read"), {
      method: "POST",
      body: JSON.stringify({ path, workspace_id: options?.workspace_id }),
    });
  },

  getGitBranch(options?: { workspace_id?: string | null }) {
    return request<CodingBranchResponse>(
      withQuery(defaultspackContractRoute("api/coding/git/branch"), { workspace_id: options?.workspace_id }),
    );
  },

  switchGitBranch(branch: string, create = false, options?: { workspace_id?: string | null }) {
    return request<CodingBranchResponse>(defaultspackContractRoute("api/coding/git/branch"), {
      method: "POST",
      body: JSON.stringify({ action: "switch", branch, create, workspace_id: options?.workspace_id }),
    });
  },

  listCodingWorkspaces() {
    return request<CodingWorkspacesResponse>(defaultspackContractRoute("api/coding/workspaces"), { cache: "no-store" });
  },

  getCodingWorkspace(workspaceId: string) {
    return request<{ workspace: CodingWorkspaceRecord }>(
      withQuery(defaultspackContractRoute("api/coding/workspaces/get"), { workspace_id: workspaceId }),
      { cache: "no-store" },
    );
  },

  createCodingWorkspace(payload: {
    root_path?: string;
    workspace_root?: string;
    label?: string;
    workspace_id?: string;
    trusted?: boolean;
    metadata?: Record<string, unknown>;
  }) {
    return request<{ workspace: CodingWorkspaceRecord }>(defaultspackContractRoute("api/coding/workspaces"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  updateCodingWorkspace(workspaceId: string, updates: Partial<CodingWorkspaceRecord> & { workspace_root?: string }) {
    return request<{ workspace: CodingWorkspaceRecord }>(defaultspackContractRoute("api/coding/workspaces/update"), {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId, ...updates }),
    });
  },

  selectCodingWorkspace(workspaceId: string) {
    return request<{ workspace: CodingWorkspaceRecord; selected_workspace_id: string }>(defaultspackContractRoute("api/coding/workspaces/select"), {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId }),
    });
  },

  trustCodingWorkspace(workspaceId: string) {
    return request<{ workspace: CodingWorkspaceRecord }>(defaultspackContractRoute("api/coding/workspaces/trust"), {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId }),
    });
  },

  selectDirectory(prompt?: string) {
    return request<DirectorySelectionResponse>(defaultspackContractRoute("api/ui/select-directory"), {
      method: "POST",
      body: JSON.stringify({ prompt }),
    });
  },

  prepareChatGroupStorage(rootPath: string) {
    return request<ChatGroupStorageResponse>(defaultspackContractRoute("api/chat/group-storage"), {
      method: "POST",
      body: JSON.stringify({ root_path: rootPath }),
    });
  },

  getGitStatus(options?: { workspace_id?: string | null }) {
    return request<CodingGitStatus & { workspace_id?: string | null; workspace_root?: string | null }>(
      withQuery(defaultspackContractRoute("api/coding/git/status"), { workspace_id: options?.workspace_id }),
      { cache: "no-store" },
    );
  },

  getGitDiff(options?: { workspace_id?: string | null; ref?: string | null }) {
    return request<CodingDiffResponse>(
      withQuery(defaultspackContractRoute("api/coding/git/diff"), { workspace_id: options?.workspace_id, ref: options?.ref }),
      { cache: "no-store" },
    );
  },

  runTerminalCommand(command: string, options?: {
    workspace_id?: string | null;
    cwd?: string | null;
    timeout?: number;
    approval_token?: string;
  }) {
    return request<CodingTerminalResponse>(defaultspackContractRoute("api/coding/terminal/exec"), {
      method: "POST",
      body: JSON.stringify({ command, ...(options ?? {}) }),
    });
  },

  listCodingApprovals(options?: { status?: string; include_expired?: boolean; limit?: number }) {
    return request<{ requests: CodingApprovalRequest[]; pending: CodingApprovalRequest[]; count: number }>(
      withQuery(defaultspackContractRoute("api/coding/approvals"), {
        status: options?.status,
        include_expired: options?.include_expired,
        limit: options?.limit,
      }),
      { cache: "no-store" },
    );
  },

  async approveCodingApproval(requestId: string) {
    const uiOperator = await nativeCodingApprovalOperator(requestId, "approve");
    return request<CodingApprovalDecision>(defaultspackContractRoute("api/coding/approvals/approve"), {
      method: "POST",
      body: JSON.stringify({ approval_request_id: requestId, ui_operator: uiOperator }),
    });
  },

  async denyCodingApproval(requestId: string, reason?: string) {
    const uiOperator = await nativeCodingApprovalOperator(requestId, "deny");
    return request<Record<string, unknown>>(defaultspackContractRoute("api/coding/approvals/deny"), {
      method: "POST",
      body: JSON.stringify({ approval_request_id: requestId, reason, ui_operator: uiOperator }),
    });
  },

  listAuthorityRequests(options?: { status?: "all" | "pending" | "approved" | "denied" | "expired" | string }) {
    return request<AuthorityRequestsResponse>(
      withQuery(defaultspackContractRoute("api/authority/requests"), { status: options?.status ?? "all" }),
      { cache: "no-store" },
    );
  },

  getAuthorityRequest(requestId: string) {
    return request<AuthorityRequest>(
      defaultspackContractRoute(`api/authority/requests/${encodeURIComponent(requestId)}`),
      { cache: "no-store" },
    );
  },

  async createBrowserAuthorityExchange(_binding: AuthorityBrowserExchangeBinding) {
    throw new Error("AUTHORITY_BROWSER_TEST_DISABLED");
  },

  async browserAuthorityUiOperator(
    _binding: AuthorityBrowserExchangeBinding,
    _exchangeCode: string,
  ) {
    throw new Error("AUTHORITY_BROWSER_TEST_DISABLED");
  },

  async revokeBrowserAuthorityExchange(
    _binding: AuthorityBrowserExchangeBinding,
    _exchangeId: string,
  ) {
    throw new Error("AUTHORITY_BROWSER_TEST_DISABLED");
  },

  approveAuthorityApproval(
    requestId: string,
    options?: {
      scope?: AuthorityApprovalScope;
      config?: Record<string, unknown>;
      expires_in_seconds?: number;
      related_permissions?: string[];
      ui_operator?: AuthorityUiOperator;
    },
  ) {
    return request<AuthorityApprovalDecision>(defaultspackContractRoute(`api/authority/requests/${encodeURIComponent(requestId)}/approve`), {
      method: "POST",
      body: JSON.stringify({
        scope: options?.scope ?? "once",
        config: options?.config,
        expires_in_seconds: options?.expires_in_seconds,
        related_permissions: options?.related_permissions,
        ui_operator: options?.ui_operator,
      }),
    });
  },

  denyAuthorityApproval(
    requestId: string,
    reasonOrOptions?: string | { reason?: string; persist?: boolean; ui_operator?: AuthorityUiOperator },
    persist?: boolean,
  ) {
    const options = typeof reasonOrOptions === "object" && reasonOrOptions !== null
      ? reasonOrOptions
      : { reason: reasonOrOptions, persist };
    return request<Record<string, unknown>>(defaultspackContractRoute(`api/authority/requests/${encodeURIComponent(requestId)}/deny`), {
      method: "POST",
      body: JSON.stringify({
        reason: options.reason,
        persist: Boolean(options.persist),
        ui_operator: options.ui_operator,
      }),
    });
  },

  listInteractiveApprovals() {
    return request<InteractiveApprovalRequestsResponse>(
      defaultspackContractRoute("api/interactive-approval/v1/list"),
      { cache: "no-store" },
    );
  },

  getInteractiveApproval(requestId: string) {
    return request<InteractiveApprovalRequest>(
      defaultspackContractRoute("api/interactive-approval/v1/get"),
      {
        method: "POST",
        body: JSON.stringify({ request_id: requestId }),
        cache: "no-store",
      },
    );
  },

  approveInteractiveApproval(
    requestId: string,
    options: {
      confirmation_text: string;
      ui_operator: AuthorityUiOperator;
    },
  ) {
    return request<InteractiveApprovalRequest>(
      defaultspackContractRoute("api/interactive-approval/v1/approve"),
      {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId,
          confirmation_text: options.confirmation_text,
          ui_operator: options.ui_operator,
        }),
      },
    );
  },

  denyInteractiveApproval(
    requestId: string,
    options: { ui_operator: AuthorityUiOperator },
  ) {
    return request<InteractiveApprovalRequest>(
      defaultspackContractRoute("api/interactive-approval/v1/deny"),
      {
        method: "POST",
        body: JSON.stringify({
          request_id: requestId,
          ui_operator: options.ui_operator,
        }),
      },
    );
  },

  listCodingCheckpoints(options?: { workspace_id?: string | null; limit?: number }) {
    return request<{ checkpoints: CodingCheckpoint[]; workspace_id?: string | null; workspace_root?: string | null }>(
      withQuery(defaultspackContractRoute("api/coding/checkpoints"), { workspace_id: options?.workspace_id, limit: options?.limit }),
      { cache: "no-store" },
    );
  },

  createCodingCheckpoint(payload?: {
    workspace_id?: string | null;
    paths?: string[];
    operation?: string;
    metadata?: Record<string, unknown>;
  }) {
    return request<{ checkpoint: CodingCheckpoint; workspace_id?: string | null; workspace_root?: string | null }>(
      defaultspackContractRoute("api/coding/checkpoints"),
      {
        method: "POST",
        body: JSON.stringify(payload ?? {}),
      },
    );
  },

  listRumiLogs(options?: { workspace_id?: string | null; limit?: number; kind?: string | string[] | null }) {
    return request<RumiLogResponse>(
      withQuery(defaultspackContractRoute("api/coding/rumi-log"), {
        workspace_id: options?.workspace_id,
        limit: options?.limit,
        kind: options?.kind,
      }),
      { cache: "no-store" },
    );
  },

  appendRumiLog(payload: {
    workspace_id?: string | null;
    kind?: string;
    actor_id?: string;
    agent_role?: string;
    session_id?: string;
    status?: string;
    message?: string;
    branch?: string;
    commit_hash?: string;
    remote?: string;
    paths?: string[];
    mentions?: string[];
    task_id?: string;
    task_title?: string;
    task_status?: string;
    metadata?: Record<string, unknown>;
  }) {
    return request<RumiLogResponse>(defaultspackContractRoute("api/coding/rumi-log"), {
      method: "POST",
      body: JSON.stringify({ action: "append", ...payload }),
    });
  },

  seedRumiLogPlan(payload?: { workspace_id?: string | null }) {
    return request<RumiLogResponse>(defaultspackContractRoute("api/coding/rumi-log"), {
      method: "POST",
      body: JSON.stringify({ action: "seed_local_plan", ...(payload ?? {}) }),
    });
  },

  restoreCodingSnapshot(snapshotId: string, options?: {
    workspace_id?: string | null;
    paths?: string[];
    approval_token?: string;
  }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/coding/files/restore"), {
      method: "POST",
      body: JSON.stringify({ snapshot_id: snapshotId, ...(options ?? {}) }),
    });
  },

  listBrowserArtifacts(options?: { session_id?: string; limit?: number }) {
    return request<{ artifacts: BrowserArtifact[]; count: number }>(
      withQuery(defaultspackContractRoute("api/browser/artifacts"), { session_id: options?.session_id, limit: options?.limit }),
      { cache: "no-store" },
    );
  },

  listMcpServers() {
    return request<{ servers: McpServerRecord[]; count: number }>(defaultspackContractRoute("api/tools/mcp"), { cache: "no-store" });
  },

  registerMcpServer(server: Partial<McpServerRecord> & { server_id?: string; name?: string; config?: Record<string, unknown> }) {
    return request<{ server: McpServerRecord }>(defaultspackContractRoute("api/tools/mcp"), {
      method: "POST",
      body: JSON.stringify({ server }),
    });
  },

  connectMcpServer(payload: {
    server_id?: string;
    server_name?: string;
    config?: Record<string, unknown>;
    approval_token?: string;
    workspace_id?: string | null;
  }) {
    return request<McpConnectResponse>(defaultspackContractRoute("api/tools/mcp/connect"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  manageMcpServer(payload: {
    action: "disconnect" | "reconnect" | "remove";
    server_id: string;
    confirm?: boolean;
    approval_token?: string;
  }) {
    return request<Record<string, unknown>>(defaultspackContractRoute("api/tools/mcp"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  createCodingAgentSession(payload: {
    task: string;
    workspace_id?: string | null;
    agents?: Array<Record<string, unknown>>;
    metadata?: Record<string, unknown>;
  }) {
    return request<{ session: CodingAgentSession; merge_report?: Record<string, unknown> }>(
      defaultspackContractRoute("api/coding/agent/sessions"),
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  getCodingAgentSessionStatus(sessionId: string) {
    return request<CodingAgentSession>(
      withQuery(defaultspackContractRoute("api/coding/agent/sessions/status"), { session_id: sessionId }),
      { cache: "no-store" },
    );
  },

  getCodingAgentMergeReport(sessionId: string) {
    return request<{ session_id: string; merge_report: Record<string, unknown> }>(
      withQuery(defaultspackContractRoute("api/coding/agent/sessions/merge-report"), { session_id: sessionId }),
      { cache: "no-store" },
    );
  },

  revokeMobileDevice(deviceId: string) {
    return request<{ ok: boolean; device_id: string }>(
      `/api/mobile/v1/devices/${encodeURIComponent(deviceId)}`,
      { method: "DELETE" },
    );
  },

};
