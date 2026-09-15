import type { ComposerSkillItem, DroppedWidget } from "../renderers/types";
import type { ComposerEntityReference } from "./composerReferences";
import { composerSkillMentionWidget, skillMentionIdsFromText } from "./composerWidgets";

export const SETTINGS_ASSISTANT_SKILL_ID = "settings_assistant";
export const DEFAULT_COMPOSER_HOME_TITLE = "Tobkiri";
export const MAX_COMPOSER_HOME_TITLE_LENGTH = 48;

const HOME_TITLE_RESET_VALUES = new Set(["reset", "default", "tobkiri"]);

export function normalizeComposerHomeTitle(value: unknown): string {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  if (!normalized || HOME_TITLE_RESET_VALUES.has(normalized.toLocaleLowerCase())) {
    return DEFAULT_COMPOSER_HOME_TITLE;
  }
  return Array.from(normalized).slice(0, MAX_COMPOSER_HOME_TITLE_LENGTH).join("");
}

export const FALLBACK_SETTINGS_ASSISTANT_SKILL: ComposerSkillItem = {
  id: SETTINGS_ASSISTANT_SKILL_ID,
  label: "Settings",
  description: "Inspect, explain, and safely change Tobkiri settings through normal chat.",
  aliases: ["settings", "setting_mode", "settings_mode"],
};

export type SettingsModeDraft = {
  input: string;
  references: ComposerEntityReference[];
  widgets: DroppedWidget[];
};

export type ComposerHomeMode = {
  id: string;
  priority: number;
  skillId: string;
  title: string;
};

const COMPOSER_HOME_MODES: ComposerHomeMode[] = [
  {
    id: "settings",
    priority: 100,
    skillId: SETTINGS_ASSISTANT_SKILL_ID,
    title: "Settings Mode",
  },
];

export function resolveSettingsAssistantSkill(skills: ComposerSkillItem[]): ComposerSkillItem {
  return skills.find((skill) => skill.id === SETTINGS_ASSISTANT_SKILL_ID)
    ?? FALLBACK_SETTINGS_ASSISTANT_SKILL;
}

export function createSettingsModeDraft(skill: ComposerSkillItem): SettingsModeDraft {
  const mention = `@${skill.label}`;
  return {
    input: `${mention} `,
    widgets: [composerSkillMentionWidget(skill)],
    references: [{ kind: "skill", id: skill.id, syntax: mention }],
  };
}

export function isSettingsModeInput(input: string, skill: ComposerSkillItem): boolean {
  return skillMentionIdsFromText(input, [skill]).includes(SETTINGS_ASSISTANT_SKILL_ID);
}

export function resolveComposerHomeMode(
  input: string,
  skills: ComposerSkillItem[],
): ComposerHomeMode | null {
  const mentionedSkillIds = new Set(skillMentionIdsFromText(input, skills));
  return [...COMPOSER_HOME_MODES]
    .sort((left, right) => right.priority - left.priority || left.id.localeCompare(right.id))
    .find((mode) => mentionedSkillIds.has(mode.skillId)) ?? null;
}

export function resolveComposerHomeTitle(
  input: string,
  skills: ComposerSkillItem[],
  fallback = DEFAULT_COMPOSER_HOME_TITLE,
): string {
  return resolveComposerHomeMode(input, skills)?.title ?? fallback;
}
