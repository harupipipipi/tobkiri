"""
blocks/agent/org/get.py — 組織詳細ブロック

GET /api/agent/org/{id}

input_data:
    id : str (必須) 組織 ID
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager
from domain.agent.inter_agent_comm import InterAgentComm
from domain.agent.context_transfer import ContextTransferManager


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    org_id = input_data.get("id")
    if not org_id:
        return error("id is required")

    manager = OrgManager()
    org = manager.get_org(org_id)
    if org is None:
        return error("organization not found: " + str(org_id))

    comm = InterAgentComm()
    recent_messages = comm.get_org_messages(org_id, limit=20)

    transfer_mgr = ContextTransferManager()
    transfers = transfer_mgr.list_transfers_for_org(org_id)

    org["recent_messages"] = recent_messages
    org["transfers"] = transfers

    return ok(org)
