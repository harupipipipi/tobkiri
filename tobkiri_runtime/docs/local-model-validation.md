# Local text model validation

This experimental route adds keyless, Host-brokered text inference. It does not
make the legacy LM Studio or Ollama adapters part of the active v4 conversation
path. It does not yet provide automatic model installation or an installer-ready
local default.

## Boundaries

- The cloud credential transport and its HTTPS requirement are unchanged
- Only the captured provider generate/stream Functions receive local transport
- A provider connection alone grants no loopback access. An operator registration
  independently binds the exact Profile, connection ID, IPv4 loopback port and
  model alias
- The transport accepts only `http://127.0.0.1:<port>/v1`, with the fixed chat
  completion path. It uses no proxy, credentials, redirect, DNS hostname or
  caller-supplied HTTP headers
- Messages are plain text. Exact saved text blocks are joined without fetching
  media. Tool calls, vision, embedding and image generation are unavailable
- Requests and responses are bounded. The original Broker lease, revocations,
  deadline, cancellation and configuration are checked around nested reads and
  before sending or releasing results

## Developer validation flow

Use an isolated, explicitly approved test installation. Start an official CPU
runner with a verified model file, bound only to IPv4 loopback. Record the runner
version, model digest, port and model alias. Do not add a fake API key or change
cloud endpoint policy to make a local endpoint work.

The current smoke-test model is Liquid AI LFM2.5-1.2B-Instruct Q4_K_M. Its license
is available at:
https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF/blob/main/LICENSE
The model is for integration validation; its success is not evidence of coding
quality or general agent/tool capability.

Once the runner is ready, invoke the operator-only helper against the actual
Tobkiri Host user-data directory and the intended Profile:

```sh
python -B scripts/register_local_model.py \
  --user-data /absolute/path/to/the/actual/host-user-data \
  --profile defaults \
  --provider provider.liquid-local \
  --endpoint http://127.0.0.1:18080/v1 \
  --model lfm-local
```

The helper neither downloads nor launches a runner. It refuses conflicting
registrations and existing connection replacements. Repeating the same exact
registration is idempotent. It is not exposed as a Pack action or ordinary
Settings field. The Host allowlist is under `host_local_models/allowlist.json`;
Pack-owned provider registry writes cannot modify this independent approval.

In a build containing the reviewed artifacts, use the existing model-route form
to select the registered connection and the exact model alias. Save the route,
select it in ordinary chat, send two turns, and reopen the conversation to verify
persisted history. The connection UI says credentials are unnecessary, without
claiming the runner has been contacted or verified.

## Evidence required

Keep these outcomes separate:

1. Runner starts and responds directly
2. The local transport produces two real model responses
3. The captured production Broker, registry, gateway and adapter dispatch locally
4. Launcher → Defaults → saved conversation produces and persists real replies
5. Windows/macOS/Linux installation and restart work without manual developer setup

Focused tests, mocked IO, a healthy VM, or an open chat screen do not substitute
for steps 4 and 5. Migration review/receipt freshness and actual chat acceptance
are separate gates; neither may be fabricated from the other.
