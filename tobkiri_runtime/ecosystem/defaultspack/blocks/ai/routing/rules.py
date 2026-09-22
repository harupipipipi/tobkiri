
from blocks._common import ok, error
from domain.ai_client.client import AIClient
from domain.ai_client.model_router import ModelRouter


def run(input_data, context):
    """ルーティングルール CRUD。

    GET (一覧):
        input_data: {} or {"_method": "GET"}
        Returns: ok({"rules": [...]})

    POST (作成):
        input_data: {
            "_method": "POST",
            "name": str,
            "condition": dict,
            "target_model": str,
            "priority": int (optional, default 0)
        }
        Returns: ok({"rule": dict})

    DELETE (削除):
        input_data: {
            "_method": "DELETE",
            "id": str,  # path_inject で注入される
        }
        Returns: ok({"deleted": str})
    """
    client = AIClient()
    router = ModelRouter(client)

    method = input_data.get("_method", "GET").upper()

    if method == "GET":
        rules = router.list_rules()
        return ok({"rules": rules})

    elif method == "POST":
        name = input_data.get("name")
        condition = input_data.get("condition")
        target_model = input_data.get("target_model")

        if not name:
            return error("name is required", "MISSING_PARAM")
        if not condition or not isinstance(condition, dict):
            return error("condition dict is required", "MISSING_PARAM")
        if not target_model:
            return error("target_model is required", "MISSING_PARAM")

        # condition のフィールド検証
        valid_condition_keys = {
            "task_type", "complexity", "min_tokens", "max_tokens",
            "has_code", "has_images", "language_hint",
        }
        invalid_keys = set(condition.keys()) - valid_condition_keys
        if invalid_keys:
            return error(
                "Invalid condition keys: {}. Valid keys: {}".format(
                    ", ".join(sorted(invalid_keys)),
                    ", ".join(sorted(valid_condition_keys))
                ),
                "INVALID_INPUT",
            )

        priority = input_data.get("priority", 0)
        if not isinstance(priority, int):
            return error("priority must be an integer", "INVALID_INPUT")

        rule = router.add_rule(name, condition, target_model, priority=priority)
        return ok({"rule": rule})

    elif method == "DELETE":
        rule_id = input_data.get("id")
        if not rule_id:
            return error("rule id (path parameter) is required", "MISSING_PARAM")
        success = router.remove_rule(rule_id)
        if not success:
            return error("Rule '{}' not found".format(rule_id), "NOT_FOUND")
        return ok({"deleted": rule_id})

    else:
        return error("Unsupported method: {}".format(method), "INVALID_METHOD")
