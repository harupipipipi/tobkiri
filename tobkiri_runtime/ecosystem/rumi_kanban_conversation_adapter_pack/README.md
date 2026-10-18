# Rumi Kanban Conversation Adapter Pack

Reads a conversation through `rumi.resource.conversation.v1`, extracts only
deterministic task lines, and projects them through `rumi.action.kanban.v1`.
It does not import or write the chat store, and it never calls agent, Company,
connector, or scheduler implementations. Kanban transitions obtain a scoped
host-authority receipt for each exact revision-bound action.

