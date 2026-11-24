# Voice input and TaskPet acceptance

## Composer voice input

PR #1311's reviewable dictation is adapted to the current shared composer. It
uses the existing `startPinchAudioRecorder` and narrow transcription route.
The original draft and selection are retained until an explicit Insert at
cursor / Append / Replace draft action. Recording does not submit or attach
audio automatically. Start first reads Tobkiri microphone permission; no grant
call is made. OS permission is requested only by the explicit start action.

Cancel, profile/conversation change (`voiceScopeKey`), hidden page, device
change, unmount, and a newer recording invalidate delayed capture/transcription
replies. A delayed recorder is immediately stopped. Pending audio locks sending
and the draft; an external draft change cannot be overwritten on insertion.

With the actual integrated app, verify: existing draft with a selection; review
and edit the transcript; each insertion mode; discard during permission prompt
and transcription; retry after denied microphone, silence, network failure;
switch conversation/Profile; close the screen; cancel twice; start twice.
Observe that no draft/attachment mutation happens before explicit insertion and
no late capture remains active. Only the human may grant required microphone
permission. Unit fixtures are separate from microphone/native acceptance.

## TaskPet

`TaskPet` takes `profileId`, `scope` (Profile/conversation/turn), and the existing
versioned `SavedTurnEventSnapshot`, plus `snapshotProfileId` captured when the
request began. The App owner retains the last authoritative
snapshot for the selected saved turn and resets it on Profile/conversation
change. A `completed` snapshot requires matching turn/request/revision binding
and a result receipt with outcome digest. A boolean generation flag, UI idle,
stream end, a timer, or a raw exception cannot produce success.

Run `/pet` in the composer or select its slash-command candidate to open or
restore the independent companion window. This frontend command is consumed
before chat submission, steering, or model API-key checks and never sends the
command to the AI. `//pet` remains escaped literal chat text. The chat has no
permanent pet or notification launcher controls. The mounted `TaskPet` publisher
still synchronizes the bounded task projection and observes authoritative
completion receipts; the child is a separate native Shell window or browser
popup. Native hide/reopen retains that window and its position for the current
Shell lifetime. Closing the main Shell still exits the companion too.

Pet completion/failure notifications are configured in Settings → Features →
Tobkiri pet, using the current runtime Profile rather than a model Profile. This
setting is distinct from chat error-banner notifications. Moving the control does
not change existing opt-in values or request permission. Only explicit enable
requests notification permission. Import `components/TaskPet.css` in the central
frontend stylesheet. The asset under `public/pet/` comes from PR #1326; no remote
image request is added.

Notifications start disabled per Profile. The explicit notification button
requests OS/browser permission, then saves opt-in; globally granted notification
permission by itself is insufficient. Notices omit prompt/error text, dedupe the
exact selected turn, and require an observed active-to-authoritative-terminal
transition. Historical replay, cancellation, idle, crossed scope, stale revision,
or an approval overlay never notifies. Hidden pages stop visual animation and
reduced motion removes motion.

Verify running/approval wait/cancel/failure/success, switch Profile/conversation
while running, replay old results, hide/show, deny notifications, storage failure,
notifications off, background completion, and approval-modal keyboard focus.
The standalone state tests do not prove integrated/native notifications.

## Status distinction

Reviewable composer voice input has source and unit/contract coverage. The
conversation voice agent from PR #1493 has staged source only and is unavailable:
installation/activation and end-to-end capture/STT/TTS/dispatch are unverified.
No generated release/migration proof was changed to claim acceptance.
