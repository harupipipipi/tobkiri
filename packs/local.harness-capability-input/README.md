# Capability projection input Pack

This Pack is the typed request entry for the capability projection Profile
Flow. It bounds the UTF-8 payload and passes the original string as the
`payload` member of a finite JSON request frame. It does not parse or rewrite
the payload, interpret projected targets, or approve a request.

The Flow dispatches to the separate
`local.harness-capability-projection-python` Pack. Keeping the request entry
and projection in separate components avoids re-entering the same component
while its `submit` operation is active. The final projection call is the
ordinary Flow completion value; explicit `return` steps are reserved for
overlays by the current Flow contract.

This source does not establish the complete dynamic-host or frontend contract,
nor does it prove GUI wiring. Existing Core bindings, readiness checks, Gate
verification, and the dedicated Setup review remain responsible for the
actual request permissions and approval. This Pack adds no terminal or user
document access and holds no resources or authority.
