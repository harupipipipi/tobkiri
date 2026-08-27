import {
  defaultspackApiFetch,
  defaultspackContractRoute,
  defaultspackContractUrl,
  explainDefaultspackApiError,
  type DefaultspackContractRoute,
} from "./api";

type ApiErrorPayload = {
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
};

type ApiEnvelope<T> = {
  status?: "ok" | "error" | string;
  data?: T;
  error?: ApiErrorPayload;
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
};

export class AdaptiveApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly status: number;

  constructor(message: string, options: { code?: string; details?: Record<string, unknown>; status?: number } = {}) {
    super(message);
    this.name = "AdaptiveApiError";
    this.code = options.code ?? "ADAPTIVE_API_ERROR";
    this.details = options.details ?? {};
    this.status = options.status ?? 0;
  }
}

export type AdaptiveTone = "neutral" | "good" | "warning" | "danger" | "info";

export type AdaptiveUseCase = {
  id: string;
  label: string;
  description: string;
  enabled: boolean;
};

export type AdaptiveRoleProfile = {
  title: string;
  scope: string;
  stakeholders: string[];
};

export type AdaptiveAutonomyLevel = "draft" | "confirm" | "supervised" | "autonomous";

export type AdaptiveAutonomyProfile = {
  level: AdaptiveAutonomyLevel;
  label: string;
  guardrails: string[];
};

export type AdaptivePermission = {
  id: string;
  label: string;
  risk: "low" | "medium" | "high" | string;
  mode: string;
  description: string;
};

export type AdaptivePackRecommendation = {
  id: string;
  label: string;
  reason: string;
  status: "recommended" | "enabled" | "needs_review" | string;
};

export type AdaptiveScenario = {
  id: string;
  label: string;
  prompt: string;
  expectedOutcome: string;
  requiredApprovals: string[];
};

export type AdaptiveSettingsDiff = {
  id: string;
  label: string;
  before: string;
  after: string;
  tone?: AdaptiveTone;
};

export type AdaptiveOnboardingState = {
  profileId?: string | null;
  completedStepId?: string | null;
  useCases: AdaptiveUseCase[];
  role: AdaptiveRoleProfile;
  autonomy: AdaptiveAutonomyProfile;
  responsibilities: {
    owned: string[];
    excluded: string[];
  };
  review: {
    cadence: string;
    reviewers: string[];
    gates: string[];
  };
  permissions: AdaptivePermission[];
  privacyMemory: {
    memoryMode: string;
    retention: string;
    sensitiveBoundaries: string[];
  };
  skillLearning: {
    enabled: boolean;
    sources: string[];
    reviewRequired: boolean;
  };
  packRecommendations: AdaptivePackRecommendation[];
  scenarioSimulation: AdaptiveScenario[];
  settingsDiff: AdaptiveSettingsDiff[];
};

export type AdaptiveOperatingProfile = {
  id: string;
  resourceId: string;
  revision: number;
  name: string;
  summary: string;
  role: AdaptiveRoleProfile;
  autonomy: AdaptiveAutonomyProfile;
  focusAreas: string[];
  boundaries: string[];
  approvalPolicy: AdaptivePermission[];
  privacyMemory: AdaptiveOnboardingState["privacyMemory"];
  skillLearning: AdaptiveOnboardingState["skillLearning"];
  packRecommendations: AdaptivePackRecommendation[];
  review: AdaptiveOnboardingState["review"];
  updatedAt?: string | null;
};

export type AdaptiveActivityStatus = "queued" | "running" | "needs_review" | "blocked" | "done" | string;

export type AdaptiveActivityItem = {
  id: string;
  title: string;
  kind: "task" | "approval" | "memory" | "automation" | "incident" | string;
  status: AdaptiveActivityStatus;
  summary: string;
  actor: string;
  startedAt: string;
  evidenceCount: number;
  requiresReview?: boolean;
  toolLabel?: string | null;
  internalToolId?: string | null;
};

export type AdaptiveReviewQueueItem = {
  id: string;
  title: string;
  reason: string;
  risk: "low" | "medium" | "high" | string;
  requestedBy: string;
  ageLabel: string;
};

export type AdaptiveActivityState = {
  items: AdaptiveActivityItem[];
  reviewQueue: AdaptiveReviewQueueItem[];
  counters: {
    running: number;
    needsReview: number;
    blocked: number;
    completedToday: number;
  };
};

export type AdaptiveAutomationStep = {
  id: string;
  label: string;
  capabilityLabel?: string | null;
  internalToolId?: string | null;
  requiresApproval?: boolean;
};

export type AdaptiveAutomation = {
  id: string;
  resourceId: string;
  revision: number;
  name: string;
  description: string;
  trigger: string;
  schedule: string;
  enabled: boolean;
  risk: "low" | "medium" | "high" | string;
  lastRun?: string | null;
  steps: AdaptiveAutomationStep[];
};

export type AdaptiveAutomationTemplate = {
  id: string;
  name: string;
  description: string;
};

export type AdaptiveAutomationState = {
  revision: number;
  automations: AdaptiveAutomation[];
  templates: AdaptiveAutomationTemplate[];
  simulation: {
    scenario: string;
    result: string;
    approvals: string[];
  };
};

export type AdaptiveEvidenceItem = {
  id: string;
  title: string;
  kind: "file" | "approval" | "runtime" | "memory" | "test" | string;
  sourceLabel: string;
  capturedAt: string;
  summary: string;
  confidence: number;
  redactions: string[];
  links?: Array<{ label: string; href: string }>;
  internalToolId?: string | null;
};

export type AdaptiveEvidenceBundle = {
  selectedId?: string | null;
  items: AdaptiveEvidenceItem[];
};

export type AdaptiveRepositoryPath = {
  path: string;
  role: string;
  status: "owned" | "read_only" | "external" | string;
};

export type AdaptiveRepositoryMapSection = {
  id: string;
  label: string;
  description: string;
  paths: AdaptiveRepositoryPath[];
};

export type AdaptiveRepositoryMap = {
  rootLabel: string;
  branch?: string | null;
  sections: AdaptiveRepositoryMapSection[];
  risks: string[];
};

export type AdaptiveContextBudgetSegment = {
  id: string;
  label: string;
  tokens: number;
  tone?: AdaptiveTone;
};

export type AdaptiveContextBudget = {
  used: number;
  limit: number;
  reserved: number;
  riskLevel: "low" | "medium" | "high" | string;
  lastTrim?: string | null;
  segments: AdaptiveContextBudgetSegment[];
  compressionPlan: string[];
};

export const adaptiveOnboardingActionIds = [
  "discuss",
  "propose",
  "read_local",
  "local_write",
  "terminal",
  "git_write",
  "git_commit",
  "git_push",
  "git_merge",
  "browser_control",
  "computer_control",
  "external_send",
  "secrets_access",
] as const;

export type AdaptiveOnboardingActionId = (typeof adaptiveOnboardingActionIds)[number];
export type AdaptiveOnboardingActionLevel = "deny" | "ask" | "allow";
export type AdaptiveOnboardingPreset = "discussion_only" | "balanced_local" | "max_local_autonomy";

export type AdaptiveOnboardingAnswers = {
  profile_id: string;
  preset_id: AdaptiveOnboardingPreset;
  use_cases: Record<string, boolean>;
  actions: Partial<Record<AdaptiveOnboardingActionId, AdaptiveOnboardingActionLevel>>;
  memory_mode: string;
  skill_learning_enabled: boolean;
  skill_learning_review_required: boolean;
  pack_recommendations: string[];
};

export type AdaptiveScenarioResult = {
  scenarioId: string;
  label: string;
  actions: string[];
  allowed: string[];
  approvalRequired: string[];
  blocked: string[];
};

export type AdaptiveOnboardingApiResult = {
  profileId: string;
  normalizedProfile?: Record<string, unknown>;
  operatingProfile?: Record<string, unknown>;
  plan?: Record<string, unknown>;
  diagnostics: Record<string, unknown>[];
  scenarioSimulation: AdaptiveScenarioResult[];
  settingsDiff: AdaptiveSettingsDiff[];
  localOnly: boolean;
  wouldWrite: string[];
  applied: boolean;
  historyId?: string | null;
  planId?: string | null;
  path?: string | null;
  raw: Record<string, unknown>;
};

function fallbackApiHeaders(method: string, headers?: HeadersInit): Headers {
  const nextHeaders = new Headers(headers);
  if (!nextHeaders.has("Content-Type")) {
    nextHeaders.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase()) && !nextHeaders.has("X-Rumi-CSRF")) {
    nextHeaders.set("X-Rumi-CSRF", `adaptive-${Date.now().toString(36)}`);
  }
  return nextHeaders;
}

function fallbackApiFetch(input: RequestInfo | URL | DefaultspackContractRoute, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const requestInput = typeof input === "object" && "kind" in input
    ? defaultspackContractUrl(input as DefaultspackContractRoute, method)
    : input;
  return fetch(requestInput, {
    ...init,
    method,
    headers: fallbackApiHeaders(method, init.headers),
  });
}

function formatFallbackApiError(status: number, error?: ApiErrorPayload, statusText?: string): string {
  const label = status ? `HTTP ${status}${statusText ? ` ${statusText}` : ""}` : "adaptive API error";
  const code = error?.code ? ` (${error.code})` : "";
  const message = error?.message ? `: ${error.message}` : "";
  return `${label}${code}${message}`;
}

function adaptiveFetch(input: RequestInfo | URL | DefaultspackContractRoute, init?: RequestInit): Promise<Response> {
  const fetcher = typeof defaultspackApiFetch === "function" ? defaultspackApiFetch : fallbackApiFetch;
  return fetcher(input, init);
}

function explainAdaptiveError(status: number, error?: ApiErrorPayload, statusText?: string): string {
  if (typeof explainDefaultspackApiError === "function") {
    return explainDefaultspackApiError(status, error, statusText);
  }
  return formatFallbackApiError(status, error, statusText);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function recordValue(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function isEnvelope<T>(value: unknown): value is ApiEnvelope<T> {
  return isRecord(value) && ("status" in value || "data" in value || "error" in value);
}

function errorPayloadFrom(value: unknown): ApiErrorPayload | undefined {
  if (!isRecord(value)) return undefined;
  const nested = recordValue(value.error);
  const code = nested.code ?? value.code;
  const message = nested.message ?? value.message;
  const details = isRecord(nested.details) ? nested.details : isRecord(value.details) ? value.details : undefined;
  if (code === undefined && message === undefined) return undefined;
  return {
    code: code === undefined ? undefined : String(code),
    message: message === undefined ? undefined : String(message),
    details,
  };
}

export async function adaptiveApiRequest<T>(path: DefaultspackContractRoute, init: RequestInit = {}): Promise<T> {
  const response = await adaptiveFetch(path, init);
  let payload: unknown;

  try {
    payload = await response.json();
  } catch {
    if (!response.ok) {
      throw new Error(explainAdaptiveError(response.status, undefined, response.statusText));
    }
    throw new Error("adaptive API returned an invalid JSON response");
  }

  if (isEnvelope<T>(payload)) {
    if (!response.ok || payload.status === "error") {
      const error = errorPayloadFrom(payload);
      throw new AdaptiveApiError(
        explainAdaptiveError(response.status, error, response.statusText),
        { code: error?.code, details: error?.details, status: response.status },
      );
    }
    if ("data" in payload) return payload.data as T;
  }

  if (!response.ok) {
    const error = errorPayloadFrom(payload);
    throw new AdaptiveApiError(
      explainAdaptiveError(response.status, error, response.statusText),
      { code: error?.code, details: error?.details, status: response.status },
    );
  }

  return payload as T;
}

export function fetchAdaptiveOnboarding(): Promise<AdaptiveOnboardingState> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/onboarding/status"), { cache: "no-store" })
    .then(toOnboardingState);
}

export function normalizeAdaptiveOnboardingAnswers(
  answers: AdaptiveOnboardingAnswers,
): Promise<AdaptiveOnboardingApiResult> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/onboarding/answers/normalize"), {
    method: "POST",
    body: JSON.stringify({ draft: answers }),
  }).then(toOnboardingApiResult);
}

export function compileAdaptiveOnboardingAnswers(
  answers: AdaptiveOnboardingAnswers,
): Promise<AdaptiveOnboardingApiResult> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/onboarding/compile"), {
    method: "POST",
    body: JSON.stringify(onboardingAnswersPayload(answers)),
  }).then(toOnboardingApiResult);
}

export function simulateAdaptiveOnboardingAnswers(
  answers: AdaptiveOnboardingAnswers,
): Promise<AdaptiveOnboardingApiResult> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/onboarding/simulate"), {
    method: "POST",
    body: JSON.stringify(onboardingAnswersPayload(answers)),
  }).then(toOnboardingApiResult);
}

export function applyAdaptiveOnboardingPlan(
  answers: AdaptiveOnboardingAnswers,
  plan?: Record<string, unknown>,
): Promise<AdaptiveOnboardingApiResult> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/onboarding/apply"), {
    method: "POST",
    body: JSON.stringify(plan ? { plan } : onboardingAnswersPayload(answers)),
  }).then(toOnboardingApiResult);
}

export function fetchAdaptiveOperatingProfile(): Promise<AdaptiveOperatingProfile> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/onboarding/status"), { cache: "no-store" })
    .then(toOperatingProfile);
}

export function saveAdaptiveOperatingProfile(
  profile: AdaptiveOperatingProfile,
  options: { expectedRevision: number; requestId: string },
): Promise<AdaptiveOperatingProfile> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute(`api/operating-profiles/${encodeURIComponent(profile.id)}`), {
    method: "PUT",
    body: JSON.stringify({
      patch: {
        summary: profile.summary,
        autonomy: profile.autonomy,
      },
      expected_revision: options.expectedRevision,
      request_id: options.requestId,
    }),
  }).then((updated) => {
    const draft = recordValue(updated.profile ?? updated);
    const autonomy = recordValue(draft.autonomy);
    return {
      ...profile,
      id: String(draft.id ?? profile.id),
      resourceId: String(draft.resource_id ?? profile.resourceId),
      revision: revisionFrom(draft.revision),
      summary: String(draft.summary ?? profile.summary),
      autonomy: {
        ...profile.autonomy,
        level: autonomyLevelFrom(autonomy.level) ?? profile.autonomy.level,
        label: String(autonomy.label ?? profile.autonomy.label),
      },
      updatedAt: String(draft.updated_at ?? profile.updatedAt),
    };
  });
}

export function fetchAdaptiveActivity(): Promise<AdaptiveActivityState> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/activity-center"), { cache: "no-store" })
    .then(toActivityState);
}

export function fetchAdaptiveAutomations(): Promise<AdaptiveAutomationState> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/activity-center"), { cache: "no-store" })
    .then(toAutomationState);
}

export function updateAdaptiveAutomation(
  automationId: string,
  patch: Partial<AdaptiveAutomation>,
  options: { expectedRevision: number; requestId: string },
): Promise<AdaptiveAutomation> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute(`api/automations/${encodeURIComponent(automationId)}`), {
    method: "PUT",
    body: JSON.stringify({
      patch,
      expected_revision: options.expectedRevision,
      request_id: options.requestId,
    }),
  }).then((updated) => toAutomation(recordValue(updated.automation ?? updated), automationId));
}

export function createAdaptiveRequestId(resourceId: string): string {
  const suffix = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${resourceId}:${suffix}`;
}

export function fetchAdaptiveEvidence(): Promise<AdaptiveEvidenceBundle> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/context/evidence"), {
    method: "POST",
    body: JSON.stringify({ items: [] }),
  }).then(toEvidenceBundle);
}

export function fetchAdaptiveRepositoryMap(): Promise<AdaptiveRepositoryMap> {
  return adaptiveApiRequest<Record<string, unknown>>(defaultspackContractRoute("api/context/repository-map"), { cache: "no-store" })
    .then(toRepositoryMap);
}

export function fetchAdaptiveContextBudget(): Promise<AdaptiveContextBudget> {
  return Promise.resolve({
    used: 0,
    limit: 1,
    reserved: 0,
    riskLevel: "low",
    lastTrim: null,
    segments: [],
    compressionPlan: ["Context budget is calculated locally from bounded evidence and search results."],
  });
}

function onboardingAnswersPayload(answers: AdaptiveOnboardingAnswers): Record<string, unknown> {
  return {
    answers,
    pack_recommendations: answers.pack_recommendations.map((packId) => ({
      pack_id: packId,
      reason: "Selected during adaptive onboarding.",
    })),
  };
}

function toOnboardingApiResult(payload: Record<string, unknown>): AdaptiveOnboardingApiResult {
  const operatingProfile = isRecord(payload.operating_profile)
    ? payload.operating_profile
    : isRecord(payload.profile)
      ? payload.profile
      : undefined;
  const normalizedProfile = isRecord(payload.profile) ? payload.profile : undefined;
  const plan = isRecord(payload.plan) ? payload.plan : undefined;
  return {
    profileId: String(payload.profile_id ?? operatingProfile?.profile_id ?? ""),
    normalizedProfile,
    operatingProfile,
    plan,
    diagnostics: Array.isArray(payload.diagnostics)
      ? payload.diagnostics.filter(isRecord)
      : [],
    scenarioSimulation: adaptiveScenarioResultsFrom(payload.scenario_simulation),
    settingsDiff: settingsDiffFrom(payload.settings_diff),
    localOnly: payload.local_only === true,
    wouldWrite: Array.isArray(payload.would_write) ? payload.would_write.map(String) : [],
    applied: payload.applied === true,
    historyId: payload.history_id === undefined ? null : String(payload.history_id),
    planId: payload.plan_id === undefined ? plan?.plan_id === undefined ? null : String(plan.plan_id) : String(payload.plan_id),
    path: payload.path === undefined ? null : String(payload.path),
    raw: payload,
  };
}

function adaptiveScenarioResultsFrom(value: unknown): AdaptiveScenarioResult[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => {
    const record = recordValue(item);
    const scenarioId = String(record.scenario_id ?? record.id ?? `scenario-${index}`);
    return {
      scenarioId,
      label: titleCase(scenarioId),
      actions: stringArray(record.actions),
      allowed: stringArray(record.allowed),
      approvalRequired: stringArray(record.approval_required ?? record.required_approvals),
      blocked: stringArray(record.blocked),
    };
  });
}

function adaptiveScenariosFrom(value: unknown): AdaptiveScenario[] {
  return adaptiveScenarioResultsFrom(value).map((scenario) => ({
    id: scenario.scenarioId,
    label: scenario.label,
    prompt: scenario.actions.length ? `Actions: ${scenario.actions.join(", ")}` : "No scenario actions returned.",
    expectedOutcome: [
      scenario.allowed.length ? `Allowed: ${scenario.allowed.join(", ")}` : "",
      scenario.blocked.length ? `Blocked: ${scenario.blocked.join(", ")}` : "",
    ].filter(Boolean).join(" "),
    requiredApprovals: scenario.approvalRequired,
  }));
}

function settingsDiffFrom(value: unknown): AdaptiveSettingsDiff[] {
  if (!Array.isArray(value)) return [];
  return value.map((item, index) => {
    const record = recordValue(item);
    return {
      id: String(record.id ?? `diff-${index}`),
      label: String(record.label ?? record.id ?? `Setting ${index + 1}`),
      before: String(record.before ?? ""),
      after: String(record.after ?? ""),
      tone: toneFrom(record.tone),
    };
  });
}

function onboardingPackRecommendations(
  profile: Record<string, unknown>,
  answers: Record<string, unknown>,
): AdaptivePackRecommendation[] {
  const raw = Array.isArray(profile.recommended_packs)
    ? profile.recommended_packs
    : Array.isArray(answers.pack_recommendations)
      ? answers.pack_recommendations
      : [];
  return raw.map((item) => {
    const id = String(item);
    return {
      id,
      label: titleCase(id),
      reason: "Selected or recommended for this operating profile.",
      status: "recommended",
    };
  });
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function toneFrom(value: unknown): AdaptiveTone | undefined {
  if (value === "neutral" || value === "good" || value === "warning" || value === "danger" || value === "info") {
    return value;
  }
  return undefined;
}

export function toOnboardingState(payload: Record<string, unknown>): AdaptiveOnboardingState {
  const profile = recordValue(payload.operating_profile ?? payload.profile);
  const sideEffect = recordValue(profile.side_effect_policy ?? profile.policy);
  const answers = recordValue(profile.answers);
  const useCases = recordValue(answers.use_cases);
  const uses = Array.isArray(profile.uses) ? profile.uses : [];
  const presetLabel = String(recordValue(profile.source).preset_id ?? profile.preset_id ?? "Guided");
  const hasProfile = Object.keys(profile).length > 0;
  return {
    profileId: String(payload.profile_id ?? profile.profile_id ?? "default"),
    completedStepId: hasProfile ? "settings-diff" : null,
    useCases: (Object.keys(useCases).length
      ? Object.entries(useCases).map(([id, enabled]) => ({ id, enabled }))
      : uses.length
        ? uses
        : [{ id: "coding" }, { id: "research" }]
    ).map((item) => {
      const record = recordValue(item);
      const id = String(record.id ?? "coding");
      return { id, label: titleCase(id), description: `${titleCase(id)} work`, enabled: record.enabled !== false };
    }),
    role: {
      title: String(recordValue(profile.role_context).title ?? "Local operator"),
      scope: "Profile-scoped local-first runtime",
      stakeholders: ["User", "AI reviewer"],
    },
    autonomy: {
      level: "supervised",
      label: presetLabel,
      guardrails: ["External delivery still requires confirmation", "Secrets are never returned in responses", "Pack recommendations remain suggestions"],
    },
    responsibilities: {
      owned: ["Local planning", "Bounded context", "Evidence collection"],
      excluded: ["Production deploy without approval", "Raw secret access", "Purchase or payment"],
    },
    review: {
      cadence: "Review high-risk changes before commit",
      reviewers: ["User", "AI verifier"],
      gates: ["Git push", "External message", "Secret use"],
    },
    permissions: Object.entries(sideEffect).slice(0, 8).map(([id, mode]) => ({
      id,
      label: titleCase(id),
      risk: ["git_push", "external_message", "secret_use"].includes(id) ? "high" : "medium",
      mode: String(mode),
      description: "Compiled by the adaptive permission lattice.",
    })),
    privacyMemory: {
      memoryMode: String(recordValue(profile.memory_policy).mode ?? "explicit"),
      retention: "Profile-scoped retention",
      sensitiveBoundaries: ["secrets", "external sends", "cross-profile sharing"],
    },
    skillLearning: {
      enabled: Boolean(answers.skill_learning_enabled ?? recordValue(profile.skill_learning_policy).enabled),
      sources: ["failure-to-success episodes", "verified tests", "user corrections"],
      reviewRequired: answers.skill_learning_review_required !== false,
    },
    packRecommendations: onboardingPackRecommendations(profile, answers),
    scenarioSimulation: adaptiveScenariosFrom(payload.scenario_simulation),
    settingsDiff: settingsDiffFrom(payload.settings_diff),
  };
}

export function toOperatingProfile(payload: Record<string, unknown>): AdaptiveOperatingProfile {
  const profile = recordValue(payload.operating_profile);
  const draft = recordValue(payload.operating_profile_draft);
  const sideEffect = recordValue(profile.side_effect_policy ?? profile.policy);
  const presetLabel = String(recordValue(profile.source).preset_id ?? profile.preset_id ?? "guided");
  const profileId = String(draft.id ?? profile.profile_id ?? "default");
  const draftAutonomy = recordValue(draft.autonomy);
  const autonomyLevel = autonomyLevelFrom(draftAutonomy.level) ?? "supervised";
  return {
    id: profileId,
    resourceId: String(draft.resource_id ?? profileId),
    revision: revisionFrom(draft.revision),
    name: String(profile.operating_profile_id ?? presetLabel),
    summary: String(draft.summary ?? "Deterministic local-first adaptive runtime profile."),
    role: { title: String(recordValue(profile.role_context).title ?? "Local operator"), scope: "Profile-scoped", stakeholders: ["User"] },
    autonomy: {
      level: autonomyLevel,
      label: String(draftAutonomy.label ?? presetLabel),
      guardrails: ["No occupation-based authority widening"],
    },
    focusAreas: (Array.isArray(profile.uses) ? profile.uses : []).map((item) => titleCase(String(recordValue(item).id ?? item))),
    boundaries: ["External messages", "Secrets", "Production deploys"],
    approvalPolicy: Object.entries(sideEffect).slice(0, 12).map(([id, mode]) => ({ id, label: titleCase(id), risk: "medium", mode: String(mode), description: "Compiled side-effect policy" })),
    privacyMemory: { memoryMode: "explicit", retention: "Profile-scoped", sensitiveBoundaries: ["secrets"] },
    skillLearning: { enabled: false, sources: ["verified episodes"], reviewRequired: true },
    packRecommendations: [],
    review: { cadence: "Before high-risk actions", reviewers: ["User"], gates: ["Exact plan"] },
    updatedAt: String(draft.updated_at ?? profile.updated_at ?? ""),
  };
}

export function toActivityState(payload: Record<string, unknown>): AdaptiveActivityState {
  const prepared = Array.isArray(payload.prepared_actions) ? payload.prepared_actions : [];
  const conflicts = Array.isArray(payload.memory_conflicts) ? payload.memory_conflicts : [];
  const events = Array.isArray(payload.events) ? payload.events : [];
  const items: AdaptiveActivityItem[] = [
    ...prepared.map((item, index) => {
      const record = recordValue(item);
      return {
        id: String(record.id ?? `prepared-${index}`),
        title: String(record.operation ?? "Prepared action"),
        kind: "approval",
        status: String(record.status ?? "needs_review"),
        summary: "Prepared exact-plan action",
        actor: "Adaptive runtime",
        startedAt: String(record.created_at ?? ""),
        evidenceCount: Array.isArray(record.evidence_refs) ? record.evidence_refs.length : 0,
        requiresReview: true,
        toolLabel: "Prepared action",
      };
    }),
    ...events.slice(0, 6).map((item, index) => {
      const record = recordValue(item);
      return {
        id: String(record.id ?? `event-${index}`),
        title: String(record.type ?? "Adaptive event"),
        kind: "task",
        status: activityStatusFromEvent(record),
        summary: "Durable adaptive event",
        actor: "Runtime",
        startedAt: String(record.created_at ?? ""),
        evidenceCount: 0,
      };
    }),
  ];
  return {
    items,
    reviewQueue: conflicts.map((item, index) => {
      const record = recordValue(item);
      return { id: String(record.id ?? `conflict-${index}`), title: "Memory conflict", reason: String(record.resolution ?? "Needs review"), risk: "medium", requestedBy: "Memory", ageLabel: String(record.created_at ?? "") };
    }),
    counters: {
      running: items.filter((item) => item.status === "running").length,
      needsReview: items.filter((item) => item.status === "needs_review").length,
      blocked: items.filter((item) => item.status === "blocked").length,
      completedToday: items.filter((item) => item.status === "done").length,
    },
  };
}

function activityStatusFromEvent(record: Record<string, unknown>): AdaptiveActivityStatus {
  const payload = recordValue(record.payload);
  const raw = String(payload.status ?? record.status ?? record.delivery_status ?? "").trim().toLowerCase();
  if (raw === "running" || raw === "queued" || raw === "needs_review" || raw === "blocked" || raw === "done") {
    return raw;
  }
  if (/review|approval/.test(raw)) return "needs_review";
  if (/block|fail|error|dead_letter/.test(raw)) return "blocked";
  if (/success|succeed|passed|verified|complete|done|acked|delivered/.test(raw)) return "done";
  return "done";
}

export function toAutomationState(payload: Record<string, unknown>): AdaptiveAutomationState {
  const automations = Array.isArray(payload.automations) ? payload.automations : [];
  const templates = Array.isArray(payload.automation_templates ?? payload.templates)
    ? (payload.automation_templates ?? payload.templates) as unknown[]
    : [];
  const simulation = recordValue(payload.automation_simulation ?? payload.simulation);
  return {
    revision: revisionFrom(payload.automation_revision),
    automations: automations.map((item, index) => toAutomation(recordValue(item), `automation-${index}`)),
    templates: templates.map((item, index) => {
      const record = recordValue(item);
      return {
        id: String(record.id ?? `template-${index}`),
        name: String(record.name ?? record.label ?? `Template ${index + 1}`),
        description: String(record.description ?? ""),
      };
    }),
    simulation: {
      scenario: String(simulation.scenario ?? "Draft automation"),
      result: String(simulation.result ?? "Automation remains inactive until reviewed and activated."),
      approvals: stringArray(simulation.approvals),
    },
  };
}

function toAutomation(record: Record<string, unknown>, fallbackId: string): AdaptiveAutomation {
  const id = String(record.id ?? record.automation_id ?? fallbackId);
  const steps = Array.isArray(record.steps) ? record.steps : [];
  return {
    id,
    resourceId: String(record.resource_id ?? id),
    revision: revisionFrom(record.revision),
    name: String(record.name ?? titleCase(id)),
    description: String(record.description ?? ""),
    trigger: String(record.trigger ?? "manual"),
    schedule: String(record.schedule ?? "on demand"),
    enabled: record.enabled === true,
    risk: String(record.risk ?? "medium"),
    lastRun: record.lastRun === undefined && record.last_run === undefined ? null : String(record.lastRun ?? record.last_run),
    steps: steps.map((item, index) => {
      const step = recordValue(item);
      return {
        id: String(step.id ?? `step-${index}`),
        label: String(step.label ?? `Step ${index + 1}`),
        capabilityLabel: step.capabilityLabel === undefined && step.capability_label === undefined ? null : String(step.capabilityLabel ?? step.capability_label),
        internalToolId: step.internalToolId === undefined && step.internal_tool_id === undefined ? null : String(step.internalToolId ?? step.internal_tool_id),
        requiresApproval: step.requiresApproval === true || step.requires_approval === true,
      };
    }),
  };
}

function revisionFrom(value: unknown): number {
  const revision = Number(value);
  return Number.isSafeInteger(revision) && revision >= 0 ? revision : 0;
}

function autonomyLevelFrom(value: unknown): AdaptiveAutonomyLevel | null {
  return value === "draft" || value === "confirm" || value === "supervised" || value === "autonomous"
    ? value
    : null;
}

export function toEvidenceBundle(payload: Record<string, unknown>): AdaptiveEvidenceBundle {
  const items = Array.isArray(payload.items) ? payload.items : [];
  return {
    selectedId: null,
    items: items.map((item, index) => {
      const record = recordValue(item);
      return {
        id: String(record.path ?? `evidence-${index}`),
        title: String(record.path ?? "Evidence"),
        kind: "file",
        sourceLabel: "Bounded file read",
        capturedAt: "",
        summary: `${Array.isArray(record.lines) ? record.lines.length : 0} bounded lines`,
        confidence: 1,
        redactions: [],
      };
    }),
  };
}

export function toRepositoryMap(payload: Record<string, unknown>): AdaptiveRepositoryMap {
  const files = Array.isArray(payload.files) ? payload.files.map(String) : [];
  return {
    rootLabel: String(payload.root ?? "workspace"),
    branch: null,
    sections: [{
      id: "files",
      label: "Files",
      description: "Bounded repository map",
      paths: files.slice(0, 50).map((path) => ({ path, role: "source", status: "read_only" })),
    }],
    risks: payload.truncated ? ["Repository map truncated by budget"] : [],
  };
}

function titleCase(value: string): string {
  return value.replace(/[_-]/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}
