import adaptiveBackendFixture from "./adaptiveBackend.fixture.json" with { type: "json" };
import type {
  AdaptiveActivityState,
  AdaptiveAutomationState,
  AdaptiveContextBudget,
  AdaptiveEvidenceBundle,
  AdaptiveOnboardingState,
  AdaptiveOperatingProfile,
  AdaptiveRepositoryMap,
} from "../lib/adaptiveApi";
import {
  toActivityState,
  toAutomationState,
  toEvidenceBundle,
  toOnboardingState,
  toOperatingProfile,
  toRepositoryMap,
} from "../lib/adaptiveApi";

type AdaptiveBackendFixture = {
  onboarding_status: Record<string, unknown>;
  activity_center: Record<string, unknown>;
  context_evidence: Record<string, unknown>;
  repository_map: Record<string, unknown>;
  context_budget: AdaptiveContextBudget;
};

export const demoBackendFixture = adaptiveBackendFixture as unknown as AdaptiveBackendFixture;

export const demoOnboardingState: AdaptiveOnboardingState = toOnboardingState(demoBackendFixture.onboarding_status);

export const demoOperatingProfile: AdaptiveOperatingProfile = toOperatingProfile(demoBackendFixture.onboarding_status);

export const demoActivityState: AdaptiveActivityState = toActivityState(demoBackendFixture.activity_center);

export const demoAutomationState: AdaptiveAutomationState = toAutomationState(demoBackendFixture.activity_center);

export const demoEvidenceBundle: AdaptiveEvidenceBundle = toEvidenceBundle(demoBackendFixture.context_evidence);

export const demoRepositoryMap: AdaptiveRepositoryMap = toRepositoryMap(demoBackendFixture.repository_map);

export const demoContextBudget: AdaptiveContextBudget = demoBackendFixture.context_budget;
