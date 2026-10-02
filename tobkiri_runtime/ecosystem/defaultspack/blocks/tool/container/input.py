"""blocks.tool.container.input — キーボード/マウス入力送信"""

from blocks._common import ok, error


def run(input_data, context):
    """POST /api/container/{id}/input — コンテナにキーボード/マウス入力を送信する"""
    from domain.tool.screen_controller import (
        send_mouse_click,
        send_keyboard_input,
        send_mouse_move,
        send_mouse_drag,
        send_scroll,
    )

    if not isinstance(input_data, dict):
        return error("request body must be a JSON object", "INVALID_INPUT")

    container_id = input_data.get("id")
    if not container_id:
        return error("container id is required", "MISSING_PARAM")

    input_type = input_data.get("type", "click")

    try:
        if input_type == "click":
            x = input_data.get("x", 0)
            y = input_data.get("y", 0)
            button = input_data.get("button", "left")
            result = send_mouse_click(container_id, x, y, button)

        elif input_type == "type":
            text = input_data.get("text", "")
            result = send_keyboard_input(container_id, text=text)

        elif input_type == "key":
            key = input_data.get("key", "Return")
            result = send_keyboard_input(container_id, key=key)

        elif input_type == "keyboard":
            text = input_data.get("text")
            key = input_data.get("key")
            result = send_keyboard_input(container_id, text=text, key=key)

        elif input_type == "move":
            x = input_data.get("x", 0)
            y = input_data.get("y", 0)
            result = send_mouse_move(container_id, x, y)

        elif input_type == "drag":
            x1 = input_data.get("x1", 0)
            y1 = input_data.get("y1", 0)
            x2 = input_data.get("x2", 100)
            y2 = input_data.get("y2", 100)
            button = input_data.get("button", "left")
            result = send_mouse_drag(container_id, x1, y1, x2, y2, button)

        elif input_type == "scroll":
            x = input_data.get("x", 512)
            y = input_data.get("y", 384)
            direction = input_data.get("direction", "down")
            clicks = input_data.get("clicks", 3)
            result = send_scroll(container_id, x, y, direction, clicks)

        else:
            return error("Unknown input type: {}".format(input_type), "INVALID_TYPE")

    except KeyError as exc:
        return error(str(exc), "NOT_FOUND")
    except Exception as exc:
        return error("Input failed: {}".format(exc), "INPUT_ERROR")

    return ok(result)
