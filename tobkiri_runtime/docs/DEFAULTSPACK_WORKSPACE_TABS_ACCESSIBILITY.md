# Defaults Profile workspace tabs accessibility contract

The Defaults Profile workspace switcher follows the WAI-ARIA Authoring
Practices tabs pattern with **automatic activation**.

- The horizontal container is a `tablist`. Each workspace has one `tab` and
  one stable, controlled `tabpanel`; only the active panel is rendered with
  content.
- Exactly one tab participates in sequential focus. Left/Right Arrow wraps
  between tabs and activates the focused workspace. Home and End activate the
  first and last workspace. Enter and Space explicitly activate the focused
  tab. Delete closes it when more than one workspace remains.
- Tab leaves the tab itself for the active close action, the new-workspace
  trigger, and then the active panel. Inactive close actions are excluded from
  sequential focus, so keyboard users do not traverse every tab twice.
- Closing the active tab selects and focuses its preceding neighbor, or the
  new first tab when the closed tab was first. The last workspace cannot be
  closed.
- The new-workspace trigger opens a labelled modal dialog, moves focus to its
  first enabled choice, supports Arrow/Home/End roving, contains Tab and
  Shift+Tab, closes on Escape or selection, and restores focus to its trigger.
  Unavailable future workspaces are exposed as disabled notes rather than
  selectable actions.
- Keyboard focus uses the shell's global visible focus indicator. Close
  controls become visible for hover, focus, and focus-within and use a minimum
  32-by-32 CSS-pixel target.

Component and deterministic keyboard/screen-reader regression coverage lives
in `src/components/WorkspaceTabs.test.tsx`.
