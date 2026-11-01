# Tobkiri Agent State Store Pack

Owns agent profiles, run lifecycle, planning/tool limits, subagent parent-child
links, guidance, cancellation, handoff references, and bounded audit events. It
does not invoke AI, tools, connectors, Company, or UI. Every transition is
revision-bound and consumes an exact authority receipt.


New runs atomically capture an owner-written `agent_profile_snapshot` and
`agent_definition_revision`. Later definition edits affect new runs, not an
already admitted run's prompt, model/tool references, limits, or subagent policy.
A new child captures its own current definition; the parent's saved snapshot
controls its child limit. These settings do not grant Host/tool permissions.

Runs created before this feature retain the existing current-definition behavior
and are never assigned a fabricated historical snapshot. This is a bounded
revision-isolation repair, not the full Flow-backed Agent / Human / MoA feature.
