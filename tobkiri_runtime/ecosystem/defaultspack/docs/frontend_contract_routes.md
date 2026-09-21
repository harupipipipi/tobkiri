# Defaultspack frontend route contract

The defaultspack web application addresses Host-owned HTTP operations through
one canonical endpoint:

```
/api/contracts/defaultspack/<url-encoded "METHOD /api/...?...">
```

`src/lib/api.ts` is the frontend API map.  A call site creates a
`DefaultspackContractRoute` with `defaultspackContractRoute("api/...")` and
passes that value to `defaultspackApiFetch`.  The browser therefore never
chooses an implementation handler directly.  Configuration values that need a
stable path key use `defaultspackCanonicalRouteKey`, while execution still
uses the typed route object.

The Host decodes the operation in
`core_runtime/frontend_contract_routes.py`, validates the method, path, query,
and registered API family, and then sends the resolved target through the
ordinary `PackAPIHandler` route tables.  Authentication, CSRF, local guards,
profile/pack approval, and handler authorization are consequently applied
after resolution as well.  A malformed token, traversal path, method
mismatch, recursive contract path, or unknown operation returns a structured
error and never falls through to a legacy URL.

Adding a frontend operation requires a registered Host/defaultspack route and
a typed API-map call.  Do not add a raw `/api/...` fetch, `sendBeacon`, or
action endpoint; use the contract route helper so tests can assert the exact
operation and the Host can fail closed.
