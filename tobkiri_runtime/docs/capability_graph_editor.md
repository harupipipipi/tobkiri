# Capability Graph Editor (retired)

The historical `/api/graphs` and `/api/panel/graphs` routes are retired. They
have no current handler, persistence, compiler, or execution authority and
must not be restored as a fallback around Pack Architecture v4.

Graph-capable clients use `tobkiri_workflow_pack`'s exact
`graph.compile-preview` Contract operation. It compiles a bounded
`rumi_graph` document against the activation-scoped catalog into a Workflow v4
Definition without saving the graph or reserving execution authority. See
[the Workflow graph compiler contract](flow_graph_editor_todo.md) for the
supported schema, runtime mapping, and simulation boundary.
