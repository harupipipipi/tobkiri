import { Check, ChevronDown, FolderOpen, Link2, Loader2, Plus, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { CodingWorkspaceRecord } from "../../lib/api";
import { ErrorNotice } from "../../components/ErrorNotice";
import { addProject, filterProjects, newProjectId, type ProjectInfo } from "./projectStorage";

type ProjectPickerProps = {
  projects: ProjectInfo[];
  selectedProjectId?: string | null;
  disabled?: boolean;
  codingWorkspaces?: CodingWorkspaceRecord[];
  onSelect: (project: ProjectInfo | null) => void;
  onDirectorySelect?: () => Promise<string | null | undefined>;
  onCodingWorkspaceCreate?: (rootPath?: string) => Promise<CodingWorkspaceRecord | null | undefined> | void;
  onProjectStoragePrepare?: (rootPath: string) => Promise<{ rootPath: string; rumiDataPath: string } | null | undefined>;
};

function folderName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

export function ProjectPicker({
  projects,
  selectedProjectId = null,
  disabled = false,
  codingWorkspaces = [],
  onSelect,
  onDirectorySelect,
  onCodingWorkspaceCreate,
  onProjectStoragePrepare,
}: ProjectPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState("");
  const [folderPath, setFolderPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null;
  const visibleProjects = useMemo(() => filterProjects(projects, query), [projects, query]);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);

  const resetCreate = () => {
    setCreating(false);
    setTitle("");
    setFolderPath("");
    setError(null);
  };

  const pickFolder = async () => {
    if (!onDirectorySelect || busy) return;
    setBusy(true);
    setError(null);
    try {
      const selected = await onDirectorySelect();
      if (selected) {
        setFolderPath(selected);
        if (!title.trim()) setTitle(folderName(selected));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Folder selection failed.");
    } finally {
      setBusy(false);
    }
  };

  const createProject = async () => {
    if (busy) return;
    const projectTitle = title.trim();
    if (!projectTitle) {
      setError("Project name is required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      let workspace = folderPath
        ? codingWorkspaces.find((candidate) => candidate.root_path === folderPath) ?? null
        : null;
      if (folderPath && !workspace) {
        const created = await onCodingWorkspaceCreate?.(folderPath);
        if (!created?.workspace_id) throw new Error("Workspace creation did not return a workspace.");
        workspace = created;
      }
      let rumiDataPath: string | null = null;
      if (folderPath) {
        const prepared = await onProjectStoragePrepare?.(folderPath);
        if (!prepared?.rumiDataPath) throw new Error("Project storage preparation did not return a path.");
        rumiDataPath = prepared.rumiDataPath;
      }
      const project: ProjectInfo = {
        id: newProjectId(),
        title: projectTitle,
        workspaceId: workspace?.workspace_id ?? null,
        workspaceLabel: workspace?.label ?? null,
        workspaceRoot: workspace?.root_path ?? (folderPath || null),
        rumiDataPath,
      };
      addProject(project);
      onSelect(project);
      resetCreate();
      setOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Project creation failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={rootRef} className="relative flex min-w-0 max-w-[13rem]">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        aria-label={`Project: ${selectedProject?.title ?? "None"}`}
        aria-expanded={open}
        className="flex h-11 min-h-11 min-w-0 items-center gap-1.5 rounded-xl border border-white/[0.09] bg-white/[0.045] px-3 text-[11px] font-medium text-zinc-300 transition-colors hover:border-white/[0.15] hover:bg-white/[0.07] hover:text-zinc-100 disabled:opacity-50"
      >
        <FolderOpen size={14} className="shrink-0 text-emerald-300" aria-hidden="true" />
        <span className="truncate">{selectedProject?.title ?? "Project"}</span>
        <ChevronDown size={12} className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden="true" />
      </button>

      {open && (
        <div className="absolute bottom-full left-0 rumi-layer-local-popover mb-2 w-[min(22rem,calc(100vw-2rem))] overflow-hidden rounded-2xl border border-white/[0.1] bg-[#15161a]/98 p-2 shadow-2xl backdrop-blur-xl">
          {creating ? (
            <div className="space-y-2 p-1">
              <div className="flex min-h-9 items-center justify-between">
                <div>
                  <p className="text-xs font-semibold text-zinc-100">New Project</p>
                  <p className="text-[10px] text-zinc-500">Optionally link an existing folder.</p>
                </div>
                <button type="button" onClick={resetCreate} className="flex h-9 w-9 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100" aria-label="Back to projects">
                  <X size={14} />
                </button>
              </div>
              <input
                autoFocus
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Project name"
                className="h-11 w-full rounded-xl border border-zinc-800 bg-black/25 px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-emerald-500/50"
              />
              <button
                type="button"
                onClick={() => void pickFolder()}
                disabled={!onDirectorySelect || busy}
                className="flex min-h-11 w-full items-center gap-2 rounded-xl border border-zinc-800 bg-black/20 px-3 text-left text-xs text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900 disabled:opacity-50"
              >
                {busy ? <Loader2 size={14} className="animate-spin" /> : <Link2 size={14} />}
                <span className="min-w-0 flex-1">
                  <span className="block font-medium">{folderPath ? "Linked folder" : "Link existing folder"}</span>
                  {folderPath && <span className="mt-0.5 block truncate font-mono text-[10px] text-zinc-500">{folderPath}</span>}
                </span>
              </button>
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
                disabled={busy}
                className="flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-zinc-100 text-xs font-semibold text-zinc-950 hover:bg-white disabled:opacity-60"
              >
                {busy && <Loader2 size={14} className="animate-spin" />}
                Create Project
              </button>
            </div>
          ) : (
            <>
              <label className="flex h-11 items-center gap-2 rounded-xl border border-zinc-800 bg-black/25 px-3">
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
              <div className="mt-2 max-h-56 overflow-y-auto">
                <button
                  type="button"
                  onClick={() => {
                    onSelect(null);
                    setOpen(false);
                  }}
                  className="flex min-h-11 w-full items-center gap-2 rounded-xl px-3 text-left text-xs text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-100"
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
                    className="flex min-h-11 w-full items-center gap-2 rounded-xl px-3 text-left hover:bg-white/[0.05]"
                  >
                    <span className="w-4 shrink-0 text-emerald-300">{selectedProject?.id === project.id && <Check size={14} />}</span>
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
                className="mt-2 flex h-11 w-full items-center justify-center gap-2 rounded-xl border border-emerald-500/25 bg-emerald-500/10 text-xs font-semibold text-emerald-200 hover:border-emerald-500/45 hover:bg-emerald-500/15"
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
