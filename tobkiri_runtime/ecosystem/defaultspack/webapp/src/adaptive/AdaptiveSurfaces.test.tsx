import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ActivityCenter } from "./ActivityCenter";
import { AutomationStudio } from "./AutomationStudio";
import { ResourceBanner } from "./AdaptivePrimitives";
import { ContextBudgetPanel, EvidenceViewer, RepositoryMapPanel } from "./EvidencePanels";
import {
  OnboardingShell,
  onboardingAnswersFromState,
  updateOnboardingActionLevel,
  updateOnboardingPreset,
  updateOnboardingUseCase,
} from "./OnboardingShell";
import { OperatingProfilePage } from "./OperatingProfilePage";
import {
  compileAdaptiveOnboardingAnswers,
  toActivityState,
  toAutomationState,
  toEvidenceBundle,
  toOnboardingState,
  toOperatingProfile,
  toRepositoryMap,
  updateAdaptiveAutomation,
} from "../lib/adaptiveApi";
import {
  demoActivityState,
  demoAutomationState,
  demoBackendFixture,
  demoOnboardingState,
  demoOperatingProfile,
} from "./demoData";

function routeKey(path: string): string {
  return `/${path}`;
}

function requestTarget(input: RequestInfo | URL): string {
  const raw = String(input);
  const marker = "/api/contracts/defaultspack/";
  const markerIndex = raw.indexOf(marker);
  if (markerIndex < 0) return raw;
  const operation = decodeURIComponent(raw.slice(markerIndex + marker.length));
  const separator = operation.indexOf(" ");
  return separator < 0 ? operation : operation.slice(separator + 1);
}

test("OnboardingShell renders the adaptive setup steps", () => {
  const html = renderToStaticMarkup(createElement(OnboardingShell, { initialState: demoOnboardingState }));

  assert.match(html, /Onboarding/);
  assert.match(html, /Use cases/);
  assert.match(html, /Privacy and memory/);
  assert.match(html, /Settings diff/);
  assert.match(html, /Normalize/);
  assert.match(html, /Compile/);
  assert.match(html, /Simulate/);
  assert.match(html, /Apply unavailable/);
  assert.match(html, /aria-label="Apply unavailable: approval flow is not connected"/);
  assert.match(html, /title="Approval flow is not connected."/);
  assert.match(html, /disabled=""/);
});

test("onboarding draft helpers preserve controlled checkbox and radio changes", () => {
  const initial = onboardingAnswersFromState(demoOnboardingState);

  assert.equal(initial.use_cases.uc_learning, false);
  assert.equal(initial.actions.local_write, "ask");
  assert.equal(initial.actions.external_send, "ask");

  const withUseCase = updateOnboardingUseCase(initial, "uc_learning", true);
  const withPreset = updateOnboardingPreset(withUseCase, "max_local_autonomy");
  const withTerminal = updateOnboardingActionLevel(withPreset, "terminal", "allow");

  assert.equal(initial.use_cases.uc_learning, false);
  assert.equal(withUseCase.use_cases.uc_learning, true);
  assert.equal(withPreset.preset_id, "max_local_autonomy");
  assert.equal(withTerminal.actions.terminal, "allow");
});

test("adaptive demo data is mapped from the shared backend fixture", () => {
  const onboarding = toOnboardingState(demoBackendFixture.onboarding_status);
  const profile = toOperatingProfile(demoBackendFixture.onboarding_status);
  const activity = toActivityState(demoBackendFixture.activity_center);
  const automations = toAutomationState(demoBackendFixture.activity_center);
  const evidence = toEvidenceBundle(demoBackendFixture.context_evidence);
  const repositoryMap = toRepositoryMap(demoBackendFixture.repository_map);

  assert.deepEqual(onboarding, demoOnboardingState);
  assert.deepEqual(profile, demoOperatingProfile);
  assert.deepEqual(activity, demoActivityState);
  assert.deepEqual(automations, demoAutomationState);
  assert.equal(onboarding.useCases.find((item) => item.id === "uc_learning")?.enabled, false);
  assert.equal(onboarding.permissions.find((item) => item.id === "local_write")?.mode, "ask");
  assert.equal(activity.counters.running, 1);
  assert.equal(activity.counters.needsReview, 1);
  assert.equal(activity.counters.blocked, 1);
  assert.equal(activity.counters.completedToday, 1);
  assert.equal(automations.automations[0]?.id, "automation_daily_context");
  assert.equal(evidence.items[1]?.summary, "2 bounded lines");
  assert.equal(repositoryMap.sections[0]?.paths.length, 4);
});

test("onboarding compile posts current draft answers to the API", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = (async (input, init) => {
    calls.push({ input, init });
    return new Response(JSON.stringify({
      status: "ok",
      data: {
        profile_id: "default",
        compiled: true,
        plan: { plan_id: "plan_123" },
        scenario_simulation: [{
          scenario_id: "coding",
          actions: ["read_local", "terminal"],
          allowed: ["read_local"],
          approval_required: ["terminal"],
          blocked: [],
        }],
        settings_diff: [{ id: "terminal", label: "Terminal", before: "deny", after: "ask", tone: "warning" }],
        local_only: true,
      },
    }), { status: 200 });
  }) as typeof fetch;

  const answers = updateOnboardingActionLevel(
    updateOnboardingPreset(onboardingAnswersFromState(demoOnboardingState), "discussion_only"),
    "terminal",
    "deny",
  );
  const result = await compileAdaptiveOnboardingAnswers(answers);

  assert.equal(requestTarget(calls[0]?.input ?? ""), routeKey("api/onboarding/compile"));
  const body = JSON.parse(String(calls[0]?.init?.body));
  assert.equal(body.answers.preset_id, "discussion_only");
  assert.equal(body.answers.actions.terminal, "deny");
  assert.deepEqual(body.pack_recommendations.map((item: { pack_id: string }) => item.pack_id), ["pack_coding", "pack_evidence"]);
  assert.equal(result.planId, "plan_123");
  assert.equal(result.scenarioSimulation[0]?.approvalRequired[0], "terminal");
});

test("onboarding API errors reject visibly instead of returning demo data", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = (async () => new Response(JSON.stringify({
    status: "error",
    code: "ADAPTIVE_FROZEN",
    message: "adaptive runtime is frozen",
  }), { status: 409, statusText: "Conflict" })) as typeof fetch;

  await assert.rejects(
    () => compileAdaptiveOnboardingAnswers(onboardingAnswersFromState(demoOnboardingState)),
    /ADAPTIVE_FROZEN[\s\S]*adaptive runtime is frozen/,
  );
});

test("automation updates use the local automation route without high-risk prepared commit", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = (async (input, init) => {
    calls.push({ input, init });
    const url = requestTarget(input);
    if (url === routeKey("api/automations/automation_daily_context")) {
      return new Response(JSON.stringify({
        status: "ok",
        data: {
          automation: {
            id: "automation_daily_context",
            name: "Daily context refresh",
            description: "Refresh context",
            trigger: "daily",
            schedule: "local 09:00",
            enabled: true,
          },
        },
      }), { status: 200 });
    }
    return new Response(JSON.stringify({ status: "error", message: `unexpected route ${url}` }), { status: 404 });
  }) as typeof fetch;

  const automation = await updateAdaptiveAutomation("automation_daily_context", { enabled: true });

  assert.equal(automation.enabled, true);
  assert.equal(calls.length, 1);
  assert.equal(requestTarget(calls[0]?.input ?? ""), routeKey("api/automations/automation_daily_context"));
  assert.equal(calls[0]?.init?.method, "PUT");
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), { patch: { enabled: true } });
});

test("ResourceBanner renders API errors without demo fallback copy", () => {
  const longError = "backend rejected compile because signed plan metadata is missing settings revision and pack digest";
  const html = renderToStaticMarkup(createElement(ResourceBanner, {
    status: "error",
    error: longError,
  }));

  assert.match(html, /Adaptive API error/);
  assert.match(html, /signed plan metadata is missing settings revision/);
  assert.match(html, /data-error-icon="adaptive-api"/);
  assert.match(html, /data-copy-icon=""/);
  assert.match(html, /aria-label="Copy adaptive API error"/);
  assert.doesNotMatch(html, /Demo adaptive state/);
  assert.doesNotMatch(html, /Local placeholder adaptive state/);
});

test("OperatingProfilePage renders profile controls and guardrails", () => {
  const html = renderToStaticMarkup(createElement(OperatingProfilePage, { initialProfile: demoOperatingProfile }));

  assert.match(html, /Operating Profile/);
  assert.match(html, /Profile summary/);
  assert.match(html, /Autonomy mode/);
  assert.match(html, /Approval policy/);
});

test("ActivityCenter renders activity counters and review queue", () => {
  const html = renderToStaticMarkup(createElement(ActivityCenter, { initialState: demoActivityState }));

  assert.match(html, /Activity Center/);
  assert.match(html, /Running/);
  assert.match(html, /Needs review/);
  assert.match(html, /Review queue/);
});

test("AutomationStudio renders automations, templates, and simulation", () => {
  const html = renderToStaticMarkup(createElement(AutomationStudio, { initialState: demoAutomationState }));

  assert.match(html, /Automation Studio/);
  assert.match(html, /Daily context refresh/);
  assert.match(html, /Simulation/);
  assert.match(html, /Templates/);
});

test("adaptive surfaces do not render demo payloads unless initial state is explicit", () => {
  const html = renderToStaticMarkup(createElement(ActivityCenter));

  assert.match(html, /Adaptive activity is unavailable/);
  assert.doesNotMatch(html, /Review queue/);
});

test("OnboardingShell without live data does not expose demo operation buttons", () => {
  const html = renderToStaticMarkup(createElement(OnboardingShell));

  assert.match(html, /Adaptive onboarding is unavailable/);
  assert.doesNotMatch(html, /Normalize onboarding answers/);
  assert.doesNotMatch(html, /Compile onboarding profile/);
  assert.doesNotMatch(html, /Simulate onboarding profile/);
  assert.doesNotMatch(html, /Apply signed onboarding profile plan/);
});

test("evidence, repository, and budget panels render compact degraded surfaces", () => {
  const html = [
    renderToStaticMarkup(createElement(EvidenceViewer)),
    renderToStaticMarkup(createElement(RepositoryMapPanel)),
    renderToStaticMarkup(createElement(ContextBudgetPanel)),
  ].join("\n");

  assert.match(html, /Evidence Viewer/);
  assert.match(html, /Repository Map/);
  assert.match(html, /Context Budget/);
});
