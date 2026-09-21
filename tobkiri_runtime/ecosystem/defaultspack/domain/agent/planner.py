"""Planner — generates execution plans for agent tasks."""


from blocks._common import gen_id


class Planner:
    """Generates structured execution plans for agent tasks.

    MVP: creates a single-step plan matching the task description.
    Future: uses LLM to decompose complex tasks into multiple steps.
    """

    def generate_plan(self, task, agent_def):
        """Generate an execution plan for the given task.

        Returns a plan dict with plan_id, task, steps, and total_estimated_iterations.
        """
        plan_id = gen_id()
        step_id = gen_id()
        return {
            "plan_id": plan_id,
            "task": task,
            "steps": [
                {
                    "step_id": step_id,
                    "description": task,
                    "tools_hint": [],
                    "estimated_iterations": 1,
                    "status": "pending",
                }
            ],
            "total_estimated_iterations": 1,
        }
