"""ContextManager — manages context window size via compression."""



class ContextManager:
    """Handles context compression when message count exceeds threshold."""

    def compress_if_needed(self, messages, agent_def):
        """Compress messages if they exceed the compress_after threshold.

        MVP: keeps system messages + most recent compress_after messages.
        Returns the (possibly compressed) message list.
        """
        memory_config = agent_def.get("memory", {})
        short_term = memory_config.get("short_term", {})
        compress_after = short_term.get("compress_after", 50)
        if len(messages) <= compress_after:
            return messages
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        recent = non_system[-compress_after:]
        return system_msgs + recent
