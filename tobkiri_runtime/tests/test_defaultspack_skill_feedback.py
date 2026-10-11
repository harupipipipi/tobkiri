from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_skill_create_from_feedback_writes_valid_skill_and_dream(monkeypatch, tmp_path):
    from blocks.skill.create_from_feedback import run as create_skill
    from domain import skill_feedback
    from domain.extensions import runtime as extension_runtime
    from domain.extensions.runtime import get_extension_registry

    extensions_root = tmp_path / "user_data" / "shared" / "extensions"
    monkeypatch.setattr(skill_feedback, "_skills_root", lambda payload: extensions_root / "skills")
    monkeypatch.setattr(
        extension_runtime,
        "get_extensions_roots",
        lambda: extension_runtime.build_extensions_roots(
            DEFAULTSPACK_ROOT,
            extra_roots=[extensions_root],
        ),
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_MEMORY2_DIR", str(tmp_path / "memory2"))

    result = create_skill(
        {
            "feedback": "次からLINE groupではメンションされた時だけ反応して",
            "name": "line mention correction",
            "triggers": ["LINE", "mention"],
            "applies_to_tools": ["line_reply"],
            "conversation_id": "c1",
        },
        {},
    )

    assert result["status"] == "ok"
    data = result["data"]
    manifest_path = Path(data["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["category"] == "skill"
    assert manifest["enabled"] is False
    assert manifest["triggers"] == ["LINE", "mention"]
    assert manifest["applies_to_tools"] == ["line_reply"]
    assert data["activation_required"] is True
    assert Path(data["dream_path"]).read_text(encoding="utf-8").count("[feedback-skill]") == 1
    skills = get_extension_registry(force_reload=True).skills().list(enabled_only=False)
    assert any(item["id"] == data["skill_id"] for item in skills)
    assert not any(item["id"] == data["skill_id"] for item in get_extension_registry(force_reload=True).skills().list())


def test_skill_create_from_feedback_rejects_public_extensions_root(monkeypatch, tmp_path):
    from blocks.skill.create_from_feedback import run as create_skill
    from domain import skill_feedback

    safe_root = tmp_path / "safe" / "extensions"
    attacker_root = tmp_path / "attacker"
    monkeypatch.setattr(skill_feedback, "_skills_root", lambda payload: safe_root / "skills")

    result = create_skill(
        {
            "feedback": "勝手に外部rootへskillを書かない",
            "extensions_root": str(attacker_root),
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_INPUT"
    assert not (attacker_root / "skills").exists()
