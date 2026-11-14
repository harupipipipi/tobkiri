# Structured Harness chat contract, 2.0

This standalone candidate retains the 1.0 port topology but uses a new major
version for structured messages and events. The 1.0 source and installed Packs
remain unchanged; cross-major use requires an explicit adapter. The shape files
alone register no Pack, provider, Flow, store, UI, or product route.

Parts preserve order and identify complete tool calls and their results by ID.
Tool definitions include a JSON input schema and describe available tools; they
do not grant authority. JSON fields use the core JSON alias and must contain
valid JSON at the boundary. Tool delta `arguments` is instead a text fragment:
consumers concatenate fragments for each index and validate the final JSON.
Indexes are scoped to a response; IDs bind completed calls to later results.
Events retain provider order. Usage is optional per counter, so an unknown count
is distinct from zero, and cached tokens are part of prompt usage when reported.
A finish event is terminal; a stream that errors or ends without one is not a
successful completed turn. Runtime implementations must define bounds, handle
cancellation and enforce the existing authority and approval paths.

Adoption requires implementing and declaring version 2 ports and sidecars,
wiring a version 2 Flow, translating provider-specific streams, and updating
prompt, output, input and durable storage. The conversation store must retain
structured parts and only commit a successful turn atomically; old stored text
and 1.0 consumers require an explicit migration or adapter. The 1.0 `done`
contains complete text whereas 2.0 `finish` contains a reason; adapters must
accumulate text and decide how to handle unrepresentable structured data.
