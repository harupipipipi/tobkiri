# Rumi Connector OAuth Broker Pack

Coordinates an OAuth authorization-code flow without implementing vendors or
network exchange itself. Begin is receipt-gated; callback consumes a hashed
state and PKCE verifier exactly once. The code and exchanged secret material
are never persisted by this pack or returned to callers.

An exact selected provider performs URL preparation and exchange. The broker
stores the result through the credential owner, then binds only its opaque
handle to the connector registry with a second internal receipt. If binding
fails, the newly created handle is revoked.

