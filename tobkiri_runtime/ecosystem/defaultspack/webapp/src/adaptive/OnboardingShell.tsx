import {
  BrainCircuit,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ClipboardCheck,
  GraduationCap,
  KeyRound,
  PackageCheck,
  PlayCircle,
  ShieldCheck,
  SlidersHorizontal,
  UserRound,
  Workflow,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type {
  AdaptiveOnboardingActionId,
  AdaptiveOnboardingActionLevel,
  AdaptiveOnboardingAnswers,
  AdaptiveOnboardingApiResult,
  AdaptiveOnboardingPreset,
  AdaptiveOnboardingState,
} from "../lib/adaptiveApi";
import { ErrorNotice } from "../components/ErrorNotice";
import {
  adaptiveOnboardingActionIds,
  compileAdaptiveOnboardingAnswers,
  fetchAdaptiveOnboarding,
  normalizeAdaptiveOnboardingAnswers,
  simulateAdaptiveOnboardingAnswers,
} from "../lib/adaptiveApi";
import {
  AdaptiveEmptyState,
  ResourceBanner,
  SurfaceHeader,
  ToneBadge,
  adaptiveControlClass,
  adaptivePageClass,
  adaptivePanelClass,
  adaptivePrimaryControlClass,
  adaptiveSectionClass,
  toneForRisk,
} from "./AdaptivePrimitives";
import { demoOnboardingState } from "./demoData";
import { useAdaptiveResource } from "./useAdaptiveResource";

const steps = [
  { id: "use-cases", label: "Use cases", icon: Workflow },
  { id: "role", label: "Role", icon: UserRound },
  { id: "autonomy", label: "Autonomy", icon: BrainCircuit },
  { id: "responsibility", label: "Responsibility", icon: ClipboardCheck },
  { id: "review", label: "Review", icon: CheckCircle2 },
  { id: "permissions", label: "Permissions", icon: KeyRound },
  { id: "privacy-memory", label: "Privacy and memory", icon: ShieldCheck },
  { id: "skill-learning", label: "Skill learning", icon: GraduationCap },
  { id: "pack-recommendations", label: "Packs", icon: PackageCheck },
  { id: "scenario-simulation", label: "Simulation", icon: PlayCircle },
  { id: "settings-diff", label: "Settings diff", icon: SlidersHorizontal },
] as const;

type StepId = (typeof steps)[number]["id"];

type OnboardingOperation = "normalize" | "compile" | "simulate";

const presetOptions: Array<{ value: AdaptiveOnboardingPreset; label: string; summary: string }> = [
  { value: "discussion_only", label: "Discussion only", summary: "Draft and discuss; writes stay blocked." },
  { value: "balanced_local", label: "Balanced local", summary: "Read locally and ask before write-like actions." },
  { value: "max_local_autonomy", label: "Max local autonomy", summary: "Allow local actions while external sends stay blocked." },
];

const actionLevelOptions: Array<{ value: AdaptiveOnboardingActionLevel; label: string }> = [
  { value: "deny", label: "Deny" },
  { value: "ask", label: "Ask" },
  { value: "allow", label: "Allow" },
];

const memoryModeOptions = [
  { value: "off", label: "Off" },
  { value: "explicit", label: "Explicit only" },
  { value: "project_summaries", label: "Project summaries" },
] as const;

const defaultActions: Record<AdaptiveOnboardingActionId, AdaptiveOnboardingActionLevel> = {
  discuss: "allow",
  propose: "allow",
  read_local: "allow",
  local_write: "ask",
  terminal: "ask",
  git_write: "ask",
  git_commit: "ask",
  git_push: "ask",
  git_merge: "ask",
  browser_control: "ask",
  computer_control: "deny",
  external_send: "deny",
  secrets_access: "deny",
};

const actionLabels: Record<AdaptiveOnboardingActionId, string> = {
  discuss: "Discuss",
  propose: "Propose",
  read_local: "Read local files",
  local_write: "Write local files",
  terminal: "Terminal",
  git_write: "Git write",
  git_commit: "Git commit",
  git_push: "Git push",
  git_merge: "Git merge",
  browser_control: "Browser control",
  computer_control: "Computer control",
  external_send: "External send",
  secrets_access: "Secrets access",
};

const permissionIdAliases: Record<string, AdaptiveOnboardingActionId> = {
  permission_workspace_read: "read_local",
  permission_host_write: "local_write",
  permission_network: "external_send",
};

const operationLabels: Record<OnboardingOperation, string> = {
  normalize: "Normalize",
  compile: "Compile",
  simulate: "Simulate",
};

function permissionActionId(id: string): AdaptiveOnboardingActionId | null {
  if ((adaptiveOnboardingActionIds as readonly string[]).includes(id)) return id as AdaptiveOnboardingActionId;
  return permissionIdAliases[id] ?? null;
}

function actionLevelFromMode(mode: string): AdaptiveOnboardingActionLevel {
  const normalized = mode.trim().toLowerCase();
  if (/\ballow|allowed|auto|enabled\b/.test(normalized)) return "allow";
  if (/\bdeny|denied|block|blocked|disabled|never\b/.test(normalized)) return "deny";
  return "ask";
}

function presetFromState(state: AdaptiveOnboardingState): AdaptiveOnboardingPreset {
  if (state.autonomy.level === "draft") return "discussion_only";
  if (state.autonomy.level === "autonomous") return "max_local_autonomy";
  return "balanced_local";
}

function normalizedMemoryMode(value: string): AdaptiveOnboardingAnswers["memory_mode"] {
  const normalized = value.trim().toLowerCase().replace(/\s+/g, "_").replace(/-/g, "_");
  if (normalized.includes("off") || normalized.includes("disabled")) return "off";
  if (normalized.includes("project")) return "project_summaries";
  return "explicit";
}

export function onboardingAnswersFromState(state: AdaptiveOnboardingState): AdaptiveOnboardingAnswers {
  const actions = { ...defaultActions };
  for (const permission of state.permissions) {
    const actionId = permissionActionId(permission.id);
    if (actionId) actions[actionId] = actionLevelFromMode(permission.mode);
  }
  return {
    profile_id: state.profileId ?? "default",
    preset_id: presetFromState(state),
    use_cases: Object.fromEntries(state.useCases.map((useCase) => [useCase.id, useCase.enabled])),
    actions,
    memory_mode: normalizedMemoryMode(state.privacyMemory.memoryMode),
    skill_learning_enabled: state.skillLearning.enabled,
    skill_learning_review_required: state.skillLearning.reviewRequired,
    pack_recommendations: state.packRecommendations
      .filter((pack) => pack.status !== "needs_review")
      .map((pack) => pack.id),
  };
}

export function updateOnboardingUseCase(
  answers: AdaptiveOnboardingAnswers,
  useCaseId: string,
  enabled: boolean,
): AdaptiveOnboardingAnswers {
  return { ...answers, use_cases: { ...answers.use_cases, [useCaseId]: enabled } };
}

export function updateOnboardingActionLevel(
  answers: AdaptiveOnboardingAnswers,
  actionId: AdaptiveOnboardingActionId,
  level: AdaptiveOnboardingActionLevel,
): AdaptiveOnboardingAnswers {
  return { ...answers, actions: { ...answers.actions, [actionId]: level } };
}

export function updateOnboardingPreset(
  answers: AdaptiveOnboardingAnswers,
  presetId: AdaptiveOnboardingPreset,
): AdaptiveOnboardingAnswers {
  return { ...answers, preset_id: presetId };
}

function updateOnboardingPack(
  answers: AdaptiveOnboardingAnswers,
  packId: string,
  enabled: boolean,
): AdaptiveOnboardingAnswers {
  const current = new Set(answers.pack_recommendations);
  if (enabled) current.add(packId);
  else current.delete(packId);
  return { ...answers, pack_recommendations: [...current].sort() };
}

function ResultPanel({
  result,
  message,
  error,
}: {
  result: AdaptiveOnboardingApiResult | null;
  message: string | null;
  error: string | null;
}) {
  if (!result && !message && !error) return null;
  return (
    <div className={adaptiveSectionClass} aria-live="polite">
      {error ? (
        <ErrorNotice
          className="rounded-md p-3 text-xs leading-5"
          copyLabel="Copy onboarding operation error"
          copyText={error}
          errorIcon="onboarding-operation"
          message={error}
        />
      ) : null}
      {message ? (
        <div className="rounded-md border border-cyan-500/30 bg-cyan-500/10 p-3 text-xs leading-5 text-cyan-100">{message}</div>
      ) : null}
      {result ? (
        <div className="mt-3 grid gap-3 lg:grid-cols-3">
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Profile</p>
            <p className="mt-1 text-sm font-semibold text-zinc-100">{result.profileId || "default"}</p>
            <p className="mt-1 text-xs text-zinc-500">{result.localOnly ? "Local only" : "Runtime response"}</p>
          </div>
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Plan</p>
            <p className="mt-1 break-all font-mono text-xs text-zinc-100">{result.planId ?? "No plan returned"}</p>
          </div>
          <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Apply</p>
            <p className="mt-1 text-sm font-semibold text-zinc-100">{result.applied ? "Applied" : "Not applied"}</p>
            {result.historyId ? <p className="mt-1 break-all font-mono text-[11px] text-zinc-500">{result.historyId}</p> : null}
          </div>
        </div>
      ) : null}
      {result?.scenarioSimulation.length ? (
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {result.scenarioSimulation.map((scenario) => (
            <article key={scenario.scenarioId} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
              <p className="text-sm font-semibold text-zinc-100">{scenario.label}</p>
              <p className="mt-2 text-xs leading-5 text-zinc-400">Actions: {scenario.actions.join(", ") || "none"}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {scenario.allowed.map((action) => <ToneBadge key={`allowed-${action}`} tone="good">{action}</ToneBadge>)}
                {scenario.approvalRequired.map((action) => <ToneBadge key={`ask-${action}`} tone="warning">{action}</ToneBadge>)}
                {scenario.blocked.map((action) => <ToneBadge key={`blocked-${action}`} tone="danger">{action}</ToneBadge>)}
              </div>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function StepBody({
  stepId,
  state,
  draft,
  setDraft,
  onRunSimulation,
  busyAction,
  result,
}: {
  stepId: StepId;
  state: AdaptiveOnboardingState;
  draft: AdaptiveOnboardingAnswers;
  setDraft: (next: AdaptiveOnboardingAnswers) => void;
  onRunSimulation: () => void;
  busyAction: OnboardingOperation | null;
  result: AdaptiveOnboardingApiResult | null;
}) {
  if (stepId === "use-cases") {
    return (
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {state.useCases.map((useCase) => (
          <label key={useCase.id} className="flex min-h-24 gap-3 rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <input
              type="checkbox"
              checked={draft.use_cases[useCase.id] ?? false}
              onChange={(event) => setDraft(updateOnboardingUseCase(draft, useCase.id, event.target.checked))}
              className="mt-1 h-4 w-4 accent-cyan-300"
              aria-label={`Enable ${useCase.label}`}
            />
            <span className="min-w-0">
              <span className="block text-sm font-semibold text-zinc-100">{useCase.label}</span>
              <span className="mt-1 block text-xs leading-5 text-zinc-400">{useCase.description}</span>
            </span>
          </label>
        ))}
      </div>
    );
  }

  if (stepId === "role") {
    return (
      <div className="grid gap-3 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
          <p className="text-sm font-semibold text-zinc-100">{state.role.title}</p>
          <p className="mt-2 text-xs leading-5 text-zinc-400">{state.role.scope}</p>
        </div>
        <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Stakeholders</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {state.role.stakeholders.map((stakeholder) => (
              <ToneBadge key={stakeholder} tone="neutral">{stakeholder}</ToneBadge>
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (stepId === "autonomy") {
    return (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <ToneBadge tone="info">{presetOptions.find((option) => option.value === draft.preset_id)?.label ?? state.autonomy.label}</ToneBadge>
          <span className="text-xs text-zinc-500">Current mode keeps actions reviewable.</span>
        </div>
        <div className="grid gap-2 md:grid-cols-3" role="radiogroup" aria-label="Operating profile preset">
          {presetOptions.map((option) => (
            <label key={option.value} className="flex min-h-24 gap-3 rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
              <input
                type="radio"
                name="adaptive-onboarding-preset"
                value={option.value}
                checked={draft.preset_id === option.value}
                onChange={() => setDraft(updateOnboardingPreset(draft, option.value))}
                className="mt-1 h-4 w-4 accent-cyan-300"
              />
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-zinc-100">{option.label}</span>
                <span className="mt-1 block text-xs leading-5 text-zinc-400">{option.summary}</span>
              </span>
            </label>
          ))}
        </div>
        <ul className="grid gap-2 md:grid-cols-3">
          {state.autonomy.guardrails.map((guardrail) => (
            <li key={guardrail} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs leading-5 text-zinc-300">
              {guardrail}
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (stepId === "responsibility") {
    return (
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-emerald-300">Owned</h3>
          <ul className="mt-2 space-y-2">
            {state.responsibilities.owned.map((item) => (
              <li key={item} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">{item}</li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-rose-300">Out of bounds</h3>
          <ul className="mt-2 space-y-2">
            {state.responsibilities.excluded.map((item) => (
              <li key={item} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">{item}</li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  if (stepId === "review") {
    return (
      <div className="grid gap-3 lg:grid-cols-[1fr_1fr]">
        <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
          <p className="text-sm font-semibold text-zinc-100">{state.review.cadence}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {state.review.reviewers.map((reviewer) => (
              <ToneBadge key={reviewer} tone="info">{reviewer}</ToneBadge>
            ))}
          </div>
          <label className="mt-4 flex items-center gap-2 text-xs text-zinc-300">
            <input
              type="checkbox"
              checked={draft.skill_learning_review_required}
              onChange={(event) => setDraft({ ...draft, skill_learning_review_required: event.target.checked })}
              className="h-4 w-4 accent-cyan-300"
              aria-label="Require review for learned skills"
            />
            Require review for learned skills
          </label>
        </div>
        <ul className="space-y-2">
          {state.review.gates.map((gate) => (
            <li key={gate} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">{gate}</li>
          ))}
        </ul>
      </div>
    );
  }

  if (stepId === "permissions") {
    return (
      <div className="grid gap-2 md:grid-cols-3">
        {adaptiveOnboardingActionIds.map((actionId) => (
          <fieldset key={actionId} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <legend className="text-sm font-semibold text-zinc-100">{actionLabels[actionId]}</legend>
            <div className="mt-1 flex items-start justify-end gap-2">
              <ToneBadge tone={toneForRisk(draft.actions[actionId] === "allow" ? "low" : draft.actions[actionId] === "deny" ? "high" : "medium")}>
                {draft.actions[actionId] ?? "deny"}
              </ToneBadge>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-1">
              {actionLevelOptions.map((option) => (
                <label key={option.value} className="flex items-center justify-center gap-1 rounded-md border border-zinc-800 bg-black/20 px-2 py-1 text-[11px] text-zinc-300">
                  <input
                    type="radio"
                    name={`adaptive-action-${actionId}`}
                    value={option.value}
                    checked={(draft.actions[actionId] ?? "deny") === option.value}
                    onChange={() => setDraft(updateOnboardingActionLevel(draft, actionId, option.value))}
                    className="h-3.5 w-3.5 accent-cyan-300"
                    aria-label={`${actionLabels[actionId]} ${option.label}`}
                  />
                  {option.label}
                </label>
              ))}
            </div>
          </fieldset>
        ))}
      </div>
    );
  }

  if (stepId === "privacy-memory") {
    return (
      <div className="space-y-3">
        <div className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
          <p className="text-sm font-semibold text-zinc-100">{state.privacyMemory.memoryMode}</p>
          <p className="mt-1 text-xs leading-5 text-zinc-400">{state.privacyMemory.retention}</p>
        </div>
        <div className="grid gap-2 md:grid-cols-3" role="radiogroup" aria-label="Memory mode">
          {memoryModeOptions.map((option) => (
            <label key={option.value} className="flex items-center gap-2 rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">
              <input
                type="radio"
                name="adaptive-memory-mode"
                value={option.value}
                checked={draft.memory_mode === option.value}
                onChange={() => setDraft({ ...draft, memory_mode: option.value })}
                className="h-4 w-4 accent-cyan-300"
              />
              {option.label}
            </label>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          {state.privacyMemory.sensitiveBoundaries.map((boundary) => (
            <ToneBadge key={boundary} tone="warning">{boundary}</ToneBadge>
          ))}
        </div>
      </div>
    );
  }

  if (stepId === "skill-learning") {
    return (
      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <ToneBadge tone={draft.skill_learning_enabled ? "good" : "neutral"}>{draft.skill_learning_enabled ? "Enabled" : "Disabled"}</ToneBadge>
          <ToneBadge tone={draft.skill_learning_review_required ? "warning" : "good"}>
            {draft.skill_learning_review_required ? "Review required" : "Auto-apply allowed"}
          </ToneBadge>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="flex items-center gap-2 rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">
            <input
              type="checkbox"
              checked={draft.skill_learning_enabled}
              onChange={(event) => setDraft({ ...draft, skill_learning_enabled: event.target.checked })}
              className="h-4 w-4 accent-cyan-300"
              aria-label="Enable skill learning"
            />
            Enable skill learning
          </label>
          <label className="flex items-center gap-2 rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">
            <input
              type="checkbox"
              checked={draft.skill_learning_review_required}
              onChange={(event) => setDraft({ ...draft, skill_learning_review_required: event.target.checked })}
              className="h-4 w-4 accent-cyan-300"
              aria-label="Require skill learning review"
            />
            Require review
          </label>
        </div>
        <ul className="grid gap-2 md:grid-cols-3">
          {state.skillLearning.sources.map((source) => (
            <li key={source} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3 text-xs text-zinc-300">{source}</li>
          ))}
        </ul>
      </div>
    );
  }

  if (stepId === "pack-recommendations") {
    return (
      <div className="grid gap-2 md:grid-cols-3">
        {state.packRecommendations.map((pack) => (
          <label key={pack.id} className="flex min-h-28 gap-3 rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
            <input
              type="checkbox"
              checked={draft.pack_recommendations.includes(pack.id)}
              onChange={(event) => setDraft(updateOnboardingPack(draft, pack.id, event.target.checked))}
              className="mt-1 h-4 w-4 accent-cyan-300"
              aria-label={`Select ${pack.label}`}
            />
            <div className="flex items-start justify-between gap-2">
              <span className="min-w-0">
                <span className="block text-sm font-semibold text-zinc-100">{pack.label}</span>
                <span className="mt-2 block text-xs leading-5 text-zinc-400">{pack.reason}</span>
              </span>
              <ToneBadge tone={toneForRisk(pack.status)}>{pack.status.replace("_", " ")}</ToneBadge>
            </div>
          </label>
        ))}
      </div>
    );
  }

  if (stepId === "scenario-simulation") {
    const scenarios = result?.scenarioSimulation.length
      ? result.scenarioSimulation.map((scenario) => ({
        id: scenario.scenarioId,
        label: scenario.label,
        prompt: scenario.actions.join(", "),
        expectedOutcome: [
          scenario.allowed.length ? `Allowed: ${scenario.allowed.join(", ")}` : "",
          scenario.approvalRequired.length ? `Review: ${scenario.approvalRequired.join(", ")}` : "",
          scenario.blocked.length ? `Blocked: ${scenario.blocked.join(", ")}` : "",
        ].filter(Boolean).join(" "),
        requiredApprovals: scenario.approvalRequired,
      }))
      : state.scenarioSimulation;
    return (
      <div className="space-y-3">
        <button
          type="button"
          className={adaptivePrimaryControlClass}
          onClick={onRunSimulation}
          disabled={busyAction !== null}
          aria-label="Run onboarding simulation"
        >
          <PlayCircle size={14} aria-hidden="true" />
          {busyAction === "simulate" ? "Simulating..." : "Run simulation"}
        </button>
        <div className="grid gap-2 lg:grid-cols-2">
          {scenarios.map((scenario) => (
            <div key={scenario.id} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
              <p className="text-sm font-semibold text-zinc-100">{scenario.label}</p>
              <p className="mt-2 text-xs leading-5 text-zinc-300">{scenario.prompt}</p>
              <p className="mt-2 text-xs leading-5 text-zinc-500">{scenario.expectedOutcome}</p>
              <div className="mt-3 flex flex-wrap gap-2">
                {scenario.requiredApprovals.map((approval) => (
                  <ToneBadge key={approval} tone="warning">{approval}</ToneBadge>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  const diffs = result?.settingsDiff.length ? result.settingsDiff : state.settingsDiff;
  return (
    <div className="grid gap-2 md:grid-cols-3">
      {diffs.map((diff) => (
        <div key={diff.id} className="rounded-md border border-zinc-800 bg-zinc-950/45 p-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm font-semibold text-zinc-100">{diff.label}</p>
            <ToneBadge tone={diff.tone ?? "neutral"}>change</ToneBadge>
          </div>
          <dl className="mt-3 grid gap-2 text-xs">
            <div>
              <dt className="text-zinc-600">Before</dt>
              <dd className="mt-0.5 text-zinc-300">{diff.before}</dd>
            </div>
            <div>
              <dt className="text-zinc-600">After</dt>
              <dd className="mt-0.5 text-zinc-100">{diff.after}</dd>
            </div>
          </dl>
        </div>
      ))}
    </div>
  );
}

export function OnboardingShell({ initialState }: { initialState?: AdaptiveOnboardingState }) {
  const { data, status, error, refresh } = useAdaptiveResource({
    demoData: demoOnboardingState,
    initialData: initialState,
    load: fetchAdaptiveOnboarding,
  });
  const displayState = data ?? initialState ?? null;
  const [draft, setDraftValue] = useState(() => onboardingAnswersFromState(initialState ?? demoOnboardingState));
  const [draftTouched, setDraftTouched] = useState(false);
  const [busyAction, setBusyAction] = useState<OnboardingOperation | null>(null);
  const [result, setResult] = useState<AdaptiveOnboardingApiResult | null>(null);
  const [operationMessage, setOperationMessage] = useState<string | null>(null);
  const [operationError, setOperationError] = useState<string | null>(null);
  const initialIndex = displayState ? Math.max(0, steps.findIndex((step) => step.id === displayState.completedStepId)) : 0;
  const [activeIndex, setActiveIndex] = useState(initialIndex > 0 ? initialIndex : 0);
  const activeStep = steps[activeIndex] ?? steps[0];
  const progress = useMemo(() => `${activeIndex + 1} / ${steps.length}`, [activeIndex]);
  const applyDisabledReason = "Approval flow is not connected.";
  const setDraft = (next: AdaptiveOnboardingAnswers) => {
    setDraftTouched(true);
    setResult(null);
    setOperationMessage(null);
    setOperationError(null);
    setDraftValue(next);
  };

  useEffect(() => {
    if (!data || draftTouched) return;
    setDraftValue(onboardingAnswersFromState(data));
  }, [data, draftTouched]);

  const runOperation = async (operation: OnboardingOperation) => {
    setBusyAction(operation);
    setOperationError(null);
    setOperationMessage(null);
    try {
      const nextResult =
        operation === "normalize"
          ? await normalizeAdaptiveOnboardingAnswers(draft)
          : operation === "compile"
            ? await compileAdaptiveOnboardingAnswers(draft)
            : await simulateAdaptiveOnboardingAnswers(draft);
      setResult(nextResult);
      setOperationMessage(`${operationLabels[operation]} completed.`);
    } catch (err) {
      setOperationError(`${operationLabels[operation]} failed. ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <section className={`${adaptivePageClass} ${adaptivePanelClass}`} aria-label="Adaptive onboarding">
      <SurfaceHeader
        eyebrow="Adaptive runtime setup"
        title="Onboarding"
        description="Shape use cases, autonomy, review, privacy, skill learning, pack recommendations, and simulation before enabling runtime behavior."
        action={<ToneBadge tone="info">{progress}</ToneBadge>}
      />
      <ResourceBanner status={status} error={error} onRefresh={refresh} />
      {!displayState ? (
        <AdaptiveEmptyState>Adaptive onboarding is unavailable until the API returns live state.</AdaptiveEmptyState>
      ) : (
        <>
      <div className={`${adaptiveSectionClass} flex flex-wrap gap-2`}>
        <button
          type="button"
          className={adaptiveControlClass}
          onClick={() => void runOperation("normalize")}
          disabled={busyAction !== null}
          aria-label="Normalize onboarding answers"
        >
          Normalize
        </button>
        <button
          type="button"
          className={adaptiveControlClass}
          onClick={() => void runOperation("compile")}
          disabled={busyAction !== null}
          aria-label="Compile onboarding profile"
        >
          Compile
        </button>
        <button
          type="button"
          className={adaptiveControlClass}
          onClick={() => void runOperation("simulate")}
          disabled={busyAction !== null}
          aria-label="Simulate onboarding profile"
        >
          Simulate
        </button>
        <button
          type="button"
          className={adaptivePrimaryControlClass}
          disabled
          title={applyDisabledReason}
          aria-label="Apply unavailable: approval flow is not connected"
        >
          Apply unavailable
        </button>
      </div>
      <ResultPanel result={result} message={operationMessage} error={operationError} />

      <div className="grid min-h-[520px] lg:grid-cols-[240px_1fr]">
        <nav className="border-t border-zinc-800/70 p-2 lg:border-r lg:border-t-0" aria-label="Adaptive onboarding steps">
          <div className="grid gap-1 sm:grid-cols-2 lg:grid-cols-1">
            {steps.map((step, index) => {
              const Icon = step.icon;
              const selected = index === activeIndex;
              return (
                <button
                  key={step.id}
                  type="button"
                  onClick={() => setActiveIndex(index)}
                  aria-current={selected ? "step" : undefined}
                  className={`flex min-h-9 items-center gap-2 rounded-md px-2 text-left text-xs transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/60 ${
                    selected ? "bg-cyan-400/10 text-cyan-100" : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
                  }`}
                >
                  <Icon size={14} aria-hidden="true" />
                  <span className="truncate">{step.label}</span>
                </button>
              );
            })}
          </div>
        </nav>

        <div className="min-w-0">
          <div className={adaptiveSectionClass}>
            <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <p className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Step</p>
                <h2 className="mt-1 text-sm font-semibold text-zinc-50">{activeStep.label}</h2>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  className={adaptiveControlClass}
                  onClick={() => setActiveIndex((value) => Math.max(0, value - 1))}
                  disabled={activeIndex === 0}
                  aria-label="Previous onboarding step"
                >
                  <ChevronLeft size={14} aria-hidden="true" />
                  Previous
                </button>
                <button
                  type="button"
                  className={adaptivePrimaryControlClass}
                  onClick={() => setActiveIndex((value) => Math.min(steps.length - 1, value + 1))}
                  disabled={activeIndex === steps.length - 1}
                  aria-label="Next onboarding step"
                >
                  Next
                  <ChevronRight size={14} aria-hidden="true" />
                </button>
              </div>
            </div>
            <StepBody
              stepId={activeStep.id}
              state={displayState}
              draft={draft}
              setDraft={setDraft}
              onRunSimulation={() => void runOperation("simulate")}
              busyAction={busyAction}
              result={result}
            />
          </div>
        </div>
      </div>
        </>
      )}
    </section>
  );
}
