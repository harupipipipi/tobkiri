
from blocks._common import timestamp, gen_id
from domain.agent.step import AgentStep


class AgentExecution:
    def __init__(self, execution_id, task, tools, model, system_prompt):
        self.execution_id = execution_id
        self.task = task
        self.tools = tools
        self.model = model
        self.system_prompt = system_prompt
        self.status = "created"
        self.steps = []
        self.current_step = 0
        self.result = None
        self.error = None
        self.messages = []
        self.pending_tool_call = None
        self.queued_tool_calls = []
        self.context = {}
        self.created_at = timestamp()
        self.updated_at = timestamp()

    def add_step(self, step_type, content):
        step = AgentStep(
            step_id=gen_id("step_"),
            step_number=len(self.steps) + 1,
            step_type=step_type,
            content=content,
            created_at=timestamp(),
        )
        self.steps.append(step)
        self.current_step = len(self.steps)
        self.updated_at = timestamp()
        return step

    def to_dict(self):
        return {
            "execution_id": self.execution_id,
            "task": self.task,
            "tools": self.tools,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "current_step": self.current_step,
            "result": self.result,
            "error": self.error,
            "pending_tool_call": self.pending_tool_call,
            "queued_tool_calls": self.queued_tool_calls,
            "context": self.context,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
