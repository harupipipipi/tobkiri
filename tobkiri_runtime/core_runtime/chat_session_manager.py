"""
domain/chat/session_manager.py - Session management logic

Sessions group multiple conversations together, functioning like
browser tabs for parallel conversation management.

Uses the singleton pattern consistent with ChatStore.
Does NOT modify ChatStore itself.
"""

from __future__ import annotations

import time
import copy
import types
import uuid
from typing import Any, Callable


def _gen_id():
    return str(uuid.uuid4())


def _now_ms():
    return int(time.time() * 1000)


class SessionManager:
    _instance: SessionManager | None = None
    _chat_store_factory: Callable[[], Any] | None = None
    _sessions: dict[str, dict[str, Any]]
    _active_session_id: str | None

    def __new__(cls, *args, **kwargs):
        if cls.__dict__.get("_instance") is None:
            cls._instance = super().__new__(cls)
            cls._instance._sessions = {}
            cls._instance._active_session_id = None
        return cls._instance

    def __init__(self, chat_store_factory: Callable[[], Any] | None = None):
        if chat_store_factory is not None:
            self._chat_store_factory = chat_store_factory

    @classmethod
    def with_dependencies(
        cls,
        *,
        chat_store_factory: Callable[[], Any] | None = None,
    ) -> type["SessionManager"]:
        dependencies = {
            key: value
            for key, value in {"chat_store_factory": chat_store_factory}.items()
            if value is not None
        }

        def bound_init(self: SessionManager, *args: Any, **kwargs: Any) -> None:
            for key, value in dependencies.items():
                kwargs.setdefault(key, value)
            cls.__init__(self, *args, **kwargs)

        BoundSessionManager = types.new_class(
            cls.__name__,
            (cls,),
            exec_body=lambda namespace: namespace.update({"__init__": bound_init}),
        )

        BoundSessionManager.__name__ = cls.__name__
        BoundSessionManager.__qualname__ = cls.__qualname__
        BoundSessionManager.__module__ = cls.__module__
        return BoundSessionManager

    def _chat_store(self):
        if self._chat_store_factory is None:
            raise RuntimeError("chat_store_dependency_missing")
        return self._chat_store_factory()

    # ----------------------------------------------------------
    # Session CRUD
    # ----------------------------------------------------------
    def create_session(self, name=None, metadata=None):
        """Create a new session.

        Args:
            name: Display name for the session. Defaults to 'New Session'.
            metadata: Arbitrary dict of extra metadata.

        Returns:
            Deep copy of the created session dict.
        """
        sid = _gen_id()
        now = _now_ms()
        session = {
            "id": sid,
            "name": name if name else "New Session",
            "created_at": now,
            "updated_at": now,
            "last_accessed_at": now,
            "conversation_ids": [],
            "metadata": metadata if metadata is not None else {},
        }
        self._sessions[sid] = session
        # If this is the first session, auto-activate it
        if self._active_session_id is None:
            self._active_session_id = sid
        return copy.deepcopy(session)

    def get_session(self, session_id):
        """Get a session by ID.

        Returns:
            Deep copy of the session dict, or None if not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return copy.deepcopy(session)

    def list_sessions(self, limit=50, offset=0):
        """List all sessions sorted by last_accessed_at descending.

        Returns:
            Tuple of (list of session dicts, total count).
        """
        results = list(self._sessions.values())
        results.sort(key=lambda s: s["last_accessed_at"], reverse=True)
        total = len(results)
        page = results[offset: offset + limit]
        return [copy.deepcopy(s) for s in page], total

    def update_session(self, session_id, updates):
        """Update session fields.

        Protected fields (id, created_at, conversation_ids) cannot be
        overwritten through this method.

        Returns:
            Deep copy of the updated session, or None if not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        protected = {"id", "created_at", "conversation_ids"}
        for key, value in updates.items():
            if key not in protected:
                session[key] = value
        session["updated_at"] = _now_ms()
        return copy.deepcopy(session)

    def delete_session(self, session_id):
        """Delete a session. Does NOT delete the conversations themselves.

        Returns:
            True if deleted, False if not found.
        """
        if session_id not in self._sessions:
            return False
        del self._sessions[session_id]
        # If the active session was deleted, reset or pick another
        if self._active_session_id == session_id:
            if self._sessions:
                # Pick the most recently accessed remaining session
                remaining = sorted(
                    self._sessions.values(),
                    key=lambda s: s["last_accessed_at"],
                    reverse=True,
                )
                self._active_session_id = remaining[0]["id"]
            else:
                self._active_session_id = None
        return True

    # ----------------------------------------------------------
    # Conversation membership
    # ----------------------------------------------------------
    def add_conversation(self, session_id, conversation_id):
        """Add a conversation to a session.

        Validates that the conversation exists in ChatStore.
        Prevents duplicate additions.

        Returns:
            Deep copy of the updated session, or None if session not found.
            Raises ValueError if conversation not found or already in session.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        # Verify conversation exists
        store = self._chat_store()
        conv = store.get_conversation(conversation_id)
        if conv is None:
            raise ValueError("conversation_not_found")
        # Check for duplicates
        if conversation_id in session["conversation_ids"]:
            raise ValueError("conversation_already_in_session")
        session["conversation_ids"].append(conversation_id)
        session["updated_at"] = _now_ms()
        session["last_accessed_at"] = _now_ms()
        return copy.deepcopy(session)

    def remove_conversation(self, session_id, conversation_id):
        """Remove a conversation from a session.

        Does NOT delete the conversation itself from ChatStore.

        Returns:
            Deep copy of the updated session, or None if session not found.
            Raises ValueError if conversation is not in the session.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if conversation_id not in session["conversation_ids"]:
            raise ValueError("conversation_not_in_session")
        session["conversation_ids"].remove(conversation_id)
        session["updated_at"] = _now_ms()
        return copy.deepcopy(session)

    def list_conversations(self, session_id):
        """List all conversations within a session.

        Fetches full conversation data from ChatStore for each
        conversation_id in the session.

        Returns:
            List of conversation dicts, or None if session not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        store = self._chat_store()
        conversations = []
        for cid in session["conversation_ids"]:
            conv = store.get_conversation(cid)
            if conv is not None:
                conversations.append(conv)
        return conversations

    # ----------------------------------------------------------
    # Active session
    # ----------------------------------------------------------
    def switch_active_session(self, session_id):
        """Set the active session.

        Updates last_accessed_at on the target session.

        Returns:
            Deep copy of the now-active session, or None if not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        self._active_session_id = session_id
        session["last_accessed_at"] = _now_ms()
        session["updated_at"] = _now_ms()
        return copy.deepcopy(session)

    def get_active_session_id(self):
        """Return the currently active session ID, or None."""
        return self._active_session_id
