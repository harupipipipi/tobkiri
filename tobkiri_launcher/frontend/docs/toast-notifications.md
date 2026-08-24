# Toast notification contract

Launcher toasts are supplementary feedback. An error that requires recovery or
an important decision must also remain visible in the page that owns the
operation; a toast must never be the only recovery path.

## Semantics and lifetime

- Success and informational notifications use a polite `status` live region.
- Warnings use a polite `status` live region with a visible `Warning` prefix.
- Urgent errors use an assertive `alert` live region with a visible `Error`
  prefix. Only the message itself is a live region; the queue is not one.
- Every notification has a visible dismiss button. Defaults are six seconds for
  success/information, ten seconds for warnings, and twelve seconds for errors.
- Hovering a notification or moving keyboard focus into it pauses its remaining
  time. Moving the pointer and focus away resumes from that remaining time.
- An action raises the minimum lifetime to fifteen seconds. It stays available
  while focused and while running. A failed action leaves the toast paused so
  the user can retry or dismiss it.
- `persistent: true` disables automatic dismissal. Persistence is reserved for
  supplementary messages whose related page also retains the actionable state.
- Appearance and removal animations use `motion-reduce` fallbacks.

Toasts never receive focus when they appear. Their action and dismiss controls
enter normal document tab order, after the currently focused page controls.

## Rapid events

The store retains at most five notifications. When a sixth distinct event is
added, the oldest notification is removed. Exact type/message duplicates (or
events with the same explicit `dedupeKey`) are ignored while the first remains,
so they produce one announcement. A `replacementKey` updates the existing card
in place and intentionally produces one announcement of its new severity and
message. `updateToast` provides the same in-place behavior when the caller has
the toast ID. Queue insertion and replacement never move keyboard focus.
