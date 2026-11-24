"""
blocks/agent/org/list.py — 組織一覧ブロック

GET /api/agent/org

input_data: (任意フィルタ)
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager


def run(input_data, context):
    manager = OrgManager()
    orgs = manager.list_orgs()

    return ok({
        "organizations": orgs,
        "total": len(orgs),
    })
