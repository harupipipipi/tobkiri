export type FrontendContributionKind =
  | "route"
  | "renderer"
  | "shell_region"
  | "action"
  | "data_source"
  | "settings"
  | "command";

export type FrontendContributionMode =
  | "declarative"
  | "isolated"
  | "same_origin_builtin";

export type VerifiedFrontendContribution = {
  contribution_id: string;
  kind: FrontendContributionKind;
  mode: FrontendContributionMode;
  label: string;
  description?: string | null;
  priority: number;
  owner_pack_id: string;
  owner_pack_hash: string;
  build_identity: string;
  resolved_profile_id: string;
  resolved_profile_revision: string;
  resolved_activation_id: string;
  resolved_plan_hash: string;
  descriptor_hash: string;
  route?: string | null;
  region?: string | null;
  renderer?: string | null;
  action_contract?: string | null;
  data_source_contract?: string | null;
  schema?: Record<string, unknown> | null;
  view?: Record<string, unknown> | null;
  module?: {
    path: string;
    export: string;
    content_hash: string;
  } | null;
  isolated?: {
    path: string;
    rpc_contracts: string[];
  } | null;
  localization: Record<string, string>;
  accessibility: {
    name: string;
    keyboard: boolean;
    live?: "off" | "polite" | "assertive";
  };
};

export type FrontendCatalog = {
  version: "rumi.ui.contribution.v1";
  profile_id: string;
  profile_revision: string;
  activation_id: string;
  plan_hash: string;
  contributions: VerifiedFrontendContribution[];
  diagnostics: Array<{
    code: string;
    severity: string;
    message: string;
    owner_pack_id?: string | null;
    contribution_id?: string | null;
  }>;
  quarantined_pack_ids: string[];
  catalog_hash: string;
};

export type CapabilityInvocation = {
  contractId: string;
  payload: Record<string, unknown>;
  // Compatibility hints from a contribution implementation are never used as
  // Host identity. DynamicFrontendHost overwrites them from its catalog capture.
  contributionId?: string;
  ownerPackId?: string;
  planHash?: string;
  catalogHash?: string;
};

export type CapturedCapabilityInvocation = CapabilityInvocation & {
  profileId: string;
  profileRevision: string;
  activationId: string;
  planHash: string;
  catalogHash: string;
  contributionId: string;
  ownerPackId: string;
};

export type FrontendCapabilityClient = {
  invokeAction: (request: CapabilityInvocation) => Promise<unknown>;
  readDataSource: (request: CapabilityInvocation) => Promise<unknown>;
};

export type FrontendCapabilityInvoker = {
  invokeAction: (request: CapturedCapabilityInvocation) => Promise<unknown>;
  readDataSource: (request: CapturedCapabilityInvocation) => Promise<unknown>;
};
