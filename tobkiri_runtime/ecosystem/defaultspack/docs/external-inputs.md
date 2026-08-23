# External Inputs

External inputs are messages that enter Rumi from systems outside the local UI:
webhooks, chat platforms, automation callbacks, tunnels, local scripts, or
future connectors. They all use the same framework boundary:

```text
provider payload
  -> ExternalEvent
  -> AudiencePolicy
  -> InputProfile
  -> dispatch_input / submit_input
  -> ResponsePromptPolicy
  -> ResponsePlanner
  -> ResponseAdapter
```

The goal is to keep provider details at the edge. Chat, agent, and flow logic
should receive normalized input, not Slack, Discord, LINE, or tunnel-specific
payloads.

## Core Types

`ExternalEvent` is the normalized inbound record. It contains stable fields:
`provider`, `workspace`, `scope`, `actor`, `conversation`, `event`, `payload`,
`verified`, and redacted `metadata`. Provider-specific identifiers are absorbed
into those principals. Raw request bodies may be used for signature checks, but
raw secrets and token values are never exposed in the event object returned to
UI, logs, or docs.

`AudiencePolicy` decides whether an event is allowed to enter Rumi. Policy can
gate by provider, team, channel, user, mention style, direct message status,
rate limit, or required verification. Policy output is explicit: `allow`,
`ignore`, `deny`, or `needs_approval`.

`InputProfile` maps an allowed event to a `RumiInputEnvelope`: role, input text,
chat external key/title/model, source metadata, params, and tools. It performs
transformation only; it does not decide whether an event is allowed.

Input and output configuration are separate. Input profiles answer "what came
in and how should it enter chat?". Output profiles answer "where can a response
go and through which transport?". Built-in input templates exist for LINE,
Discord, Slack, and generic webhooks; custom templates can be registered through
`/api/external/templates` or placed in `user_data/shared/external_io_templates`.
Built-in templates expose `setup_mode: copy_paste_select`: the UI renders
template/profile/provider choices plus copyable route paths and paste-only token
or target fields. Free-form YAML/profile editing belongs in Custom.
For webhook providers such as LINE, Slack, and Discord interactions, the
External Input panel includes a Temporary Public URL launcher. The Cloudflare
Quick Tunnel button creates a temporary public URL for the selected route path,
for example `/api/integrations/line/webhook`, so the user can paste the full URL
into the provider dashboard.

The Setup Flow and Audience Policies summaries follow the selected built-in
provider/template. The editable Local Tobkiri URL defaults to the active
`DEFAULTS_HTTP_HOST` and `DEFAULTS_HTTP_PORT`; an untouched legacy
`http://127.0.0.1:8766` default is migrated to that runtime address, while an
explicitly edited URL is preserved.

`submit_input` is the compatibility entrypoint after profile transformation.
Internally it now forwards to `dispatch_input`, which routes a
`RumiInputEnvelope` by `delivery.action_id`.

`ResponsePlanner` converts the runtime result into a provider-neutral response
plan. It decides whether to reply, acknowledge only, defer, split, truncate, or
skip.

`ResponsePromptPolicy` is an optional planning-only layer before the planner.
It can choose actions such as `reply_text`, `store_only`, `run_browser_use`,
`run_python`, or `ask_for_approval`, but it only returns a decision object.
Tool execution still goes through the normal tool policy, approval, and turn
runner paths.

`ResponseAdapter` renders and delivers that plan through a provider-specific
surface such as a Slack thread, LINE reply token, Discord interaction response,
or generic webhook response.

Default input templates set `include_source_context: true`. Rumi tells the turn
runner that an input came from LINE, Discord, Slack, or another provider before
the user's text, while keeping raw tokens and request secrets out of the prompt.

## Event Contract

Example normalized event:

```json
{
  "provider": "line",
  "workspace": {
    "type": "line_destination",
    "id": "destination-id"
  },
  "scope": {
    "type": "group",
    "id": "C123"
  },
  "actor": {
    "type": "user",
    "id": "U123"
  },
  "conversation": {
    "type": "external",
    "id": "line:group:C123"
  },
  "event": {
    "id": "evt_01",
    "message_id": "msg_01",
    "type": "message",
    "message_type": "text"
  },
  "payload": {
    "type": "message"
  },
  "verified": true,
  "metadata": {
    "reply_token": "short-lived-provider-handle"
  }
}
```

Short-lived provider reply handles are kept in metadata for adapter use. They
must not be treated as long-lived configured tokens or displayed back to UI.

## Processing Rules

1. Verify the request before parsing trust-sensitive fields.
2. Normalize provider payloads into `ExternalEvent`.
3. Drop duplicates using `provider + event_id`.
4. Evaluate `AudiencePolicy`.
5. Select `InputProfile`.
6. Call `submit_input`.
7. Optionally run `ResponsePromptPolicy` to produce a safe action decision.
8. Run `ResponsePlanner`.
9. Deliver through `ResponseAdapter`.

If any step rejects the event, the adapter should return the provider-expected
acknowledgement without creating a chat message.

Prompt-routed response actions are planning-only: a `response_prompt` may return
a `ResponsePlan` decision, but external delivery still passes through the
adapter path, where allowed actions, sensitivity, capabilities, and approval
requirements are checked again.

## Local First Boundary

External input support does not make the local runtime public by default. The
gateway and HTTP transport bind to loopback unless configuration explicitly
allows otherwise. A public URL provider is just a replaceable edge component.
Cloudflare Quick Tunnel can be used during development, but it is not part of
the core architecture and must remain swappable with another tunnel, reverse
proxy, or platform ingress.

## Built-In Setup Shape

The built-in UI is intentionally a guided setup, not a YAML editor:

- `External Input`: choose provider/template/profile, generate or copy the
  webhook URL, then choose the default response behavior.
- `External Output`: choose the send mode and output template, paste masked
  external tokens, and paste non-secret target ids such as Discord `channel_id`.
- `External Custom`: register or drop custom templates/profiles, and keep
  free-form response prompts such as computer-use browser workflows.

LINE uses a provider-created webhook URL plus `Channel Secret` verification and
`Channel Access Token` replies. Discord has two outbound modes: `Bot + Channel`
uses a bot token and `channel_id`, while `Webhook URL` uses the channel webhook
URL as a masked external token. Slack uses the Events Request URL, Signing
Secret, Bot Token, and thread-aware `chat.postMessage`.

## Safety Notes

- Webhook endpoint management and public URL creation routes are treated as
  local-admin sensitive routes and require the local auth guard.
- External inbound webhook routes remain externally reachable, but each endpoint
  is expected to enforce provider signatures or shared-secret verification.
- Newly created generic webhook endpoints default to disabled + shared_secret
  unless explicitly configured otherwise.
- Cloudflare Quick Tunnel is only a swappable public URL provider. It is not a
  security boundary; endpoint security and local-admin route guards remain
  required.

## Known Limitations

- LINE and Discord adapters in this PR are MVP text-response adapters, not
  complete production bot implementations.
- LINE non-text messages are normalized into placeholder text for now.
- Discord interaction handling is intentionally minimal; full deferred/follow-up
  interaction behavior should be handled in a follow-up PR.
- Cloudflare Quick Tunnel is only a swappable public URL provider. It should not
  be treated as the security boundary; endpoint security and local-admin route
  guards remain required.

## Current Defaultspack Routes

Current integration routes are provider-specific adapters that should converge
on the framework boundary above:

| Route | Purpose |
|---|---|
| `POST /api/integrations/slack/events` | Slack Events API intake |
| `POST /api/integrations/line/webhook` | LINE Messaging API webhook intake |
| `POST /api/integrations/discord/interactions` | Discord interaction intake |
| `POST /api/integrations/discord/events` | Discord message event intake |
| `GET /api/integrations/secrets` | Secret status only |
| `POST /api/integrations/secrets` | Set or clear write-only secrets |
| `GET /api/external/tokens` | API-key-like external token status |
| `POST /api/external/tokens` | Upsert, rename, or delete named external tokens |
| `GET /api/external/templates` | List built-in and custom input/output templates |
| `POST /api/external/templates` | Register a custom input or output template |
| `POST /api/webhooks/inbound/{webhook_id}` | Generic webhook intake |
| `GET /api/webhooks/endpoints` | List webhook endpoint configs |

## Localhost Input Endpoints

AI-created inbound endpoints use `input_endpoint_create` and return only
localhost URLs:

```text
http://localhost:{port}/api/webhooks/inbound/{endpoint_id}
```

These endpoints require a shared secret and default TTL protection. Public
Cloudflare or tunnel URLs remain a separate concern.
