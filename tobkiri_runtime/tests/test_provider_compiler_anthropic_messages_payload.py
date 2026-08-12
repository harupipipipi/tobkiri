from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provider_payloads"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_anthropic_messages_compiler_tool_use_snapshot_and_parser():
    from domain.ai_client.bridge_plan import PlannedProviderRequest
    from domain.ai_client.provider_compiler.anthropic_messages import AnthropicMessagesCompiler
    from domain.chat.ir_legacy_adapter import legacy_standard_messages_to_ir

    compiler = AnthropicMessagesCompiler()
    planned = PlannedProviderRequest(
        ir=legacy_standard_messages_to_ir([{"role": "assistant", "content": None, "tool_calls": [{"id": "tc", "function": {"name": "lookup", "arguments": "{}"}}]}], "c"),
        model="claude-test",
        provider_capabilities={"provider_id": "anthropic", "api_family": "anthropic_messages"},
        provider_tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        params={},
    )
    compiled = compiler.compile_complete(planned)
    parsed = compiler.parse_response(
        {
            "content": [
                {"type": "thinking", "thinking": "private plan"},
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "tc2", "name": "lookup", "input": {}},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        compiled,
    )

    assert compiled.body == json.loads((FIXTURES / "anthropic_messages_tool_use.json").read_text())
    assert [block.type for block in parsed.content] == ["text", "tool_call"]
    assert parsed.content[1].tool_call.id == "tc2"
    assert parsed.metadata["thinking"]["transcript"] == "private plan"
