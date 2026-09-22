from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from domain.frontend_settings import frontend_settings_path


TOOL_ASSIST_DEFAULT_MODE = "auto"
TOOL_ASSIST_AUTO_MODE = "auto"
TOOL_ASSIST_VECTOR_MODE = "vector"
TOOL_ASSIST_ALL_SCHEMAS_MODE = "all_schemas"
TOOL_ASSIST_MODES = {
    TOOL_ASSIST_AUTO_MODE,
    TOOL_ASSIST_VECTOR_MODE,
    TOOL_ASSIST_ALL_SCHEMAS_MODE,
    "off",
}
TOOL_ASSIST_LEGACY_MODE_ALIASES: dict[str, str] = {
    "all": TOOL_ASSIST_AUTO_MODE,
    "manual": "off",
}
DEFAULT_TOOL_RECOMMENDATION_LIMIT = 8
DEFAULT_TOOL_RECOMMENDATION_THRESHOLD = 0.08

_WORD_RE = re.compile(r"[A-Za-z0-9_./:-]+|[\u3040-\u30ff\u3400-\u9fff]+")
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_SIDECAR_DOC_NAMES = ("README.md", "README.ja.md", "SKILL.md", "docs.md", "DOCUMENTATION.md")
_MAX_DOC_CHARS = 20_000

_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("coding", "code", "source", "repo", "repository", "workspace", "コード", "コーディング"),
    ("file", "files", "path", "workspace", "document", "ファイル", "パス"),
    ("read", "reader", "open", "cat", "view", "読む", "読んで", "読み取り", "表示"),
    ("write", "create", "edit", "update", "patch", "modify", "save", "書く", "作成", "編集", "更新", "修正"),
    ("search", "find", "lookup", "query", "grep", "ripgrep", "検索", "探す", "調べる"),
    ("web", "internet", "browser", "online", "ウェブ", "ネット"),
    ("tool", "tools", "schema", "json", "ツール"),
    ("skill", "prompt", "instructions", "system", "スキル", "プロンプト"),
)

_PHRASE_EXPANSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("コード", ("coding", "code", "workspace", "file", "edit", "patch", "read", "search")),
    ("コーディング", ("coding", "code", "workspace", "file", "edit", "patch")),
    ("ファイル", ("file", "workspace", "path", "read", "write", "create", "search")),
    ("読む", ("read", "file", "workspace")),
    ("読ん", ("read", "file", "workspace")),
    ("書", ("write", "create", "edit", "file")),
    ("編集", ("edit", "patch", "modify", "write", "file")),
    ("修正", ("patch", "modify", "edit", "file")),
    ("検索", ("search", "find", "query")),
    ("探", ("search", "find")),
    ("調べ", ("search", "web", "query")),
    ("ウェブ", ("web", "search", "internet")),
    ("ネット", ("web", "search", "internet")),
    ("ツール", ("tool", "tools", "schema", "json")),
    ("スキル", ("skill", "prompt", "instructions")),
    ("プロンプト", ("skill", "prompt", "instructions")),
)

_SYNONYM_INDEX: dict[str, tuple[str, ...]] = {}
for group in _SYNONYM_GROUPS:
    normalized_group = tuple(item.casefold() for item in group)
    for token in normalized_group:
        _SYNONYM_INDEX[token] = normalized_group


def effective_tool_assist_mode(settings: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> str:
    """Return the selected tool-assist mode.

    The setting defaults to lexical auto-selection. Legacy "all" is treated as
    auto so enabling omitted-tools auto selection does not suddenly expose every
    registered tool. Use "all_schemas" only for explicit debug compatibility.
    """

    values = settings if isinstance(settings, dict) else _read_frontend_settings(pack_root)
    tools = values.get("tools") if isinstance(values, dict) else {}
    tools = tools if isinstance(tools, dict) else {}
    if tools.get("tool_assist_enabled") is False:
        return "off"
    mode = str(tools.get("tool_assist_mode") or TOOL_ASSIST_DEFAULT_MODE).strip().lower()
    mode = TOOL_ASSIST_LEGACY_MODE_ALIASES.get(mode, mode)
    return mode if mode in TOOL_ASSIST_MODES else TOOL_ASSIST_DEFAULT_MODE


def tool_assist_limit(settings: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> int:
    values = settings if isinstance(settings, dict) else _read_frontend_settings(pack_root)
    tools = values.get("tools") if isinstance(values, dict) else {}
    tools = tools if isinstance(tools, dict) else {}
    try:
        limit = int(tools.get("tool_assist_limit", DEFAULT_TOOL_RECOMMENDATION_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_TOOL_RECOMMENDATION_LIMIT
    return max(1, min(24, limit))


def recommend_tool_ids(
    user_text: str,
    tools: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_TOOL_RECOMMENDATION_LIMIT,
    threshold: float = DEFAULT_TOOL_RECOMMENDATION_THRESHOLD,
) -> list[str]:
    return [item["tool_id"] for item in search_tools(user_text, tools, limit=limit, threshold=threshold)]


def search_tools(
    user_text: str,
    tools: list[dict[str, Any]],
    *,
    limit: int = DEFAULT_TOOL_RECOMMENDATION_LIMIT,
    threshold: float = DEFAULT_TOOL_RECOMMENDATION_THRESHOLD,
    include_schema: bool = False,
) -> list[dict[str, Any]]:
    query_vector = _text_vector(user_text)
    if not query_vector:
        return []
    scored: list[tuple[float, str, dict[str, Any], Counter[str]]] = []
    for tool in tools:
        tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip()
        if not tool_id:
            continue
        tool_vector = _tool_vector(tool)
        score = _cosine_similarity(query_vector, tool_vector)
        score += _exact_boost(user_text, tool)
        if score >= threshold:
            scored.append((score, tool_id, tool, tool_vector))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        _tool_search_result(tool, tool_id=tool_id, score=score, query_vector=query_vector, tool_vector=tool_vector, include_schema=include_schema)
        for score, tool_id, tool, tool_vector in scored[: max(1, limit)]
    ]


def _read_frontend_settings(pack_root: Path | None = None) -> dict[str, Any]:
    path = frontend_settings_path(pack_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_vector(tool: dict[str, Any]) -> Counter[str]:
    return _text_vector(" ".join(_tool_text_parts(tool)))


def _tool_text_parts(tool: dict[str, Any]) -> list[str]:
    parts: list[str] = [
        str(tool.get("tool_id") or ""),
        str(tool.get("name") or ""),
        str(tool.get("summary") or ""),
        str(tool.get("description") or ""),
        str(tool.get("category") or ""),
        str(tool.get("action_type") or ""),
        str(tool.get("approval_policy") or ""),
        " ".join(str(tag) for tag in tool.get("tags", []) if tag),
        " ".join(str(skill) for skill in tool.get("skills", []) if skill),
    ]
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    ui = tool.get("ui") if isinstance(tool.get("ui"), dict) else {}
    for container in (metadata, ui):
        parts.extend(
            str(container.get(key) or "")
            for key in (
                "description",
                "summary",
                "keywords",
                "group_id",
                "label",
                "source",
                "server_id",
                "server_name",
                "mcp_tool_name",
                "docs",
                "documentation",
                "help",
                "skill_triggers",
                "skill_instructions",
            )
        )
        for key in ("skills", "required_skills", "skill_ids", "keywords", "aliases", "triggers", "docs"):
            value = container.get(key)
            if isinstance(value, list):
                parts.append(" ".join(str(item) for item in value if item))
            elif isinstance(value, dict):
                parts.append(_flatten_text(value))
            elif value:
                parts.append(str(value))
    parts.extend(_manifest_sidecar_texts(metadata))
    schema = tool.get("schema") if isinstance(tool.get("schema"), dict) else {}
    parameters = schema.get("parameters") if isinstance(schema.get("parameters"), dict) else schema
    if isinstance(parameters, dict) and isinstance(parameters.get("inputSchema"), dict):
        parameters = parameters["inputSchema"]
    properties = parameters.get("properties") if isinstance(parameters, dict) else {}
    if isinstance(properties, dict):
        parts.append(" ".join(str(name) for name in properties.keys()))
        for value in properties.values():
            if isinstance(value, dict):
                parts.append(" ".join(str(value.get(key) or "") for key in ("title", "description")))
    return parts


def _tool_search_result(
    tool: dict[str, Any],
    *,
    tool_id: str,
    score: float,
    query_vector: Counter[str],
    tool_vector: Counter[str],
    include_schema: bool,
) -> dict[str, Any]:
    overlap = sorted((set(query_vector) & set(tool_vector)), key=lambda token: (-(query_vector[token] * tool_vector[token]), token))
    result = {
        "tool_id": tool_id,
        "name": str(tool.get("name") or tool_id),
        "summary": str(tool.get("summary") or tool.get("description") or ""),
        "category": str(tool.get("category") or ""),
        "tags": [str(tag) for tag in (tool.get("tags") or []) if str(tag).strip()],
        "skills": [str(skill) for skill in (tool.get("skills") or []) if str(skill).strip()],
        "risk": str(tool.get("risk") or ((tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}).get("risk")) or ""),
        "score": round(float(score), 4),
        "why": overlap[:8],
    }
    if include_schema:
        result["schema"] = tool.get("schema") or {}
        result["usage"] = {
            "phase": "schema",
            "rule": "Use this schema only after choosing the tool by capability; fill JSON arguments according to the tool's parameter contract.",
        }
    else:
        result["usage"] = {
            "phase": "overview",
            "rule": "Use this result to decide whether the tool is relevant; request schema details only for tools you intend to call.",
        }
    return result


def _text_vector(text: str) -> Counter[str]:
    normalized = str(text or "").casefold()
    expansions = _phrase_expansion_terms(normalized)
    if expansions:
        normalized = "{} {}".format(normalized, " ".join(expansions))
    vector: Counter[str] = Counter()
    for token in _WORD_RE.findall(normalized):
        token = token.strip(" \t\r\n.,!?()[]{}")
        if not token:
            continue
        vector[token] += 2
        for synonym in _SYNONYM_INDEX.get(token, ()):
            if synonym != token:
                vector[synonym] += 1
        if "_" in token:
            for part in token.split("_"):
                if part:
                    vector[part] += 1
                    for synonym in _SYNONYM_INDEX.get(part, ()):
                        if synonym != part:
                            vector[synonym] += 1
        if _JAPANESE_RE.search(token):
            for gram in _char_ngrams(token):
                vector[gram] += 1
    return vector


def _phrase_expansion_terms(normalized_text: str) -> list[str]:
    terms: list[str] = []
    for phrase, expansion in _PHRASE_EXPANSIONS:
        if phrase.casefold() in normalized_text:
            terms.extend(expansion)
    return terms


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "")


def _manifest_sidecar_texts(metadata: dict[str, Any]) -> list[str]:
    manifest_path = metadata.get("manifest_path") or metadata.get("component_manifest_path")
    if not isinstance(manifest_path, str) or not manifest_path.strip():
        return []
    path = Path(manifest_path).expanduser()
    texts: list[str] = []
    for candidate in [path, *(path.parent / name for name in _SIDECAR_DOC_NAMES)]:
        try:
            if candidate.is_file():
                texts.append(candidate.read_text(encoding="utf-8", errors="ignore")[:_MAX_DOC_CHARS])
        except OSError:
            continue
    return texts


def _char_ngrams(token: str, size: int = 2) -> list[str]:
    if len(token) <= size:
        return [token]
    return [token[index:index + size] for index in range(len(token) - size + 1)]


def _cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = set(left) & set(right)
    numerator = sum(left[key] * right[key] for key in overlap)
    if numerator <= 0:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _exact_boost(user_text: str, tool: dict[str, Any]) -> float:
    haystack = str(user_text or "").casefold()
    if not haystack:
        return 0.0
    boost = 0.0
    tool_id = str(tool.get("tool_id") or "").casefold()
    if tool_id and tool_id in haystack:
        boost += 0.25
    for tag in tool.get("tags", []) or []:
        text = str(tag or "").casefold()
        if text and text in haystack:
            boost += 0.08
    return min(boost, 0.4)
