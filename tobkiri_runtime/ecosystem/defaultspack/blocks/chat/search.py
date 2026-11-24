from blocks._common import ok, error, gen_id, timestamp

from domain.chat.store import ChatStore


def run(input_data, context):
    store = ChatStore()
    query = input_data.get("query")
    if not query:
        return error("query is required", "INVALID_INPUT")
    conversation_id = input_data.get("conversation_id")
    if input_data.get("mode") == "conversations":
        results, total = store.search_conversations(
            query,
            conversation_id=conversation_id,
            limit=int(input_data.get("limit", 20) or 20),
            offset=int(input_data.get("offset", 0) or 0),
            date_filter=input_data.get("date_filter"),
            is_starred=input_data.get("is_starred"),
            is_archived=input_data.get("is_archived"),
            role=input_data.get("role"),
        )
        return ok({"results": results, "total": total, "query": query})
    results = store.search(query, conversation_id=conversation_id)
    return ok({"results": results})
