

from blocks._common import ok
from domain.agent.role_registry import RoleRegistry


def run(input_data, context):
    roles = RoleRegistry().list_roles()
    return ok({"roles": roles, "total": len(roles)})
