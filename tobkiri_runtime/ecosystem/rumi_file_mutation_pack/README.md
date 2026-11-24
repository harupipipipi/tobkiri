# Rumi File Mutation Pack

Provides atomic write, create, delete, and move operations under an exact
workspace mount. Every call redeems a short-lived authority receipt. Absolute
paths and symlink escapes are denied, and optional expected hashes reject stale
mutations. It contains no read/search, shell, Git, tool, chat, or UI runtime.

