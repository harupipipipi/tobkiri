# Independent extension evidence boundary

The two workers used public contracts/docs/schema/SDK and their own directories.
They did not read each other's implementation. Root reviewed public declarations
and reran each worker's offline tests separately. No application core code,
production catalog, live Profile, approval, activation or existing state changed.

The documented Host admission API was called for both unsigned draft roots with
an isolated absent publisher-policy path. It denied at the policy transaction
boundary. This proves no admission in that setup; it does **not** establish
signature checking or executable validation. The exact observations are in
`unsigned-admission-observation.json`.

| Requested check | Actual evidence |
| --- | --- |
| Offline CLI transcript success/input-output denial | Pure renderer and public-schema tests |
| Temporal threshold/DST/order/isolation behavior | Pure reducer tests; returned snapshot, no durable write |
| Named CLI Profile | Schema-valid unresolved intent only |
| Flow/prompt replacement and removal | Explicit draft source variants only |
| Trusted final completion persistence/hidden model context | Missing public contract; Issue1409 incomplete |
| Generic executable ABI/digest generation | Missing documented authoring surface; no invented ABI admitted |
| Actual CLI frontend replacement | Incomplete public CLI Application composition |
| Signed install/activation/Broker/PackVM execution | Unperformed |
| Composition/removal/renamed IDs/unselected/unknown/conflict execution | Unperformed, blocked before valid executable admission |

Pure test results are not release evidence. No legacy fallback, in-process
conformance backend, fake approval/activation or feature-specific core branch was
used to hide an integration gap. These authoring experiments are reviewable
source components and reproductions, not installable feature releases.
