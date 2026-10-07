"""
domain/chat/history_editor.py - Conversation history editing logic.

Provides pure-logic operations for editing conversation history.
All operations use ChatStore's existing public API without modifying it.
"""

import copy
import time
import uuid


from blocks.chat._prompt_helpers import build_text_from_content


def _gen_id():
    return str(uuid.uuid4())


def _now_ms():
    return int(time.time() * 1000)


def _build_text_from_content(content):
    """Extract plain text from a content field (str or list of blocks)."""
    return build_text_from_content(content)


def delete_range(store, conversation_id, start_message_id, end_message_id):
    """Delete all messages in the specified range [start, end] inclusive.

    Args:
        store: ChatStore instance
        conversation_id: target conversation ID
        start_message_id: first message ID of the range
        end_message_id: last message ID of the range

    Returns:
        dict with keys: deleted_count, deleted_message_ids, conversation
        or None on failure (with error string as second element of tuple)
    """
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return None, "Conversation not found"

    range_result = store.get_messages_range(
        conversation_id, start_message_id, end_message_id
    )
    if range_result is None:
        return None, "Invalid message range: start or end message not found"

    range_messages, start_idx = range_result
    if not range_messages:
        return None, "No messages in specified range"

    message_ids = [m["id"] for m in range_messages]
    deleted_count = store.delete_messages_bulk(conversation_id, message_ids)

    updated_conv = store.get_conversation(conversation_id)
    return {
        "deleted_count": deleted_count,
        "deleted_message_ids": message_ids,
        "conversation": updated_conv,
    }, None


def replace_range_with_message(store, conversation_id, start_message_id,
                               end_message_id, replacement_msg_dict):
    """Delete messages in range and insert a replacement message at the same position.

    Correctly re-links parent_id / children_ids so the conversation chain
    remains intact.

    Args:
        store: ChatStore instance
        conversation_id: target conversation ID
        start_message_id: first message ID of the range
        end_message_id: last message ID of the range
        replacement_msg_dict: message dict for the replacement (role, content, etc.)

    Returns:
        (result_dict, None) on success
        (None, error_string) on failure
    """
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return None, "Conversation not found"

    range_result = store.get_messages_range(
        conversation_id, start_message_id, end_message_id
    )
    if range_result is None:
        return None, "Invalid message range: start or end message not found"

    range_messages, start_idx = range_result
    if not range_messages:
        return None, "No messages in specified range"

    original_ids = [m["id"] for m in range_messages]
    first_msg = range_messages[0]
    last_msg = range_messages[-1]

    parent_of_range = first_msg.get("parent_id")
    delete_set = set(original_ids)
    surviving_children = [
        cid for cid in last_msg.get("children_ids", [])
        if cid not in delete_set
    ]

    store.delete_messages_bulk(conversation_id, original_ids)

    inserted = store.insert_message_at(
        conversation_id, replacement_msg_dict, start_idx,
        parent_id=parent_of_range,
        children_ids=surviving_children,
    )
    if inserted is None:
        return None, "Failed to insert replacement message"

    updated_conv = store.get_conversation(conversation_id)
    return {
        "replacement_message": inserted,
        "deleted_message_ids": original_ids,
        "deleted_count": len(original_ids),
        "conversation": updated_conv,
    }, None


def extract_messages(store, conversation_id, message_ids,
                     target_conversation_id=None, remove_from_source=True):
    """Extract specified messages to a new or existing conversation.

    Messages are copied in the order they appear in the source conversation.
    Optionally removes them from the source conversation.

    Args:
        store: ChatStore instance
        conversation_id: source conversation ID
        message_ids: list of message IDs to extract
        target_conversation_id: if provided, add to this conversation;
                                otherwise create a new one
        remove_from_source: if True (default), delete extracted messages
                           from source conversation

    Returns:
        (result_dict, None) on success
        (None, error_string) on failure
    """
    conv = store.get_conversation(conversation_id)
    if conv is None:
        return None, "Source conversation not found"

    all_messages = conv.get("messages", [])
    id_set = set(message_ids)
    extracted = [m for m in all_messages if m["id"] in id_set]

    if not extracted:
        return None, "No matching messages found in conversation"

    found_ids = {m["id"] for m in extracted}
    missing_ids = [mid for mid in message_ids if mid not in found_ids]

    if target_conversation_id is not None:
        target_conv = store.get_conversation(target_conversation_id)
        if target_conv is None:
            return None, "Target conversation not found"
    else:
        source_title = conv.get("title", "New Conversation")
        target_conv = store.create_conversation(
            model=conv.get("model"),
            system_prompt_id=conv.get("system_prompt_id"),
            agent_id=conv.get("agent_id"),
            tags=list(conv.get("tags", [])),
        )
        store.update_conversation(target_conv["id"], {
            "title": source_title + " (extract)",
        })
        target_conversation_id = target_conv["id"]

    added_messages = []
    for msg in extracted:
        new_msg_dict = {
            "role": msg.get("role", "user"),
            "content": copy.deepcopy(msg.get("content", [])),
            "metadata": {
                "extracted_from": conversation_id,
                "original_message_id": msg["id"],
            },
        }
        added = store.add_message(target_conversation_id, new_msg_dict)
        if added is not None:
            added_messages.append(added)

    if remove_from_source:
        store.delete_messages_bulk(conversation_id, list(found_ids))

    updated_source = store.get_conversation(conversation_id)
    updated_target = store.get_conversation(target_conversation_id)

    return {
        "source_conversation": updated_source,
        "target_conversation": updated_target,
        "extracted_count": len(added_messages),
        "extracted_message_ids": list(found_ids),
        "missing_message_ids": missing_ids,
        "target_conversation_id": target_conversation_id,
    }, None


def identify_compactable_segments(messages, min_segment_length=3,
                                  max_text_length=500):
    """Heuristic identification of message segments that may be compactable.

    This is a rule-based pre-filter. The AI will make the final decision.
    Looks for patterns like:
    - Long sequences of assistant messages (work logs)
    - Messages with very long content followed by short summaries
    - Sequences of alternating user/assistant with short content (iterative debugging)

    Args:
        messages: list of message dicts from the conversation
        min_segment_length: minimum number of messages to form a segment
        max_text_length: text longer than this is considered verbose

    Returns:
        list of dicts with start_id, end_id, reason, message_count
    """
    if len(messages) < min_segment_length:
        return []

    segments = []

    # Pattern 1: consecutive assistant messages (work logs)
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "assistant":
            run_start = i
            while (i < len(messages)
                   and messages[i].get("role") == "assistant"):
                i += 1
            run_length = i - run_start
            if run_length >= min_segment_length:
                segments.append({
                    "start_id": messages[run_start]["id"],
                    "end_id": messages[i - 1]["id"],
                    "reason": "consecutive_assistant_messages",
                    "message_count": run_length,
                })
        else:
            i += 1

    # Pattern 2: verbose exchanges (many messages where most have long content)
    window_size = max(min_segment_length, 5)
    for start in range(0, len(messages) - window_size + 1, window_size):
        window = messages[start:start + window_size]
        verbose_count = 0
        for msg in window:
            text = _build_text_from_content(msg.get("content", []))
            if len(text) > max_text_length:
                verbose_count += 1
        if verbose_count >= window_size * 0.6:
            seg_start_id = window[0]["id"]
            seg_end_id = window[-1]["id"]
            already_covered = False
            for existing in segments:
                if existing["start_id"] == seg_start_id and existing["end_id"] == seg_end_id:
                    already_covered = True
                    break
            if not already_covered:
                segments.append({
                    "start_id": seg_start_id,
                    "end_id": seg_end_id,
                    "reason": "verbose_exchange",
                    "message_count": len(window),
                })

    # Pattern 3: rapid back-and-forth (short messages, likely iterative debugging)
    i = 0
    while i < len(messages) - min_segment_length:
        short_count = 0
        j = i
        while j < len(messages):
            text = _build_text_from_content(messages[j].get("content", []))
            if len(text) < 100:
                short_count += 1
                j += 1
            else:
                break
        run_length = j - i
        if run_length >= min_segment_length and short_count >= run_length * 0.7:
            segments.append({
                "start_id": messages[i]["id"],
                "end_id": messages[j - 1]["id"],
                "reason": "rapid_short_exchanges",
                "message_count": run_length,
            })
            i = j
        else:
            i += 1

    return segments
