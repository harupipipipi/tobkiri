# Managed Desktop Keyboard Control

The Desktops workspace keeps keyboard navigation local until a user explicitly
starts keyboard control for a running desktop that they currently control.
Focusing or clicking a live snapshot does not start keyboard capture.

## Entering and leaving control

1. Acquire human control of the target desktop.
2. Activate **Start keyboard control**. The live snapshot receives focus, its
   accessible role changes from `group` to `application`, and the status names
   the target desktop.
3. Press **Escape** or **Control+Alt+Shift+Escape** to release. Both forms are
   reserved locally and are never sent to the desktop.

While capture is active, Tab, Shift+Tab, navigation keys, printable text, and
supported in-page modifier shortcuts are sent to the remote desktop and their
local defaults are suppressed. Browser- or OS-reserved shortcuts may never be
delivered to the page; they remain local and are explicitly unsupported.
Outside capture, Tab and Shift+Tab retain their normal Tobkiri
focus-navigation behavior.

The invoking keyboard-control button remains focusable when control becomes
unavailable. Focus returns there after a manual release, control-lease loss,
desktop switch, seat stop, or rejected/failed remote input. Capture state is
ephemeral and is not restored after reload.

## Text and key support

- Printable text is sent as text, including international characters.
- Paste is sent once as text and its local default is suppressed.
- IME/dead-key composition events are not forwarded while composing; committed
  composition text is sent once when the browser delivers composition events
  to the snapshot. IMEs that require an editable caret are unsupported.
- Navigation keys and supported shortcuts are sent as one-shot key commands.
- The transport does not model physical key-up or held-key state. Browser
  auto-repeat can produce repeated one-shot commands. Caps Lock and unsupported
  function or media keys are not sent. The live status announces an
  unsupported key instead of allowing it to trigger a local action.

Escape always releases capture, including during composition. It never acts as
a remote composition-cancel key.

The visible and screen-reader instructions expose these limits before capture,
so the one-shot transport is not presented as a full physical keyboard event
model.

## Authority and failure boundaries

The UI is not an authority source. Every input still requires the active,
server-issued desktop control lease and follows the existing Pack Architecture
v4 route and audit contract. Client capture state cannot substitute for a
lease, ProfileLock, ResolvedPlan, Authority Kernel decision, or PackVM boundary.
There is no legacy lookup, implicit fallback, second authority path, or host
fallback.

If the server rejects an input, the UI releases capture, restores focus, shows
the reason, and refreshes authoritative desktop state. Typed text remains
excluded from audit fields by the backend input policy.
