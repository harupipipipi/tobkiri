"""ContextBuilder — builds LLM context messages from agent definition and session."""



class ContextBuilder:
    """Builds the message list to send to the LLM."""

    def build(self, agent_def, session, new_input):
        """Build context messages from agent_def and session history.

        Returns a list of message dicts with role and content keys.
        """
        messages = []
        system_prompt = agent_def.get("system_prompt", "You are a helpful assistant.")
        messages.append({"role": "system", "content": system_prompt})
        for msg in session.messages:
            messages.append({"role": msg["role"], "content": msg["content"]})
        return messages
