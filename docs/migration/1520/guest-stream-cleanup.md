# Guest stream cleanup and Store generations

A guest stream or callback retains the retirement record of the actual Store
that produced it. Aliases retain that same record. Ordinary pulls and transfers
still require the currently admitted Pack, scope and generation.

Cleanup retires only the captured generation. All handles from that generation
join one retained cleanup task, using its original two-second observer deadline.
Dropping a waiter does not restart the cleanup task. A later Store under the same
Pack ID cannot be selected as an old handle's cleanup target.

The retirement record observes destruction only after the actual Store and its
external physical reservation have been dropped. If a retirement task exists,
replacement also requires its join to be observed. An absent Store, failed task
or unavailable join is not a successful destruction receipt. Partially initialized
Stores are owned before their first asynchronous initialization step.

Physical retirement and individual handle results remain separate. A joined
Store cannot erase filesystem or SQL commit-unknown results, pending data moves,
unconfirmed native workers, or a previously selected terminal outcome. Neither
retirement nor an ordinary stream cancellation acknowledgement authorizes replay.

This change covers guest Store generations. Core-owned source close still has
a separate two-second timeout around its close future; retaining that future
through an interrupted observer is a remaining lifecycle issue. The existing
64-stream session-close regression remains unchanged and must continue to pass.
