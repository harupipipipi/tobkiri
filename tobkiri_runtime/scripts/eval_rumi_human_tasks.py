#!/usr/bin/env python3
"""Parallel smoke evaluator for a human-launched Rumi API server.

The script intentionally sends ordinary user prompts with no selected tools.
It is meant for finding transport, timeout, and provider failures while the
desktop app/API server is already running the way a human would launch it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_MODEL = "google/gemma-4-31b-it"
DEFAULT_TIMEOUT_SECONDS = 240.0
DEFAULT_WORKERS = 3

DEFAULT_PROMPTS = [
    {
        "id": "ja_daily_planning",
        "prompt": "明日の朝にやることを、現実的な順番で5つに整理して。短めで。",
    },
    {
        "id": "en_email_polish",
        "prompt": "Rewrite this politely but casually: I cannot make the meeting today. Can we move it to next week?",
    },
    {
        "id": "ja_reasoning",
        "prompt": "AさんはBさんより2歳年上、BさんはCさんより3歳年下。Cさんが20歳ならAさんは何歳？途中式も少しだけ。",
    },
    {
        "id": "code_explain",
        "prompt": "Pythonのlist内包表記を、初心者向けに3行の例で説明して。",
    },
    {
        "id": "creative",
        "prompt": "雨の日に集中するための小さな工夫を、少し詩的だけど実用的に教えて。",
    },
    {
        "id": "no_web_current_events",
        "prompt": "Web検索なしで答えて。最近のニュースではなく、一般論としてAIツールを比較するときの観点を5つ。",
    },
]


class HttpJsonError(Exception):
    def __init__(self, status: int, payload: Any, message: str):
        super().__init__(message)
        self.status = status
        self.payload = payload


@dataclasses.dataclass
class EvalResult:
    task_id: str
    ok: bool
    classification: str
    attempts: int
    elapsed_seconds: float
    conversation_id: str = ""
    http_status: int | None = None
    error: str = ""
    response_preview: str = ""
    response_chars: int = 0
    turn_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _read_token(path: Path | None) -> str:
    from core_runtime.host_contract import host_contract_value

    env_token = host_contract_value("desktop_api_token")
    if env_token:
        return env_token
    candidates: list[Path] = []
    if path is not None:
        candidates.append(path)
    root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            root / ".desktop_api_token",
            root.parent / ".desktop_api_token",
        ]
    )
    for candidate in candidates:
        try:
            token = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if len(token) >= 8:
            return token
    try:
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from core_runtime.hmac_key_manager import HMACKeyManager

        token = HMACKeyManager().get_active_key()
        if token:
            return str(token).strip()
    except Exception:
        pass
    return ""


def _json_request(
    base_url: str,
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            if not data:
                return response.status, {}
            return response.status, json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload: Any = raw.decode("utf-8", errors="replace")[:1000] if raw else ""
        try:
            payload = json.loads(payload) if payload else ""
        except json.JSONDecodeError:
            pass
        raise HttpJsonError(
            exc.code,
            payload,
            "HTTP {}: {}".format(exc.code, payload or exc.reason),
        ) from exc


def _classify_exception(exc: BaseException) -> str:
    if isinstance(exc, HttpJsonError):
        if exc.status == 401:
            return "unauthorized"
        if exc.status == 404:
            return "not_found"
        if exc.status >= 500:
            return "server_error"
        return "http_error"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(reason, ConnectionRefusedError):
            return "server_unreachable"
        return "transport_error"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "server_unreachable"
    return "unexpected_error"


def _extract_response_text(envelope: dict[str, Any]) -> str:
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return ""
    nested = data.get("data")
    if isinstance(nested, dict):
        nested_text = _extract_response_text({"data": nested})
        if nested_text:
            return nested_text
    messages = data.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                message_text = _extract_text_from_message(message)
                if message_text:
                    return message_text
    raw_text = data.get("raw_text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()
    content = data.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts).strip()
    return ""


def _extract_text_from_message(message: dict[str, Any]) -> str:
    raw_text = message.get("raw_text")
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts).strip()
    return ""


def _extract_preview(envelope: dict[str, Any]) -> str:
    return _extract_response_text(envelope)[:240]


def _is_ok_chat_response(envelope: dict[str, Any]) -> bool:
    if not isinstance(envelope, dict):
        return False
    if envelope.get("status") == "ok":
        return bool(_extract_response_text(envelope) or envelope.get("data"))
    if envelope.get("success") is True:
        data = envelope.get("data")
        if isinstance(data, dict) and data.get("status") == "ok":
            return bool(_extract_response_text(data) or data.get("data"))
        return bool(data)
    return False


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError("{} must be a string or list".format(field_name))
    result = []
    for item in values:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _validate_response_text(task: dict[str, Any], text: str) -> tuple[bool, str]:
    min_chars = int(task.get("min_chars", 20))
    if len(text.strip()) < min_chars:
        return False, "response shorter than min_chars {}".format(min_chars)
    lower_text = text.casefold()
    for expected in _string_list(task.get("must_include"), "must_include"):
        if expected.casefold() not in lower_text:
            return False, "missing required text: {}".format(expected)
    for forbidden in _string_list(task.get("must_not_include"), "must_not_include"):
        if forbidden.casefold() in lower_text:
            return False, "forbidden text present: {}".format(forbidden)
    return True, ""


def _task_turns(task: dict[str, Any]) -> list[dict[str, Any]]:
    turns = task.get("turns")
    if isinstance(turns, list) and turns:
        return turns
    return [
        {
            "prompt": task["prompt"],
            "min_chars": task.get("min_chars", 20),
            "must_include": task.get("must_include", []),
            "must_not_include": task.get("must_not_include", []),
        }
    ]


def _create_conversation(
    base_url: str,
    token: str,
    model: str,
    timeout: float,
    title: str,
) -> str:
    status, envelope = _json_request(
        base_url,
        "POST",
        "/api/chat/conversations",
        token,
        {"title": title, "model": model},
        timeout,
    )
    if status >= 400:
        raise RuntimeError("conversation create failed with HTTP {}".format(status))
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("conversation create returned no id")
    return str(data["id"])


def _get_conversation(
    base_url: str,
    token: str,
    timeout: float,
    conversation_id: str,
) -> tuple[int, dict[str, Any]]:
    return _json_request(
        base_url,
        "GET",
        "/api/chat/conversations/{}".format(
            urllib.parse.quote(conversation_id, safe=""),
        ),
        token,
        None,
        timeout,
    )


def _send_task_once(
    base_url: str,
    token: str,
    model: str,
    timeout: float,
    task: dict[str, Any],
) -> tuple[str, int, dict[str, Any]]:
    conversation_id = _create_conversation(
        base_url,
        token,
        model,
        timeout,
        "eval " + task["id"],
    )
    status, envelope = _send_turn_once(
        base_url,
        token,
        model,
        timeout,
        conversation_id,
        {"prompt": task["prompt"]},
    )
    return conversation_id, status, envelope


def _send_turn_once(
    base_url: str,
    token: str,
    model: str,
    timeout: float,
    conversation_id: str,
    turn: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    payload = {
        "conversation_id": conversation_id,
        "model": model,
        "message": {
            "role": "user",
            "content": turn["prompt"],
            "metadata": {"selected_tools": []},
        },
        "tools": [],
        "params": {
            "temperature": 0.2,
            "tool_policy": {"selected_tools": []},
            "retry": {"enabled": True, "max_attempts": 3},
        },
    }
    status, envelope = _json_request(
        base_url,
        "POST",
        "/api/chat/conversations/{}/messages".format(
            urllib.parse.quote(conversation_id, safe=""),
        ),
        token,
        payload,
        timeout,
    )
    return status, envelope


def run_task(
    base_url: str,
    token: str,
    model: str,
    timeout: float,
    task: dict[str, Any],
    retries: int,
) -> EvalResult:
    started = time.monotonic()
    turns = _task_turns(task)
    attempts = 0
    conversation_id = ""
    last_error = ""
    last_status: int | None = None
    last_classification = "not_run"
    last_preview = ""
    last_response_chars = 0
    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            conversation_id = _create_conversation(
                base_url,
                token,
                model,
                timeout,
                "eval " + task["id"],
            )
            for turn_index, turn in enumerate(turns, start=1):
                status, envelope = _send_turn_once(
                    base_url,
                    token,
                    model,
                    timeout,
                    conversation_id,
                    turn,
                )
                last_status = status
                if not _is_ok_chat_response(envelope):
                    error_payload = envelope.get("error") if isinstance(envelope, dict) else None
                    last_error = json.dumps(
                        error_payload or envelope,
                        ensure_ascii=False,
                    )[:500]
                    last_classification = "app_error"
                    break
                response_text = _extract_response_text(envelope)
                if not response_text:
                    _conversation_status, conversation_envelope = _get_conversation(
                        base_url,
                        token,
                        timeout,
                        conversation_id,
                    )
                    response_text = _extract_response_text(conversation_envelope)
                last_preview = response_text[:240]
                last_response_chars = len(response_text)
                content_ok, content_error = _validate_response_text(
                    turn,
                    response_text,
                )
                if not content_ok:
                    last_error = "turn {}: {}".format(turn_index, content_error)
                    last_classification = "quality_error"
                    break
            else:
                return EvalResult(
                    task_id=task["id"],
                    ok=True,
                    classification="ok",
                    attempts=attempts,
                    elapsed_seconds=time.monotonic() - started,
                    conversation_id=conversation_id,
                    http_status=last_status,
                    response_preview=last_preview,
                    response_chars=last_response_chars,
                    turn_count=len(turns),
                )
        except Exception as exc:
            last_classification = _classify_exception(exc)
            if isinstance(exc, HttpJsonError):
                last_status = exc.status
            last_error = "{}: {}".format(type(exc).__name__, exc)
            if os.environ.get("RUMI_EVAL_DEBUG_TRACEBACK") == "1":
                last_error += "\n" + traceback.format_exc()
        if attempt < retries and last_classification in {
            "timeout",
            "transport_error",
            "server_unreachable",
            "unexpected_error",
        }:
            time.sleep(min(2.0 * (attempt + 1), 8.0))
            continue
        break
    return EvalResult(
        task_id=task["id"],
        ok=False,
        classification=last_classification,
        attempts=attempts,
        elapsed_seconds=time.monotonic() - started,
        conversation_id=conversation_id,
        http_status=last_status,
        error=last_error,
        response_preview=last_preview,
        response_chars=last_response_chars,
        turn_count=len(turns),
    )


def _normalize_turn(
    raw_turn: Any,
    task_id: str,
    turn_index: int,
    default_min_chars: int,
) -> dict[str, Any]:
    if isinstance(raw_turn, str):
        raw = {"prompt": raw_turn}
    elif isinstance(raw_turn, dict):
        raw = raw_turn
    else:
        raise ValueError(
            "task {} turn {} is not an object or string".format(
                task_id,
                turn_index,
            )
        )
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("task {} turn {} has no prompt".format(task_id, turn_index))
    turn: dict[str, Any] = {
        "prompt": prompt,
        "min_chars": int(raw.get("min_chars", default_min_chars)),
    }
    for field_name in ("must_include", "must_not_include"):
        values = _string_list(raw.get(field_name), field_name)
        if values:
            turn[field_name] = values
    return turn


def _load_tasks(path: Path | None, default_min_chars: int) -> list[dict[str, Any]]:
    if path is None:
        return [
            {
                **task,
                "min_chars": int(task.get("min_chars", default_min_chars)),
            }
            for task in DEFAULT_PROMPTS
        ]
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("tasks file must be a JSON list")
    tasks = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("task {} is not an object".format(index))
        task_id = str(item.get("id") or "task_{}".format(index + 1))
        task_default_min_chars = int(item.get("min_chars", default_min_chars))
        turns_raw = item.get("turns")
        if turns_raw is not None:
            if not isinstance(turns_raw, list) or not turns_raw:
                raise ValueError("task {} turns must be a non-empty list".format(task_id))
            turns = [
                _normalize_turn(turn, task_id, turn_index, task_default_min_chars)
                for turn_index, turn in enumerate(turns_raw, start=1)
            ]
            for field_name in ("must_include", "must_not_include"):
                values = _string_list(item.get(field_name), field_name)
                if values and not turns[-1].get(field_name):
                    turns[-1][field_name] = values
            tasks.append({"id": task_id, "turns": turns})
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("task {} has no prompt".format(task_id))
        task: dict[str, Any] = {
            "id": task_id,
            "prompt": prompt,
            "min_chars": task_default_min_chars,
        }
        for field_name in ("must_include", "must_not_include"):
            values = _string_list(item.get(field_name), field_name)
            if values:
                task[field_name] = values
        tasks.append(task)
    return tasks


def _default_output_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    return root / "user_data" / "eval" / ("rumi_human_tasks_" + _now_stamp() + ".jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("RUMI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=os.environ.get("RUMI_EVAL_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--min-chars", type=int, default=20)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--output", type=Path, default=_default_output_path())
    args = parser.parse_args(argv)

    token = _read_token(args.token_file)
    if not token:
        print("RUMI_API_TOKEN or .desktop_api_token is required", file=sys.stderr)
        return 2

    tasks = _load_tasks(args.tasks, args.min_chars)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    print(
        "Running {} tasks against {} with model {} using {} workers".format(
            len(tasks),
            args.base_url,
            args.model,
            args.workers,
        )
    )
    print("Writing JSONL results to {}".format(args.output))

    results: list[EvalResult] = []
    with args.output.open("a", encoding="utf-8") as out:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(
                    run_task,
                    args.base_url,
                    token,
                    args.model,
                    args.timeout,
                    task,
                    max(0, args.retries),
                ): task
                for task in tasks
            }
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results.append(result)
                out.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")
                out.flush()
                status = "ok" if result.ok else result.classification
                print(
                    "[{}] {} in {:.1f}s after {} attempt(s), {} turn(s)".format(
                        result.task_id,
                        status,
                        result.elapsed_seconds,
                        result.attempts,
                        result.turn_count,
                    )
                )

    ok_count = sum(1 for result in results if result.ok)
    by_classification: dict[str, int] = {}
    for result in results:
        by_classification[result.classification] = by_classification.get(result.classification, 0) + 1
    print(
        "Summary: {}/{} ok; classifications={}".format(
            ok_count,
            len(results),
            json.dumps(by_classification, sort_keys=True),
        )
    )
    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
