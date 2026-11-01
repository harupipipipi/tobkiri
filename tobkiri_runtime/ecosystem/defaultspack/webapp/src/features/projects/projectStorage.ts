import { api, type ProjectStateRecord as APIProjectStateRecord, type ProjectStateSnapshot as APIProjectStateSnapshot } from "../../lib/api";

export type ProjectWorkspaceBinding = {
  workspaceId: string;
  workspaceLabel: string;
  workspaceRoot: string;
};

type OwnerWorkspaceBinding = { workspace_id: string; workspace_label: string; workspace_root: string };
type ProjectStateRecord = APIProjectStateRecord & { workspace_bindings?: OwnerWorkspaceBinding[] };
type ProjectStateSnapshot = Omit<APIProjectStateSnapshot, "projects"> & { projects: ProjectStateRecord[] };

export type ProjectInfo = {
  id: string;
  title: string;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  workspaceRoot?: string | null;
  rumiDataPath?: string | null;
  workspaceBindings?: ProjectWorkspaceBinding[];
};

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

function validateBindings(value: unknown, primary: ProjectInfo): ProjectWorkspaceBinding[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 32) throw new Error("Project workspace binding count is invalid.");
  const seen = new Set<string>();
  const result = value.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) throw new Error("Project workspace binding is invalid.");
    const record = item as Record<string, unknown>;
    const snake = Object.prototype.hasOwnProperty.call(record, "workspace_id");
    const fields = snake ? ["workspace_id", "workspace_label", "workspace_root"] : ["workspaceId", "workspaceLabel", "workspaceRoot"];
    if (Object.keys(record).length !== 3 || fields.some((field) => !Object.prototype.hasOwnProperty.call(record, field))) throw new Error("Unknown Project workspace binding fields.");
    const [id, label, root] = fields.map((field) => record[field]);
    if (typeof id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(id) || seen.has(id)
      || typeof label !== "string" || !label || label !== label.trim() || label.length > 512 || label.includes("\0")
      || typeof root !== "string" || !/^(?:\/(?!\/)|[A-Za-z]:\/|\/\/[^/]+\/[^/]+(?:\/|$))/.test(root.replace(/\\/g, "/")) || root !== root.trim() || root.length > 4096 || root.includes("\0")
      || root.replace(/\\/g, "/").split("/").some((part) => part === "." || part === "..")) throw new Error("Project workspace binding metadata is invalid.");
    seen.add(id);
    return { workspaceId: id, workspaceLabel: label, workspaceRoot: root };
  });
  if (!result.some((binding) => binding.workspaceId === primary.workspaceId && binding.workspaceLabel === primary.workspaceLabel && binding.workspaceRoot === primary.workspaceRoot)) throw new Error("Project primary workspace must match an exact binding.");
  return result;
}

export function projectFromStorageItem(item: unknown): ProjectInfo | null {
  if (!item || typeof item !== "object") return null;
  const record = item as Record<string, unknown>;
  const id = stringOrNull(record.id);
  const title = stringOrNull(record.title);
  if (!id || !title) return null;
  const project: ProjectInfo = {
    id,
    title,
    workspaceId: stringOrNull(record.workspaceId ?? record.workspace_id),
    workspaceLabel: stringOrNull(record.workspaceLabel ?? record.workspace_label),
    workspaceRoot: stringOrNull(record.workspaceRoot ?? record.workspace_root ?? record.rootPath),
    rumiDataPath: stringOrNull(record.rumiDataPath ?? record.rumi_data_path ?? record.rumiDPPath),
  };
  try {
    if (Object.prototype.hasOwnProperty.call(record, "workspaceBindings") || Object.prototype.hasOwnProperty.call(record, "workspace_bindings")) {
      const bindings = Object.prototype.hasOwnProperty.call(record, "workspaceBindings") ? record.workspaceBindings : record.workspace_bindings;
      project.workspaceBindings = validateBindings(bindings, project);
    }
    return project;
  } catch { return null; }
}

export function projectFromOwner(item: ProjectStateRecord): ProjectInfo {
  const project: ProjectInfo = {
    id: item.id,
    title: item.title,
    workspaceId: item.workspace_id,
    workspaceLabel: item.workspace_label,
    workspaceRoot: item.workspace_root,
    rumiDataPath: item.rumi_data_path,
  };
  if (item.workspace_bindings !== undefined) project.workspaceBindings = validateBindings(item.workspace_bindings, project);
  return project;
}

export function projectToOwner(item: ProjectInfo): ProjectStateRecord {
  return {
    id: item.id,
    title: item.title,
    workspace_id: item.workspaceId ?? null,
    workspace_label: item.workspaceLabel ?? null,
    workspace_root: item.workspaceRoot ?? null,
    rumi_data_path: item.rumiDataPath ?? null,
    ...(item.workspaceBindings !== undefined ? { workspace_bindings: validateBindings(item.workspaceBindings, item).map((binding) => ({
      workspace_id: binding.workspaceId, workspace_label: binding.workspaceLabel, workspace_root: binding.workspaceRoot,
    })) } : {}),
  };
}

function publish(next: ProjectStateSnapshot): ProjectInfo[] {
  snapshot = next;
  const projects = next.projects.map(projectFromOwner);
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
      .filter((item): item is ProjectInfo => Boolean(item)).map(projectToOwner);
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
  return snapshot.projects.map(projectFromOwner);
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
    projects: projects.map(projectToOwner),
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
    ...(project.workspaceBindings ? { workspaceBindings: project.workspaceBindings.map((binding) => ({ ...binding })) } : {}),
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
