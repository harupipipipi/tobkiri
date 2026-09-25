# Search Home Pack

`search_home_pack` is a Startup Profile surface pack that gives Tobkiri a local
search-first home screen while reusing `defaultspack` for AI classification,
chat responses, and web search.

## What It Owns

- localhost browser UI
- deterministic URL safety checks
- route selection for URL, Google, AI, and AI-with-search
- cross-pack function bridge to `defaultspack`

## Route Privacy

Search queries, candidate URLs, and route decisions remain in the current
Search Home component memory. They are not sent through page-wide
`postMessage`, copied to Browser Companion, written to browser storage, or
restored from a local route-state file. Reloading or closing the surface drops
the review state. Startup removes legacy browser, extension, and backend route
records created by older releases.

Browser Companion route synchronization is deliberately unavailable until a
dedicated authenticated, least-data channel with an explicit user-visible
retention contract exists. Destination review and manual navigation continue
to work without that integration.

## What It Does Not Own

- model runtime settings
- provider configuration
- model routing internals
- provider key storage
- web search implementation

Those stay inside `defaultspack`, which remains the source of truth.

## Local Endpoints

- `GET /health`
- `GET /api/health`
- `GET /api/models`
- `GET /api/settings`
- `POST /api/route`
- `POST /api/answer`
- `POST /api/settings/model`

## Webapp

The editable React source lives in [webapp](./webapp). The desktop app serves
the compiled assets from [ui](./ui).

To rebuild the UI bundle:

```bash
cd tobkiri_runtime/ecosystem/search_home_pack/webapp
npm install
npm run build
```
