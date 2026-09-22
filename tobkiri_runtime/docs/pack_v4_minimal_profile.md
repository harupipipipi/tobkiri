# Pack v4 minimal Profile

`tests.conformance_support.minimal_profile` is the smallest independent Profile
fixture for validating the v4 execution spine before Defaults Profile integration.

It contains only:

- one minimal Base artifact with one caller Function;
- one metadata-only Shell artifact;
- one Normal Pack root (`pack.v4.json`, `contracts.v4.json`,
  `artifact-index.v4.json`, `executables.v4.json`, and one runtime file) with
  one pure `echo` Contract Operation;
- one pinned Operation catalog route and one Authority ceiling;
- one Profile, ProfileLock, ResolvedPlan, and active ActivationRecord.

The fixture deliberately does not import `capture_default_profile`, the bundled
Defaults catalog, a legacy registry, or a platform supervisor. Its backend is
marked `conformance_only` and can only be selected by the explicitly Host-owned
`RequestBroker(production=False)` mode. This proves the interfaces without
silently turning a test backend into a production fallback.

The test compiles the target Pack root through `ProductionRuntimeV4.capture`.
The runtime file is only an integrity-pinned Pack artifact in this slice; the
deterministic conformance backend supplies the actual in-process invocation.

The test path is:

```text
Profile -> ProfileLock -> ResolvedPlan -> ActivationRecord
        -> artifact inventory -> OperationCatalog -> Authority ceilings
        -> Contract validation -> admission -> materialization -> lease/audit
        -> RequestEnvelope -> one Pack operation -> response
```

Run it with:

```bash
just pack-v4-minimal-profile
```

The production Defaults Profile is now integrated separately under
`ecosystem/defaultspack/v4/`. It uses the same record graph, with additional
canonical bindings for the source Profile definition, finite bundle, exact
Application Pack, constraints, deterministic closure, provenance, and every
caller-specific Authority edge. The minimal fixture remains intentionally
independent so it can prove the Host interfaces without importing the large
Defaults composition.

The bundle exposes one canonical named Profile, `defaults`. The bundled Tauri
and CLI Shell definitions are compatible Shell providers, not separate Profile
personas. Only the Tauri composition currently has the complete canonical
Application and frontend Contract Map required by the product surface, so the
catalog does not manufacture a duplicate CLI Profile entry. A future named CLI
Profile must add its own complete, generated composition rather than relabeling
the Tauri Application closure.
