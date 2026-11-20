# OpenAI-compatible provider

`config.json` is the approved provider configuration. Existing configurations
remain valid. `base_url` accepts an absolute HTTP or HTTPS URL without credentials,
query, fragment, whitespace or backslashes. An origin URL uses `/v1`; an explicit
path such as `/v1` or `/custom/api` is kept as the API prefix. Trailing slashes are
removed before appending `/chat/completions`. Network access still requires the
exact origin granted in the Pack and Profile permissions.

`model`, `secret_name` and `timeout_ms` retain their existing meanings. An optional
`temperature` accepts a number or a decimal string such as `"0.4"`, with finite
values from 0 through 2, and defaults to 0. Use the string form for fractional
values in canonical Pack settings; the API request always receives a JSON number.
Invalid strings and explicit `null` are rejected.
The prompt's positive `max_tokens` value is passed to the API without the former
512-token cap; zero retains its one-token fallback. The upstream model may reject
unsupported settings, and existing host resource limits still apply.

A Pack using the saved-model route can set `{"registered_models":true,"timeout_ms":20000}`.
The Profile binds `local.model-registry.routing@1.0` and
`local.provider-registry.routing@1.0`. In this mode the prompt's model field
selects a saved model registration or alias. Both lookups use Core's live
Profile. The request uses the saved model ID and the lower of the requested
and saved token ceilings. The provider must be a keyless loopback connection
with the usual Core network grant for its exact origin.

HTTP failures include the status and bounded API message, type, code and parameter
(or non-JSON response text). At most 4096 response bytes are read and the displayed
summary is limited to 512 characters. Authentication/permission failures omit
upstream messages that may echo rejected keys. Other error text removes control
characters and redacts common Bearer, `sk-`, `api_key=` and `api-key=` patterns.
This is diagnostic text sanitization, not detection of every possible arbitrary
secret format. Error-body reads use the existing network deadline and cancellation;
complete JSON errors return without waiting for more response chunks.

HTTP failures and SSE API error frames publish one existing `chat-event` with
`kind: "error"`, the sanitized diagnostic in `text`, and null token fields.
The next guest pull returns `Unavailable` and releases the stream through its
usual cleanup path. Core retains its existing generic component-failure facade
for pull errors; the chat notification carries the diagnostic rather than Core
error or audit text. Previously decoded deltas precede the error event; each pull keeps
its requested batch limit. Cancellation takes priority and releases the stream
without a diagnostic event. Successful SSE output is unchanged.

Successful v1 generations still carry text and usage only.

Building with `--features structured-chat` exports both the existing 1.0 provider
port and the 2.0 port. A test Pack explicitly declares both ports and applies the
same saved routing and origin permissions. The 2.0 port translates structured
messages and JSON tool schemas to Chat Completions, and streams reasoning, tool
fragments, usage, and a terminal reason. It requires a finish reason followed by
`[DONE]`. The provider forwards tool calls as data. Flow dispatch and durable
structured history require their own integration. The shipped Pack declares 1.0.
