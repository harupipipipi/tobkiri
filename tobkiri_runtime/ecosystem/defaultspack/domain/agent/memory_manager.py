"""MemoryManager — manages project and workspace memory for agents."""



class MemoryManager:
    """Handles long-term memory operations for agents.

    Project memory persistence is not implemented in this runtime.
    """

    def update_project_memory(self, session, agent_def, messages):
        """Update project memory based on conversation.

        Return an explicit structured error instead of pretending the write
        succeeded.
        """
        return {
            "success": False,
            "error": "Agent project memory persistence is not implemented",
            "error_type": "not_implemented",
        }

    def read_memory(self, memory_type, workspace):
        """Read memory of the given type from the workspace.

        Return an explicit structured error instead of fabricating empty memory.
        """
        return {
            "success": False,
            "value": "",
            "error": "Agent memory read is not implemented",
            "error_type": "not_implemented",
        }
