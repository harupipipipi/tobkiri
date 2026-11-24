"""
blocks/agent/org/add_member.py — メンバー追加ブロック

POST /api/agent/org/{id}/members

input_data:
    id          : str (必須) 組織 ID
    agent_id    : str (任意) エージェント ID（指定しなければ自動生成）
    agent_name  : str (必須) エージェント名
    role_key    : str (必須) ロールキー（pm, coder, searcher, reviewer, etc.）
    model       : str (任意) AI モデル文字列
"""


from blocks._common import ok, error, gen_id
from domain.agent.org_manager import OrgManager
from domain.agent.role_registry import RoleRegistry


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    org_id = input_data.get("id")
    if not org_id:
        return error("id (org_id) is required")

    agent_name = input_data.get("agent_name")
    if not agent_name or not isinstance(agent_name, str) or not agent_name.strip():
        return error("agent_name is required and must be a non-empty string")

    role_key = input_data.get("role_key")
    if not role_key or not isinstance(role_key, str) or not role_key.strip():
        return error("role_key is required")

    role_registry = RoleRegistry()
    role = role_registry.get_role(role_key.strip())
    if role is None:
        return error(
            "role not found: " + str(role_key)
            + ". Define it first via POST /api/agent/org/roles or use a builtin: "
            "pm, coder, searcher, reviewer"
        )

    agent_id = input_data.get("agent_id")
    if not agent_id:
        agent_id = "agent_" + gen_id()

    model = input_data.get("model", "default")

    manager = OrgManager()
    member = manager.add_member(
        org_id=org_id,
        agent_id=agent_id,
        agent_name=agent_name.strip(),
        role_key=role_key.strip(),
        model=model,
    )
    if member is None:
        return error("organization not found: " + str(org_id))

    member["role"] = role

    return ok(member)
