# Structured Harness chat contract, 2.0

This standalone candidate retains the 1.0 port topology but uses a new major
version for structured messages and events. The 1.0 source and installed Packs
remain unchanged; cross-major use requires an explicit adapter. The shape files
alone register no Pack, provider, Flow, store, UI, or product route.

Parts preserve the order of text, reasoning, complete tool calls, and tool
results. Each tool result references the ID of its corresponding call.
Tool definitions include a JSON input schema and describe available tools; they
do not grant authority. JSON fields use the core JSON alias and must contain
valid JSON at the boundary. Tool delta `arguments` is instead a text fragment:
consumers concatenate fragments for each index and validate the final JSON.
Indexes are scoped to a response; IDs bind completed calls to later results.
Events retain provider order. Usage is optional per counter, so an unknown count
is distinct from zero, and cached tokens are part of prompt usage when reported.
A completed assistant message carries a response summary with the finish reason
and any reported usage. User and tool messages omit that summary.
A finish event is terminal; a stream that errors or ends without one is not a
successful completed turn. Runtime implementations must define bounds, handle
cancellation and enforce the existing authority and approval paths.

The `model-response` port starts the prompt with loaded history, assembles
fragments only after the provider finishes, and joins results to matching call
IDs. `local.harness-turn` provides these operations. A Flow binds an explicit
tool provider and controls dispatch and iteration limits. Tool names and
arguments from the model remain data throughout the response operations.

Adoption requires implementing and declaring version 2 ports and sidecars,
wiring a version 2 Flow, translating provider-specific streams, and updating
prompt, output, input and durable storage. The conversation store appends the
input and the ordered assistant and tool messages atomically after a successful
turn. The `structured-chat` build of `local.harness-conversation-store` uses the
same private SQLite store and writes the structured history and 1.0 text view in
one Core transaction. Both versions read older text rows. The opt-in Flow test
reopens the store and uses the structured history in the next prompt. The
shipped 1.0 UI continues to render its text stream and history. A 2.0 UI route
must handle the additional structured events and the final completion status.
