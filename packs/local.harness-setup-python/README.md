# Original Harness Setup decision and response candidate

This inactive Python-on-Wasm candidate packages four complete, verbatim methods
from the original `DefaultspackProfileRuntime`: `setup_activation_decision`,
`setup_activation_success`, `setup_activation_failure`, and
`retired_setup_response`. The original frozen `SetupActivationDecision` class is
also retained. Internal `defaults`, `defaults.activate`, protocol versions and
response semantics remain unchanged. The original application and GUI remain the
product path.

The typed `local:harness-setup-python/setup@1.0.0` exports `decision`, `success`,
`failure` and `retired`. Each accepts and returns an opaque finite ordered JSON
string using the existing v2 codec, including its unchanged byte/node bounds.
The WIT pin is explicitly `string`: Core's `json` alias applies CoreStrictV1
encoding and would encode the opaque request string again. The outer Core
envelope retains its existing strict encoding and bounds.
The adapter adds only explicit DATA envelopes and an attribute view for recorded
activation facts. A decision returns both original fields, `confirmation` and
`response`, with exactly one non-null outcome. Original explicit-confirmation,
exact-key, stale-review and retired-route behavior is preserved.

These values never grant permission or certify a commit. The original
`listing=None` path accepts a Mapping for later trusted Host preparation; this
candidate preserves that behavior. It does not manufacture frozen-review
validation. Similarly `success` preserves **`restart_required:false`**, the
original intermediate projection. It is not the final native receipt required
by the existing Setup frontend. Host validation of Profile/catalog/plan/authority
snapshot/epoch, durable activation/audit, recovery, cold restart and interactive
approval remain trusted native responsibilities.

`prepare_harness_setup.py` parses bounded source without importing it. Committed
provenance binds whole original files, exact byte/line extents, transport sources
and generated output, with no method rewrites. Verification refuses drift;
staging publishes a fresh SDK format-3 source plan without following links,
overwriting an existing destination, building or activating anything. The codec
and canonical Core WIT are referenced and staged without repository copies.

```sh
python3 -B scripts/wasm/prepare_harness_setup.py --verify
python3 -B -m unittest discover -s tests -p test_harness_setup.py
python3 -B scripts/wasm/prepare_harness_setup.py --stage /fresh/physical/path
```

To deliberately refresh the extraction after reviewing original-source changes,
use `--output /fresh/physical/path` and review both files before updating the
committed `python/original_setup.py` and `python/provenance.json`.

The separate `issue-1520-setup-python.yml` workflow uses the same authenticated
custom Python 3.13 producer, sandbox, captured SDK source plan, typed composition
validation and 32 MiB component bound as the existing component builds. Its
explicit `--harness-setup` mode leaves the frozen original three/five plans
unchanged and records a distinct candidate-build receipt. Successful compilation
alone does not establish guest execution or native Core/GUI integration.

The same workflow separately supervises an actual standalone Wasm comparison for
18 complete-original-module cases. All Core imports trap, with the original
2-billion fuel, 128 MiB memory and table bounds, per-case epoch deadlines,
canonical post-return cleanup and shutdown. Compilation has an outer isolated
process supervisor. Its receipt requires every case and lifecycle to pass;
native Core, interpreter identity and full GUI parity remain unproven by this
standalone execution. No component compilation or guest runs on the developer
Mac.

The dependent macOS native job runs the same 18 original-source cases through
the public Core/Gate/Profile/Session path. It verifies exact positive bytes,
Core's public typed-error projection, one dispatch/terminal per call, unchanged
Profile head/epoch/Pack and User policies, and same-Session cleanup. Hash-bound
source/build inputs and a read-only audit sidecar are retained. Native fixture
approvals use the existing exact reviewed Host test path; this is not the real
approval GUI or an initial-bootstrap exemption. The ordinary Core still needs
a saved, approved Profile, and this fixture does not activate the product path.

The source tests import the complete original application as an independent
oracle and compare ordered response bytes. They cover exact `True` confirmation,
missing/extra keys, unchanged operation IDs, stale/malformed confirmations,
`listing=None`, intermediate success, invariant/error custody, extraction drift
and SDK source-plan validation. Generated-binding doubles test fixed typed error
mapping only. Neither native Python tests nor source staging prove an actual Wasm
build, Core replay, full Setup GUI parity, or production activation. Listing,
catalog projection/validation and all Host effects remain outside this slice.
