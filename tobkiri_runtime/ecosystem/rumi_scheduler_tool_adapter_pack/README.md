# Rumi Scheduler Tool Adapter Pack

Contributes scheduler tool definitions explicitly and projects selected calls
to global schedule contracts. Read operations use resource contracts directly.
Mutations require a consumed one-shot tool approval with an exact argument
hash before the adapter requests an owner-bound authority receipt.

The adapter imports no scheduler, target, tool broker, or defaultspack
implementation. Client-supplied `approved` flags are ignored.

