"""Rebuild seven legacy service styles as hash-pinned published Profiles.

Legacy tools/browser/workers remain in migration inventory until their own
parity passes. These examples publish behavior defaults for the new chat Flow.
"""

from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil

from build import ROOT, REPO, bind_files, canonical, sha256, write_json, intersect_permissions, portable_sources


def publish(provider: str, *, profiles_only: bool = False) -> int:
    """Publish immutable style defaults without copying private user settings."""
    template=json.loads((ROOT/"profiles"/("harness-live.profile.json" if provider=="live" else "harness-mock.profile.json")).read_text())
    prompt_source=ROOT/"local.harness-prompt"
    base=json.loads((prompt_source/"pack.json").read_text())
    count=0
    legacy=REPO/"tobkiri_runtime/profile_projections/agent-services/presets"
    for source in sorted(legacy.glob("*.preset.yaml")):
        destination=ROOT/"profiles/presets"/(source.name.removesuffix(".preset.yaml")+".published.json")
        if profiles_only:
            # Existing provenance supplies the recorded parsed legacy input;
            # verify its source bytes before reusing it without a YAML runtime.
            provenance=json.loads(destination.with_suffix(".provenance.json").read_text())
            if provenance["legacy_path"] != source.relative_to(REPO).as_posix() or provenance["legacy_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest():
                raise ValueError("legacy preset differs from its recorded provenance")
            old=provenance["legacy"]
        else:
            import yaml
            old=yaml.safe_load(source.read_text())
        name="preset-"+old["id"].replace("_","-")
        pack_id="local."+name
        target=ROOT/pack_id
        if profiles_only:
            manifest=json.loads((target/"pack.json").read_text())
            previous=json.loads(destination.read_text())
            previous_pin=next(pin for pin in previous["packs"] if pin["id"] == pack_id)
            if previous_pin["sha256"] != sha256(canonical(manifest)):
                raise ValueError("profile-only publication requires the original verified Pack pin: "+pack_id)
        else:
            target.mkdir(exist_ok=True)
            manifest=copy.deepcopy(base)
            manifest["id"]=pack_id
            for file in base["files"]:
                path=file["path"]
                (target/path).parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(prompt_source/path,target/path)
            config=json.loads((target/"config.json").read_text())
            config["system_prompt"]=("Follow this service style while staying within the active Profile permissions. "
                "Behaviors: "+", ".join(old.get("behavior",[]))+". "
                "Intended workflow: "+str(old.get("workflow",""))+". "
                "Delivery defaults: "+json.dumps(old.get("task_defaults",{}),ensure_ascii=False,sort_keys=True)+". "
                "Ask for a declared tool when a task requires a capability absent from this Profile.")
            write_json(target/"config.json",config)
            manifest["files"]=[]
            bind_files(target,manifest,[file["path"] for file in base["files"]])
        profile=copy.deepcopy(template)
        profile["id"]="prof_"+old["id"]
        profile["name"]=old["name"]
        for pin in profile["packs"]:
            if pin["id"]=="local.harness-prompt":
                pin["id"]=pack_id
                pin["sha256"]=sha256(canonical(manifest))
        profile["bindings"]["local.harness-chat.prompt-builder@1"]=pack_id
        profile["pack_limits"].pop("local.harness-prompt",None)
        profile["pack_limits"][pack_id]=intersect_permissions(manifest["permissions"],profile["ceiling"])
        published={key:value for key,value in profile.items() if key not in {"id","name"}}
        published["format"]="tobkiri.published-profile/1"
        if destination.name != old["id"]+".published.json":
            raise ValueError("legacy preset identity differs from its filename")
        portable_sources(published,destination)
        write_json(destination,published)
        if not profiles_only:
            write_json(destination.with_suffix(".provenance.json"),{
                "format":"tobkiri.preset-migration/1","name":old["name"],"legacy_path":source.relative_to(REPO).as_posix(),
                "legacy_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"legacy":old,
                "published_profile":destination.relative_to(REPO).as_posix(),"prompt_pack":pack_id,
                "status":"style-defaults-published; legacy capability parity todo",
                "pending_capabilities":old.get("enabled_capabilities",[]),
            })
        count+=1
    if count!=7: raise ValueError("expected exactly seven legacy service presets")
    return count


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider",choices=["mock","live"],default="mock")
    parser.add_argument("--profiles-only",action="store_true",help="publish JSON from existing hash-bound Packs and recorded legacy provenance without changing Pack artifacts")
    args=parser.parse_args()
    print(json.dumps({"published_profiles":publish(args.provider,profiles_only=args.profiles_only),"legacy_parity_complete":False}))
