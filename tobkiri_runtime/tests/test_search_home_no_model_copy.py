from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PACK_ROOT = ROOT / "ecosystem" / "search_home_pack"
FORBIDDEN = [
    "domain.ai_client.gateway",
    "LLMGateway",
    "AIClient",
    "ModelRuntimeSettingsService",
    "model_profiles",
    "model_router",
    "provider_registry",
    "api_keys",
    "preferred_model",
]


def _implementation_files() -> list[Path]:
    files = [PACK_ROOT / "desktop_app.py"]
    files.extend(sorted((PACK_ROOT / "domain").rglob("*.py")))
    files.extend(sorted((PACK_ROOT / "webapp" / "src").rglob("*.ts")))
    files.extend(sorted((PACK_ROOT / "webapp" / "src").rglob("*.tsx")))
    return [
        path
        for path in files
        if path.is_file() and not path.name.endswith((".test.ts", ".test.tsx"))
    ]


def test_search_home_pack_does_not_copy_defaultspack_model_runtime_logic():
    offenders: list[str] = []
    for path in _implementation_files():
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(ROOT)}: {token}")

    assert offenders == []
