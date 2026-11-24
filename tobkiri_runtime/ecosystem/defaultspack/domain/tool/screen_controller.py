"""
domain.tool.screen_controller — コンテナ画面操作。
スクリーンショット取得、キーボード入力、マウスクリック（座標指定）。
Docker コンテナ内で Xvfb + xdotool を利用して座標ベースの操作を行う。
Docker がない local-only モードでは画面操作は無効。
"""
import time

from domain.tool.container_manager import (
    get_container,
    exec_in_container,
)


def _exec(container_id, cmd):
    """コンテナ内でコマンドを実行し結果を返すラッパー"""
    result = exec_in_container(container_id, cmd)
    return result


def take_screenshot(container_id):
    """
    コンテナ内のスクリーンショットを取得する。
    Xvfb + xwd + convert (ImageMagick) or import/scrot を利用。
    戻り値: {"image_base64": str, "format": "png", "timestamp": str}
    """
    info = get_container(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))

    if info.get("status", "").startswith("local") or info.get("status", "").endswith("-local"):
        return {
            "image_base64": "",
            "format": "png",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": "Screenshot not available in local-only mode",
        }

    # DISPLAY=:99 で Xvfb が起動している前提
    # scrot が無ければ xwd + convert にフォールバック
    capture_cmd = (
        "export DISPLAY=:99 && "
        "if command -v scrot >/dev/null 2>&1; then "
        "  scrot -o /tmp/screenshot.png && base64 /tmp/screenshot.png; "
        "elif command -v import >/dev/null 2>&1; then "
        "  import -window root /tmp/screenshot.png && base64 /tmp/screenshot.png; "
        "elif command -v xwd >/dev/null 2>&1; then "
        "  xwd -root -silent -out /tmp/screenshot.xwd && "
        "  convert /tmp/screenshot.xwd /tmp/screenshot.png && "
        "  base64 /tmp/screenshot.png; "
        "else "
        "  echo 'ERROR:no_screenshot_tool'; "
        "fi"
    )

    result = _exec(container_id, capture_cmd)
    output = result.get("output", "").strip()

    if result.get("exit_code", -1) != 0 or output.startswith("ERROR:"):
        return {
            "image_base64": "",
            "format": "png",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "error": "Screenshot capture failed: {}".format(output[:200]),
        }

    return {
        "image_base64": output,
        "format": "png",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def send_mouse_click(container_id, x, y, button="left"):
    """
    コンテナ画面の指定座標にマウスクリックを送信する。
    xdotool を利用。
    """
    info = get_container(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))

    if info.get("status", "").startswith("local") or info.get("status", "").endswith("-local"):
        return {
            "success": False,
            "error": "Mouse input not available in local-only mode",
        }

    button_num = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
    cmd = "export DISPLAY=:99 && xdotool mousemove {x} {y} && xdotool click {btn}".format(
        x=int(x), y=int(y), btn=button_num
    )

    result = _exec(container_id, cmd)
    success = result.get("exit_code", -1) == 0
    return {
        "success": success,
        "action": "click",
        "x": int(x),
        "y": int(y),
        "button": button,
        "output": result.get("output", ""),
    }


def send_keyboard_input(container_id, text=None, key=None):
    """
    コンテナにキーボード入力を送信する。
    text: 文字列を入力する場合（xdotool type）
    key: 特殊キーを送信する場合（xdotool key, 例: "Return", "ctrl+c"）
    """
    info = get_container(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))

    if info.get("status", "").startswith("local") or info.get("status", "").endswith("-local"):
        return {
            "success": False,
            "error": "Keyboard input not available in local-only mode",
        }

    if text is not None and key is not None:
        # 両方指定: text 入力の後に key 送信
        escaped = text.replace("'", "'\\''")
        cmd = "export DISPLAY=:99 && xdotool type -- '{txt}' && xdotool key {k}".format(
            txt=escaped, k=key
        )
    elif text is not None:
        escaped = text.replace("'", "'\\''")
        cmd = "export DISPLAY=:99 && xdotool type -- '{}'".format(escaped)
    elif key is not None:
        cmd = "export DISPLAY=:99 && xdotool key {}".format(key)
    else:
        return {"success": False, "error": "Either text or key must be specified"}

    result = _exec(container_id, cmd)
    success = result.get("exit_code", -1) == 0
    return {
        "success": success,
        "action": "keyboard",
        "text": text,
        "key": key,
        "output": result.get("output", ""),
    }


def send_mouse_move(container_id, x, y):
    """マウスを指定座標に移動する（クリックなし）"""
    info = get_container(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))

    if info.get("status", "").startswith("local") or info.get("status", "").endswith("-local"):
        return {"success": False, "error": "Mouse input not available in local-only mode"}

    cmd = "export DISPLAY=:99 && xdotool mousemove {} {}".format(int(x), int(y))
    result = _exec(container_id, cmd)
    return {
        "success": result.get("exit_code", -1) == 0,
        "action": "move",
        "x": int(x),
        "y": int(y),
    }


def send_mouse_drag(container_id, x1, y1, x2, y2, button="left"):
    """マウスドラッグを送信する"""
    info = get_container(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))

    if info.get("status", "").startswith("local") or info.get("status", "").endswith("-local"):
        return {"success": False, "error": "Mouse input not available in local-only mode"}

    btn_num = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
    cmd = (
        "export DISPLAY=:99 && "
        "xdotool mousemove {x1} {y1} && "
        "xdotool mousedown {btn} && "
        "xdotool mousemove {x2} {y2} && "
        "xdotool mouseup {btn}"
    ).format(x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2), btn=btn_num)

    result = _exec(container_id, cmd)
    return {
        "success": result.get("exit_code", -1) == 0,
        "action": "drag",
        "from": [int(x1), int(y1)],
        "to": [int(x2), int(y2)],
    }


def send_scroll(container_id, x, y, direction="down", clicks=3):
    """マウススクロールを送信する"""
    info = get_container(container_id)
    if info is None:
        raise KeyError("container not found: {}".format(container_id))

    if info.get("status", "").startswith("local") or info.get("status", "").endswith("-local"):
        return {"success": False, "error": "Scroll not available in local-only mode"}

    button = 5 if direction == "down" else 4
    cmd = "export DISPLAY=:99 && xdotool mousemove {x} {y}".format(x=int(x), y=int(y))
    for _ in range(int(clicks)):
        cmd += " && xdotool click {}".format(button)

    result = _exec(container_id, cmd)
    return {
        "success": result.get("exit_code", -1) == 0,
        "action": "scroll",
        "x": int(x),
        "y": int(y),
        "direction": direction,
        "clicks": int(clicks),
    }
