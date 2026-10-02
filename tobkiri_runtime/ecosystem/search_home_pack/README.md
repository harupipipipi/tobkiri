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
- `GET /api/models`
- `GET /api/settings`
- `GET /api/route-state`
- `POST /api/route`
- `POST /api/answer`
- `POST /api/settings/model`
- `POST /api/route-state`

The webapp reaches these endpoints through the canonical
`/api/contracts/search_home_pack/<encoded operation>` Host contract route.
The direct paths above are the pack-local operations resolved by that route;
they are not a second authority or a Host execution fallback.

## Attachment request contract

`POST /api/route` and `POST /api/answer` accept an `attachments` array with at
most one item. A text or code item carries UTF-8 `content`:

```json
{
  "id": "search-home-attachment",
  "name": "notes.txt",
  "size": 5,
  "type": "text/plain",
  "content": "alpha"
}
```

PNG, JPEG, GIF, and WebP items carry a matching base64 data URL instead:

```json
{
  "id": "search-home-attachment",
  "name": "image.png",
  "size": 8,
  "type": "image/png",
  "dataUrl": "data:image/png;base64,iVBORw0KGgo="
}
```

Text/code is limited to 120,000 UTF-8 bytes, images to 5 MiB, and the complete
request body to 7 MiB. The browser and server both validate size and type; the
server additionally verifies exact encoded size, image signatures, safe
metadata, and the one-file limit. Invalid input fails closed with
`INVALID_ATTACHMENT`.

Attachments are supported only by **Smart Resolve** and **AI Answer**. An
attached request is forced to the AI answer route so it cannot be silently
navigated or ignored. Image input also requires a model that advertises
`supports_image_input` or `supports_vision`. Full content is passed to the
authoritative `defaultspack` `blocks.chat.send` node; only attachment metadata
is written to Search Home route state. Attachment content is untrusted
reference data and cannot override system, tool, approval, ProfileLock, or
ResolvedPlan policy.

## Webapp

The editable React source lives in [webapp](./webapp). The desktop app serves
the compiled assets from [ui](./ui).

To rebuild the UI bundle:

```bash
cd tobkiri_runtime/ecosystem/search_home_pack/webapp
npm install
npm run build
```
