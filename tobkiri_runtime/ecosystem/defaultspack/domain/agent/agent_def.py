
from blocks._common import gen_id, timestamp


class AgentDefinition:
    """マルチエージェントにおける個々のエージェントの定義。

    Attributes
    ----------
    agent_id : str
        一意識別子。
    name : str
        エージェント名（例: "coder", "reviewer"）。
    role : str
        役割の説明文（例: "You are a senior Python developer."）。
    model : str
        使用する AI モデル文字列（例: "openai/gpt-4o"）。
    system_prompt : str
        システムプロンプト。
    tools : list[dict]
        使用可能なツール定義リスト。
    """

    def __init__(
        self,
        name,
        role,
        model,
        system_prompt=None,
        tools=None,
        agent_id=None,
        workspace=None,
    ):
        self.agent_id = agent_id if agent_id else ("agentdef_" + gen_id())
        self.name = name
        self.role = role
        self.model = model if model else "default"
        self.system_prompt = system_prompt if system_prompt else ""
        self.tools = tools if tools else []
        self.workspace = workspace if isinstance(workspace, dict) else {}
        self.created_at = timestamp()

    def to_dict(self):
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
            "workspace": self.workspace,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data):
        """dict からインスタンスを生成する。"""
        return cls(
            name=data.get("name", "unnamed"),
            role=data.get("role", ""),
            model=data.get("model", "default"),
            system_prompt=data.get("system_prompt", ""),
            tools=data.get("tools", []),
            agent_id=data.get("agent_id"),
            workspace=data.get("workspace") if isinstance(data.get("workspace"), dict) else {},
        )
