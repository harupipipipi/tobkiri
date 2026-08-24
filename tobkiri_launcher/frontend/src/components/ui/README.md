# Shared control contracts

These primitives are the accessibility boundary for Tobkiri Launcher forms and
actions. Callers must preserve these contracts when composing them into a
management surface.

## `Switch`

- Supply exactly one localized accessible-name relationship with `aria-label`
  or `aria-labelledby`.
- The primitive uses native button activation. Pointer activation, Enter, and
  Space therefore converge on a click instead of duplicating keyboard logic.
- A caller `onClick` runs before `onCheckedChange`. Calling
  `event.preventDefault()` cancels the owned checked-state transition. Other
  caller handlers do not replace the primitive's activation behavior.
- The visual track remains 44 by 24 pixels inside a minimum 44 by 44 pixel
  activation target.

## `Input`

- Caller-provided IDs remain authoritative. Otherwise React `useId()` supplies
  a stable unique ID that does not depend on visible or localized label text.
- Helper, error, and caller-provided descriptions are composed into one valid
  `aria-describedby` relationship.
- Native `required` and `aria-invalid` expose state semantically. The visual
  required marker is hidden from the accessible name, and a dynamic error uses
  one `role="alert"` announcement.

## `Button`

- `size="icon"` requires `aria-label` or `aria-labelledby`; development and
  test renders fail when the accessible name is missing.
- Icon controls retain a minimum 44 by 44 pixel activation target even when a
  caller applies a smaller visual size.
- `loading` disables activation, exposes `aria-busy="true"`, renders an
  inert spinner, and replaces the content with localized pending text.
- Use `loadingLabel` for an action-specific localized pending message. The
  generic fallback is the localized `button.pending` message.
