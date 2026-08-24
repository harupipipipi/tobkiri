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
- `POST /api/ask`

## Webapp

The editable React source lives in [webapp](./webapp). The desktop app serves
the compiled assets from [ui](./ui).

To rebuild the UI bundle:

```bash
cd tobkiri_runtime/ecosystem/search_home_pack/webapp
npm install
npm test
npm run lint
npm run build
```

## Destination security boundary

Backend route results, restored session state, Browser Companion messages, and
redirect targets are untrusted. Search Home applies the same deterministic
destination policy before persistence, display, candidate selection, and final
navigation:

- only absolute HTTP(S) URLs are recognized;
- malformed, credential-bearing, control-bearing, local, private, reserved,
  file, and custom-scheme destinations fail closed;
- the normalized host is derived from the parsed URL, never from backend
  display text;
- HTTP, internationalized hostnames, and cross-origin redirects require an
  explicit review action; Browser Companion hotkeys never confirm them;
- blocked routes keep the query and review state in memory while Copy details
  emits only the bounded policy reason, not the hostile raw destination.

The backend additionally pins each probe connection to DNS answers that were
validated as public and revalidates every redirect hop. These checks remain in
the Search Home pack and Browser Companion boundaries; they do not add a host
fallback or bypass Pack Architecture v4, ProfileLock/ResolvedPlan, Authority
Kernel, or PackVM decisions.
