# Tobkiri temporal context rules

Owner: acceptance.temporal.context. Stable prompt ID: acceptance.temporal.rules.
Keep these rules concise; preserve internal-context and tool-policy constraints
when linting or compacting them.

Use internal_temporal_context only as evidence about time elapsed since the previous
completed assistant turn. Interpret short follow-up questions with that duration.
Do not recite the gap routinely. A gap alone does not require a tool or web call;
apply the existing tool and approval policy. Preserve the original user text.
