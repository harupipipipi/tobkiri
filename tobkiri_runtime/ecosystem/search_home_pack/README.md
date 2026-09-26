# Search Home Pack

`search_home_pack` is a Startup Profile surface pack that gives Rumi a local
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
npm run build
```

### Candidate-review keyboard controls

Route shortcuts are intentionally local to the visible candidate-review card.
Move focus to that card, then use Left Arrow or Right Arrow to change the
visible candidate. Press Enter as a separate confirmation to open the exact URL
shown in the card. Inputs, file and model controls, dialogs, answer content,
contenteditable regions, IME composition, repeated keys, and every modified key
combination are ignored.

Browser, operating-system, and assistive-technology shortcuts vary by platform.
Search Home does not override any Alt-, Control-, Command-, or Shift-modified
command. A restored decision must still be fresh and must be explicitly
reviewed in the current session before its review-card shortcuts are enabled.
The browser companion may retain origin-bound route state for compatibility,
but it does not install route hotkeys or navigate from that retained state.
