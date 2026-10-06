import type { ProjectDirectorySelection, ProjectDirectorySelectionSet, ProjectWorkspaceSet } from "../../lib/projectWorkspaceMount";
import { Check, ChevronDown, FolderOpen, Link2, Loader2, Plus, Search, X } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import type { CodingWorkspaceRecord } from "../../lib/api";
import { ProjectFolderSelection } from "../../lib/projectFolderSelection";
import {
  MAX_PROJECT_FOLDERS, appendProjectFolderSelections, consumeProjectFolderSelections,
  emptyProjectFolderDraft, projectFolderSelectionSet, removeProjectFolderSelection,
  setPrimaryProjectFolderSelection, type ProjectFolderDraft,
} from "../../lib/projectFolderDraft";
import { ProjectFolderList } from "./ProjectFolderList";
import { ErrorNotice } from "../../components/ErrorNotice";
import { addProject, filterProjects, newProjectId, type ProjectInfo } from "./projectStorage";

type ProjectPickerProps = {
  projects: ProjectInfo[];
  profileId?: string;
  selectedProjectId?: string | null;
  disabled?: boolean;
  codingWorkspaces?: CodingWorkspaceRecord[];
  onSelect: (project: ProjectInfo | null) => void;
  onDirectorySelect?: () => Promise<ProjectDirectorySelection | ProjectDirectorySelectionSet | null | undefined>;
  onCodingWorkspaceCreate?: (selection: ProjectDirectorySelectionSet, isCurrent: () => boolean) => Promise<ProjectWorkspaceSet | null | undefined>;
  onProjectStoragePrepare?: (rootPath: string) => Promise<{ rootPath: string; rumiDataPath: string } | null | undefined>;
};

export function ProjectPicker({
  projects,
  profileId = "unavailable",
  selectedProjectId = null,
  disabled = false,
  onSelect,
  onDirectorySelect,
  onCodingWorkspaceCreate,
}: ProjectPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [folderDraft, setFolderDraft] = useState<ProjectFolderDraft>(emptyProjectFolderDraft);
  const mountedWorkspaceSetRef = useRef<ProjectWorkspaceSet | null>(null);
  const [folderSelectionFailed, setFolderSelectionFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [menuStyle, setMenuStyle] = useState<CSSProperties>();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const operationRef = useRef(new ProjectFolderSelection());

  useEffect(() => () => operationRef.current.invalidate(), []);
  const profileRef = useRef(profileId);
  profileRef.current = profileId;
  const currentProfile = () => profileRef.current;
  useEffect(() => {
    operationRef.current.invalidate();
    setOpen(false);
    setCreating(false);
    setTitle("");
    setBusy(false);
    setFolderDraft(emptyProjectFolderDraft());
    mountedWorkspaceSetRef.current = null;
    setFolderSelectionFailed(false);
    setError(null);
  }, [profileId]);

  const closeMenu = () => {
    if (operationRef.current.creating) return;
    operationRef.current.invalidate();
    setBusy(false);
    setOpen(false);
  };
  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null;
  const visibleProjects = useMemo(() => filterProjects(projects, query), [projects, query]);

  useLayoutEffect(() => {
    if (!open) return;
    const positionMenu = () => {
      const root = rootRef.current;
      if (!root) return;
      const anchor = root.getBoundingClientRect();
      // Keep the menu inside the workspace, including beside the narrow rail.
      const workspaceLeft = root.closest(".rumi-workspace-main")?.getBoundingClientRect().left ?? 0;
      const width = Math.min(272, Math.max(0, window.innerWidth - workspaceLeft - 24));
      const left = Math.min(Math.max(anchor.left, workspaceLeft + 12), window.innerWidth - width - 12);
      setMenuStyle({ width, left: left - anchor.left });
    };
    positionMenu();
    window.addEventListener("resize", positionMenu);
    return () => window.removeEventListener("resize", positionMenu);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  const resetCreate = () => {
    if (operationRef.current.creating) return;
    operationRef.current.invalidate();
    setBusy(false);
    setCreating(false);
    setTitle("");
    setFolderDraft(emptyProjectFolderDraft());
    mountedWorkspaceSetRef.current = null;
    setFolderSelectionFailed(false);
    setError(null);
  };

  const pickFolder = async () => {
    if (!onDirectorySelect) return;
    const ticket = operationRef.current.begin("selection");
    if (ticket === null) return;
    setBusy(true);
    if (!folderSelectionFailed) setError(null);
    try {
      const selected = await onDirectorySelect();
      if (!operationRef.current.matches(ticket) || currentProfile() !== profileId) return;
      if (selected) {
        const next = appendProjectFolderSelections(folderDraft, selected);
        setFolderDraft(next);
        setError(null);
        setFolderSelectionFailed(false);
        mountedWorkspaceSetRef.current = null;
        setTitle((current) => current.trim() ? current : next.selections[0].display_name);
      }
    } catch (reason) {
      if (!operationRef.current.matches(ticket) || currentProfile() !== profileId) return;
      setFolderSelectionFailed(folderDraft.selections.length === 0);
      setError(reason instanceof Error ? reason.message : "Folder selection failed.");
    } finally {
      if (operationRef.current.finish(ticket)) setBusy(false);
    }
  };

  const createProject = async () => {
    if (folderSelectionFailed) return;
    const projectTitle = title.trim();
    if (!projectTitle) {
      setError("Project name is required.");
      return;
    }
    const ticket = operationRef.current.begin("creation");
    if (ticket === null) return;
    setBusy(true);
    setError(null);
    let mountAttempted = false;
    try {
      let mounted = mountedWorkspaceSetRef.current;
      if (folderDraft.selections.length && !mounted) {
        if (!onCodingWorkspaceCreate) throw new Error("Workspace creation is unavailable.");
        const selections = projectFolderSelectionSet(folderDraft);
        // Host consumes these tickets at prepare; preserve the draft but never
        // issue a second mount with them if approval or transport fails.
        setFolderDraft(consumeProjectFolderSelections(folderDraft));
        mountAttempted = true;
        const created = await onCodingWorkspaceCreate(selections,
          () => operationRef.current.matches(ticket) && currentProfile() === profileId);
        if (!operationRef.current.matches(ticket) || currentProfile() !== profileId) return;
        if (!created?.workspace || !Array.isArray(created.workspaces)
          || created.workspaces.length !== selections.selections.length) {
          throw new Error("Workspace creation did not return every selected folder. Choose folders again.");
        }
        const identities = new Set<string>();
        for (const workspace of created.workspaces) {
          if (!workspace.workspace_id?.trim() || !workspace.root_path?.trim()
            || !workspace.label?.trim() || identities.has(workspace.workspace_id)) {
            throw new Error("Workspace creation returned invalid folder bindings. Choose folders again.");
          }
          identities.add(workspace.workspace_id);
        }
        if (!created.workspaces.some((workspace) => workspace.workspace_id === created.workspace.workspace_id
          && workspace.root_path === created.workspace.root_path && workspace.label === created.workspace.label)) {
          throw new Error("Workspace creation returned an invalid primary folder. Choose folders again.");
        }
        mounted = created;
        mountedWorkspaceSetRef.current = created;
      }
      const workspace = mounted?.workspace;
      // Canonical Project ownership stores every approved workspace binding.
      // Cache mount results so a save retry never reuses consumed folder tickets.
      const project: ProjectInfo = {
        id: newProjectId(),
        title: projectTitle,
        workspaceId: workspace?.workspace_id ?? null,
        workspaceLabel: workspace?.label ?? null,
        workspaceRoot: workspace?.root_path ?? null,
        rumiDataPath: null,
        ...(mounted ? { workspaceBindings: mounted.workspaces.map((binding) => ({
          workspaceId: binding.workspace_id,
          workspaceLabel: binding.label,
          workspaceRoot: binding.root_path,
        })) } : {}),
      };
      if (!operationRef.current.matches(ticket) || currentProfile() !== profileId) return;
      await addProject(project);
      if (!operationRef.current.matches(ticket) || currentProfile() !== profileId) return;
      onSelect(project);
      operationRef.current.finish(ticket);
      resetCreate();
      setOpen(false);
    } catch (reason) {
      if (!operationRef.current.matches(ticket) || currentProfile() !== profileId) return;
      const message = reason instanceof Error ? reason.message : "Project creation failed.";
      setError(mountAttempted && !mountedWorkspaceSetRef.current
        ? `${message} Remove the submitted folders and choose them again; your project name is preserved.`
        : message);
    } finally {
      if (operationRef.current.finish(ticket)) setBusy(false);
    }
  };

  return (
    <div ref={rootRef} className="relative flex min-w-0 max-w-[13rem]">
      <button
        type="button"
        disabled={disabled}
        onClick={() => open ? closeMenu() : setOpen(true)}
        aria-label={`Project: ${selectedProject?.title ?? "Select project"}`}
        aria-expanded={open}
        className="flex h-11 min-h-11 min-w-0 items-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium text-zinc-300 transition-colors hover:bg-white/[0.05] hover:text-zinc-100 disabled:opacity-50"
      >
        <FolderOpen size={14} className="shrink-0 text-zinc-300" aria-hidden="true" />
        <span className="truncate">{selectedProject?.title ?? "Select project"}</span>
        <ChevronDown size={12} className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>

      {open && (
        <div style={menuStyle} className="absolute bottom-full left-0 rumi-layer-local-popover rumi-project-menu mb-2 w-[min(17rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-white/[0.1] bg-[var(--rumi-surface-overlay)] p-1.5 shadow-2xl backdrop-blur-xl">
          {creating ? (
            <div className="space-y-2 p-1">
              <div className="flex min-h-9 items-center justify-between">
                <div>
                  <p className="text-xs font-semibold text-zinc-100">New Project</p>
                  <p className="text-[10px] text-zinc-500">Optionally link existing folders.</p>
                </div>
                <button type="button" onClick={resetCreate} disabled={operationRef.current.creating} className="flex h-9 w-9 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100" aria-label="Back to projects">
                  <X size={14} />
                </button>
              </div>
              <input
                autoFocus
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Project name"
                className="h-11 w-full rounded-xl border border-zinc-800 bg-black/25 px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-500"
              />
              <button
                type="button"
                onClick={() => void pickFolder()}
                disabled={!onDirectorySelect || busy || folderDraft.selections.length >= MAX_PROJECT_FOLDERS}
                className="flex min-h-11 w-full items-center gap-2 rounded-xl border border-zinc-800 bg-black/20 px-3 text-left text-xs text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900 disabled:opacity-50"
              >
                {busy ? <Loader2 size={14} className="animate-spin" /> : <Link2 size={14} />}
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">{folderDraft.selections.length ? "Add folders" : "Link existing folders"}</span>
                  <span className="mt-0.5 block text-[10px] text-zinc-500">Select up to {MAX_PROJECT_FOLDERS} folders</span>
                </span>
              </button>
              <ProjectFolderList
                draft={folderDraft}
                disabled={busy}
                onRemove={(id) => {
                  if (operationRef.current.creating || busy) return;
                  setFolderDraft(removeProjectFolderSelection(folderDraft, id));
                  mountedWorkspaceSetRef.current = null;
                  setFolderSelectionFailed(false);
                  setError(null);
                }}
                onPrimaryChange={(id) => {
                  if (operationRef.current.creating || busy) return;
                  setFolderDraft(setPrimaryProjectFolderSelection(folderDraft, id));
                  mountedWorkspaceSetRef.current = null;
                  setError(null);
                }}
              />
              {error && (
                <ErrorNotice
                  className="px-2.5 py-2 text-[10px]"
                  copyLabel="プロジェクト選択エラーをコピー"
                  message={error}
                />
              )}
              <button
                type="button"
                onClick={() => void createProject()}
                disabled={busy || folderSelectionFailed}
                className="flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-zinc-100 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-60"
              >
                {busy && <Loader2 size={14} className="animate-spin" />}
                Create Project
              </button>
            </div>
          ) : (
            <>
              <label className="flex h-9 items-center gap-2 rounded-lg px-2">
                <Search size={14} className="shrink-0 text-zinc-500" aria-hidden="true" />
                <input
                  autoFocus
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search projects"
                  aria-label="Search projects"
                  className="min-w-0 flex-1 bg-transparent text-xs text-zinc-100 outline-none placeholder:text-zinc-600"
                />
              </label>
              <div className="mt-1 max-h-48 overflow-y-auto">
                <button
                  type="button"
                  onClick={() => {
                    onSelect(null);
                    setOpen(false);
                  }}
                  className="flex min-h-9 w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-100"
                >
                  <span className="w-4">{!selectedProject && <Check size={14} />}</span>
                  No project
                </button>
                {visibleProjects.map((project) => (
                  <button
                    type="button"
                    key={project.id}
                    onClick={() => {
                      onSelect(project);
                      setOpen(false);
                    }}
                    className="flex min-h-9 w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-white/[0.05]"
                  >
                    <span className="w-4 shrink-0 text-zinc-300">{selectedProject?.id === project.id && <Check size={14} />}</span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-zinc-200">{project.title}</span>
                      {(project.workspaceLabel || project.workspaceRoot) && (
                        <span className="mt-0.5 block truncate text-[10px] text-zinc-500">{project.workspaceLabel || project.workspaceRoot}</span>
                      )}
                    </span>
                  </button>
                ))}
                {visibleProjects.length === 0 && <p className="px-3 py-4 text-center text-[11px] text-zinc-600">No matching projects</p>}
              </div>
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  setCreating(true);
                }}
                className="mt-1 flex h-9 w-full items-center gap-2 rounded-lg px-2 text-xs font-medium text-zinc-300 hover:bg-white/[0.05] hover:text-zinc-100"
              >
                <Plus size={15} aria-hidden="true" />
                New Project
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
