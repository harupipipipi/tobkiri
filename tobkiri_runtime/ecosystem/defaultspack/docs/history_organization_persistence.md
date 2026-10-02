# History organization

History retains grouping, membership and ordering in a versioned, Profile-scoped local layout document. Conversation content and Project identity stay with their canonical Runtime owners. Filtered display is rebuilt from the full tree before search/tag filtering, so hidden conversations keep their placement.

Failed local writes remain visibly unsaved. Retry retains the original observed revision and refuses to overwrite a newer document. Explicit Export includes the unsaved layout; Reset requires confirmation and affects only the local arrangement. Undo persists through the same checked save path. Storage events reload other-window changes when there is no unsaved arrangement and preserve unsaved work otherwise.

LocalStorage read/check/write is not atomic compare-and-swap. The revision check detects observed conflicts and browser events keep views synchronized, but simultaneous writes can still race. This UI layout is a best-effort local overlay, not an authority or a transactional datastore. Legacy unscoped layouts are left intact and never automatically migrated across Profiles.
