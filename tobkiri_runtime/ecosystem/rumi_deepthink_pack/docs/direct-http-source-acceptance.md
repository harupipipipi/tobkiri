# DeepThink direct HTTP source acceptance

Root executed the temporary source probe on 2026-10-03 at 01:09 JST
(`2026-10-02T16:09:33Z`). The actual DeepThink
`strategy.tobkiri_packvm_invoke` controller and ordinary
`OpenAICompatibleCompiler` compiled and parsed four real OpenRouter HTTPS
requests. All four received HTTP 200 and parsed successfully.

This is **direct HTTP source evidence**. Route quotes came from the probe's
local adapter over the current public model catalog, rather than an admitted
Gateway. No installed PackVM supervisor, signed Pack artifact, saved-turn Host
broker, Launcher GUI, microphone, or notification acceptance is claimed.

Tested source commit: `e469fc099f96971d3abeecbcd3e18f05471221f0`.
`runtime/strategy.py` SHA-256:
`d54caa69e2b482cc0612ba3b2321d844123c82af5013b00e39ae6ec275289485`.
The evidence hash matches the checked-out source. The requested and reported
model was `openai/gpt-4.1-mini`, provider `OpenAI`; the current public catalog
listed tool support. Actual tool execution was not exercised in this probe.

| Probe / phase | Input tokens | Output tokens | Reported USD | Parsed output |
| --- | ---: | ---: | ---: | --- |
| Completion / plan | 54 | 2 | 0.0000248 | `323` |
| Completion / draft | 52 | 2 | 0.0000240 | `323` |
| Completion / review | 64 | 24 | 0.0000640 | `{"approved": true, "feedback": "The answer 323 is correct for 17 * 19."}` |
| Cancellation / plan | 54 | 2 | 0.0000248 | `323` |

Total: **224 input + 30 output = 254 tokens**, **0.0001376 USD**, **11.427 seconds**.
Reported costs exactly match the catalog-price/token calculation. The main
controller completed three calls, genuinely approved the answer, and returned
`323`. Its rounded micro-USD ledger records `0.000113` USD for those three calls;
the unrounded reported sum is `0.0001128` USD. The plan-phase output was numeric,
so this run validates phase transport and review rather than planning quality.

After one real plan in the cancellation probe, `CANCELLED` was supplied to the
actual controller before the draft phase. The controller returned `CANCELLED`.
Model POST count remained **4 before / 4 after**; a subsequent driver send was
rejected before HTTP. This exercises cancellation between phases, not an
in-flight HTTP or Host device cancellation.

Bounds were four total model POSTs, 256 output tokens per call, 600 seconds, and
0.05 USD. The driver's conservative upper reservation was **0.042048 USD**.
Every compiled request retained provider price ceilings of 1 USD/M input,
2 USD/M output, zero per-request charge, and no fallback. No rejection was
replaced with a synthetic review or answer.

Root supplied the authorized key only through non-echoing TTY `getpass`.
The ignored local evidence contains sanitized phase receipts, tokens, costs,
model IDs, and outputs. This report contains no key, header, raw response,
provider error body, or key hash. Malformed-review, empty-repair, and unsupported
tool capability cases remain separately verified by deterministic source tests.
