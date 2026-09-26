# Cloudflare Workers Python definitions Wasm conformance

The fixed tool definitions in PR #651 are a pure projection. With
`wasmtime==48.0.0` installed, build an import-free component from the Pack's
current `_definitions()` and `TOOL_ALIASES`:

```sh
python scripts/wasm/build_cloudflare_worker_definitions.py \
  --output /tmp/tobkiri-wasm/cloudflare-worker-definitions.wasm
```

From `tobkiri_runtime/`, run the real-engine comparison with the Pack's Python
definition operation:

```sh
python -B -m pytest tests/test_cloudflare_worker_definitions_wasm.py -q
```

This is a conformance artifact. The Pack v4 catalog and active Profile still
select PackVM. The fixed-tools invocation uses credentials and HTTPS, and must
stay behind its current execution and approval boundary. The definitions
Function is pure, but direct production Wasm registration on macOS still lacks
the required hard resource controller. A later cutover must update the exact
Pack artifact and Authority/Broker binding only after that boundary and
request-scoped worker cleanup are verified.
