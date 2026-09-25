import { api, type ProjectStateRecord, type ProjectStateSnapshot } from "../../lib/api";

export type ProjectInfo = {
  id: string;
  title: string;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  workspaceRoot?: string | null;
  rumiDataPath?: string | null;
};

export type ProjectLoadResult =
  | { status: "empty"; projects: ProjectInfo[] }
  | { status: "ready"; projects: ProjectInfo[] }
  | { status: "unavailable"; projects: ProjectInfo[]; message: string }
  | { status: "corrupt"; projects: ProjectInfo[]; message: string; raw: string };

// Legacy browser state is accepted only as one migration input. It is never
// returned to the UI before the canonical owner acknowledges the exact digest.
export const PROJECTS_STORAGE_KEY = "rumi-history-custom-groups";
export const PROJECTS_CHANGED_EVENT = "rumi-projects-changed";

let snapshot: ProjectStateSnapshot = {
  namespace: "defaultspack.projects.v1",
  revision: 0,
  projects: [],
};
let bootstrapPromise: Promise<ProjectInfo[]> | null = null;

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

function fromOwner(item: ProjectStateRecord): ProjectInfo {
  return {
    id: item.id,
    title: item.title,
    workspaceId: item.workspace_id,
    workspaceLabel: item.workspace_label,
    workspaceRoot: item.workspace_root,
    rumiDataPath: item.rumi_data_path,
  };
}

function toOwner(item: ProjectInfo): ProjectStateRecord {
  return {
    id: item.id,
    title: item.title,
    workspace_id: item.workspaceId ?? null,
    workspace_label: item.workspaceLabel ?? null,
    workspace_root: item.workspaceRoot ?? null,
    rumi_data_path: item.rumiDataPath ?? null,
  };
}

function publish(next: ProjectStateSnapshot): ProjectInfo[] {
  snapshot = next;
  const projects = next.projects.map(fromOwner);
  window.dispatchEvent(new CustomEvent(PROJECTS_CHANGED_EVENT, { detail: projects }));
  return projects;
}

function migrationInput(): { projects: ProjectStateRecord[]; raw: string } | null {
  try {
    const raw = window.localStorage.getItem(PROJECTS_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return null;
    const projects = parsed.map(projectFromStorageItem)
      .filter((item): item is ProjectInfo => Boolean(item)).map(toOwner);
    return projects.length ? { projects, raw } : null;
  } catch {
    return null;
  }
}

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return `sha256:${[...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

function mutationId(): string {
  return `projects:${crypto.randomUUID()}`;
}

export function loadProjects(): ProjectInfo[] {
  return snapshot.projects.map(fromOwner);
}

// Sync status view for the history UI. The canonical snapshot is authoritative
// once populated; an empty snapshot still surfaces legacy localStorage data (or
// its corruption) so recovery controls stay reachable before migration lands.
export function loadProjectsResult(): ProjectLoadResult {
  const projects = loadProjects();
  if (projects.length) return { status: "ready", projects };
  try {
    const raw = localStorage.getItem(PROJECTS_STORAGE_KEY);
    if (!raw) return { status: "empty", projects };
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) throw new Error("Stored projects must be a list.");
      const legacy = parsed
        .map(projectFromStorageItem)
        .filter((item): item is ProjectInfo => Boolean(item));
      return legacy.length
        ? { status: "ready", projects: legacy }
        : { status: "empty", projects };
    } catch (error) {
      return {
        status: "corrupt",
        projects: [],
        message: error instanceof Error ? error.message : "Project storage is corrupt.",
        raw,
      };
    }
  } catch {
    return {
      status: "unavailable",
      projects,
      message: "Project storage is unavailable.",
    };
  }
}

export function resetProjects(): boolean {
  try {
    localStorage.removeItem(PROJECTS_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}

export function bootstrapProjects(): Promise<ProjectInfo[]> {
  if (bootstrapPromise) return bootstrapPromise;
  bootstrapPromise = (async () => {
    const current = await api.projects();
    publish(current);
    const legacy = migrationInput();
    if (!legacy || current.projects.length || current.revision !== 0) return loadProjects();
    const migrationDigest = await sha256(legacy.raw);
    const acknowledgement = await api.replaceProjects({
      projects: legacy.projects,
      expected_revision: current.revision,
      mutation_id: mutationId(),
      migration_digest: migrationDigest,
    });
    if (!acknowledgement.receipt || acknowledgement.migration_digest !== migrationDigest) {
      throw new Error("Canonical Project owner did not acknowledge migration.");
    }
    publish(acknowledgement);
    if (window.localStorage.getItem(PROJECTS_STORAGE_KEY) === legacy.raw) {
      window.localStorage.removeItem(PROJECTS_STORAGE_KEY);
    }
    return loadProjects();
  })().catch((error) => {
    bootstrapPromise = null;
    throw error;
  });
  return bootstrapPromise;
}

export async function saveProjects(projects: ProjectInfo[]): Promise<ProjectInfo[]> {
  const expectedRevision = snapshot.revision;
  const acknowledgement = await api.replaceProjects({
    projects: projects.map(toOwner),
    expected_revision: expectedRevision,
    mutation_id: mutationId(),
  });
  if (!acknowledgement.receipt || acknowledgement.revision !== expectedRevision + 1) {
    throw new Error("Canonical Project owner returned an invalid acknowledgement.");
  }
  return publish(acknowledgement);
}

export async function addProject(project: ProjectInfo): Promise<ProjectInfo[]> {
  const projects = loadProjects();
  const next = [...projects.filter((item) => item.id !== project.id), project];
  return saveProjects(next);
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
