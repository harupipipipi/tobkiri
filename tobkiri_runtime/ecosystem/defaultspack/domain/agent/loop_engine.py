"""LoopEngine — main execution loop for agent interactions."""

import time

from domain.agent.context_builder import ContextBuilder


class LoopEngine:
    """Executes the agent loop: build context, call LLM, check termination.

    Calls the configured AI handler when one is available and fails closed
    instead of fabricating a successful assistant response.
    """

    def _call_ai(self, agent_def, session, context):
        if not isinstance(context, dict):
            return {
                "status": "error",
                "error": {
                    "code": "AI_HANDLER_UNAVAILABLE",
                    "message": "agent loop requires call_handler",
                },
            }
        callback = context.get("call_handler")
        if not callable(callback):
            return {
                "status": "error",
                "error": {
                    "code": "AI_HANDLER_UNAVAILABLE",
                    "message": "agent loop requires call_handler",
                },
            }
        messages = []
        system_prompt = agent_def.get("system_prompt")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(
            {"role": msg["role"], "content": msg["content"]}
            for msg in session.messages
        )
        return callback(
            "defaults.ai.complete",
            {
                "messages": messages,
                "model": agent_def.get("model", "default"),
                "tools": agent_def.get("tools", []),
            },
        )

    def run(self, agent_def, session, input_text, context):
        """Run the agent loop.

        Args:
            agent_def: dict with agent configuration (system_prompt, model, loop, etc.)
            session: AgentSession instance
            input_text: user input string
            context: handler context (contains capability_socket for future LLM calls)

        Returns:
            dict with messages, final_text, status, and metadata.
        """
        start_time = time.time()
        session.status = "running"
        session.add_message("user", input_text)

        max_iterations = agent_def.get("loop", {}).get("max_iterations", 10)
        context_builder = ContextBuilder()

        for _ in range(max_iterations):
            session.current_step += 1
            context_builder.build(agent_def, session, input_text)
            ai_result = self._call_ai(agent_def, session, context)
            if ai_result.get("status") != "ok":
                session.status = "error"
                error_data = ai_result.get("error")
                message = (
                    error_data.get("message")
                    if isinstance(error_data, dict)
                    else str(error_data)
                )
                return {
                    "messages": session.messages,
                    "final_text": "",
                    "status": "error",
                    "error": message or "AI handler failed",
                    "metadata": {
                        "total_tokens": session.total_tokens,
                        "steps": session.current_step,
                        "elapsed_ms": int((time.time() - start_time) * 1000),
                    },
                }
            data = ai_result.get("data", {})
            response_text = (
                (data.get("content") or data.get("text") or "")
                if isinstance(data, dict)
                else str(data)
            )
            session.add_message("assistant", response_text)
            session.total_tokens += len(response_text)
            break

        session.status = "completed"
        final_text = session.messages[-1]["content"] if session.messages else ""
        elapsed_ms = int((time.time() - start_time) * 1000)

        return {
            "messages": session.messages,
            "final_text": final_text,
            "status": "completed",
            "metadata": {
                "total_tokens": session.total_tokens,
                "steps": session.current_step,
                "elapsed_ms": elapsed_ms,
            },
        }
