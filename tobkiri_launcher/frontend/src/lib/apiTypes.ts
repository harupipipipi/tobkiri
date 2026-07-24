/**
 * API Type definitions — aligned with backend APIResponse envelope.
 *
 * Backend always returns: { success: boolean, data: T | null, error: string | null }
 * The apiFetch wrapper unwraps this envelope and returns data directly.
 */

// ============================================================
// Generic API envelope
// ============================================================

export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  error: string | null;
}

// ============================================================
// Backend data types (as returned inside envelope's `data`)
// ============================================================

/** GET /api/panel/packs → data.packs[] */
export interface ApiPack {
  pack_id: string;
  name: string;
  version: string;
  description: string;
  is_core: boolean;
  enabled: boolean;
  approval_status?: string;
  approval_reason?: string | null;
  approved?: boolean;
  hash_valid?: boolean | null;
  critical_changed?: boolean | null;
  approval_issues?: string[];
}

/** GET /api/panel/flows → data.flows[] */
export interface ApiFlow {
  flow_id: string;
  name: string;
  pack_id: string;
  filename: string;
}

/** GET /api/panel/flows/{id} → data */
export interface ApiFlowDetail {
  flow_id: string;
  name: string;
  pack_id: string;
  filename: string;
  yaml_content: string;
}

/** GET /api/panel/dashboard → data */
export interface ApiDashboard {
  packs: { total: number; enabled: number; disabled: number };
  flows: { total: number };
  kernel: { status: string; uptime: number | null };
  profile: { username: string; language: string; icon: string | null } | null;
  supervisor?: ApiSupervisorDashboard | null;
}

export interface ApiSupervisorRouterLayer {
  id: string;
  label: string;
  kind: string;
  priority: number;
  status: string;
  capabilities: string[];
}

export interface ApiSupervisorRouter {
  policy: string;
  structured_first: boolean;
  computer_use_role: string;
  preferred_order: string[];
  fallback_order: string[];
  operation_layers: ApiSupervisorRouterLayer[];
  fallback_layers: ApiSupervisorRouterLayer[];
  computer_driver_order: Record<string, string[]>;
}

export interface ApiSupervisorSandboxProvider {
  id: string;
  label: string;
  tier: string;
  default: boolean;
  user_burden: string;
  install_required: boolean;
  providers: string[];
  capabilities: string[];
  artifacts: string[];
}

export interface ApiSupervisorCapabilityFlags {
  snapshot: boolean;
  live_screen: boolean;
  takeover: boolean;
  replay: boolean;
}

export interface ApiSupervisorSession {
  run_id: string;
  agent_id: string | null;
  task: string;
  status: string;
  updated_at: string | null;
  heartbeat_at: string | null;
  risk: string;
  screen: {
    available: boolean;
    provider?: string | null;
    url?: string | null;
    screenshot_url?: string | null;
  };
  replay: {
    available: boolean;
    url?: string | null;
  };
  artifacts: {
    screenshots: number;
    logs: number;
    diffs: number;
    traces: number;
  };
}

export interface ApiSupervisorEvent {
  run_id: string;
  event_type: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface ApiSupervisorDashboard {
  capabilities: ApiSupervisorCapabilityFlags;
  router: ApiSupervisorRouter;
  sandbox_providers: ApiSupervisorSandboxProvider[];
  runtime_templates: Array<Record<string, unknown>>;
  metrics: {
    available: boolean;
    active_runs: number;
    waiting_approvals: number;
    stale_runs: number;
    failed_runs: number;
    screen_sessions: number;
    replay_ready: number;
    artifact_streams: string[];
  };
  sessions: ApiSupervisorSession[];
  selected_session: ApiSupervisorSession | null;
  recent_events: ApiSupervisorEvent[];
  event_schema: Array<{ type: string; description: string }>;
  storage_targets: Record<string, string>;
  action_buttons: string[];
  security_guardrails: string[];
}

/** GET /api/panel/settings/profile → data.profile */
export interface ApiProfile {
  username: string;
  language: string;
  icon: string | null;
  occupation: string | null;
}

/** GET /api/panel/version → data */
export interface ApiVersion {
  app_version?: string;
  display_version?: string;
  kernel_version: string;
  python_version: string;
  platform: string;
  platform_release: string;
}

export interface DesktopPermissionStatus {
  id: string;
  label: string;
  status: 'granted' | 'missing' | 'not_checked' | 'unsupported' | string;
  granted: boolean | null;
  detail: string;
  settings_hint: string;
}

export interface HostBrokerStatus {
  enabled: boolean;
  available?: boolean;
  status: string;
  url?: string | null;
  connection_path?: string | null;
  recovery?: string | null;
}

export interface DesktopSystemInfo {
  app_name: string;
  display_version: string;
  launcher_tauri?: boolean;
  viewer_tauri?: boolean;
  launcher_version?: string;
  viewer_version: string;
  build_channel: string;
  platform: string;
  platform_release: string;
  permission_subject?: string;
  host_broker?: HostBrokerStatus;
  permissions: DesktopPermissionStatus[];
}

export type ApiUpdateTarget = 'tobkiri' | 'defaultspack';

export interface ApiUpdateInfo {
  target: ApiUpdateTarget;
  current_version: string;
  latest_version: string;
  update_available: boolean;
  release_url: string;
  repo: string;
}

export interface ApiUpdateSettings {
  auto_update: Record<ApiUpdateTarget, boolean>;
  check_interval_hours: number;
  last_checked_at: string | null;
  last_results: Array<Record<string, unknown>>;
  updated_at: string | null;
}

// ============================================================
// Endpoint-specific response data shapes (inside envelope)
// ============================================================

export interface PacksResponseData {
  packs: ApiPack[];
  count: number;
}

export interface PackToggleResponseData {
  pack_id: string;
  enabled: boolean;
}

export interface PackApprovalResponseData {
  pack_id: string;
  approved: boolean;
  approval_status: string;
}

export interface UpdatesResponseData {
  updates: ApiUpdateInfo[];
}

export interface UpdateApplyResponseData {
  target: ApiUpdateTarget;
  current_version: string;
  latest_version: string;
  release_url: string;
  backup_dir: string;
  applied_files: string[];
  skipped_files: string[];
  applied_count: number;
  skipped_count: number;
  restart_required?: boolean;
  routes_reload_recommended?: boolean;
}

export interface ApiStartupNodePort {
  id?: string;
  port_id?: string;
  label?: string;
  display_name?: Record<string, string>;
  direction: 'input' | 'output';
  standards?: string[];
  contracts?: string[];
  multi?: boolean;
  multiple?: boolean;
}

export interface ApiStartupNodeDefinition {
  node_id: string;
  ref?: string;
  title?: string;
  subtitle?: string;
  kind: string;
  component_id?: string;
  component_type?: string;
  metadata?: Record<string, unknown>;
  character?: string;
  display_name?: Record<string, string>;
  ports: ApiStartupNodePort[];
}

export interface ApiStartupPack {
  pack_id: string;
  name: string;
  description: string;
  pack_identity: string;
  available: boolean;
  enabled: boolean;
  approval_issues: string[];
  graphs: Array<{
    graph_id: string;
    display_name?: Record<string, string>;
    description?: Record<string, string>;
    node_count?: number;
    edge_count?: number;
  }>;
  nodes: ApiStartupNodeDefinition[];
}

export interface ApiStartupGraphPort {
  port_key: string;
  node_id: string;
  port_id: string;
  target_node_ref?: string;
  target_port?: ApiStartupNodePort;
  source_node_id: string;
  source_node_ref?: string;
  source_port_id?: string;
  source_port?: ApiStartupNodePort;
  source_ref: string;
}

export interface ApiStartupCatalog {
  version: number;
  packs: ApiStartupPack[];
}

export interface ApiProfileWorkspacePaths {
  profile_id: string;
  root: string;
  profile_file: string;
  user_data_dir: string;
  database_dir?: string;
  database_path: string;
  startup_dir: string;
  flows_dir: string;
  prompts_dir: string;
  ecosystem_dir?: string;
  permissions_dir: string;
  audit_dir?: string;
  snapshots_dir?: string;
}

export interface ApiStartupProfile {
  version: number;
  profile_id: string;
  name: string;
  base_pack: string;
  graph_id: string;
  graph_ports: ApiStartupGraphPort[];
  packs: string[];
  node_overrides: Record<string, string>;
  icon?: string | null;
  created_at: number;
  updated_at: number;
  capability_profile_id?: string | null;
  default_flow?: string | null;
  default_graph?: string | null;
  system_prompt_id?: string | null;
  default_prompt_id?: string | null;
  launch_capability_graph?: boolean;
  surfaces?: Record<string, unknown>;
  enabled_nodes?: string[];
  disabled_nodes?: string[];
  node_settings?: Record<string, Record<string, unknown>>;
  policy?: Record<string, unknown>;
  permissions?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  profile_workspace?: ApiProfileWorkspacePaths;
}

export interface ApiProfileWorkspaceFile {
  name: string;
  path: string;
  size: number;
}

export interface ApiProfileWorkspaceDetail {
  profile: ApiStartupProfile;
  profile_workspace: ApiProfileWorkspacePaths;
  startup_config: Record<string, unknown>;
  flows: ApiProfileWorkspaceFile[];
  prompts: ApiProfileWorkspaceFile[];
  resource_snapshot_manifest: Record<string, unknown>;
  permissions: Record<string, { path: string; exists: boolean }>;
  flow_yaml: {
    path: string | null;
    yaml_content: string;
  };
}

export interface FlowsResponseData {
  flows: ApiFlow[];
  count: number;
}

export interface StartupProfilesResponseData {
  profiles: ApiStartupProfile[];
  active_profile_id: string | null;
  last_launched_profile_id: string | null;
  catalog: ApiStartupCatalog;
}

export interface StartupProfileMutationResponseData {
  profile: ApiStartupProfile;
  profile_workspace?: ApiProfileWorkspacePaths;
  created?: boolean;
  updated?: boolean;
  pack_added?: string;
  pack_removed?: string;
  override_set?: { port_key: string; node_id: string };
  override_cleared?: string;
  duplicated?: boolean;
  activated?: boolean;
  launched?: boolean;
  active_profile_id?: string;
  restart_requested?: boolean;
  handoff?: {
    kind: string;
    reason: string;
    restart_requested: boolean;
    exit_code?: number;
    profile_id?: string;
  };
}

export interface StartupProfileCompilePreviewResponseData {
  ok: boolean;
  profile_id: string;
  profile: ApiStartupProfile;
  capability_graph: {
    ok: boolean;
    skipped?: boolean;
    reason?: string | null;
    graph_id?: string | null;
    capability_profile_id?: string | null;
    runtime_profile_key?: string | null;
    runtime_profile?: Record<string, unknown> | null;
    surface_launch_target?: ApiSurfaceLaunchTarget | null;
    diagnostics: ApiCapabilityDiagnostic[];
  };
  surface_launch_target?: ApiSurfaceLaunchTarget | null;
  diagnostics: ApiCapabilityDiagnostic[];
}

export interface ApiProfileGraphNode {
  id: string;
  kind: string;
  label: string;
  ref: string;
  metadata: Record<string, unknown>;
}

export interface ApiProfileGraphEdge {
  id: string;
  from_id: string;
  to_id: string;
  kind: string;
  active: boolean;
  from_port?: string;
  to_port?: string;
  gate_id?: string | null;
  metadata: Record<string, unknown>;
}

export interface ApiProfileGraphSelected {
  tools: string[];
  webhooks: string[];
  api_routes: string[];
  prompts: string[];
  frontend: string[];
  flows: string[];
  nodes: string[];
  [key: string]: string[] | Record<string, unknown>;
}

export interface ApiProfileGraphDocument {
  version: number;
  profile_id: string;
  nodes: ApiProfileGraphNode[];
  edges: ApiProfileGraphEdge[];
  selected: ApiProfileGraphSelected;
}

export interface ApiProfileGraphAvailableItem {
  id: string;
  label: string;
  kind: string;
  [key: string]: unknown;
}

export interface StartupProfileGraphResponseData {
  profile_id: string;
  profile: ApiStartupProfile;
  graph: ApiProfileGraphDocument;
  available: {
    tools: ApiProfileGraphAvailableItem[];
    webhooks: ApiProfileGraphAvailableItem[];
    api_routes: ApiProfileGraphAvailableItem[];
    prompts: ApiProfileGraphAvailableItem[];
    frontend: ApiProfileGraphAvailableItem[];
    flows: ApiProfileGraphAvailableItem[];
    capability_nodes: ApiProfileGraphAvailableItem[];
    input_profiles?: ApiProfileGraphAvailableItem[];
  };
  summary: {
    selected_tool_count: number;
    available_tool_count: number;
    selected_webhook_count: number;
    available_webhook_count: number;
    api_route_count: number;
    selected_frontend_count: number;
    selected_prompt_count: number;
  };
  diagnostics: ApiCapabilityDiagnostic[];
}

export interface StartupProfileGraphCompilePreviewResponseData extends StartupProfileGraphResponseData {
  compile_preview: StartupProfileCompilePreviewResponseData;
  profile_graph_runtime_preview: {
    selected: ApiProfileGraphSelected;
    policy: Record<string, unknown>;
    tool_filter_result: Array<Record<string, unknown>>;
    prompt_resolution: Record<string, unknown>;
    webhook_status: ApiProfileGraphAvailableItem[];
    api_route_policy: Record<string, unknown>;
    frontend_selection: ApiProfileGraphAvailableItem[];
    diagnostics: ApiCapabilityDiagnostic[];
  };
}

export interface ApiAiInputNode {
  id: string;
  kind: string;
  label: string;
  ref: string;
  input_ports: string[];
  output_ports: string[];
  metadata: Record<string, unknown>;
}

export interface ApiAiInputEdge {
  id: string;
  from_id: string;
  from_port: string;
  to_id: string;
  to_port: string;
  kind: string;
  active: boolean;
  gate_id?: string | null;
  metadata: Record<string, unknown>;
}

export interface ApiPromptSegment {
  id: string;
  text?: string;
  preview?: string;
  source: string;
  source_type: string;
  tokens: number;
  priority: number;
  enabled: boolean;
  reason: string;
  metadata: Record<string, unknown>;
}

export interface ApiToolSchemaSegment {
  id: string;
  tool_id: string;
  name: string;
  schema?: Record<string, unknown>;
  tokens: number;
  enabled: boolean;
  reason: string;
  metadata: Record<string, unknown>;
}

export interface ApiAiInputConfig {
  version: number;
  disabled_edges: string[];
  gates: Record<string, Record<string, unknown>>;
  inserted_edges: ApiAiInputEdge[];
  budgets: Record<string, Record<string, unknown>>;
}

export interface StartupProfileAiInputResponseData {
  profile_id: string;
  profile: ApiStartupProfile;
  ai_input: ApiAiInputConfig;
  model_input: {
    node_id: string;
    provider?: string | null;
    model?: string | null;
  };
  graph: {
    nodes: ApiAiInputNode[];
    edges: ApiAiInputEdge[];
  };
  effective_input: {
    profile_id: string;
    model_node_id: string;
    system_segments: ApiPromptSegment[];
    developer_segments: ApiPromptSegment[];
    context_segments: ApiPromptSegment[];
    tool_schemas: ApiToolSchemaSegment[];
    policy: Record<string, unknown>;
    disabled_segments: Array<Record<string, unknown>>;
  };
  token_estimate: {
    total: number;
    by_port: Record<string, number>;
    by_node: Record<string, number>;
  };
  gate_decisions: Array<Record<string, unknown>>;
  diagnostics: ApiCapabilityDiagnostic[];
  diff?: {
    before_tokens: number;
    after_tokens: number;
    removed_segments: string[];
    added_segments: string[];
  };
}

export interface ApiAiInputTraceSummary {
  trace_id?: string | null;
  created_at?: number | null;
  conversation_id?: string | null;
  run_id?: string | null;
  profile_id?: string | null;
  blocked_count?: number;
  token_estimate?: {
    total?: number;
    by_port?: Record<string, number>;
    by_node?: Record<string, number>;
  };
  provider_payload_summary?: Record<string, unknown>;
}

export interface StartupProfileAiInputTracesResponseData {
  profile_id: string;
  traces: ApiAiInputTraceSummary[];
}

export interface ApiMapResponseData {
  nodes: ApiProfileGraphNode[];
  edges: ApiProfileGraphEdge[];
  summary: {
    node_count: number;
    edge_count: number;
    route_count: number;
    tool_count: number;
    webhook_count: number;
    flow_count?: number;
    function_count?: number;
    operation_count?: number;
    implementation_count?: number;
    selected_tool_count?: number;
    selected_route_count?: number;
  };
  runtime_paths?: ApiMapRuntimePath[];
  profile_runtime?: Record<string, unknown>;
  diagnostics: ApiCapabilityDiagnostic[];
}

export interface ApiMapRuntimePath {
  id: string;
  label: string;
  entrypoint: {
    node_id: string;
    method?: string;
    path?: string;
    source?: string;
    source_type?: string;
  };
  primary?: ApiMapRuntimeTarget | null;
  fallback?: ApiMapRuntimeTarget | null;
  steps?: ApiMapRuntimeStep[];
}

export interface ApiMapRuntimeTarget {
  kind?: string;
  id?: string;
  node_id?: string;
  block_node_id?: string;
  block_module?: string;
  resolved?: boolean;
}

export interface ApiMapRuntimeStep {
  kind?: string;
  id: string;
  node_id: string;
  step_type?: string;
  order?: number;
  target?: ApiMapRuntimeTarget | null;
}

export interface StartupProfileDeleteResponseData {
  deleted: boolean;
  deleted_profile_id: string;
  active_profile_id: string | null;
  profile_workspace_orphaned?: boolean;
}

export interface FlowCreateResponseData {
  flow_id: string;
  filename: string;
  created: boolean;
}

export interface FlowUpdateResponseData {
  flow_id: string;
  filename: string;
  updated: boolean;
}

export interface FlowDeleteResponseData {
  flow_id: string;
  deleted: boolean;
}

export interface ProfileResponseData {
  profile: ApiProfile;
  updated?: boolean;
}

export interface KernelRestartResponseData {
  restarting: boolean;
  message: string;
}

export interface OAuthStartResponseData {
  authorize_url: string;
  state: string;
}

export interface SetupStatusResponseData {
  needs_setup: boolean;
  reason?: string;
  panel_ready?: boolean;
  runtime_ready?: boolean;
  runtime_status?: 'starting' | 'panel_ready' | 'runtime_ready' | 'error';
  runtime_error?: string | null;
}

export interface HealthResponseData {
  status: 'ok' | 'error';
  needs_setup?: boolean;
  panel_ready?: boolean;
  runtime_ready?: boolean;
  runtime_status?: 'starting' | 'panel_ready' | 'runtime_ready' | 'error';
  runtime_error?: string | null;
}

export interface WindowRuntimeSnapshot {
  label: string;
  visible: boolean;
  minimized: boolean;
  focused: boolean;
}

export interface BackgroundControlStatus {
  enabled: boolean;
  app_visible: boolean;
  foreground_window: string | null;
  kernel_running: boolean;
  shutdown_requested: boolean;
  windows: WindowRuntimeSnapshot[];
}

export interface ApiCapabilityPort {
  id: string;
  label?: string | null;
  direction: 'input' | 'output' | 'bidirectional';
  standards?: string[];
  aliases?: string[];
  multiple?: boolean;
  required?: boolean;
  display_name?: Record<string, string>;
  description?: Record<string, string>;
}

export interface ApiCapabilityNodeState {
  node_id: string;
  installed: boolean;
  approved: boolean;
  enabled: boolean;
  configured: boolean;
  status: string;
  missing: string[];
  credential_ref?: string | null;
  profile_id?: string;
}

export interface ApiCapabilityNode {
  node_id: string;
  label?: string | null;
  description_label?: string | null;
  kind: string;
  ports: ApiCapabilityPort[];
  display_name?: Record<string, string>;
  description?: Record<string, string> | string;
  bindings: Record<string, unknown>;
  metadata: Record<string, unknown>;
  requirements?: Record<string, unknown>;
  permissions?: Record<string, unknown>;
  state?: ApiCapabilityNodeState;
}

export interface ApiCapabilityProfile {
  profile_id: string;
  label: string;
  description_label: string;
  locale?: string | null;
  default_graph?: string | null;
  default_flow?: string | null;
  permissions: Record<string, unknown>;
  enabled_nodes: string[];
  disabled_nodes: string[];
  node_settings: Record<string, Record<string, unknown>>;
  policy: Record<string, unknown>;
}

export interface ApiCapabilityGraph {
  graph_id: string;
  label: string;
  display_name?: Record<string, string>;
  description?: Record<string, string>;
  description_label: string;
  nodes: Array<{id: string; ref: string; display_name?: Record<string, string>; metadata?: Record<string, unknown>}>;
  edges: Array<{id: string; from: string; to: string; kind: string; metadata?: Record<string, unknown>}>;
  metadata: Record<string, unknown>;
}

export interface ApiCapabilityDiagnostic {
  level: string;
  code: string;
  message: string;
  [key: string]: unknown;
}

export interface StartupProfileRelationship {
  launch_time_source_of_truth: string;
  capability_graph_profiles_role: string;
  bridge_policy: string;
  startup_profile_api: string;
}

export interface CapabilityProfilesResponseData {
  profiles: ApiCapabilityProfile[];
  count: number;
  startup_profile_relationship: StartupProfileRelationship;
}

export interface CapabilityNodesResponseData {
  nodes: ApiCapabilityNode[];
  count: number;
}

export interface CapabilityProfileNodesResponseData {
  profile: ApiCapabilityProfile;
  nodes: ApiCapabilityNode[];
  node_state: ApiCapabilityNodeState[];
  palette_nodes: ApiCapabilityNode[];
  count: number;
  palette_count: number;
}

export interface CapabilityGraphsResponseData {
  graphs: ApiCapabilityGraph[];
  count: number;
  diagnostics: ApiCapabilityDiagnostic[];
}

export interface CapabilityGraphResponseData {
  graph: ApiCapabilityGraph;
}

export interface ApiSurfaceLaunchTarget {
  kind: string;
  pack_id: string;
  principal_id?: string;
  surface?: string;
  node_instance_id?: string;
  node_id?: string;
  component_full_id?: string;
  env?: Record<string, string>;
  source?: string;
}

export interface CapabilityGraphCompileResponseData {
  ok: boolean;
  graph_id: string;
  profile_id: string;
  runtime_profile?: Record<string, unknown> | null;
  surface_launch_target?: ApiSurfaceLaunchTarget | null;
  diagnostics: ApiCapabilityDiagnostic[];
}

export interface CapabilityGraphSaveResponseData {
  graph: ApiCapabilityGraph;
  created: boolean;
  path: string;
  diagnostics: ApiCapabilityDiagnostic[];
}

export interface CapabilityProfileCloneResponseData {
  profile: ApiCapabilityProfile;
  created: boolean;
  path: string;
  diagnostics: ApiCapabilityDiagnostic[];
}
