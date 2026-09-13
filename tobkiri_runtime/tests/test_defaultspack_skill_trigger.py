from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_runtime_skill_trigger_accepts_at_mention_alias():
    from domain.skill_trigger import RuntimeSkillTriggerService

    service = RuntimeSkillTriggerService(
        [
            {
                "id": "feedback/live-review",
                "display_name": "Live Review",
                "triggers": ["unrelated-trigger"],
                "instructions": "Require evidence-backed verification.",
            }
        ]
    )

    result = service.evaluate(user_text="この回答は @live-review でチェック", tool_names=[], context={})

    assert result["matched"][0]["id"] == "feedback/live-review"
    assert "evidence-backed" in result["instructions"]


def test_runtime_skill_trigger_reads_skill_md_file(tmp_path):
    from domain.skill_trigger import RuntimeSkillTriggerService

    manifest = tmp_path / "manifest.json"
    prompt = tmp_path / "SKILL.md"
    manifest.write_text("{}", encoding="utf-8")
    prompt.write_text("Always check concrete logs before claiming success.", encoding="utf-8")
    service = RuntimeSkillTriggerService(
        [
            {
                "id": "feedback/log-check",
                "display_name": "Log Check",
                "source_path": str(manifest),
                "triggers": ["logs"],
                "instructions": {"max_tokens": 100},
            }
        ]
    )

    result = service.evaluate(user_text="@log-check", tool_names=[], context={})

    assert result["matched"][0]["instruction"] == "Always check concrete logs before claiming success."
