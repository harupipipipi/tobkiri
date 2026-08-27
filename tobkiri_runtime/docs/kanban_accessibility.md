# Kanban accessibility interaction model

The defaultspack Kanban uses nested lists: the board exposes an ordered list of
named columns, and each column exposes an ordered list of cards. Column labels
include their position, card count, and WIP limit state. Each card has a native
button title that opens its details and a stable accessible summary containing
its column and position, priority, blocked state, due date, run state, checklist
progress, and sync source.

Pointer drag is optional. Every card provides visible 44-by-44 CSS pixel (or
larger) controls to move before or after an adjacent card and to choose a named
destination column. The canonical Kanban state owner resolves relative card IDs
and enforces destination WIP limits atomically, so client ordering and WIP
preflight are usability aids rather than an authority boundary.

Keyboard move is the drag-equivalent interaction. Activate **Keyboard move**,
then use Up or Down to reorder, Left or Right to change columns, Enter to drop,
or Escape to restore the original position. The active card is marked as the
current item, focus follows it after each host response, and a live region
announces pickup, movement, rejection, cancellation, drop, and Undo results.
Pointer drag also announces pickup, cancellation, and results.

Delete uses the platform confirmation dialog. After a successful move, edit,
failure, or delete, focus returns to the affected card or the closest surviving
card. Failed host operations reload no speculative UI state, and WIP rejection
leaves the card in its authoritative position.
