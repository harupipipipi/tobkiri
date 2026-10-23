# QA Frontend Input Pack

This fixture exercises a signed external v4 Normal Pack's frontend route. The
committed source is `tests/fixtures/qa_frontend_input_pack/` and is unsigned.
For GUI-only acceptance, the ready-to-select signed pair is committed at:

- Pack folder: `tests/fixtures/qa_frontend_input_signed/qa.frontend.input/`
- Public key: `tests/fixtures/qa_frontend_input_signed/qa.frontend.input.public.pem`

The private key that produced this pair was never written to disk or committed.

From `tobkiri_runtime`, prepare a signed copy for a Launcher run:

```bash
python3 -m tests.qa_frontend_input_pack /tmp/qa.frontend.input
```

The command creates `/tmp/qa.frontend.input/` and the separate
`/tmp/qa.frontend.input.public.pem`. It refuses existing output paths. The
Ed25519 private key is generated in memory and is not written to disk.

In Launcher, use **Packs → Trust and add signed Pack** and select either the
committed signed folder above or a newly generated one, then select its sibling
public-key PEM. Install, approve, enable, and
activate a Profile containing `qa.frontend.input`. Open `/qa-frontend-input`
in the Shell and type into **QA note**. The text remains local to that route;
there is no submit action or backend invocation.

Automated coverage compiles the committed Pack, signs a copy, admits it through
the Host-owned catalog, and projects the input route from admitted CAS. Run
`python3 -B -m pytest --noconftest tests/test_qa_frontend_input_pack.py -q`
on Linux or macOS. On Windows, the source compilation test runs; the signing
test is skipped because Windows Python does not express the required POSIX
`0644` signed-file mode.

The current production projector in
`ecosystem/defaultspack/defaultspack/v4_frontend_contributions.py` accepts only
an inert `route` with `mode: declarative` and a `view` containing `title`,
`body`, and an optional local `input`. It rejects `isolated`, `module`, action,
and data-source declarations. It also rejects collisions with built-in routes.
Thus this sample adds a route; it cannot replace the whole frontend or modify
the Chat composer. Supporting full replacement would require a verified
Pack-owned asset serving and isolation path from admitted CAS, a Shell mount
contract, and explicit ownership and collision policy. This UI-only sample has
no executable Function and does not prove PackVM Guest execution.
