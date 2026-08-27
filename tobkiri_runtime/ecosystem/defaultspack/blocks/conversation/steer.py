

from blocks._common import error, ok
from domain.chat.deferred_steer import (
    DeferredSteerFacade,
    DeferredSteerFacadeError,
)
from domain.chat.steer import ConversationSteerStore


def run(input_data, context=None):
    payload = input_data if isinstance(input_data, dict) else {}
    action = str(payload.get("action") or "enqueue").strip().lower()
    store = ConversationSteerStore()
    try:
        if action in {"register_deferred", "deferred.register"}:
            return ok(DeferredSteerFacade(context).register(payload))
        if action in {"enqueue", "create", "queue"}:
            return ok(store.enqueue(payload))
        if action == "list":
            target_id = payload.get("target_id") or payload.get("conversation_id")
            live_items = store.list(
                status=payload.get("status"),
                target_id=target_id,
            )
            deferred_items = DeferredSteerFacade(context).list(
                scope_type=str(payload.get("scope_type") or "conversation"),
                scope_id=str(payload.get("scope_id") or target_id or ""),
                include_history=bool(payload.get("include_history", False)),
            )
            return ok({"items": [*live_items, *deferred_items]})
        if action in {"list_deferred", "deferred.list"}:
            return ok(
                {
                    "items": DeferredSteerFacade(context).list(
                        scope_type=str(payload.get("scope_type") or ""),
                        scope_id=str(payload.get("scope_id") or ""),
                        include_history=bool(payload.get("include_history", False)),
                    )
                }
            )
        deferred_actions = {
            "update_deferred": "update",
            "deferred.update": "update",
            "checkpoint_deferred": "checkpoint",
            "deferred.checkpoint": "checkpoint",
            "defer_deferred": "defer",
            "deferred.defer": "defer",
            "apply_deferred": "apply",
            "deferred.apply": "apply",
            "complete_deferred": "complete",
            "deferred.complete": "complete",
            "dismiss_deferred": "dismiss",
            "deferred.dismiss": "dismiss",
            "fail_deferred": "fail",
            "deferred.fail": "fail",
        }
        if action in deferred_actions:
            method = getattr(DeferredSteerFacade(context), deferred_actions[action])
            result = method(payload)
            return ok({"items": result} if isinstance(result, list) else result)
        if action == "cancel":
            item_id = str(payload.get("id") or payload.get("steer_id") or "").strip()
            if not item_id:
                return error("id is required", "INVALID_INPUT")
            item = store.cancel(item_id)
            return ok({"cancelled": item is not None, "item": item})
        if action == "process":
            return ok(
                {
                    "processed": store.process(
                        target_type=str(payload.get("target_type") or "conversation"),
                        target_id=str(
                            payload.get("target_id") or payload.get("conversation_id") or ""
                        ),
                        conversation_id=str(payload.get("conversation_id") or ""),
                        context=context or {},
                    )
                }
            )
    except DeferredSteerFacadeError as exc:
        return error(str(exc), exc.code)
    except (KeyError, RuntimeError, ValueError) as exc:
        return error(str(exc), "INVALID_INPUT")
    return error("unsupported action", "INVALID_INPUT")
