"""
domain/chat/messaging.py — Channel messaging, mentions, and threads.

Manages messages within channels. In-memory store with JSON file persistence.
Each channel's messages are stored in a separate JSON file.
"""

import copy
import json
import os
import threading
import time
import uuid

from domain.mention import extract_mention_values

def _gen_id():
    return str(uuid.uuid4())


def _now_ms():
    return int(time.time() * 1000)


_PERSIST_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", ".data", "channel_messages"
)


class MessagingService:
    """Singleton messaging service for channel-based chat."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._messages = {}   # channel_id -> list[message_dict]
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._load_from_disk()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _ensure_persist_dir(self):
        os.makedirs(_PERSIST_DIR, exist_ok=True)

    def _persist_channel_messages(self, channel_id):
        self._ensure_persist_dir()
        path = os.path.join(_PERSIST_DIR, channel_id + ".json")
        msgs = self._messages.get(channel_id, [])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(msgs, fh, ensure_ascii=False, indent=2)

    def _load_from_disk(self):
        if not os.path.isdir(_PERSIST_DIR):
            return
        for fname in os.listdir(_PERSIST_DIR):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(_PERSIST_DIR, fname)
            channel_id = fname[:-5]
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    self._messages[channel_id] = data
            except (json.JSONDecodeError, OSError):
                continue

    # ------------------------------------------------------------------
    # Mention parsing
    # ------------------------------------------------------------------

    @staticmethod
    def parse_mentions(text, known_values=None):
        """Extract @mentions from message text.

        Returns a list of mentioned names. '@all' is returned as 'all'.
        """
        return list(dict.fromkeys(extract_mention_values(text, known_values)))

    # ------------------------------------------------------------------
    # Send message
    # ------------------------------------------------------------------

    def send_message(self, channel_id, sender_id, sender_name, content,
                     thread_id=None, metadata=None, mention_values=None):
        """Post a message to a channel.

        Args:
            channel_id: Target channel.
            sender_id: ID of the sender (agent or user).
            sender_name: Display name of the sender.
            content: Message text.
            thread_id: If replying in a thread, the parent message ID.
            metadata: Optional dict of extra data.

        Returns:
            (message_dict, mentions_list)
        """
        mentions = self.parse_mentions(content, mention_values)
        msg_id = _gen_id()
        now = _now_ms()

        message = {
            "id": msg_id,
            "channel_id": channel_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
            "mentions": mentions,
            "thread_id": thread_id,
            "reply_count": 0,
            "created_at": now,
            "metadata": metadata if metadata else {},
        }

        with self._lock:
            if channel_id not in self._messages:
                self._messages[channel_id] = []
            self._messages[channel_id].append(message)

            if thread_id is not None:
                for msg in self._messages[channel_id]:
                    if msg["id"] == thread_id:
                        msg["reply_count"] = msg.get("reply_count", 0) + 1
                        break

            self._persist_channel_messages(channel_id)

        return copy.deepcopy(message), mentions

    # ------------------------------------------------------------------
    # Get messages
    # ------------------------------------------------------------------

    def get_messages(self, channel_id, limit=50, offset=0, thread_id=None):
        """Get messages for a channel, optionally filtered by thread.

        If thread_id is given, returns only the parent message and its replies.
        If thread_id is None, returns only top-level messages (thread_id is None).
        """
        msgs = self._messages.get(channel_id, [])

        if thread_id is not None:
            filtered = [
                m for m in msgs
                if m["id"] == thread_id or m.get("thread_id") == thread_id
            ]
        else:
            filtered = [m for m in msgs if m.get("thread_id") is None]

        filtered.sort(key=lambda m: m["created_at"])
        total = len(filtered)
        page = filtered[offset: offset + limit]
        return [copy.deepcopy(m) for m in page], total

    def get_message(self, channel_id, message_id):
        """Get a single message by ID."""
        msgs = self._messages.get(channel_id, [])
        for msg in msgs:
            if msg["id"] == message_id:
                return copy.deepcopy(msg)
        return None

    def get_thread(self, channel_id, parent_message_id, limit=50, offset=0):
        """Get a thread: the parent message and all replies."""
        msgs = self._messages.get(channel_id, [])
        thread_msgs = []
        for msg in msgs:
            if msg["id"] == parent_message_id or msg.get("thread_id") == parent_message_id:
                thread_msgs.append(msg)
        thread_msgs.sort(key=lambda m: m["created_at"])
        total = len(thread_msgs)
        page = thread_msgs[offset: offset + limit]
        return [copy.deepcopy(m) for m in page], total

    def get_all_channel_messages(self, channel_id, limit=100):
        """Get all messages in a channel (top-level + threads), sorted by time."""
        msgs = self._messages.get(channel_id, [])
        sorted_msgs = sorted(msgs, key=lambda m: m["created_at"])
        capped = sorted_msgs[-limit:] if len(sorted_msgs) > limit else sorted_msgs
        return [copy.deepcopy(m) for m in capped]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def delete_channel_messages(self, channel_id):
        """Remove all messages for a channel."""
        with self._lock:
            if channel_id in self._messages:
                del self._messages[channel_id]
            path = os.path.join(_PERSIST_DIR, channel_id + ".json")
            if os.path.exists(path):
                os.remove(path)
