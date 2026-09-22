from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _parse(text: str, connected: set[str] | None = None):
    from domain.chat.stream_engine import _text_tool_call_blocks

    return _text_tool_call_blocks(
        {"content": [{"type": "text", "text": text}]},
        {"rumi_api"} if connected is None else connected,
    )


def test_exact_single_connected_text_tool_call_is_recovered():
    blocks = _parse(
        """
<tool_call>
<function=rumi_api>
<parameter=action>list_routes</parameter>
</function>
</tool_call>
"""
    )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_use"
    assert blocks[0]["name"] == "rumi_api"
    assert blocks[0]["input"] == {"action": "list_routes"}
    assert blocks[0]["metadata"] == {
        "recovered_from_text_tool_call": True,
        "text_tool_syntax": "tool_call",
    }


def test_text_tool_parameters_decode_json_and_html_entities():
    blocks = _parse(
        """<tool_call><function=rumi_api>
<parameter=action>call_route</parameter>
<parameter=payload>{&quot;path&quot;:&quot;/api/routes&quot;,&quot;limit&quot;:3}</parameter>
</function></tool_call>"""
    )

    assert blocks[0]["input"] == {
        "action": "call_route",
        "payload": {"path": "/api/routes", "limit": 3},
    }


def test_unknown_tool_name_remains_plain_assistant_text():
    assert _parse(
        "<tool_call><function=not_connected><parameter=action>list_routes</parameter></function></tool_call>"
    ) == []


def test_empty_connected_tool_allowlist_does_not_recover_a_tool() -> None:
    """An explicitly empty selection must never inherit the fixture default."""
    assert _parse(
        "<tool_call><function=rumi_api><parameter=action>list_routes</parameter>"
        "</function></tool_call>",
        connected=set(),
    ) == []


def test_prose_before_or_after_tool_block_is_not_executed():
    block = "<tool_call><function=rumi_api><parameter=action>list_routes</parameter></function></tool_call>"

    assert _parse(f"I will inspect the routes.\n{block}") == []
    assert _parse(f"{block}\nInspection complete.") == []


def test_multiple_text_tool_blocks_are_not_recovered_in_strict_mode():
    first = "<tool_call><function=rumi_api><parameter=action>list_routes</parameter></function></tool_call>"
    second = "<tool_call><function=rumi_api><parameter=action>health</parameter></function></tool_call>"

    assert _parse(first + second) == []


def test_malformed_or_unparsed_body_is_not_executed():
    assert _parse(
        "<tool_call><function=rumi_api><parameter=action>list_routes</function></tool_call>"
    ) == []
    assert _parse(
        "<tool_call><function=rumi_api>run list_routes now</function></tool_call>"
    ) == []


def test_non_tool_prose_is_unchanged():
    assert _parse("The route list is currently unavailable.") == []
