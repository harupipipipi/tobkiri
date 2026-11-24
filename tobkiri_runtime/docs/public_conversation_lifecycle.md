# Public completion timing and internal context

The existing public SDK is `tobkiri_protocol.conversation_lifecycle`. It exports
`completion_source`, `task_gap_context`, `active_task_gap_context`, and
`task_gap_prompt`. These are dependency-free projections; they do **not**
authenticate an arbitrary mapping supplied by a caller.

Obtain the conversation through the selected, captured public Contract
`tobkiri.resource.conversation.v1`, Operation
`rumi_conversation_store_pack.conversation-resource`, using `operation: get`,
the captured `profile_id`, and exact `conversation_id`. This resource binds
its store root and Profile at Host capture. No ambient file path or caller
completion flag is accepted as a trust source.

The lifecycle owner is `rumi_conversation_store_pack`. Its durable conversation
snapshot carries `lifecycle.version: tobkiri.conversation-lifecycle.v1` and
completion/receipt fields. A saved assistant append is only a completion
candidate. The captured completion Contract confirms it against the public
durable turn resource, saved input/outcome receipt, caller identity, active
user message and current conversation node. Waiting, tools, cancellation,
failed work and stale parallel terminals cannot supply a successful completion.
The confirmed timestamp survives process restart in the owner store.

On the next accepted user append, the owner records the runtime receipt time
and prior confirmed completion in the same transaction. For a gap of at least
3,600 seconds, `active_task_gap_context` emits offset-bearing UTC timestamps
and elapsed seconds. Smaller or negative gaps emit nothing. A new confirmed
completion updates the baseline; edits/branch changes/reset retire invalid
completion candidates. Duration uses UTC milliseconds, not model inference.

The canonical saved-turn bridge already reads this owner-bound snapshot and
places `task_gap_prompt` in a separate `role: system` model message. The user
message stays unchanged. This conveys timing information, never approval,
permissions, or an obligation to call tools. The Defaults request builder has
the corresponding internal-context projection for its request path.

The independent temporal Pack in `acceptance/independent_extensions` remains a
pure reducer of explicit inputs. Its selected Workflow source can be compiled
against the active exact operation palette, but caller-supplied reducer events
are not authenticated lifecycle evidence. Its own durable snapshot and delivery
into the saved-turn bridge have not been connected. Do not replace the existing
trusted owner path with a second timestamp store or claim reducer tests prove
native model delivery.

Regression coverage exists in `tests/test_conversation_completion_confirmation.py`,
`tests/test_conversation_lifecycle_saved_boundary.py`, and
`tests/test_conversation_lifecycle_pack.py`: exact 59m59s/60m00s boundaries,
durable owner settlement, stale/cancelled/waiting evidence, resumed user receipt,
unchanged user text, internal system message, and timezone/DST duration.
These isolated tests do not establish Launcher/Host/VM live acceptance.
