# Handwritten Route Inventory

Last updated: 2026-07-10

This inventory covers route families still coordinated by `PackAPIHandler` or
its API mixins after manifest matching. The transport-wide rate limiter and
authentication gate continue to run before these branches. Cookie-backed panel
mutations also retain the existing origin and CSRF checks.

| Family | Current location | Auth / principal | CSRF / origin | Rate limit | Audit owner | Replacement | Legacy until |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Health, root redirect, static mounts | `pack_api_server.py` GET transport helpers | route-specific / anonymous where declared | CORS and mount policy | default per path | runtime.transport | web mount and pre-auth manifests | transport-owned |
| Setup and OAuth bootstrap | `pack_api_server.py` pre-auth helpers | pre-auth bootstrap principal | setup origin policy | default per path | runtime.setup | core setup functions | v2.4 |
| Auth tokens and whoami | `pack_api_server.py` verb handlers, `api/auth_gate.py` | panel or bearer; core principal for token administration | required for panel cookie mutations | default per path | runtime.auth | core auth manifest functions | v2.4 |
| Authority requests and grants | `pack_api_server.py` verb handlers | panel or bearer; route authorizer principal | required for mutations | default per path | runtime.authority | core authority manifest functions | v2.4 |
| Pack scan, import, approval, and status | `pack_api_server.py` verb handlers | core principal | required for mutations | default per path | runtime.pack_management | pack-management functions | v2.4 |
| Network grants | `pack_api_server.py` POST branches | authenticated principal | required | default per path | runtime.network | capability functions | v2.4 |
| Secret values and grants | `pack_api_server.py` GET/POST/DELETE branches | authenticated principal with secret policy | required for mutations | default per path | runtime.secrets | secrets capability functions | v2.4 |
| Stores, units, and shared stores | `pack_api_server.py` POST branches | authenticated principal with workspace policy | required | default per path | runtime.store | store and unit functions | v2.4 |
| PIP and capability requests/grants | `pack_api_server.py` GET/POST branches | core principal / capability principal | required for mutations | default per path | runtime.capability | capability lifecycle functions | v2.4 |
| Containers and privileges | `pack_api_server.py` POST/DELETE branches | core principal with approval checks | required | default per path | runtime.privilege | container and privilege functions | v2.4 |
| Flow execution and route reload | `pack_api_server.py` POST branches | core principal | required | default per path | runtime.flow | flow capability and route administration functions | v2.4 |
| Defaultspack compatibility HTTP routes | `ecosystem/defaultspack/transport/registry.py` | metadata-resolved local transport policy | local origin; CSRF for sensitive mutations | transport default | per-entry owner in `legacy_http_routes.yaml` | declared `function_id` or `none` | metadata-resolved |

The chat-channel family is no longer in the compatibility row. Its former
legacy-manifest declaration is historical only; the v4 integrity boundary
reads the finite Pack and executable catalogs and does not dispatch from that
projection.
