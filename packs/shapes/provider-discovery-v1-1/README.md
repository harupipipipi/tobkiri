# Provider discovery shapes, 1.1

These standalone shape sources extend the existing `local.harness-agent.tool-provider`
and `local.harness-chat.command-provider` 1.0 ports. `list-tools` returns names,
labels, descriptions and JSON input schemas; `list-commands` returns names and
descriptions. Tool metadata uses the existing `pi-extension` tool-definition
fields. An empty inventory is represented by an empty list; errors are returned
through the existing core error type.

The lists are provider-supplied metadata, not grants or proof that an invocation
will succeed. Invocation still uses the unchanged 1.0 operation and the ordinary
capability, approval and policy checks.

To register either shape, package its WIT as a `wit/` file, place the corresponding
`<interface>.shape.json` in `wit/`, and declare the port shape with its existing
name and version `1.1` in the owning Pack manifest. A provider must implement the
1.1 interface before advertising it. Existing 1.0 providers continue to advertise
1.0; they do not acquire a listing operation. These source files alone do not
register the new shapes, implement the operations, or wire inventory into a host.
