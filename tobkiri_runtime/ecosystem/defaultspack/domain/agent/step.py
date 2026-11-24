
from blocks._common import timestamp


class AgentStep:
    def __init__(self, step_id, step_number, step_type, content, created_at):
        self.step_id = step_id
        self.step_number = step_number
        self.step_type = step_type
        self.content = content
        self.status = "completed"
        self.created_at = created_at

    def to_dict(self):
        return {
            "step_id": self.step_id,
            "step_number": self.step_number,
            "step_type": self.step_type,
            "content": self.content,
            "status": self.status,
            "created_at": self.created_at,
        }
