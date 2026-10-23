# pi compatibility

The wrapper and adapter are separate unsigned `local.*` Packs. Upstream extension
files are stored unchanged and pinned to commit
`b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6` of `badlogic/pi-mono`.

| API | Mapping and current scope |
| --- | --- |
| `defineTool` | Identity at build time |
| `Type` | Real `@sinclair/typebox` re-export |
| `registerTool` | Adapter-owned `pi-extension` registrations → `local.harness-agent.tool-provider` |
| `on("tool_call")` | `local.harness-agent.tool-call-guard`; Flow overlay wiring and file-effect proof pending |
| `on("before_agent_start")` | Wrapper can dispatch the event; prompt assembly overlay integration pending |
| `registerCommand` | `local.harness-chat.command-provider` |
| `ctx.ui.notify` | Generic `host-io.display`, subject to declared permission |
| `ctx.hasUI` | Host embedding capability from the adapter context |
| Other APIs/events | Throw `unsupported by tobkiri.pi-adapter: <api>` |

esbuild aliases resolve the pi imports at build time. componentize-js compiles
the resulting JS into a component. Neither pi nor Node is a distribution runtime.
The adapter never authorizes an effect: every port call and overlay `next` goes
through the core gate. Full acceptance requires an overlay which skips `next`
for protected paths, plus proof that an allowed write still receives the ordinary
fs grant and approval. The current guard-decision replay does not prove those
file effects or the complete Harness tool loop.

Reverify stored sources:

```sh
curl -fsSL https://raw.githubusercontent.com/badlogic/pi-mono/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/examples/extensions/hello.ts | shasum -a 256
curl -fsSL https://raw.githubusercontent.com/badlogic/pi-mono/b06c1f2cf1dea6af8748ddeee18c23587d2cb7c6/packages/coding-agent/examples/extensions/protected-paths.ts | shasum -a 256
```

Expected hashes are in each wrapper's `upstream.json`. Native JS behavior tests
are development diagnostics. M4 acceptance additionally requires execution of
the compiled wrapper plus adapter in a gated Flow; diagnostics alone do not
resolve any migration inventory row.
