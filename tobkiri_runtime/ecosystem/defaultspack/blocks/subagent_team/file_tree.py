from blocks._common import ok, error
from domain.subagent_team.file_tree import build_file_tree, open_file_tree_node

from ._helpers import invalid, require_dict


def run(input_data, context, *, settings_owner=None):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    try:
        if str(input_data.get("action") or "").lower() == "open":
            return ok(
                open_file_tree_node(
                    input_data,
                    context if isinstance(context, dict) else {},
                    settings_owner=settings_owner,
                )
            )
        return ok(build_file_tree(input_data, context if isinstance(context, dict) else {}))
    except PermissionError as exc:
        return error(str(exc), str(getattr(exc, "code", "") or "PATH_RESTRICTED"))
    except ValueError as exc:
        return error(str(exc), "PATH_TRAVERSAL")
    except Exception as exc:
        return error("subagent team file tree failed: " + str(exc), "SUBAGENT_TEAM_FILE_TREE_ERROR")
