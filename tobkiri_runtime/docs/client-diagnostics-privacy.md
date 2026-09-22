# Client diagnostic privacy

Tobkiri client diagnostics use the versioned `rumi.client_diagnostic.v2` schema. Remote reporting is opt-in. A new browser profile defaults to `local_only`, which makes no diagnostic network request.

Users can choose the mode under **Settings → Diagnostics → Client diagnostic privacy**:

- `local_only`: keep recovery information on the current device and do not send diagnostics.
- `standard`: send the redacted, allowlisted schema shown in the Settings preview. Server records use short retention.
- `disabled`: do not prepare or send global client diagnostics.
- `private`: a session-level policy that prevents reporting even if the user enabled `standard` mode.

The browser preference is an upper privacy boundary. A caller or session may make reporting stricter, but cannot override `local_only`, `private`, or `disabled` to enable remote reporting.

## Public schema

The client sends only opaque event, session, context, and fingerprint identifiers plus normalized source, category, level, a bounded redacted message, and these optional detail fields:

- `error_name`
- `error_code`
- `route`
- `line`
- `column`
- `stack`
- `component_stack`
- `reason_type`
- `http_status`
- `frame_count`

Arbitrary nested detail, prompts, messages, tool arguments/results, provider payloads, headers, attachments, and source content are not part of the public schema. URLs, credentials, emails, local paths, auth headers, and long opaque values are redacted. Stacks retain only bounded application frames.

The backend independently rejects unknown fields, unsupported schema/privacy modes, oversized payloads, and rate-limit excesses, then redacts and normalizes the allowlisted fields again before writing a short-retention audit record.

The Settings preview is synthetic and contains no current error or user data. It is displayed before its copy action so users can inspect the exact public shape before opting in or exporting the example.
