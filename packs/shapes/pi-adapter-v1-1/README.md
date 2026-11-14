# Pi command inventory, 1.1

`local.pi-adapter.command-inventory@1.1` adds a descriptive list of command
names and descriptions. It does not authorize or invoke commands. The existing
`local.pi-adapter.pi-extension@1.0` interface remains unchanged.

To opt in while creating a wrapper, pass `--command-inventory` to
`sdk/js/wrap-pi.mjs` together with its existing `--source`, `--upstream`, `--out`
and `--id` arguments. The source pin is verified as usual. The generated world
exports both interfaces, includes `wit/deps/pi-adapter-v1-1.wit`, and the Pack
manifest advertises both ports. Programmatic callers may pass
`{ commandInventory: true }` as the third `wrapPi` argument after preparing the
same WIT closure and world. A positional rebuild remains 1.0 unless passed
`--command-inventory`, which checks the source pin and extends only the known
unmodified 1.0 world. Use a copy of the pinned Pack for that opt-in build and
prepare its existing `wit/deps/core.wit` and `wit/deps/pi-adapter.wit` first.

A compatible adapter must separately register this shape, declare a 1.1 consume
and capability, and forward `list-commands` through the ordinary port gate.
These sources and a wrapper alone do not register the shape or connect it to the
Harness command-provider. Invocation continues through the existing gate and
policy checks. Existing wrapper artifacts and default builds do not include the
new interface.
