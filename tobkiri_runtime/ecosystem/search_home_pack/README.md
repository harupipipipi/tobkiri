# Search Home Pack

`search_home_pack` is a Startup Profile surface pack that gives Tobkiri a local
search-first home screen while reusing `defaultspack` for AI classification,
chat responses, and web search.

## What It Owns

- localhost browser UI
- deterministic URL safety checks
- route selection for URL, Google, AI, and AI-with-search
- cross-pack function bridge to `defaultspack`

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
- `POST /api/route`
- `POST /api/answer`

The control-panel frontend reaches these operations through the captured
Search Home Host contract route. It does not discover or invoke a legacy Pack
endpoint directly.

## Failure Recovery

Routing and answer failures remain next to the search box with the backend's
human-readable cause plus Retry and Dismiss actions. Retrying uses the exact
failed query, action, and model snapshot; the current input and attachment are
not cleared.

Catalog and saved-model failures remain distinct inside the model control and
can be retried together. If saving a preferred model fails, Search Home restores
the previous selection, marks the attempted selection as unsaved, and offers a
targeted save retry. Error text is bounded and redacted before it is rendered so
credentials from provider or transport diagnostics are not exposed.

## Webapp

The editable React source lives in [webapp](./webapp). The desktop app serves
the compiled assets from [ui](./ui).

To rebuild the UI bundle:

```bash
cd tobkiri_runtime/ecosystem/search_home_pack/webapp
npm install
npm run build
```
