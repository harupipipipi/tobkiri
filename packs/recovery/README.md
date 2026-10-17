# Read-only generation observations

`local.openrouter-generation-reader` is an ordinary Wasm Pack. It exports the typed value port `local.openrouter-generation-reader.generation-reader@1.0`, operation `lookup`, and uses Core Network/Secret imports. It has no model POST, storage imports, checkpoint writes, workflow completion, regeneration operation or credential-reveal permission.

The reader is an independent Cargo workspace. Do not add it to `packs/Cargo.toml`, update that workspace's lock, or re-stage the old provider as part of recovery. Existing Provider and DeepThink Settings hashes belong to the saved Run's identity.

## Request and result

The port takes a WIT `selection` record under the `request` argument:

```json
{
  "request": {
    "generation-id": "gen-123-AbCd",
    "expected-model": "xiaomi/mimo-v2.6-flash",
    "expected-provider": "Darkbloom",
    "include-public-content": false
  }
}
```

The generation ID, model and optional actual provider must come from the saved attempt. A provider routing preference is not its observed actual provider. `include-public-content=false` makes exactly one metadata GET; true permits one separate content GET only after matched metadata. No automatic retry is made. The URLs are fixed to OpenRouter's generation endpoints and the ID is validated and encoded.

`tobkiri.generation-observation.v1` distinguishes metadata availability, provider-reported terminal state, observed native/normalized token counts, exact decimal cost, and optional stored public completion. Missing cost is not zero. Stored public output is not proof of normal completion or a successful Goal phase. Raw input, reasoning, response headers and HTTP error bodies are not returned. HTTP error bodies are not read. Content 403, 404 or missing output remain explicit availability states; successful metadata accounting is retained in those cases. Core import failures retain their typed error class with a fixed content-free message.

`identity_diagnostics` reports exact ID/model comparison booleans, an optional provider comparison, a closed mismatch reason, and bounded observed model/provider names. It never returns the response generation ID. Names must be nonempty, at most 256 UTF-8 bytes and free of control characters. A mismatch still returns no accepted usage, cost, terminal evidence or public content and prevents the optional content GET. Diagnostic names do not approve aliases, change saved settings, or enable workflow continuation.

OpenRouter may publish a requested model `id` and a distinct `canonical_slug`. If generation metadata reports the latter, keep the saved attempt's original requested model unchanged. Read the public [single-model API](https://openrouter.ai/api/v1/model/xiaomi/mimo-v2.6-flash), record its exact `id`/`canonical_slug` pair and observation time as separate evidence, and check that both values equal the original request and observed generation model respectively. The public [endpoint catalog](https://openrouter.ai/api/v1/models/xiaomi/mimo-v2.6-flash/endpoints) can additionally associate the requested ID with the observed provider. Only after that explicit verification should a new readonly lookup supply the canonical value as `expected-model`, while retaining the original requested model, mapping evidence, actual canonical model and actual provider separately. This is a current catalog observation, not a guarantee that an alias never changed. The reader does not accept prefixes, infer dated aliases, change expectations automatically, or rewrite the saved attempt.

The ordinary `api-key` Vault name is shared within a Profile, but each Pack obtains its own opaque principal-bound reference under Gate authorization. The reader does not accept a key, secret name, arbitrary URL or HTTP method in its request/config. Its only config field is a timeout bounded by 300 seconds; the existing caller deadline/resource ceilings still apply. Private input/output logging is neither enabled nor inspected by this Pack.

Official sources:

- [Generation metadata API](https://openrouter.ai/docs/api/api-reference/generations/get-request-%26-usage-metadata-for-a-generation)
- [Stored generation content API](https://openrouter.ai/docs/api/api-reference/generations/get-stored-prompt-completion-and-error-content-for-a-generation) — the documented 403 may restrict access to management keys; no fallback key acquisition is implemented.
- [Stored-content availability](https://openrouter.ai/blog/announcements/activity-dashboard/) — content must have been logged at request time; enabling logging later cannot reconstruct it.

## Local verification and staging

Commands below are preparation/verification instructions. They do not report that a build or real API lookup has already succeeded. Use a dedicated target directory and keep the original Pack archives.

```text
cargo test --manifest-path packs/recovery/local.openrouter-generation-reader/Cargo.toml --lib --target-dir <reader-target>
cargo build --manifest-path packs/recovery/local.openrouter-generation-reader/Cargo.toml --locked --release --target wasm32-wasip2 --target-dir <reader-target>
python -m unittest discover -s packs/recovery/tests -p test_generation_reader_build.py -v
python packs/recovery/build_generation_reader.py stage --target-dir <reader-target> --build-profile release --destination <new-reader-stage>
<verified-cli> pack build <new-reader-stage> --output <reader.tpk>
<verified-cli> pack verify <reader.tpk>
python packs/recovery/build_generation_reader.py profile --baseline <frozen-portable-profile.json> --reader-archive <reader.tpk> --destination <new-recovery-definition>
```

The first Cargo command creates this workspace's own `Cargo.lock`; commit/freeze it before a locked build. Stage requires it and an explicitly selected real component artifact. Every destination must be new; no baseline file is overwritten. The profile helper requires existing `.tpk` file sources, checks their identities/inventory hashes, copies each archive byte-for-byte, preserves any declared Profile ID and every old pin/config, and adds only the reader Pack/binding/limit plus its User port. An ID-less definition stays ID-less; the normal Core command selects its identity. `PREPARATION.json` records this distinction. Existing OpenRouter and opaque-key ceiling authority must already be present. It does not apply the definition, approve permissions or start a Session. Use normal Core Profile apply with the actual expected head and normal user approval separately.

Pure Rust tests use public synthetic JSON and a fake fetch closure; Python tests use explicit non-Wasm marker bytes and disposable local ustar archives. Neither proves ABI admission, networking, Vault ownership or live recovery. Official CLI Pack verification plus a dedicated Core fake/real reader test remains necessary after source compilation. A normal gate-denied test must perform no HTTP request. No model call is required to test the reader.

## Deliberately unresolved workflow recovery

This Pack returns observations only. It does not settle the existing pending attempt, recover an unknown local write, mark an answer complete, or permit another paid generation. Such changes need a separately versioned immutable resolution receipt, original attempt/config binding, accounting counted once, full-content prefix/schema checks when available, explicit user abandonment when content is unavailable, and a durable one-time regeneration claim. Existing KV atomic replacement alone is not a multi-caller compare-and-swap guarantee. Old generations, partial output and costs must remain preserved, and external tool effects must never be replayed by a model retry.
