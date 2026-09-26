# DeepThink profile extensions

DeepThink is an explicit execution mode, not a synonym for sending a request
to a model. It is provider-neutral and disabled by default. The user enters or
leaves the mode with `/deepthink`; Settings configures the model and delegation
policy but cannot activate the current task. Planning, generation, review, and
profile-defined phases all use the resolved model; no provider or model id is
built into DeepThink.

After planning, DeepThink evaluates the selected model against the task. It may
consider relevant past outputs, benchmarks, public reports, and tool observations
when those are available, and records missing or uncertain evidence explicitly.
The capability assessment is passed to tool/skill selection and later phases so
the chosen method can compensate for a task-specific weakness (for example,
using available computer-use tools for a visual workflow). Model-reported
sources are recorded as claims rather than automatically treated as verified.
Assessment does not install software, expose unavailable tools, or bypass the
normal approval path. When there is no accessible evidence, it says so instead
of fabricating a benchmark score or claiming to have inspected prior work.

DeepThink reads contributions only from packs selected by the active resolved
profile. A pack can add a manifest at:

```text
extensions/deepthink/<extension-id>/manifest.json
```

Example:

```json
{
  "id": "regulated_research",
  "category": "deepthink",
  "version": "1.0.0",
  "enabled": true,
  "priority": 120,
  "config": {
    "discovery_tools": ["tool_search", "skill_search", "legal_lookup"],
    "phases": [
      {
        "id": "legal_review",
        "label": "法務確認",
        "prompt": "Check applicable legal constraints and cite only public rationale."
      }
    ],
    "perspectives": [
      {
        "id": "legal",
        "name": "法務視点",
        "mission": "Find material legal constraints and required safeguards."
      }
    ]
  }
}
```

The integration-planning phase sees every enabled skill in the resolved
profile and only the host-provided tool definitions for the current turn.
Model-selected ids are validated against those catalogs. Tools explicitly
selected by the caller and skills already matched for the turn are mandatory
inputs and cannot be removed by the model.

Tool calls are returned to the normal chat tool loop. DeepThink never invokes
write, browser, terminal, or integration tools directly, so existing approval,
workspace, audit, and cancellation policies remain authoritative.

A tool result, including completion of a long-running CI command, resumes the
same durable DeepThink run from its checkpoint. Scheduler, automation, monitor,
approval-followup, and other background followup turns do not start a new
DeepThink run unless the caller explicitly opts into background DeepThink.
Delegated agents also never inherit the parent's mode. A delegation must
request DeepThink explicitly and the user must enable “Allow DeepThink for
delegated agents” in Settings; both conditions are required.

Discussion is an always-loaded reasoning tool. Every Discussion phase uses the
current conversation model and has no provider-specific fallback.
