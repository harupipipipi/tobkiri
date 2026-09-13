from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Message:
    role: str
    content: Any
    message_id: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.message_id:
            self.message_id = uuid.uuid4().hex
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass
class Conversation:
    chat_id: str
    title: str
    messages: List[Message] = field(default_factory=list)
    queued_messages: List[Message] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.chat_id,
            "chat_id": self.chat_id,
            "title": self.title,
            "messages": [message.to_dict() for message in self.messages],
            "queued_messages": [message.to_dict() for message in self.queued_messages],
        }

    def add_message(self, message: Message) -> Message:
        self.messages.append(message)
        return message

    def queue_message(self, message: Message) -> Message:
        self.queued_messages.append(message)
        return message

    def flush_queue(self) -> List[Message]:
        flushed = list(self.queued_messages)
        self.queued_messages.clear()
        return flushed


class ChatManager:
    def __init__(self) -> None:
        self._conversations: Dict[str, Conversation] = {}
        self._queued: Dict[str, List[Message]] = {}
        self._stop_flags: Dict[str, bool] = {}

    def create(self, title: str = "") -> Conversation:
        return self.create_conversation(title=title)

    def create_conversation(self, title: str = "") -> Conversation:
        conversation = Conversation(chat_id=uuid.uuid4().hex, title=title or "New Conversation")
        self._conversations[conversation.chat_id] = conversation
        return conversation

    def get_conversation(self, chat_id: str) -> Optional[Conversation]:
        return self._conversations.get(chat_id)

    def get_history(self, chat_id: str) -> Optional[Dict[str, Any]]:
        conversation = self._conversations.get(chat_id)
        return conversation.to_dict() if conversation else None

    def add_message(self, chat_id: str, message: Message) -> Message:
        conversation = self._conversations.setdefault(
            chat_id, Conversation(chat_id=chat_id, title=chat_id)
        )
        conversation.add_message(message)
        return message

    def queue_message(self, chat_id: str, message: Message) -> Message:
        self._queued.setdefault(chat_id, []).append(message)
        conversation = self._conversations.setdefault(
            chat_id, Conversation(chat_id=chat_id, title=chat_id)
        )
        conversation.queue_message(message)
        return message

    def pop_queued(self, chat_id: str) -> Optional[Message]:
        queued = self._queued.get(chat_id)
        if not queued:
            return None
        return queued.pop(0)

    def flush_queue(self, chat_id: str) -> List[Message]:
        conversation = self._conversations.get(chat_id)
        if conversation is None:
            return []
        flushed = conversation.flush_queue()
        self._queued.pop(chat_id, None)
        return flushed

    def list_messages(self, chat_id: str) -> List[Message]:
        conversation = self._conversations.get(chat_id)
        return list(conversation.messages) if conversation else []

    def list_conversations(self) -> List[Dict[str, Any]]:
        return [conversation.to_dict() for conversation in self._conversations.values()]

    def get_history_json(self, chat_id: str) -> Dict[str, Any]:
        return self.get_history(chat_id) or {"conversation_id": chat_id, "messages": []}

    def request_stop(self, chat_id: str) -> None:
        self._stop_flags[chat_id] = True

    def should_stop(self, chat_id: str) -> bool:
        return self._stop_flags.get(chat_id, False)


ChatMessage = Message
ChatConversation = Conversation

_CHAT_MANAGER: ChatManager | None = None


def get_chat_manager() -> ChatManager:
    global _CHAT_MANAGER
    if _CHAT_MANAGER is None:
        _CHAT_MANAGER = ChatManager()
    return _CHAT_MANAGER
