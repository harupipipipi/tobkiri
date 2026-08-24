# Conversation Spotlight accessibility contract

Conversation Spotlight is a modal search dialog. Its search field keeps DOM focus
and controls one listbox through `aria-controls` and `aria-activedescendant`.
Result rows are non-tabbable options with stable IDs and `aria-selected`; this is
an active-descendant combobox pattern, not a roving-tabindex pattern.

## Keyboard model

- `Arrow Up` and `Arrow Down` move the active result by one without wrapping.
- `Home` and `End` move to the first and last result.
- `Page Up` and `Page Down` move by five results and clamp at either end.
- `Enter` opens the active result when one exists.
- `Escape`, the named close button, and the backdrop close the dialog.
- `Tab` and `Shift+Tab` remain contained in the dialog while it is open.

The exact element that opened Spotlight regains focus after every close path.
Query, selected filter, and active conversation identity survive closing and
reopening. When an asynchronous response replaces the result set, the active
conversation remains selected if it still exists; otherwise selection moves to
the first result or clears for an empty result set.

## Screen-reader status model

One polite, atomic status region inside the dialog announces loading, result
count, recent/empty state, or no results. Visible copies of those messages are
hidden from the accessibility tree to avoid duplicate speech. A separate
persistent route-level status announces the title of a result after it opens,
because the dialog status unmounts during navigation.

Filters form a named button group and expose their selected state through
`aria-pressed`. Pointer activation returns focus to the combobox so the documented
result-navigation keys continue to work.

## Regression evidence

The component contract tests cover dialog, combobox, listbox, option, filter,
control-name, and live-region DOM state. The UI contract tests cover keyboard-only
navigation, containment, background inertness, Escape/backdrop dismissal, opener
restoration, filter changes, preserved state, asynchronous replacement, empty
results, long content, a 320 px viewport, and result-open announcements.
