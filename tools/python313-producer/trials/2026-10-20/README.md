# Versioned Python 3.13 guest trial

This dated, inactive v2 trial reuses the previously approved and preserved
custom producer; it does not change official SDK producer verification or
production defaults. `componentize-trial.py` differs from the previous dated
v1 driver only in its reviewed source-plan hash. The v1 artifact and receipts
remain unchanged.

The source plan uses `app_v2` and
`local:ai-tool-bridge-ordered-python/bridge@2.0.0`. It binds twelve files,
including the unchanged original normalization source and both codec versions.
Reference answers and Host provider implementations are not guest inputs.

The component build completed under separate network and PID namespaces with
read-only captured inputs. `/work` was the only writable host bind; the private
root, `/dev` and `/proc` were also writable. Environment/home/site isolation and
input read-only behavior were checked using a system-Python-only probe before
running the custom producer. Native Core acceptance and production activation
remain false. Guest runtime identity and behavior require a separate replay.

Build evidence:

- Plan SHA256: `14d659b6c661aa9a5d190912f046a7dd122b356f07473635c812bcf8f8af558e`
- Producer SHA256: `ba5ce27a1109faeaf80d0f1fe3491f1863a3bb67db08c11845151e55cd483987`
- Component SHA256: `0f28f4a87158b18a24fe045a9be1c24e8d9405549a0af1b56546b2017ac92a46`
- Component size: 17,881,199 bytes
- Elapsed time: 32.85 seconds; sampled peak RSS: 441,008,128 bytes
- All 1,503 mounted-input inventory records were unchanged afterward.
- Typed outer composition validated the two exact exports and five canonical
  Core imports. This is not native dispatch or authority evidence.

The external supervisor preserved the earlier trial's 8 GiB per-process address
space limit, 1,200-second wall deadline, 3 GiB sampled aggregate RSS guard,
64-process sampled guard, 8 GiB free-disk floor, 4 GiB initial available-memory
floor and 1 GiB running available-memory floor. Sampling occurs every 0.1 seconds
plus census time; these are not hard RSS/task caps or generic hostile-process
containment guarantees. The group leader remained unreaped until cleanup, and
no sampled known descendants remained. The supervisor and mount inventory are
separate execution evidence; this driver alone does not establish isolation.

The official Wasmtime 48 wheel was verified against the SDK lock hash and both
archive/installed RECORD manifests. Installed archive members matched byte for
byte except pip's rewritten RECORD. WAC 0.12.0 matched the existing SDK release
hash. No new unrecognized producer dependency or interpreter change was used.
