export type ProjectInfo = {
  id: string;
  title: string;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  workspaceRoot?: string | null;
  rumiDataPath?: string | null;
};

// Keep the legacy key and ids so existing Group data remains readable.
export const PROJECTS_STORAGE_KEY = "rumi-history-custom-groups";
export const PROJECTS_CHANGED_EVENT = "rumi-projects-changed";

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function projectFromStorageItem(item: unknown): ProjectInfo | null {
  if (!item || typeof item !== "object") return null;
  const record = item as Record<string, unknown>;
  const id = stringOrNull(record.id);
  const title = stringOrNull(record.title);
  if (!id || !title) return null;
  return {
    id,
    title,
    workspaceId: stringOrNull(record.workspaceId ?? record.workspace_id),
    workspaceLabel: stringOrNull(record.workspaceLabel ?? record.workspace_label),
    workspaceRoot: stringOrNull(record.workspaceRoot ?? record.workspace_root ?? record.rootPath),
    rumiDataPath: stringOrNull(record.rumiDataPath ?? record.rumi_data_path ?? record.rumiDPPath),
  };
}

export function loadProjects(): ProjectInfo[] {
  try {
    const raw = localStorage.getItem(PROJECTS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.map(projectFromStorageItem).filter((item): item is ProjectInfo => Boolean(item))
      : [];
  } catch {
    return [];
  }
}

export function saveProjects(projects: ProjectInfo[]): void {
  try {
    localStorage.setItem(PROJECTS_STORAGE_KEY, JSON.stringify(projects));
    window.dispatchEvent(new CustomEvent(PROJECTS_CHANGED_EVENT, { detail: projects }));
  } catch {
    // Storage and Window can be unavailable in restricted/server-rendered contexts.
  }
}

export function addProject(project: ProjectInfo): ProjectInfo[] {
  const projects = loadProjects();
  const next = [...projects.filter((item) => item.id !== project.id), project];
  saveProjects(next);
  return next;
}

export function projectTaskContext(project: ProjectInfo | null) {
  if (!project) return null;
  return {
    groupId: project.id,
    workspaceId: project.workspaceId ?? null,
    workspaceLabel: project.workspaceLabel ?? null,
    workspaceRoot: project.workspaceRoot ?? null,
    rumiDataPath: project.rumiDataPath ?? null,
  };
}

export function newProjectId(now = Date.now()): string {
  // The group- prefix is intentionally retained for API/storage compatibility.
  return `group-${now}`;
}

export function filterProjects(projects: ProjectInfo[], query: string): ProjectInfo[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return projects;
  return projects.filter((project) => [
    project.title,
    project.workspaceLabel,
    project.workspaceRoot,
  ].filter(Boolean).join(" ").toLowerCase().includes(normalized));
}
