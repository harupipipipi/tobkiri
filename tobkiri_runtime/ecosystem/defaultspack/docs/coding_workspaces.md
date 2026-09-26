# Coding Workspaces

Coding Workspaces are the defaultspack boundary for local file, terminal, and
git work. They let agents inspect and change project files while keeping every
operation rooted in one configured workspace.

## Root Resolution

The workspace root is resolved from explicit input, runtime context,
`RUMI_DEFAULTSPACK_WORKSPACE_ROOT`, or the default user data workspace. The
resolved root is created when needed and treated as the authority for path
checks.

All file paths are normalized against that root:

- relative paths are joined under the root;
- absolute paths are allowed only if they resolve inside the root;
- path traversal outside the root is rejected;
- returned paths should be workspace-relative when possible.

## File Rules

`FileOps` enforces root confinement for read, write, create, delete, move, diff,
patch, snapshot, and restore. Text reads reject binary files and oversized text
inputs. Mutations under protected workspace internals such as `.git` and
`.rumi_snapshots` are blocked.

Write-like operations should be previewable and approval-aware. Destructive
delete and restore paths use snapshots so recovery remains possible when policy
allows the action.

## Terminal Rules

Terminal commands run with `cwd` confined to the workspace root. The terminal
classifier marks shell syntax, installers, network utilities, privilege changes,
and destructive commands as approval-required. Only a small allowlist of
environment variables is forwarded, with `RUMI_` variables explicitly allowed
for runtime integration.

P2P or external input cannot execute terminal commands directly. It can only
create a request that the local agent may plan, classify, and send through local
approval.

## Git Rules

Git operations execute from the workspace root, and the discovered git
top-level directory must also be inside the workspace root. Status and diff are
read operations; commit and push remain sensitive and require the local approval
path when policy demands it.

## Execution checkout modes

Coding execution uses the following canonical modes. The mode is part of the
durable execution-attempt contract; a Member or agent name is not an ownership
substitute.

- `metadata_only` records repository metadata and creates no writable checkout.
- `isolated_copy` creates a bounded snapshot of tracked files (or an explicit
  allowlist). Git metadata, credentials, `.env*`, caches, user data, devices,
  sockets, links, and unrelated untracked files are excluded by default. Size
  and file-count admission happens before any destination is published.
- `git_worktree` runs `git worktree add --detach` against an exact full commit
  object. The Git worktree registry path, head, physical path, Execution
  Attempt, lease, and fencing token are recorded together. A request using the
  old `worktree` spelling maps to this mode and fails if a real Git worktree
  cannot be created; it never falls back to a copy.

`copy` and `isolated` remain compatibility aliases for `isolated_copy`. A
reviewer receives an independently pinned checkout and read-only access where
the host platform supports it. Cleanup is fenced and evidence-preserving:
dirty, active, conflicted, protected, donor, and evidence-retaining checkouts
are not removed. Startup reconciliation quarantines mismatches between the
durable registry, filesystem, and Git worktree registry for explicit review.

## Capability Graph

`defaultspack.coding_workspace` is the minimal graph shape that binds start,
AI client, tools, agent, CLI surface, and frontend surface. Compiling the graph
does not relax any workspace restrictions; it only assembles the runtime profile
that uses the same confined handlers.
