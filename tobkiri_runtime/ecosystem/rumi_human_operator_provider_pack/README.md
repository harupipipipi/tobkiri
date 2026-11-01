# Rumi Human Operator Provider Pack

This pack replaces the defaultspack human provider with an exact routing-key
provider. It emits an approval-required handoff intent containing identifiers,
not copied conversation context. It cannot approve the request, grant
authority, write conversations, or open/control UI.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including exact routing-key precedence, intent
binding, approval handoff, stream normalization, missing conversation, pack
removal, and confirmation that no authority or conversation write occurs.

