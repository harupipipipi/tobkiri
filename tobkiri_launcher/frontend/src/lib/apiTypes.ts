/**
 * v4 frontend contract and Launcher command result types.
 *
 * HTTP calls unwrap the common envelope before returning these shapes. The
 * browser-facing routes are defined by the v4 frontend contract map; the
 * presentation and desktop-control shapes are returned by Launcher-owned
 * Tauri commands.
 */

export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  error: string | null;
}

export interface PackControlBinding {
  profile_id: string;
  workspace_id: string;
  profile_revision: string;
  plan_digest: string;
  catalog_revision: string;
}

export interface ApiPackCapability {
  name: string;
  description: string;
}

export interface ApiPackOperation {
  operation_id: string;
  contract_id: string;
  provider_id: string;
  function_id?: string;
  capabilities?: string[];
  required_capabilities?: string[];
  input_schema?: Record<string, unknown>;
  invokable?: boolean;
}

export interface ApiPack extends PackControlBinding {
  pack_id: string;
  name: string;
  version: string;
  description: string;
  is_core: boolean;
  required?: boolean;
  installed: boolean;
  enabled: boolean;
  artifact_digest: string;
  approval_status?: string;
  approval_reason?: string | null;
  approved?: boolean;
  hash_valid?: boolean | null;
  critical_changed?: boolean | null;
  approval_issues?: string[];
  capabilities?: ApiPackCapability[];
  operations?: ApiPackOperation[];
  declared_operations?: ApiPackOperation[];
  invokable_operations?: ApiPackOperation[];
  flows?: string[];
  dependencies?: string[];
}

export interface ApiFrontendContribution {
  contribution_id: string;
  owner_pack_id: string;
  label: string;
  action_contract?: string | null;
  operation_id?: string | null;
  provider_id?: string | null;
  function_id?: string | null;
  kind?: string;
  mode?: string;
  route?: string;
  owner_pack_hash?: string;
  build_identity?: string;
  resolved_profile_revision?: string;
  resolved_plan_hash?: string;
  descriptor_hash?: string;
  view?: {type?: string} | null;
}

export interface ApiFrontendDiagnostic {
  code: string;
  severity?: string;
  message: string;
  owner_pack_id?: string | null;
  contribution_id?: string | null;
  pack_id?: string | null;
  operation_id?: string | null;
}

export interface ApiDynamicFrontendCatalog {
  version: string;
  profile_id: string;
  profile_revision: string;
  plan_hash: string;
  contributions: ApiFrontendContribution[];
  diagnostics: ApiFrontendDiagnostic[];
  quarantined_pack_ids: string[];
  catalog_hash: string;
}

export interface ApiUiCatalogData {
  dynamic_host?: ApiDynamicFrontendCatalog | null;
  packs?: ApiPack[];
  count?: number;
}

export interface FrontendCapabilityInvocation {
  profileId: string;
  planHash: string;
  catalogHash: string;
  contributionId: string;
  ownerPackId: string;
  contractId: string;
  payload: Record<string, unknown>;
}

export interface ApiPackVMProvisioningPlan {
  backend_id: string;
  instance: string;
  launcher_reason: string | null;
  runtime_path_status: 'ready' | 'unsafe';
  architecture: string;
  image_source: string;
  image_digest: string;
  image_size_bytes: number;
  image_download_required: boolean;
  config_digest: string;
  guest_runner_digest: string;
  host_build_digest: string;
  ceremony_nonce: string;
  plan_digest: string;
  confirmation: string;
}

export interface ApiPackVMConsent {
  consent_id: string;
  plan_digest: string;
  image_source: string;
  image_digest: string;
  image_size_bytes: number;
  image_download_approved: boolean;
}

export type ApiPackVMOperationState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export interface ApiPackVMOperation {
  operation_id: string;
  operation_kind: 'provision' | 'cleanup';
  consent_digest?: string;
  state: ApiPackVMOperationState;
  plan_digest: string;
  updated_unix: number;
  doctor?: ApiPackVMDoctor;
  error?: string;
  error_type?: string;
  diagnostic?: {
    code: string;
    stage: string;
    kind: 'timeout' | 'exit';
    exit_code: number | null;
    stderr: string | null;
  };
  result?: ApiPackVMCleanupResult;
}

export interface ApiPackVMDoctor {
  ready: boolean;
  backend_id: string;
  platform: string;
  instance: string;
  reason: string | null;
  attestation_digest: string | null;
}

export interface ApiPackVMCleanupResult {
  ready: false;
  instance: string;
  cleanup_confirmation: string;
  missing: boolean;
}

export interface ApiDashboard {
  packs: {total: number; enabled: number; disabled: number};
  flows: {total: number};
  kernel: {status: string; uptime: number | null};
  profile: {username: string; language: string; icon: string | null} | null;
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
  event_schema: Array<{type: string; description: string}>;
  storage_targets: Record<string, string>;
  action_buttons: string[];
  security_guardrails: string[];
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

export type DebugApprovalState =
  | 'disabled'
  | 'pending'
  | 'armed'
  | 'active'
  | 'expired'
  | 'revoked';

export type DebugApprovalDuration = '1h' | '1d' | '1w' | '1mo' | 'permanent';

export interface DebugApprovalStatus {
  state: DebugApprovalState;
  reason?: string | null;
  armed_remaining_seconds?: number | null;
  session_id?: string | null;
  run_id?: string | null;
  workspace?: string | null;
  workspace_digest?: string | null;
  pack_id?: string | null;
  profile_id?: string | null;
  guardian_owned?: boolean;
  lease_epoch?: number | null;
  expires_at?: number | null;
  duration?: DebugApprovalDuration | null;
  instance_nonce: string;
}

export interface PacksResponseData extends PackControlBinding {
  packs: ApiPack[];
  count: number;
}

export interface PackInstallResponseData extends PackControlBinding {
  pack_id: string;
  installed: boolean;
}

export interface PackToggleResponseData extends PackControlBinding {
  pack_id: string;
  enabled: boolean;
}

export interface PackApprovalResponseData extends PackControlBinding {
  pack_id: string;
  approved: boolean;
  approval_status: string;
  enabled?: boolean;
}

export interface KernelRestartResponseData {
  restarting: boolean;
  message: string;
}

export type PresentationFamily = 'graphical' | 'terminal' | 'headless';
export type PresentationKind =
  | 'declarative'
  | 'isolated_web'
  | 'packaged_process'
  | 'terminal_stdio'
  | 'remote_ui';

export type PresentationApprovalState =
  | 'verified'
  | 'pending'
  | 'blocked'
  | 'not_required';

export interface ApiPresentationApproval {
  state: PresentationApprovalState;
  provider_trust: 'verified' | 'pending' | 'blocked' | 'not_required';
  grant_state: 'not_minted' | 'available' | 'missing' | 'blocked';
  authority_mode: 'lease_only' | 'os_entitlement' | 'none';
  execution_domain: string;
  effect_scope: string[];
  blast_radius: string;
  reason?: string | null;
}

export type PresentationArtifactStatus =
  | 'verified'
  | 'missing'
  | 'unverified'
  | 'digest_mismatch'
  | 'development_only'
  | 'unsupported_platform';

export interface ApiPresentationArtifact {
  artifact_id: string;
  variant: string;
  platform: string;
  architecture: string;
  path: string | null;
  sha256: string | null;
  prebuilt: boolean;
  production: boolean;
  development_command: string | null;
  bundle_identifier: string | null;
  status: PresentationArtifactStatus;
  status_detail: string;
}

export interface ApiPresentationArtifactVariant {
  artifact_id: string;
  variant: string;
  platform: string;
  architecture: string;
  artifact_ref: string;
  entrypoint: string;
  artifact_kind: string;
  descriptor_digest: string;
  path: string | null;
  sha256: string | null;
  prebuilt: boolean;
  production: boolean;
  development_command?: string | null;
  bundle_identifier?: string | null;
}

export interface ApiPresentationContribution {
  contribution_id: string;
  owner_pack_id: string;
  contract_id: string;
  contract_revision_digest: string;
  family: PresentationFamily;
  label: string;
  artifact_ref: string;
  digest: string;
  presentation_kind: PresentationKind;
  technology: string;
  host_authority: string;
  materialization: 'selected_only' | string;
}

export interface ApiBasePackDescriptor {
  pack_id: string;
  display_name: string;
  version: string;
  artifact_digest: string;
  backend_provider_ids: string[];
  state_owners: string[];
  backend_identity_digest: string;
  required_capabilities: string[];
  allowed_families: PresentationFamily[];
  approval: ApiPresentationApproval;
}

export interface ApiShellProviderDescriptor {
  provider_id: string;
  display_name: string;
  contract_id: 'app.shell.v1' | string;
  contract_revision_digest: string;
  experience_role: 'shell';
  presentation_kind: PresentationKind;
  presentation_family: PresentationFamily;
  technology: string;
  capabilities: string[];
  consumes_contracts: string[];
  contributions: ApiPresentationContribution[];
  artifact_variants: ApiPresentationArtifactVariant[];
  artifact: ApiPresentationArtifact | null;
  approval: ApiPresentationApproval;
  protocol_revision_digest?: string | null;
}

export interface ApiPresentationContractRevision {
  contract_id: string;
  revision: string;
  digest: string;
  source_path: string;
}

export interface ApiPresentationSelection {
  base_pack_id: string;
  shell_provider_id: string;
}

export interface ApiPresentationCatalog {
  schema: 'io.tobkiri.launcher.presentation-catalog.v1' | string;
  generator: string;
  generator_version: string;
  default_profile_id: string;
  default_profile_source: string;
  default_profile_digest: string;
  default_selection: ApiPresentationSelection;
  contract_revisions: ApiPresentationContractRevision[];
  source_manifest_digests: Record<string, string>;
  base_packs: ApiBasePackDescriptor[];
  shell_providers: ApiShellProviderDescriptor[];
  generated_at: number;
}

export type PresentationMaterializationStatus =
  | 'not_selected'
  | 'materialized'
  | 'blocked';

export interface ApiPresentationMaterialization {
  status: PresentationMaterializationStatus;
  base_pack_id: string | null;
  shell_provider_id: string | null;
  selected_contributions: ApiPresentationContribution[];
  artifact: ApiPresentationArtifact | null;
  reason: string | null;
}

export interface ApiPresentationState {
  catalog: ApiPresentationCatalog;
  selection: ApiPresentationSelection | null;
  materialization: ApiPresentationMaterialization;
}

export interface PresentationLaunchResponse {
  status: 'launched';
  provider_id: string;
  artifact_id: string;
  message: string;
}

export type RuntimeStatus =
  | 'starting'
  | 'panel_ready'
  | 'runtime_ready'
  | 'profile_reconfirmation_required'
  | 'error';

export interface HealthResponseData {
  status: 'ok' | 'error';
  needs_setup: boolean;
  panel_ready: boolean;
  runtime_ready: boolean;
  runtime_status: RuntimeStatus;
  runtime_error: string | null;
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
