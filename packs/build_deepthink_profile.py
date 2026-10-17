"""Compose DeepThink + real tools + OpenRouter using only verified Pack data.

This build-time script never reads credentials or starts a product HTTP server.
Existing local Gemma and mock defaults retain their own provider configuration.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

import build
import build_deepthink
import build_deepthink_input
import build_harness_tools
import publish_presets
from recovery import build_generation_reader

ROOT = Path(__file__).resolve().parent
MODEL = "xiaomi/mimo-v2.6-flash"
ORIGIN = "https://openrouter.ai"


def read_manifest(name: str) -> dict[str, Any]:
    return json.loads((ROOT / ("local." + name) / "pack.json").read_text(encoding="utf-8"))


def variant(source: str, name: str, config: dict[str, Any] | None = None,
            network: bool = False,
            asset_overrides: dict[str, Path] | None = None) -> dict[str, Any]:
    """Copy hash-bound files, alter explicit settings, and rebind every digest."""
    original = read_manifest(source)
    directory = ROOT / ("local." + name)
    paths = [entry["path"] for entry in original["files"]]
    overrides = asset_overrides or {}
    unknown = set(overrides) - set(paths)
    if unknown:
        raise ValueError("asset override is not declared by the source Pack: "
                         + ", ".join(sorted(unknown)))
    for relative, selected in overrides.items():
        if not Path(selected).is_file():
            raise FileNotFoundError("asset override source is missing: " + str(selected))
    directory.mkdir(exist_ok=True)
    for relative in paths:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        selected = Path(overrides.get(relative, ROOT / ("local." + source) / relative))
        if selected.resolve() != target.resolve():
            shutil.copyfile(selected, target)
    manifest = copy.deepcopy(original)
    manifest["id"] = "local." + name
    if config is not None:
        build.write_json(directory / "config.json", config)
    if network:
        manifest["permissions"]["network"]["origins"] = [ORIGIN]
        manifest["permissions"]["secrets"] = [{"name": "api-key", "reveal": False, "optional": False}]
    manifest["files"] = []
    return build.bind_files(directory, manifest, paths)


def workflow_harness_variant() -> dict[str, Any]:
    """Declare the overlaid call in its actual Flow principal's authority.

    Overlay code ownership does not replace the base Flow's CallCx principal.
    Keep the defining Pack identity and its authoritative WIT unchanged, while
    pinning this explicit permission/dependency variant only in DeepThink.
    """
    original = read_manifest("harness-chat")
    directory = ROOT / "deepthink-variants/local.harness-chat"
    paths = [entry["path"] for entry in original["files"]]
    for relative in paths:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "local.harness-chat" / relative, target)
    manifest = copy.deepcopy(original)
    manifest["permissions"]["ports"].append("local.deepthink.workflow@1.0")
    manifest["ports"]["consumes"].append(build.port("local.deepthink.workflow"))
    manifest["files"] = []
    return build.bind_files(directory, manifest, paths)


def stage_generation_reader(target_dir: Path,
                            build_profile: str = "debug") -> dict[str, Any]:
    """Stage the real Reader with its defining shape and ordinary authority.

    The independent Reader helper requires a new destination. Complete that
    stage first, then publish its bound files into this builder's generated
    Pack directory. Repeated staging never falls back to a different artifact.
    """
    with tempfile.TemporaryDirectory(
        prefix=".deepthink-reader-", dir=ROOT,
    ) as temporary:
        source = Path(temporary) / build_generation_reader.PACK_ID
        manifest = build_generation_reader.stage(
            target_dir, source, build_profile=build_profile,
        )
        contents = {}
        for entry in manifest["files"]:
            content = (source / entry["path"]).read_bytes()
            if (len(content) != entry["size"]
                    or build.sha256(content) != entry["sha256"]):
                raise ValueError("staged Reader file differs from its bound manifest")
            contents[entry["path"]] = content
        destination = ROOT / build_generation_reader.PACK_ID
        for relative, content in contents.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        build.write_json(destination / "pack.json", manifest)
    return manifest


def compose(manifests: dict[str, dict[str, Any]], *, ui: bool) -> dict[str, Any]:
    """Use declared providers, overlay ordering and explicit per-Pack limits."""
    profile = build.profile("harness-chat", manifests,
                            provider="openai-compat-provider", origin=ORIGIN)
    # Replace only the two provider/prompt identities in this new Profile.
    identities = {"local.openai-compat-provider": "local.openrouter-mimo-provider",
                  "local.harness-prompt": "local.mimo-prompt"}
    for pin in profile["packs"]:
        pin["id"] = identities.get(pin["id"], pin["id"])
    profile["bindings"] = {key: identities.get(value, value)
                            for key, value in profile["bindings"].items()}
    for name in ["harness-agent", "harness-tools", "deepthink", "deepthink-input",
                 "openrouter-generation-reader"] + (["deepthink-ui"] if ui else []):
        profile["packs"].append({"id": "local." + name, "version": "0.1.0"})
    profile["id"] = "prof_deepthink_mimo_ui" if ui else "prof_deepthink_mimo"
    profile["name"] = "Tobkiri DeepThink · MiMo" + (" UI" if ui else "")
    profile["bindings"].update({
        "local.harness-agent.tool-provider@1": "local.harness-tools",
        "local.harness-chat.structured-llm-provider@1": "local.openrouter-mimo-provider",
        "local.deepthink.workflow@1": "local.deepthink",
        "local.deepthink.continuation-input@1": "local.deepthink-input",
        "local.deepthink.input-context@1": "local.deepthink",
        "local.openrouter-generation-reader.generation-reader@1":
            "local.openrouter-generation-reader",
    })
    # Flow IDs are resolved by implements, never routed around Core.
    extra_ports = ["local.harness-agent.tool-provider@1.0",
                   "local.harness-chat.structured-llm-provider@1.0",
                   "local.harness-chat.generate@1.0",
                   "local.deepthink.workflow@1.0",
                   "local.deepthink.continuation-input@1.0",
                   "local.deepthink.continuation-flow@1.0",
                   "local.deepthink.regeneration-flow@1.0",
                   "local.deepthink.control-flow@1.0", "local.deepthink.input-context@1.0",
                   "local.openrouter-generation-reader.generation-reader@1.0"]
    profile["ceiling"]["ports"] = sorted(set(profile["ceiling"]["ports"] + extra_ports))
    profile["ceiling"]["secrets"] = [{"name": "api-key", "reveal": False, "optional": False}]
    profile["ceiling"]["loops"] = {"max_iterations": 512, "max_wall_ms": 3600000, "max_cost_units": 100000}
    profile["overlay_order"] = [{"target": {"role": "local.harness-chat.generate@1.0"},
                                  "order": ["local.deepthink/deepthink-generate"]}]
    profile["user_permissions"] = build.permission_set(ports=[
        "local.harness-chat.user-input@1.0",
        "local.harness-chat.conversation-store@1.0",
        "local.deepthink.continuation-input@1.0",
    ])
    if ui:
        profile["startup"]["ui"] = "local.deepthink-ui"
    profile["pack_limits"] = {}
    for pin in profile["packs"]:
        name = pin["id"].removeprefix("local.")
        manifest = manifests[name]
        pin["sha256"] = build.sha256(build.canonical(manifest))
        pin["source"] = "file:../" + pin["id"]
        profile["pack_limits"][pin["id"]] = build.intersect_permissions(manifest["permissions"], profile["ceiling"])
    return profile


def stage(target_dir: Path, build_profile: str = "debug") -> list[Path]:
    """Publish two portable Profiles without acquiring credentials or approval."""
    tools_manifest = build_harness_tools.stage(target_dir, build_profile=build_profile)
    reader_manifest = stage_generation_reader(target_dir, build_profile=build_profile)
    input_manifest = build_deepthink_input.stage(target_dir, build_profile=build_profile)
    provider = variant("openai-compat-provider", "openrouter-mimo-provider", {
        "base_url": ORIGIN + "/api/v1", "model": MODEL,
        "secret_name": "api-key", "timeout_ms": 300000, "provider_order": ["xiaomi"],
        "json_mode": True,
    }, network=True)
    prompt = variant("harness-prompt", "mimo-prompt", {
        "model": MODEL, "system_prompt": "Follow the active Profile, use declared tools, and report verified outcomes accurately.",
        "max_tokens": 0,
    })
    ui = variant("harness-ui", "deepthink-ui", asset_overrides={
        "ui/app.js": ROOT / "local.deepthink-ui/ui/app.js",
    })
    view = "local.deepthink.continuation-input@1.0"
    ui["permissions"]["ports"].append(view)
    ui["ports"]["consumes"].append(build.port(view.rsplit("@", 1)[0]))
    ui["ui"]["ports_used"].append(view)
    build.write_json(ROOT / "local.deepthink-ui/pack.json", ui)
    publish_presets.publish("mock")
    preset_variants = []
    for source in sorted((ROOT / "profiles/presets").glob("*.published.json")):
        preset = json.loads(source.read_text(encoding="utf-8"))
        original_id = preset["bindings"]["local.harness-chat.prompt-builder@1"]
        original_name = original_id.removeprefix("local.")
        name = "deepthink-" + original_name
        preset_config = json.loads((ROOT / original_id / "config.json").read_text(encoding="utf-8"))
        preset_config.update({"model": MODEL, "max_tokens": 0})
        prompt_manifest = variant(original_name, name, preset_config)
        preset_variants.append((source, original_name, name, prompt_manifest))
    config = build_deepthink.default_config()
    config["tools"] = build_harness_tools.inventory()
    config["environment"].update({
        "model": MODEL, "provider": "OpenRouter", "api_base": ORIGIN + "/api/v1",
        "provider_pack_hash": build.sha256(build.canonical(provider)),
        "tool_pack_hash": build.sha256(build.canonical(tools_manifest)),
        "prompt_pack_hashes": {item["id"]: build.sha256(build.canonical(item))
                               for item in [prompt] + [item[3] for item in preset_variants]},
    })
    deepthink = build_deepthink.stage(target_dir, config=config, build_profile=build_profile)
    deepthink["permissions"]["loops"] = {"max_iterations": 512, "max_wall_ms": 3600000, "max_cost_units": 100000}
    build.write_json(ROOT / "local.deepthink/pack.json", deepthink)
    names = ["harness-chat", "terminal-input", "harness-conversation-store",
             "harness-prompt", "openai-compat-provider", "terminal-output", "harness-agent"]
    manifests = {name: read_manifest(name) for name in names}
    manifests.update({"harness-chat": workflow_harness_variant(),
                      "deepthink": deepthink, "harness-tools": tools_manifest,
                      "deepthink-input": input_manifest,
                      "openrouter-mimo-provider": provider, "mimo-prompt": prompt,
                      "openrouter-generation-reader": reader_manifest,
                      "deepthink-ui": ui})
    source_overrides = {
        "local.harness-chat": ROOT / "deepthink-variants/local.harness-chat"
    }
    destinations = []
    for has_ui in [False, True]:
        document = compose(manifests, ui=has_ui)
        name = "deepthink-mimo-ui" if has_ui else "deepthink-mimo"
        destination = ROOT / "profiles" / (name + ".profile.json")
        build.portable_sources(document, destination, source_overrides)
        build.write_json(destination, document)
        published = {key: value for key, value in document.items() if key not in {"id", "name"}}
        published["format"] = "tobkiri.published-profile/1"
        build.write_json(destination.with_name(name + ".published.json"), published)
        destinations.append(destination)
    # Rebuild every existing style Pack from the current prompt component and
    # current pins. Legacy capabilities remain explicitly outside these Packs.
    template = compose(manifests, ui=True)
    for source, original_name, name, prompt_manifest in preset_variants:
        document = copy.deepcopy(template)
        document["id"] = "prof_" + name.replace("-", "_")
        document["name"] = "DeepThink · " + original_name
        document["bindings"]["local.harness-chat.prompt-builder@1"] = "local." + name
        document["pack_limits"].pop("local.mimo-prompt")
        document["pack_limits"]["local." + name] = build.intersect_permissions(prompt_manifest["permissions"], document["ceiling"])
        for pin in document["packs"]:
            if pin["id"] == "local.mimo-prompt":
                pin.update({"id": "local." + name, "sha256": build.sha256(build.canonical(prompt_manifest))})
        destination = ROOT / "profiles/deepthink-presets" / source.name.replace(".published.json", ".profile.json")
        build.portable_sources(document, destination, source_overrides)
        build.write_json(destination, document)
        published = {key: value for key, value in document.items() if key not in {"id", "name"}}
        published["format"] = "tobkiri.published-profile/1"
        build.write_json(destination.with_name(source.name), published)
        build.write_json(destination.with_suffix(".provenance.json"), {
            "source_profile": source.relative_to(ROOT).as_posix(),
            "status": "style plus DeepThink and nine Core storage/utility tools",
            "legacy_capability_parity": False,
            "platform_runtime_verified": False,
        })
        destinations.append(destination)
    return destinations


def main() -> None:
    """Stage the workflow using the explicit profile for both nested components."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--build-profile", choices=("debug", "release"), default="debug")
    arguments = parser.parse_args()
    for profile_path in stage(arguments.target_dir, build_profile=arguments.build_profile):
        print(profile_path)


if __name__ == "__main__":
    main()
