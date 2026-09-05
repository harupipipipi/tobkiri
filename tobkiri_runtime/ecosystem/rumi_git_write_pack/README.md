# Rumi Git Write Pack

Provides receipt-gated stage and commit. Branch create/switch and restore are
explicitly unavailable until the Host provides an exclusive workspace mutation
lease. It has no push, remote mutation, publication, network, shell, tool,
chat, or UI code. Repository and paths are workspace jailed; Git metadata,
environment files, credential files, and key material are denied. Each staged
file is read through a nofollow descriptor and is capped at 64 MiB; larger
inputs fail with `Git stage input exceeds maximum size` before any Git object
is written.
