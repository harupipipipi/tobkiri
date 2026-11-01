import type { TaskPetMood, TaskPetViewModel } from "./taskPet";

export type TaskPetPresentation = {
  profileId: string;
  view: TaskPetViewModel;
  enabled: boolean;
};

export type TaskPetWindowMessage =
  | { type: "task-pet-state"; presentation: TaskPetPresentation }
  | { type: "task-pet-hidden"; profileId: string }
  | { type: "task-pet-ready" };

const moods = new Set<TaskPetMood>(["idle", "thinking", "waiting", "completed", "error", "cancelled"]);
const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;
const hasControl = (text: string) => /[\u0000-\u001f\u007f-\u009f]/.test(text);

export function isTaskPetPresentation(value: unknown): value is TaskPetPresentation {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<TaskPetPresentation>;
  const view = item.view;
  if (typeof item.profileId !== "string" || !identifier.test(item.profileId)
    || typeof item.enabled !== "boolean" || !view || typeof view !== "object"
    || !moods.has(view.mood) || !Number.isSafeInteger(view.revision) || view.revision < 0
    || typeof view.label !== "string" || view.label.length > 80 || hasControl(view.label)
    || typeof view.title !== "string" || view.title.length > 160
    || hasControl(view.title)
    || typeof view.detail !== "string" || view.detail.length > 180 || hasControl(view.detail)
    || !(view.key === null || (typeof view.key === "string" && view.key.length <= 1024))) return false;
  if (view.key !== null) {
    try {
      const key = JSON.parse(view.key);
      if (!Array.isArray(key) || key.length !== 3 || key.some((part) => typeof part !== "string")
        || key[0] !== item.profileId || !identifier.test(key[1]) || !identifier.test(key[2])) return false;
    } catch { return false; }
  }
  return true;
}

export function isTaskPetWindowMessage(value: unknown): value is TaskPetWindowMessage {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  if (item.type === "task-pet-ready") return true;
  if (item.type === "task-pet-hidden") {
    return typeof item.profileId === "string" && identifier.test(item.profileId);
  }
  return item.type === "task-pet-state" && isTaskPetPresentation(item.presentation);
}

export function taskPetWindowUrl(href: string, profileId: string): string {
  const url = new URL(href);
  url.pathname = `/p/${encodeURIComponent(profileId)}/chat`;
  url.search = "?surface=task-pet";
  url.hash = "";
  return url.toString();
}

export function taskPetWindowName(profileId: string): string {
  return `tobkiri-task-pet-${encodeURIComponent(profileId)}`;
}
