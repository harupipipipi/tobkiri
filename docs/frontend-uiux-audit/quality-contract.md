# Frontend UI/UX quality contract

Normative terms **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are intentional. Exceptions require a linked issue, scoped evidence, an owner, and an expiration date.

## 1. One truthful state model

Every asynchronous or mutable surface MUST distinguish the states relevant to its task. Common states include:

- not requested / idle;
- loading or verifying;
- empty;
- ready with fresh data;
- pending local intent;
- committed success;
- partial success;
- stale data;
- offline or local-only;
- authorization or reauthentication required;
- conflict or externally changed;
- recoverable error;
- terminal/unavailable error.

A failed optimistic mutation MUST roll back or remain visibly failed/pending. It MUST NOT continue to look committed.

A control MUST NOT be enabled unless activation has an intentional result. Production no-op buttons, links, menu items, and toggles are forbidden.

## 2. Drafts and navigation

Editable surfaces MUST derive dirty state from a known loaded revision.

Switching selection, tabs, routes, profiles, models, workspaces, browser pages, reload, close, or background refresh MUST NOT silently discard or replace a dirty draft. Provide the applicable combination of:

- Save;
- Save as override/copy;
- Discard;
- Cancel navigation;
- Compare/reload/merge conflict;
- recoverable local draft/export.

A failed Save MUST preserve value, selection, cursor, scroll, and contextual choices.

## 3. Mutations and destructive actions

Mutations MUST be single-submit and associated with the exact affected resource.

High-impact operations MUST use expected revisions, idempotency/mutation IDs, or status lookup so timeout-after-commit is not treated as safe to repeat.

Confirmation MUST explain:

- the object and action;
- irreversible or external effects;
- scope and persistence;
- what will remain;
- whether Undo/restore exists.

Failure MUST remain attached to the action with a safe explanation and the correct Retry/Cancel/repair choice.

## 4. Approval and authority boundaries

The requester MUST NOT silently approve its own request.

Approval UI MUST be backed by enforcement metadata rather than model-written prose alone. It MUST identify consequence, target, affected data, requested capability, scope, persistence, requester, expiry, risk, and settlement state.

Approval capabilities and credentials MUST NOT appear in URLs, browser storage, logs, analytics, diagnostics, screenshots, clipboard content, or untrusted DOM.

Approve/deny MUST be request-bound, revision-bound, single-use where applicable, auditable, and resistant to replay and stale settlement.

## 5. Navigation and external content

Every programmatic destination and user-generated/assistant-generated link MUST pass through one centralized parser and policy.

The UI MUST distinguish internal navigation, normal web destinations, downloads, local/private-network targets, and unsupported/custom schemes.

Untrusted remote images and embedded documents MUST NOT load merely because history or a message was opened. Trusted attachments require stable identity; other content requires validation, isolation, or explicit consent.

Unknown content blocks MUST fail closed to a safe placeholder. Raw internal JSON is not a user-facing fallback.

## 6. Keyboard and focus

Every pointer task MUST have a non-pointer equivalent unless a documented equivalent structured view provides the same result.

Focus MUST:

- enter modal and composite widgets predictably;
- remain contained only where the pattern requires it;
- never become trapped without a guaranteed exit;
- return to the exact opener or nearest surviving logical target;
- remain stable after insert, delete, move, filter, refresh, and async replacement.

Global shortcuts MUST be scoped to an explicit focused interaction context and respect native controls, contenteditable/code editors, IME composition, assistive-technology commands, platform modifiers, and key repeat.

## 7. Semantic patterns

Use one complete, documented pattern rather than a visual imitation:

- dialog / alert dialog;
- menu / menu button;
- generic popover/disclosure;
- combobox/listbox;
- tabs/tabpanel;
- grid/tree/list;
- drag-and-drop with keyboard move alternatives;
- status/live region;
- remote-control/application mode.

Do not mix roles from different patterns. Stable IDs and `aria-controls`, `aria-labelledby`, `aria-describedby`, selected/current/expanded/busy/invalid state are required where the chosen pattern calls for them.

## 8. Target size and readable content

Repeated touch/click targets SHOULD be at least 44×44 logical/CSS pixels. Smaller targets require spacing, an equivalent larger target, and tested justification.

Important state, actions, errors, labels, and counts MUST NOT rely on 8–11 px text, color, opacity, icon, animation, truncation, or hover alone.

Truncated content MUST have an accessible and operable full-content path.

## 9. Responsive and zoom behavior

Core tasks MUST remain operable at:

- 320 CSS px width;
- short viewport heights with the on-screen keyboard;
- browser zoom to 200%;
- text zoom/reflow scenarios up to 400% where applicable;
- mobile safe areas and display cutouts;
- landscape and portrait where supported.

No action may be permanently offscreen, hidden behind a fixed layer, or available only on hover.

## 10. Motion and rendering

Animations MUST respect reduced-motion preference and MUST NOT be required to understand state.

The shared policy covers entrance, layout, hover, loading, streaming, scrolling,
and decorative motion. Web surfaces MUST use `prefers-reduced-motion: reduce` to
make nonessential animation and transitions effectively instant, force automatic
scrolling to `auto`, and replace self-animating media with a static equivalent.
Flutter surfaces MUST use `MediaQueryData.disableAnimations` through the shared
motion helper; looping indicators stop, animated scroll becomes a jump, and
component transition durations become zero. Labels, live regions, color, shape,
and explicit state text MUST remain sufficient when all motion is removed.

New components MUST consume surface motion tokens/helpers rather than defining
an independent accessibility preference. Browser-extension UI MUST follow the
same CSS preference even when its current design has no animation.

Loading skeletons MUST not imply content that may never arrive indefinitely. Long operations need status, cancellation where real, timeout/recovery, and preserved context.

Images, panels, fonts, and late content SHOULD avoid layout shift. Large lists/canvases MUST have a performance strategy and remain focus-stable under virtualization.

## 11. Copy and localization

Primary copy MUST be factual, specific, and action-oriented.

Do not promise preservation, reporting, encryption, safety, completion, or synchronization unless the code can verify it. Avoid anthropomorphic reassurance and generic AI-marketing language in operational UI.

All visible and accessible strings MUST use the product locale system. Date, time, number, list, plural, and direction handling MUST be locale-aware. Raw IDs are secondary technical details, not primary labels.

## 12. Errors and diagnostics

Raw exception text MUST NOT be the primary user message. Map failures to safe actionable copy and a diagnostic reference.

Technical details MAY be available through an explicit disclosure/copy action after redaction. Secrets, private prompts/content, credentials, local paths, approval tokens, and unrestricted payloads MUST NOT enter diagnostics.

Important/actionable errors MUST persist in the related surface; a transient toast is supplementary.

## 13. Visual language and polish

Use the product token system for spacing, radius, type, elevation, color, focus, motion, and density. Avoid arbitrary one-off values without a documented need.

Hierarchy MUST come from content and layout before glow, gradient, blur, oversized radius, badges, or nested cards.

Every control MUST define default, hover, focus-visible, pressed/selected, disabled, loading, success, warning, and error states as applicable.

## 14. Required evidence

A remediation PR MUST include the evidence applicable to the change:

- before/after screenshots or recording;
- keyboard walkthrough;
- accessibility-tree or screen-reader result;
- narrow/zoom/large-text result;
- reduced-motion/high-contrast result;
- failure/offline/conflict result;
- automated tests;
- threat-model or redaction evidence for trust boundaries.

A screenshot of the happy path alone is not completion evidence.
