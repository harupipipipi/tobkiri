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

### Terminal history privacy

The Coding Cockpit keeps its visible terminal-history records—including command,
stdout, stderr, path, risk-reason, and approval fields—in memory only. The panel
does not read or write those records through `localStorage`, `sessionStorage`,
IndexedDB, conversation metadata, or another durable client store. Panel history
is discarded when the user clears it, changes workspaces, closes the panel, or
reloads the application.

Copied, corrupt, expired, or wrong-workspace browser records are therefore not
a terminal-history input and cannot authorize replay. An approved retry requires
both a pending terminal action created in the currently mounted panel and the
authoritative approval decision returned by the approval service. The UI labels
this scope as **Memory only** and **Private session**.

There is no durable-history opt-in today. If one is introduced, it must use an
authenticated, versioned, workspace-scoped service with explicit consent,
redaction, access controls, retention and expiry, untrusted-schema validation,
visible saved/error state, and deletion. Raw browser storage is not an allowed
terminal-history backend, and storage failures must never be presented as a
successful save.

## Git Rules

Git operations execute from the workspace root, and the discovered git
top-level directory must also be inside the workspace root. Status and diff are
read operations; commit and push remain sensitive and require the local approval
path when policy demands it.

## Capability Graph

`defaultspack.coding_workspace` is the minimal graph shape that binds start,
AI client, tools, agent, CLI surface, and frontend surface. Compiling the graph
does not relax any workspace restrictions; it only assembles the runtime profile
that uses the same confined handlers.
