# History organization persistence

Tobkiri stores History drag, regroup, ungroup, and explicit ordering changes in
the local `tobkiri-history-organization-v1` document. The document is a
versioned, local-first layout overlay. Conversation content remains owned by the
conversation store, while the overlay records only group hierarchy, chat group
membership, and stable order.

The History board always reconstructs its full tree from all conversations and
then applies the saved overlay. Search and tag filters are display-only views of
that full tree, so hidden conversations cannot be dropped from saved membership
or ordering.

Each write compares the last observed revision with the current local revision.
A concurrent write, corrupt JSON, unavailable storage, or quota failure is
fail-closed: Tobkiri does not claim the arrangement is saved. The board keeps an
explicit unsaved state and offers Retry, JSON Export, and a two-step Reset.
Successful drag, regroup, ungroup, create, and rename operations expose Undo and
announce the result through one polite live region.

Project names and workspace links keep using the compatibility project record
key. Its writer now reports failure, so the History board can restore the
previous visible value instead of silently accepting a project mutation that
will disappear after restart.
