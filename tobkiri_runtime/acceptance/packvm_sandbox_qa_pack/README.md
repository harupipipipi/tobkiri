# PackVM sandbox acceptance Pack

This directory is a purpose-built acceptance fixture. It is deliberately
outside `ecosystem/`, is not included in the Defaults Profile, and is not
installed or granted any capability by a normal Tobkiri build.

`runtime/probe.py` exposes a finite Pack ABI used to exercise the real Linux
PackVM child boundary. The operation must be captured, admitted, materialized,
and invoked by the normal Host Broker. Running the module directly on the Host
is rejected and is never acceptance evidence.

The supported scenarios are:

- `probe_isolation`: inspect the child namespace and prove that Host paths,
  network sockets, VSOCK, child processes, and the guest agent key are denied.
- `stdout_overflow` / `stderr_overflow`: exceed the guest runner's pipe limits.
- `deadline_hold`: remain alive until the original Broker deadline expires.
- `cancel_hold`: remain alive until authenticated Broker cancellation arrives.
- `abnormal_exit`: exit without returning a Pack result.

Use `scripts/run_packvm_sandbox_acceptance.py` only with a native adapter that
invokes the fixture through the canonical Broker and returns authenticated
guest observations. The runner rejects mock, direct-child, Host-pipe, and
unsigned observations.

Before admission, sign the exact fixture with an acceptance-only Ed25519 key
held outside this directory. Signature verification proves publisher and byte
identity but deliberately grants no authority:

```bash
python -m scripts.tobkiri_pack sign \
  acceptance/packvm_sandbox_qa_pack \
  --private-key /secure/path/packvm-acceptance.pem \
  --pack-id tobkiri_packvm_sandbox_qa_pack \
  --version 1.0.0 \
  --publisher-id dev.tobkiri.acceptance \
  --core-compatibility '>=1.0.0' \
  --contract-version tobkiri.acceptance.packvm.sandbox.v1=1.0.0 \
  --output /private/tmp/tobkiri-packvm-qa-signed-pack.json
```

The native adapter must independently verify that manifest against its pinned
acceptance public key and then use normal Profile/Authority approval. The
fixture requests zero capabilities; signature possession is never a grant.
