# Tobkiri Search

Open `/search/` on the running Tobkiri Defaults localhost server. Search uses
the same authenticated application, provider registry, model catalog and model
settings as Defaults. No separate provider pack or Search credentials are needed.

The initial action is Google search. You can also ask the selected AI model or
review a website/URL before opening it. AI answers use the model's knowledge;
use Google for current web information. Each AI request is a single question,
without saved conversation state or automatic tool execution.

Search's finite contract endpoint is `POST /api/search/answer`, exposed through
the shared `/api/contracts/defaultspack/` namespace. The Host binds the active
Profile and dispatches to `rumi_ai_gateway_pack` and
`rumi_provider_adapters_pack`. Model discovery and preferences use the existing
Defaults model-search and model-state contracts.

Credential-free local OpenAI-compatible providers may use an explicitly
registered literal-loopback endpoint such as `http://127.0.0.1:18080/v1`.
In Defaults' Provider setup, select OpenAI-compatible, enter that local URL,
and leave the API key empty. The existing prepare/approval/resume flow owns
the connection write; then register the model ID in the shared model settings.
Their requests retain the existing Broker lease, cancellation, revocation and
durable audit checks. Cloud providers retain their credential transport.

The editable React source is in [webapp](./webapp). The build copies its three
assets into Defaults' owned `ui/search` directory and verifies byte parity:

```sh
cd tobkiri_runtime/ecosystem/search_home_pack/webapp
npm ci
npm test
npm run lint
npm run build
```

The historical `desktop_app.py` and its legacy `/api/route` and `/api/ask`
server are not used by this integrated surface. Start the canonical packaged
Defaults runtime for `/search/`; a verified development bundle and active
Profile are required by the normal runtime bootstrap.
