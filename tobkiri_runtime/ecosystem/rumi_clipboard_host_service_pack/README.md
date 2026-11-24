# Rumi Clipboard Host Service Pack

This pack splits clipboard read and write into separately permissioned global
contracts. It creates HostIntents whose caller is bound by core Authority. After
exact token validation, the Viewer helper invokes this pack's fixed-command,
no-shell clipboard runner.

Clipboard text is bounded to 1 MiB. Client approval material is rejected and no
clipboard payload is persisted by this pack.

Validation was not executed by the implementation agent.
Independent testing is required before merge.

