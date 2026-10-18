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

Core-owned stream handles also share one retained source-close cell. It owns the
actual source, takes it once, and runs its close future in one prepaid task.
Aliases join that same task. The two-second observer deadline remains fixed;
timeout or a dropped observer cannot drop the actual close future or start a
replacement close. Source destruction and the task join precede a physical close
receipt. A late receipt cannot change an earlier uncertain handle outcome.

Registration, task, waiter, runtime and join failures still fail closed. In
particular, failure to admit the cell after a source was produced is not proof of
a physical join; the source's existing tracked cleanup remains responsible for
that path. This does not repair unrelated outer `CoreStream` close cancellation,
settle historical unknown effects, or prove whole-process quiescence.

The unchanged 64-stream session-close regression must continue to pass. Dedicated
tests cover shared close, interrupted observers, aliases, admission/spawn/join
failure, physical destruction order and sticky filesystem/SQL uncertainty.
