"""
domain/chat/notification.py — Notification management and AI agent auto-reply.

Handles:
- Creating notifications from mentions / @all / thread replies
- Checking if a mentioned target is an AI agent
- Triggering AI agent auto-replies via AIClient
"""

import copy
import re
import threading
import time
import uuid


def _gen_id():
    return str(uuid.uuid4())


def _now_ms():
    return int(time.time() * 1000)


class Notification:
    """A single notification record."""

    def __init__(self, channel_id, message_id, target_id, notification_type,
                 sender_id=None, sender_name=None, content_preview=""):
        self.id = _gen_id()
        self.channel_id = channel_id
        self.message_id = message_id
        self.target_id = target_id
        self.notification_type = notification_type
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.content_preview = content_preview[:200]
        self.read = False
        self.created_at = _now_ms()

    def to_dict(self):
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "target_id": self.target_id,
            "notification_type": self.notification_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "content_preview": self.content_preview,
            "read": self.read,
            "created_at": self.created_at,
        }


class NotificationService:
    """Singleton notification service.

    Manages notifications and triggers AI agent auto-replies.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._notifications = []
            cls._instance._agent_registry = {}
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

    # ------------------------------------------------------------------
    # Agent registry — register AI agents that can auto-reply
    # ------------------------------------------------------------------

    def register_agent(self, agent_id, agent_name, role="assistant",
                       model="stub/default", system_prompt=""):
        """Register an AI agent that can receive mentions and auto-reply."""
        self._agent_registry[agent_name.lower()] = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "role": role,
            "model": model,
            "system_prompt": system_prompt,
        }
        self._agent_registry[agent_id] = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "role": role,
            "model": model,
            "system_prompt": system_prompt,
        }

    def get_agent(self, identifier):
        """Look up an agent by name (case-insensitive) or by ID."""
        agent = self._agent_registry.get(identifier)
        if agent is not None:
            return agent
        return self._agent_registry.get(identifier.lower()) if isinstance(identifier, str) else None

    def is_agent(self, identifier):
        """Check if an identifier corresponds to a registered AI agent."""
        return self.get_agent(identifier) is not None

    def list_agents(self):
        """Return unique agent entries."""
        seen = set()
        agents = []
        for entry in self._agent_registry.values():
            aid = entry["agent_id"]
            if aid not in seen:
                seen.add(aid)
                agents.append(copy.deepcopy(entry))
        return agents

    # ------------------------------------------------------------------
    # Notification creation
    # ------------------------------------------------------------------

    def create_notifications(self, channel_id, message, channel_members):
        """Create notifications for a message based on its mentions.

        Args:
            channel_id: The channel where the message was posted.
            message: The message dict.
            channel_members: List of member IDs in the channel.

        Returns:
            list of Notification dicts, list of agent_ids that should auto-reply
        """
        mentions = message.get("mentions", [])
        sender_id = message.get("sender_id", "")
        sender_name = message.get("sender_name", "")
        content = message.get("content", "")
        message_id = message.get("id", "")

        notifications = []
        agents_to_reply = []
        unresolved_mentions = []
        notified_targets = set()
        member_by_key = {}
        for member_id in channel_members:
            member_by_key.setdefault(str(member_id).casefold(), []).append(member_id)

        def resolve_member(identifier):
            candidates = member_by_key.get(str(identifier).casefold(), [])
            return candidates[0] if len(candidates) == 1 else None

        def add_mention_notification(target_id):
            target_key = str(target_id).casefold()
            if target_key in notified_targets or target_key == str(sender_id).casefold():
                return
            notified_targets.add(target_key)
            notif = Notification(
                channel_id=channel_id,
                message_id=message_id,
                target_id=target_id,
                notification_type="mention",
                sender_id=sender_id,
                sender_name=sender_name,
                content_preview=content,
            )
            with self._lock:
                self._notifications.append(notif)
            notifications.append(notif.to_dict())
            if self.is_agent(target_id) and target_id not in agents_to_reply:
                agents_to_reply.append(target_id)

        if any(str(mention).casefold() == "all" for mention in mentions):
            for member_id in channel_members:
                if str(member_id).casefold() == str(sender_id).casefold():
                    continue
                notif = Notification(
                    channel_id=channel_id,
                    message_id=message_id,
                    target_id=member_id,
                    notification_type="all",
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content_preview=content,
                )
                with self._lock:
                    self._notifications.append(notif)
                notifications.append(notif.to_dict())
                notified_targets.add(str(member_id).casefold())
                if self.is_agent(member_id):
                    agents_to_reply.append(member_id)

        for mention_name in mentions:
            mention_key = str(mention_name).casefold()
            if mention_key == "all":
                continue
            agent_info = self.get_agent(mention_name)
            if agent_info is not None:
                target_id = resolve_member(agent_info["agent_id"])
                if target_id is None:
                    unresolved_mentions.append(mention_name)
                    continue
                add_mention_notification(target_id)
            else:
                target_id = resolve_member(mention_name)
                if target_id is None:
                    unresolved_mentions.append(mention_name)
                    continue
                add_mention_notification(target_id)

        message["unresolved_mentions"] = list(dict.fromkeys(unresolved_mentions))

        return notifications, agents_to_reply

    def create_thread_notification(self, channel_id, message, original_sender_id):
        """Create a notification for a thread reply to the original message author."""
        sender_id = message.get("sender_id", "")
        if sender_id == original_sender_id:
            return None
        notif = Notification(
            channel_id=channel_id,
            message_id=message.get("id", ""),
            target_id=original_sender_id,
            notification_type="thread_reply",
            sender_id=sender_id,
            sender_name=message.get("sender_name", ""),
            content_preview=message.get("content", ""),
        )
        with self._lock:
            self._notifications.append(notif)
        return notif.to_dict()

    # ------------------------------------------------------------------
    # Notification queries
    # ------------------------------------------------------------------

    def get_notifications(self, target_id, unread_only=False, limit=50):
        with self._lock:
            filtered = [
                n for n in self._notifications
                if n.target_id == target_id
            ]
        if unread_only:
            filtered = [n for n in filtered if not n.read]
        filtered.sort(key=lambda n: n.created_at, reverse=True)
        return [n.to_dict() for n in filtered[:limit]]

    def mark_read(self, notification_id):
        with self._lock:
            for n in self._notifications:
                if n.id == notification_id:
                    n.read = True
                    return True
        return False

    # ------------------------------------------------------------------
    # AI Agent auto-reply
    # ------------------------------------------------------------------

    def generate_agent_reply(self, agent_id, channel_id, trigger_message,
                             channel_messages):
        """Generate an AI reply for an agent that was mentioned.

        Args:
            agent_id: The agent's ID.
            channel_id: The channel.
            trigger_message: The message that triggered this reply.
            channel_messages: Recent channel message history for context.

        Returns:
            str — The AI-generated response text, or a fallback message.
        """
        agent_info = self.get_agent(agent_id)
        if agent_info is None:
            return "I was mentioned but I don't have a configured profile."

        model = agent_info.get("model", "stub/default")
        agent_name = agent_info.get("agent_name", "Agent")
        role = agent_info.get("role", "assistant")
        system_prompt = agent_info.get("system_prompt", "")

        system_parts = []
        if system_prompt:
            system_parts.append(system_prompt)
        system_parts.append(
            "You are '" + agent_name + "', role: " + role + ". "
            "You are participating in a channel conversation. "
            "Respond helpfully and concisely to the message that mentioned you."
        )
        system_content = "\n\n".join(system_parts)

        messages = [{"role": "system", "content": system_content}]

        for msg in channel_messages:
            if msg.get("sender_id") == agent_id:
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content", ""),
                })
            else:
                prefix = "[" + msg.get("sender_name", "unknown") + "]: "
                messages.append({
                    "role": "user",
                    "content": prefix + msg.get("content", ""),
                })

        trigger_content = trigger_message.get("content", "")
        trigger_sender = trigger_message.get("sender_name", "someone")
        already_included = any(
            m.get("content", "").endswith(trigger_content) for m in messages
            if m["role"] == "user"
        )
        if not already_included:
            messages.append({
                "role": "user",
                "content": "[" + trigger_sender + "]: " + trigger_content,
            })

        try:
            from domain.ai_client.client import AIClient
            client = AIClient()
            result = client.complete(model, messages)
            if isinstance(result, dict):
                content = result.get("content", result.get("text", ""))
                if content:
                    return content
                return str(result)
            elif isinstance(result, str):
                return result
            return str(result)
        except Exception as exc:
            return "(Auto-reply error: " + str(exc) + ")"

    def process_agent_replies(self, agents_to_reply, channel_id,
                              trigger_message, messaging_service,
                              channel_manager):
        """For each agent that needs to reply, generate and post a response.

        Args:
            agents_to_reply: List of agent IDs that should reply.
            channel_id: The channel.
            trigger_message: The message that triggered replies.
            messaging_service: The MessagingService instance.
            channel_manager: The ChannelManager instance.

        Returns:
            list of reply message dicts.
        """
        replies = []
        thread_id = trigger_message.get("thread_id") or trigger_message.get("id")
        channel_messages = messaging_service.get_all_channel_messages(
            channel_id, limit=50
        )

        for agent_id in agents_to_reply:
            agent_info = self.get_agent(agent_id)
            if agent_info is None:
                continue

            reply_text = self.generate_agent_reply(
                agent_id, channel_id, trigger_message, channel_messages
            )

            reply_msg, reply_mentions = messaging_service.send_message(
                channel_id=channel_id,
                sender_id=agent_info["agent_id"],
                sender_name=agent_info["agent_name"],
                content=reply_text,
                thread_id=thread_id,
            )

            channel_manager.touch_channel(channel_id)
            replies.append(reply_msg)

            if reply_mentions:
                members = channel_manager.get_members(channel_id)
                if members:
                    self.create_notifications(
                        channel_id, reply_msg, members
                    )

        return replies
