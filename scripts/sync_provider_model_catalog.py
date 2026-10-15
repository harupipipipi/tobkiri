#!/usr/bin/env python3
"""Refresh credential-free provider model JSON and its Defaults UI projection.

Network access is explicit (--refresh) and never happens during app startup or
builds. The snapshot is a discovery catalog, not account or capability evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "tobkiri_runtime/ecosystem/rumi_model_catalog_pack"
CATALOG = PACK / "catalog/provider-setup.json"
PROJECTION = ROOT / (
    "tobkiri_runtime/ecosystem/defaultspack/webapp/src/lib/"
    "providerModelCatalog.generated.json"
)
SOURCES = {
    "models.dev": "https://models.dev/api.json",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "avian": "https://api.avian.io/v1/models",
    "sambanova": "https://api.sambanova.ai/v1/models",
}
MAX_MODEL_FILE_BYTES = 512 * 1024


def _read(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("provider model source must be an object")
    return result


def _load_setup_catalog() -> dict[str, Any]:
    catalog = _read(CATALOG)
    for provider_id, descriptor in catalog["providers"].items():
        model_path = CATALOG.parent / "setup" / f"{provider_id}.json"
        if descriptor["models_file"] != f"setup/{provider_id}.json":
            raise ValueError("provider model file is invalid")
        data = _read(model_path)
        if data.get("provider_id") != provider_id:
            raise ValueError("provider model file identity is invalid")
        descriptor["models"] = data["models"]
    return catalog


def _write_setup_catalog(catalog: dict[str, Any]) -> None:
    index = {**catalog, "providers": {}}
    files: dict[Path, str] = {}
    for provider_id, provider in catalog["providers"].items():
        models_file = f"setup/{provider_id}.json"
        document = {"provider_id": provider_id, "models": provider["models"]}
        output = json.dumps(document, ensure_ascii=False, indent=1) + "\n"
        if len(output.encode("utf-8")) > MAX_MODEL_FILE_BYTES:
            raise ValueError(f"{provider_id} model JSON exceeds its byte budget")
        files[CATALOG.parent / models_file] = output
        index["providers"][provider_id] = {
            **{key: value for key, value in provider.items() if key != "models"},
            "models_file": models_file,
        }
    for path, output in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    CATALOG.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _model_type(inputs: list[str], outputs: list[str], reasoning: bool) -> str:
    if "image" in outputs:
        return "image"
    if "video" in outputs:
        return "video"
    if "audio" in outputs and "text" not in outputs:
        return "audio"
    if "embedding" in outputs or "embeddings" in outputs:
        return "embedding"
    if "text" in inputs and "text" in outputs:
        return "reasoning" if reasoning else "chat"
    return "unknown"


def normalize_models_dev(raw: dict[str, Any]) -> dict[str, Any]:
    """Retain provider-specific identifiers and declared model metadata."""
    modalities = raw.get("modalities") or {}
    inputs = modalities.get("input") or ["text"]
    outputs = modalities.get("output") or ["text"]
    limit = raw.get("limit") or {}
    reasoning = raw.get("reasoning") is True
    return {
        "model_id": raw["id"],
        "display_name": raw.get("name") or raw["id"],
        "type": _model_type(inputs, outputs, reasoning),
        "context_window": limit.get("context") or 0,
        "max_output_tokens": limit.get("output") or 0,
        "capabilities": {
            "text_input": "text" in inputs,
            "text_output": "text" in outputs,
            "image_input": "image" in inputs,
            "audio_input": "audio" in inputs,
            "thinking": reasoning,
            "tool_calling": raw.get("tool_call") is True,
            "structured_output": raw.get("structured_output") is True,
        },
        "modalities": {"input": inputs, "output": outputs},
        "pricing": raw.get("cost") or {},
        "source": "models.dev",
    }


def normalize_openrouter(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep every model from the official public OpenRouter inventory."""
    architecture = raw.get("architecture") or {}
    inputs = architecture.get("input_modalities") or []
    outputs = architecture.get("output_modalities") or []
    parameters = raw.get("supported_parameters") or []
    reasoning = any(
        item in parameters for item in ("reasoning", "reasoning_effort")
    )
    return {
        "model_id": raw["id"],
        "display_name": raw.get("name") or raw["id"],
        "type": _model_type(inputs, outputs, reasoning),
        "context_window": raw.get("context_length") or 0,
        "max_output_tokens": (raw.get("top_provider") or {}).get(
            "max_completion_tokens"
        ) or 0,
        "capabilities": {
            "text_input": "text" in inputs,
            "text_output": "text" in outputs,
            "image_input": "image" in inputs,
            "audio_input": "audio" in inputs,
            "thinking": reasoning,
            "tool_calling": "tools" in parameters,
            "structured_output": "response_format" in parameters,
        },
        "modalities": {"input": inputs, "output": outputs},
        "pricing": raw.get("pricing") or {},
        "supported_parameters": parameters,
        "source": "openrouter",
    }


def _bundled_models(provider_id: str) -> list[dict[str, Any]]:
    paths = [PACK / f"catalog/providers/{provider_id}/models.json"]
    paths += sorted(
        (PACK / f"extensions/llm/providers/{provider_id}/models").glob("*.json")
    )
    models: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        document = _read(path)
        for raw in document.get("models", [document]):
            model_id = str(raw.get("model_id") or raw.get("id") or "")
            if not model_id:
                continue
            models[model_id] = {
                "model_id": model_id,
                "display_name": raw.get("display_name") or model_id,
                "type": raw.get("type") or "chat",
                "context_window": raw.get("context_window") or 0,
                "capabilities": raw.get("capabilities") or {},
                "source": "bundled",
            }
    return list(models.values())


def refresh(
    catalog: dict[str, Any], models_dev: Path, openrouter: Path, retrieved: str,
    public_inventories: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Normalize complete snapshots; retain bundled models for absent sources."""
    upstream = _read(models_dev)
    router = _read(openrouter).get("data")
    if not isinstance(router, list) or not router:
        raise ValueError("OpenRouter returned no model inventory")
    for provider_id, provider in catalog["providers"].items():
        if provider_id == "openrouter":
            models = [normalize_openrouter(raw) for raw in router]
        else:
            source = upstream.get(provider.get("models_dev_id"), {})
            models = [
                normalize_models_dev(raw)
                for raw in source.get("models", {}).values()
            ]
            if not models:
                models = _bundled_models(provider_id)
        if provider_id in (public_inventories or {}):
            data = _read(public_inventories[provider_id]).get("data")
            if not isinstance(data, list) or not data:
                raise ValueError(f"{provider_id} returned no model inventory")
            models = [{
                "model_id": raw["id"],
                "display_name": raw.get("display_name") or raw["id"],
                "type": "reasoning" if raw.get("reasoning") is True else "chat",
                "context_window": raw.get("context_length") or 0,
                "max_output_tokens": raw.get("max_output")
                or raw.get("max_completion_tokens") or 0,
                "pricing": raw.get("pricing") or {},
                "capabilities": {},
                "source": provider_id,
            } for raw in data]
        by_id = {model["model_id"]: model for model in models}
        provider["models"] = [by_id[key] for key in sorted(by_id)]
    catalog["sources"] = {
        name: {
            "url": SOURCES[name],
            "retrieved_on": retrieved,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            **({"license": "MIT", "license_file": "licenses/models.dev-MIT.txt"}
               if name == "models.dev" else {}),
        }
        for name, path in {
            "models.dev": models_dev, "openrouter": openrouter,
            **(public_inventories or {}),
        }.items()
    }
    return catalog


def projection(catalog: dict[str, Any]) -> dict[str, Any]:
    """Project endpoint presets and complete model choices without secrets."""
    return {
        "version": catalog["version"],
        "sources": catalog["sources"],
        "providers": {
            key: {
                "endpoint": provider["endpoint"],
                "protocol": provider["protocol"],
                "display_name": provider["display_name"],
                "models": [
                    {field: model[field] for field in (
                        "model_id", "display_name", "type",
                    )}
                    for model in provider["models"]
                ],
            }
            for key, provider in catalog["providers"].items()
        },
    }


def main() -> int:
    """Run an explicit refresh or verify the deterministic UI projection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--models-dev", type=Path)
    parser.add_argument("--openrouter", type=Path)
    parser.add_argument("--public-inventory-dir", type=Path)
    parser.add_argument("--retrieved-on", default=date.today().isoformat())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check and args.refresh:
        parser.error("--check cannot refresh network sources")
    catalog = _load_setup_catalog()
    if args.refresh:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="tobkiri-model-catalog-") as temp:
            paths = []
            for name, provided in (("models.dev", args.models_dev),
                                   ("openrouter", args.openrouter)):
                path = provided or Path(temp) / f"{name}.json"
                if provided is None:
                    subprocess.run([
                        "curl", "--fail", "--silent", "--show-error", "--location",
                        "--max-time", "30", "--max-filesize", "16000000",
                        "--user-agent", "Tobkiri-Catalog-Sync/1",
                        SOURCES[name], "--output", str(path),
                    ], check=True)
                paths.append(path)
            public_paths = {}
            for name in ("avian", "sambanova"):
                path = (
                    args.public_inventory_dir / f"{name}.json"
                    if args.public_inventory_dir else Path(temp) / f"{name}.json"
                )
                if args.public_inventory_dir is None:
                    subprocess.run([
                        "curl", "--fail", "--silent", "--show-error", "--location",
                        "--max-time", "30", "--max-filesize", "16000000",
                        SOURCES[name], "--output", str(path),
                    ], check=True)
                public_paths[name] = path
            catalog = refresh(catalog, *paths, args.retrieved_on, public_paths)
        _write_setup_catalog(catalog)
    output = json.dumps(projection(catalog), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not PROJECTION.is_file() or PROJECTION.read_text(encoding="utf-8") != output:
            raise SystemExit("provider model UI projection is stale")
    else:
        PROJECTION.write_text(output, encoding="utf-8")
    count = sum(len(item["models"]) for item in catalog["providers"].values())
    print(f"Provider catalog: {len(catalog['providers'])} providers, {count} models")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
