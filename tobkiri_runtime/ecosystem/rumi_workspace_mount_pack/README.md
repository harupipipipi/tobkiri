# Rumi Workspace Mount Pack

Owns profile workspace IDs and canonical directory roots. It does not read or
write files, execute commands, or run Git. Mount mutations require an exact
short-lived host-authority receipt and never delete workspace contents.
Create, update, select, trust, and unmount actions are revision-bound; their
workspace ID, root, metadata, and expected revision are included in the exact
receipt scope when applicable.

