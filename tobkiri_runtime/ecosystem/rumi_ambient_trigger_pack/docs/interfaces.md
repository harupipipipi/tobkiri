# Interfaces

Backend APIs:

- `GET /api/ambient/status`
- `POST /api/ambient/monitor/start`
- `POST /api/ambient/monitor/stop`
- `POST /api/ambient/events`
- `POST /api/ambient/permissions/grant`
- `POST /api/ambient/permissions/revoke`
- `POST /api/ambient/permissions/check`

Input actions:

- `chat.message`
- `run.instruction`
- `agent.delegate`
- `defaults.console.input` compatibility alias

Gesture events:

- `pinch` + `record_audio_start` starts hold-to-record
- `pinch` + `dispatch_audio` sends reviewed ephemeral audio after explicit confirmation
- `gesture_choice` + `choice_response` sends `2`, `3`, or `4` as text
- `approval_gesture` records approve/reject intent for approval windows
