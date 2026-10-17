"""Build unsigned development Packs and pin Profiles to actual component bytes.

The guest workspace is separate from the Rust host workspace. This development
tool never embeds a provider route into the core and never reads a secret value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
GUESTS = ["openai-compat-provider", "mock-llm", "harness-conversation-store",
          "harness-prompt", "terminal-input", "echo-llm", "echo-store", "echo-prompt",
          "echo-input", "terminal-output", "echo-output", "pi-adapter"]
LOOPS = {"max_iterations": 16, "max_wall_ms": 300000, "max_cost_units": 100000}
LIMITS = {"fuel_per_call": 2000000000, "memory_mib": 128, "wall_ms": 300000,
          "table_elements": 65536, "output_bytes": 4194304}


def write_json(path: Path, value: Any) -> None:
    """Write readable JSON while preserving the StrictV1 value domain."""
    if isinstance(value, dict) and value.get("format") == "tobkiri.pack/1":
        normalize_manifest(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def normalize_manifest(manifest: dict[str, Any]) -> None:
    """Match Rust PackManifest serialization before pinning or archiving.

    Rust materializes Option fields as null and PermissionSet collections as
    sorted sets. Omitting those fields produced different pins in CLI .tpk files.
    """
    permissions = manifest["permissions"]
    permissions["network"].setdefault("user_origins", None)
    permissions["network"]["origins"] = sorted(set(permissions["network"]["origins"]))
    for field in ("ports", "proc", "host_io"):
        permissions[field] = sorted(set(permissions[field]))
    runtime = manifest["runtime"]
    if runtime["kind"] in ("wasm", "native"):
        runtime.setdefault("config", None)
        if runtime["config"] is not None:
            runtime["config"].setdefault("shape", None)
    for store in manifest["stores"]:
        store.setdefault("seed", None)
    if manifest.get("inputs") == []:
        del manifest["inputs"]


def canonical(value: Any) -> bytes:
    """New documents contain only StrictV1-compatible values."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode()


def sha256(data: bytes) -> str:
    """Return a tagged content digest used by the frozen Rust formats."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def permission_set(*, ports: list[str] | None = None, network: list[str] | None = None,
                   private: bool = False, host_io: list[str] | None = None,
                   secrets: bool = False) -> dict[str, Any]:
    """Declare narrow concrete permissions; no native or cross-Profile access."""
    return {"network": {"origins": network or []}, "fs": [], "proc": [], "pty": False,
            "secrets": [{"name": "api-key", "reveal": False, "optional": True}] if secrets else [],
            "storage": {"private": private, "public_read": False, "public_write": False},
            "native": False, "cross_profile_storage": False, "audit_read": False,
            "audit_append": True, "host_io": host_io or [], "ports": ports or [],
            "mediator": False, "wasm": LIMITS, "loops": LOOPS}


def intersect_permissions(declared: dict[str, Any], ceiling: dict[str, Any]) -> dict[str, Any]:
    """Publish explicit restrictions; never infer or extend the original ceiling."""
    def unique(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
        index = {item[key]: item for item in items}
        if len(index) != len(items): raise ValueError("duplicate permission identity: " + key)
        return index
    sets = lambda left, right: sorted(set(left) & set(right))
    result: dict[str, Any] = {}
    for field in ("proc", "host_io", "ports"):
        result[field] = sets(declared.get(field, []), ceiling.get(field, []))
    for field in ("pty", "native", "cross_profile_storage", "audit_read", "audit_append", "mediator"):
        result[field] = bool(declared.get(field, False) and ceiling.get(field, False))
    result["network"] = {"origins": sets(declared.get("network", {}).get("origins", []), ceiling.get("network", {}).get("origins", []))}
    left = declared.get("network", {}).get("user_origins")
    right = ceiling.get("network", {}).get("user_origins")
    if left and right:
        result["network"]["user_origins"] = {"max": min(left["max"], right["max"]), "allow_loopback": bool(left["allow_loopback"] and right["allow_loopback"])}
    fs_limits = unique(ceiling.get("fs", []),"root")
    unique(declared.get("fs", []),"root")
    result["fs"] = []
    for item in declared.get("fs", []):
        limit = fs_limits.get(item["root"])
        if limit:
            permission = {"root": item["root"], "read": bool(item["read"] and limit["read"]), "write": bool(item["write"] and limit["write"])}
            if permission["read"] or permission["write"]: result["fs"].append(permission)
    secret_limits = unique(ceiling.get("secrets", []),"name")
    unique(declared.get("secrets", []),"name")
    result["secrets"] = [{"name": item["name"], "reveal": bool(item["reveal"] and secret_limits[item["name"]]["reveal"]),
                          "optional": bool(item["optional"] and secret_limits[item["name"]]["optional"])}
                         for item in declared.get("secrets", []) if item["name"] in secret_limits]
    result["storage"] = {field: bool(declared.get("storage", {}).get(field, False) and ceiling.get("storage", {}).get(field, False))
                         for field in ("private", "public_read", "public_write")}
    for group in ("wasm", "loops"):
        result[group] = {field: min(value, ceiling[group][field]) for field, value in declared[group].items()}
    return result


def base_manifest(pack: str, runtime: dict[str, Any], permissions: dict[str, Any]) -> dict[str, Any]:
    """Build an unsigned local Pack; signed publisher labels are not synthesized."""
    return {"format": "tobkiri.pack/1", "id": "local." + pack, "version": "0.1.0",
            "publisher_key": None, "runtime": runtime, "shapes": [],
            "ports": {"provides": [], "consumes": [], "untyped": []},
            "flows": [], "overlays": [], "stores": [], "permissions": permissions,
            "ui": None, "files": []}


def port(shape: str, optional: bool = False) -> dict[str, Any]:
    """Declare a typed major/minor shape reference."""
    return {"shape": shape, "version": "1.0", "optional": optional}


def bind_files(directory: Path, manifest: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Bind every shipped file to actual bytes; no placeholder component hashes."""
    for relative in sorted(set(paths)):
        content = (directory / relative).read_bytes()
        manifest["files"].append({"path": relative, "sha256": sha256(content), "size": len(content)})
    write_json(directory / "pack.json", manifest)
    return manifest


def stage_shape(directory: Path, shape: str) -> list[str]:
    """Stage authoritative Pack WIT plus its required generic ABI dependency."""
    wit = directory / "wit"
    deps = wit / "deps"
    deps.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "shapes" / (shape + ".wit"), wit / (shape + ".wit"))
    shutil.copyfile(REPO / "crates/tobkiri-wit/wit/core/core.wit", deps / "core.wit")
    return ["wit/" + shape + ".wit", "wit/deps/core.wit"]


def chat_flow(shape: str) -> dict[str, Any]:
    """All input processing uses Flow operations resolved by the Profile."""
    namespace = "local." + shape
    def ref(name: str) -> str:
        return namespace + "." + name + "@1.0"
    return {"format": "tobkiri.flow/1", "id": "turn", "implements": ref("turn-flow"),
            "trigger": {"input": ref("user-input")}, "loop_limits": "profile-default",
            "steps": [
                {"id": "load", "role": ref("load-context"), "overlay": "open",
                 "call": {"shape": ref("conversation-store"), "op": "load", "args": {"conversation": "$input.conversation"}}, "out": "ctx"},
                {"id": "prompt", "role": ref("assemble-prompt"), "overlay": "open",
                 "call": {"shape": ref("prompt-builder"), "op": "build", "args": {"ctx": "$ctx", "input": "$input"}}, "out": "prompt"},
                {"id": "generate", "role": ref("generate"), "overlay": "open", "out": "turn",
                 "call": {"shape": ref("llm-provider"), "op": "chat", "args": {"prompt": "$prompt"}},
                 "stream": {"emit": ref("assistant-output"), "collect": "turn"}},
                {"id": "save", "call": {"shape": ref("conversation-store"), "op": "append",
                 "args": {"conversation": "$input.conversation", "input": "$input", "events": "$turn"}}}
            ], "on_error": {"default": "fail", "steps": {}}}


def shape_pack(shape: str) -> dict[str, Any]:
    """The Flow/data Pack owns domain shapes; the core owns none."""
    directory = ROOT / ("local." + shape)
    directory.mkdir(exist_ok=True)
    names = {"messages": "data", "user-input": "port", "conversation-store": "port",
             "prompt-builder": "port", "llm-provider": "port", "assistant-output": "port",
             "command-provider":"port", "turn-flow": "flow", "load-context": "role", "assemble-prompt": "role", "generate": "role"}
    if shape == "echo-chat": names.pop("command-provider")
    paths = stage_shape(directory, shape)
    ports = ["local." + shape + "." + name + "@1.0" for name, kind in names.items() if kind == "port" and name != "command-provider"]
    manifest = base_manifest(shape, {"kind": "data"}, permission_set(ports=ports))
    for name, kind in names.items():
        manifest["shapes"].append({"name": "local." + shape + "." + name, "version": "1.0", "kind": kind, "wit": "wit/" + shape + ".wit"})
        sidecar = {"cardinality": "one", "streams": {"chat": "chat-event"} if name == "llm-provider" else {"submit":"flow-frame"} if name == "user-input" else {},
                   "callbacks": {}, "conformance": None}
        write_json(directory / "wit" / (name + ".shape.json"), sidecar)
        paths.append("wit/" + name + ".shape.json")
    manifest["ports"]["consumes"] = [port(value.rsplit("@", 1)[0]) for value in ports]
    write_json(directory / "flows/turn.flow.json", chat_flow(shape))
    manifest["flows"] = ["flows/turn.flow.json"]
    paths.append("flows/turn.flow.json")
    return bind_files(directory, manifest, paths)


def agent_shape_pack() -> dict[str, Any]:
    directory=ROOT/"local.harness-agent"
    directory.mkdir(exist_ok=True)
    paths=stage_shape(directory,"harness-agent")
    manifest=base_manifest("harness-agent",{"kind":"data"},permission_set())
    for name,kind in [("tool-provider","port"),("tool-call-guard","port"),("tool-call","role")]:
        manifest["shapes"].append({"name":"local.harness-agent."+name,"version":"1.0","kind":kind,"wit":"wit/harness-agent.wit"})
        write_json(directory/"wit"/(name+".shape.json"),{"cardinality":"many" if name=="tool-provider" else "one","streams":{},"callbacks":{},"conformance":None})
        paths.append("wit/"+name+".shape.json")
    return bind_files(directory,manifest,paths)


def guest_manifest(name: str, target: Path, origin: str, model: str) -> dict[str, Any]:
    """Create a guest Pack from the selected workspace's real wasm component."""
    directory = ROOT / ("local." + name)
    artifact = target / "wasm32-wasip2/debug" / ("tobkiri_pack_" + name.replace("-", "_") + ".wasm")
    shutil.copyfile(artifact, directory / "component.wasm")
    shape = "echo-chat" if name.startswith("echo-") else "harness-chat"
    iface = "llm-provider" if name in ("openai-compat-provider", "mock-llm", "echo-llm") else "conversation-store" if "store" in name else "prompt-builder" if "prompt" in name else "assistant-output" if name.endswith("output") else "user-input"
    if name == "pi-adapter":
        shape, iface = "pi-adapter", "tool-provider"
    fq = "local." + shape + "."
    imports = []
    if name == "pi-adapter": imports = ["local.pi-adapter.pi-extension@1.0"]
    if name.endswith("input"): imports = ["local."+shape+".turn-flow@1.0"]
    permissions = permission_set(ports=imports, network=[origin] if name == "openai-compat-provider" else [],
                                 secrets=name == "openai-compat-provider", private="store" in name,
                                 host_io=["terminal-read-line"] if name.endswith("input") else ["terminal-write"] if name.endswith("output") else [])
    config: dict[str, Any] = {}
    if name.endswith("input"):
        config = {"conversation":"e2e","prompt":"> ","flow":"local."+shape+".turn-flow@1.0"}
    elif "prompt" in name:
        config = {"model":"mock" if name.startswith("echo-") else model,"system_prompt":"Answer in plain text. Be helpful and concise.","max_tokens":128}
    elif name=="openai-compat-provider":
        config = {"base_url":origin+"/v1","model":model,"secret_name":"api-key","timeout_ms":300000}
    write_json(directory/"config.json",config)
    manifest = base_manifest(name, {"kind": "wasm", "component": "component.wasm", "toolchain": "rust/1.94.0;wit-bindgen/0.62.0", "config":{"path":"config.json"}}, permissions)
    manifest["ports"]["provides"] = [port(fq + iface)]
    # permissions.ports is effect authority only; a Pack that places dynamic
    # port/Flow calls must also declare each consumed view in ports.consumes
    # before the host admits the call.
    manifest["ports"]["consumes"] = [port(value.rsplit("@", 1)[0]) for value in imports]
    if name.endswith("input"):
        # Author-selected physical entry, independent of provider routing.
        manifest["inputs"] = [{"exact_input_view":fq+"user-input@1.0","operation":"read"}]
    if "store" in name:
        manifest["stores"] = [{"name": "conversations", "tier": "private", "data_shape": fq + "messages@1.0"}]
    if name == "pi-adapter":
        manifest["ports"]["provides"] = [port("local.harness-agent.tool-provider"),port("local.harness-agent.tool-call-guard"),port("local.harness-chat.command-provider")]
        manifest["ports"]["consumes"] = [port(fq+"pi-extension")]
        paths = stage_shape(directory, shape)
        paths.extend(["wit/pack.wit","wit/deps/harness-chat.wit","wit/deps/harness-agent.wit"])
        manifest["shapes"].append({"name":fq+"pi-extension","version":"1.0","kind":"port","wit":"wit/pi-adapter.wit"})
        write_json(directory/"wit/pi-extension.shape.json",{"cardinality":"one","streams":{},"callbacks":{},"conformance":None})
        paths.append("wit/pi-extension.shape.json")
    else:
        paths = ["wit/pack.wit", "wit/deps/core.wit", "wit/deps/shape.wit"]
    paths.append("component.wasm")
    paths.append("config.json")
    source_paths = [directory/"src/lib.rs", directory/"build.rs", directory/"Cargo.toml", ROOT/"Cargo.toml", ROOT/"Cargo.lock", *sorted((ROOT/"guest-sdk/src").glob("*.rs")), directory/"wit/pack.wit", REPO/"crates/tobkiri-wit/wit/core/core.wit", ROOT/"shapes"/(shape+".wit")]
    if name == "pi-adapter": source_paths.append(directory/"guest-wit/pack.wit")
    write_json(directory / "component.build.json", {"format":"tobkiri.component-build/1","language":"rust","tools":{"rust":"1.94.0","wit-bindgen":"0.62.0"},
              "artifact_sha256":sha256((directory/"component.wasm").read_bytes()),"artifact_bytes":(directory/"component.wasm").stat().st_size,
              "sources":{source.relative_to(REPO).as_posix():sha256(source.read_bytes()) for source in source_paths},"production_enabled":False})
    paths.append("component.build.json")
    return bind_files(directory, manifest, paths)


def profile(shape: str, packs: dict[str, dict[str, Any]], *, provider: str, origin: str) -> dict[str, Any]:
    """Pin exact Pack hashes and require a declared provider binding."""
    harness = shape == "harness-chat"
    ids = [shape, "terminal-input" if harness else "echo-input", "harness-conversation-store" if harness else "echo-store", "harness-prompt" if harness else "echo-prompt", provider, "terminal-output" if harness else "echo-output"]
    namespace = "local." + shape + "."
    bindings = {namespace+"user-input@1": "local."+ids[1], namespace+"assistant-output@1":"local."+ids[5],
                namespace+"conversation-store@1":"local."+ids[2],namespace+"prompt-builder@1":"local."+ids[3],namespace+"llm-provider@1":"local."+provider}
    permission_ports = [key+".0" for key in bindings]+[namespace+"turn-flow@1.0"]
    ceiling = permission_set(ports=permission_ports, network=[origin] if provider == "openai-compat-provider" else [],
                             secrets=provider == "openai-compat-provider", private=True,
                             host_io=["terminal-read-line", "terminal-write", "display"])
    return {"format":"tobkiri.profile/1","id":"prof_"+shape.replace("-","_"),"name":"Tobkiri Harness" if harness else "Echo chat",
            "packs":[{"id":"local."+name,"version":"0.1.0","sha256":sha256(canonical(packs[name])),"source":"file:../local."+name} for name in ids],
            "publishers":{},"ceiling":ceiling,"pack_limits":{"local."+name:intersect_permissions(packs[name]["permissions"],ceiling) for name in ids},"bindings":bindings,"overlay_order":[],"user_overlays":[],
            "startup":{"inputs":[{"source_pack":"local."+ids[1],"exact_input_view":namespace+"user-input@1.0","operation":"read"}],"ui":None}}


def explicit_limits(document: dict[str, Any], packs: dict[str, dict[str, Any]]) -> None:
    """Restrict every existing pin while preserving its original ceiling/bindings."""
    limits = {}
    for pin in document["packs"]:
        manifest = packs[pin["id"].removeprefix("local.")]
        if pin["sha256"] != sha256(canonical(manifest)):
            raise ValueError("explicit-limit publication requires the original verified Pack pin: " + pin["id"])
        limits[pin["id"]] = intersect_permissions(manifest["permissions"],document["ceiling"])
    document["pack_limits"] = limits


def portable_sources(document: dict[str, Any], destination: Path) -> None:
    """Publish local locators relative to the input Profile file's directory."""
    for pin in document["packs"]:
        source = ROOT / pin["id"]
        if not source.is_dir():
            raise ValueError("published Pack directory is missing: " + pin["id"])
        pin["source"] = "file:" + Path(os.path.relpath(source, destination.parent)).as_posix()


def ui_pack() -> dict[str, Any]:
    """A sandboxed Pack UI with a channel client and no host IPC or HTTP."""
    directory=ROOT/"local.harness-ui"
    shutil.copyfile(REPO/"sdk/js/pack-ui/index.js",directory/"ui/ports.js")
    shutil.copyfile(REPO/"sdk/js/pack-ui/float.js",directory/"ui/float.js")
    permissions=permission_set(ports=["local.harness-chat.user-input@1.0","local.harness-chat.conversation-store@1.0"])
    manifest=base_manifest("harness-ui",{"kind":"data"},permissions)
    manifest["ports"]["consumes"]=[port("local.harness-chat.user-input"),port("local.harness-chat.conversation-store")]
    manifest["ui"]={"entry":"ui/index.html","ports_used":["local.harness-chat.user-input@1.0","local.harness-chat.conversation-store@1.0"]}
    return bind_files(directory,manifest,["ui/index.html","ui/app.js","ui/accounting.mjs","ui/style.css","ui/ports.js","ui/float.js"])


def main() -> None:
    """Build the complete source set, or only re-pin already built components."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--profiles-only", action="store_true", help="re-publish explicit limits from existing hash-bound manifests without changing component artifacts")
    parser.add_argument("--origin", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="gemma3-1b-local")
    parser.add_argument("--target-dir", type=Path, default=REPO.parent/".work/cargo-target")
    args = parser.parse_args()
    parsed = urlsplit(args.origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise SystemExit("origin must be an exact HTTP origin, with no path or credentials")
    if not args.profiles_only and shutil.disk_usage(ROOT).free < 5 * 1024**3:
        raise SystemExit("Pack build requires the agreed 5 GiB free-space floor")
    if not args.skip_build and not args.profiles_only:
        environment = {**os.environ,"CARGO_TARGET_DIR":str(args.target_dir.absolute()),"CARGO_BUILD_JOBS":"2","CARGO_INCREMENTAL":"0"}
        subprocess.run(["cargo","build","--manifest-path",str(ROOT/"Cargo.toml"),"--locked","--target","wasm32-wasip2","--workspace"],env=environment,check=True)
    if args.profiles_only:
        packs = {name:json.loads((ROOT/("local."+name)/"pack.json").read_text()) for name in [*GUESTS,"harness-chat","echo-chat","harness-ui","harness-agent"]}
    else:
        packs = {name:guest_manifest(name,args.target_dir,args.origin,args.model) for name in GUESTS}
        for name in ("harness-chat","echo-chat"): packs[name]=shape_pack(name)
        packs["harness-ui"]=ui_pack()
        packs["harness-agent"]=agent_shape_pack()
    for file, shape, provider in (("harness-live","harness-chat","openai-compat-provider"),("harness-mock","harness-chat","mock-llm"),("echo","echo-chat","echo-llm")):
        if args.profiles_only:
            document=json.loads((ROOT/"profiles"/(file+".profile.json")).read_text())
            explicit_limits(document,packs)
        else:
            document=profile(shape,packs,provider=provider,origin=args.origin)
            if file=="harness-mock": document["id"]="prof_harness_mock"
        destination = ROOT/"profiles"/(file+".profile.json")
        portable_sources(document,destination)
        write_json(destination,document)
        published={key:value for key,value in document.items() if key not in {"id","name"}}
        published["format"]="tobkiri.published-profile/1"
        write_json(ROOT/"profiles"/(file+".published.json"),published)
        # Configuration is bound to each Pack hash and recreated on import.
        # There is deliberately no host/CLI/Profile configuration map.
    if args.profiles_only:
        gui=json.loads((ROOT/"profiles/harness-ui.profile.json").read_text())
        explicit_limits(gui,packs)
    else:
        gui=profile("harness-chat",packs,provider="mock-llm",origin=args.origin)
        gui["id"]="prof_harness_ui"
        gui["packs"].append({"id":"local.harness-ui","version":"0.1.0","sha256":sha256(canonical(packs["harness-ui"])),"source":"file:../local.harness-ui"})
        gui["startup"]["ui"]="local.harness-ui"
        gui["pack_limits"]["local.harness-ui"]=intersect_permissions(packs["harness-ui"]["permissions"],gui["ceiling"])
    portable_sources(gui,ROOT/"profiles/harness-ui.profile.json")
    write_json(ROOT/"profiles/harness-ui.profile.json",gui)
    print(json.dumps({"packs":len(packs),"profiles":4,"origin":args.origin,"migration_complete":False},sort_keys=True))


if __name__ == "__main__": main()
