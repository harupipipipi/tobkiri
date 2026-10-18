"""
blocks/agent/org/create.py — 組織作成ブロック

POST /api/agent/org

input_data:
    name        : str (必須) 組織名
    description : str (任意) 説明
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    name = input_data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return error("name is required and must be a non-empty string")

    description = input_data.get("description", "")
    created_by = input_data.get("created_by", "user")

    manager = OrgManager()
    org = manager.create_org(
        name=name.strip(),
        description=description,
        created_by=created_by,
    )

    return ok(org)
