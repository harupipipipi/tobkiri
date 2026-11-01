#!/usr/bin/env python3
"""Freeze remaining source-defined seals, without runtime construction.

Python functions/constant AST nodes are executed verbatim from cd7c20e9d.
Rust source functions are copied verbatim into a small oracle harness; no
Tauri or VM build runs. Signing key, UUID and clocks are explicit fixtures.
"""
from __future__ import annotations
import argparse
import ast
import base64
import hashlib
import hmac
import importlib
import json
import math
import os
from pathlib import Path
import random
import re
import struct
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
import uuid

ROOT=Path(__file__).resolve().parents[2]
SOURCE=Path("/Users/haru/.codex/worktrees/e8bc/tobikiri-pr-1322")
COMMIT="cd7c20e9d52535674c36cccac4d03ef70e9f809b"
SEED=bytes(range(32))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root",type=Path,default=SOURCE)
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args(); source=args.source_root.resolve()
    provenance={"git_commit":COMMIT,"test_only_keys":True,"sources":{},"source_calls":{},"shim_notes":["Only deterministic key/clock/UUID inputs and IO-free error/hex adapters are fixture shims; original byte-producing source bodies are unchanged.","Approval token MAC covers base64url ASCII, not decoded JSON. Approval JSON retains ensure_ascii=True."]}
    outputs={}
    def b64(raw):return base64.b64encode(raw).decode("ascii")
    def checked(relative):
        raw=(source/relative).read_bytes()
        assert raw==subprocess.check_output(["git","-C",str(source),"show",f"{COMMIT}:{relative}"])
        provenance["sources"][relative]={"sha256_hex":hashlib.sha256(raw).hexdigest(),"length":len(raw)}
        return raw.decode()
    def python_calls(relative,names):
        text=checked(relative); tree=ast.parse(text); selected=[]
        for node in tree.body:
            name=node.name if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) else next((t.id for t in node.targets if isinstance(t,ast.Name)),None) if isinstance(node,ast.Assign) else None
            if name in names:
                selected.append(node); excerpt="\n".join(text.splitlines()[node.lineno-1:node.end_lineno])+"\n"
                provenance["source_calls"][name]={"source":relative,"first_line":node.lineno,"source_sha256_hex":hashlib.sha256(excerpt.encode()).hexdigest(),"invocation":"original AST function/constant, no module/runtime constructor"}
        assert len(selected)==len(names),(relative,names)
        env={"Any":Any,"json":json,"math":math,"re":re,"hashlib":hashlib,"hmac":hmac,"base64":base64,"uuid":SimpleNamespace(uuid4=lambda:uuid.UUID(int=0)),"_runtime_secret":lambda:SEED.hex(),"_signing_secret":lambda:SEED,"_now":lambda:1750000000}
        exec(compile(ast.Module(body=selected,type_ignores=[]),str(source/relative),"exec"),env)
        return env
    def rust_item(text,needle,relative):
        start=text.index(needle); opening=text.index("{",start); depth=0; quote=None; escape=False
        for i in range(opening,len(text)):
            c=text[i]
            if quote:
                if escape:escape=False
                elif c=="\\":escape=True
                elif c==quote:quote=None
                continue
            if c=='"':quote=c;continue
            if c=="{":depth+=1
            elif c=="}":
                depth-=1
                if depth==0:
                    body=text[start:i+1]
                    provenance["source_calls"][needle]={"source":relative,"first_line":text[:start].count("\n")+1,"source_sha256_hex":hashlib.sha256(body.encode()).hexdigest(),"invocation":"verbatim original Rust body in IO-free oracle harness"}
                    return body
        raise AssertionError(needle)
    def output(path,value):outputs[ROOT/path]=(json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False,indent=2)+"\n").encode()
    def lines(path,rows):outputs[ROOT/path]=b"".join((json.dumps(row,sort_keys=True,ensure_ascii=True,allow_nan=False)+"\n").encode() for row in rows)

    ui=python_calls("tobkiri_runtime/core_runtime/authority/ui_operator.py",{"_operator_message","sign_ui_operator","_is_untagged_sha256","UI_OPERATOR_ORIGIN","UI_OPERATOR_WINDOW_LABEL","UI_OPERATOR_VERSION","BROWSER_UI_OPERATOR_VERSION","INTERACTIVE_UI_OPERATOR_VERSION","UI_OPERATOR_TTL_SECONDS","_UNTAGGED_SHA256_RE"})
    approval=python_calls("tobkiri_runtime/ecosystem/defaultspack/domain/safety/approval.py",{"issue_execution_token","_b64url_encode","_canonicalize","hash_arguments","_TOKEN_VERSION","_ARG_HASH_IGNORE_KEYS","_DEFAULT_EXPIRES_IN_SECONDS"})
    vm=python_calls("tobkiri_runtime/ecosystem/defaultspack/backend/sandbox/isolation/resources/packvm_guest_runner.py",{"_normalise_bridge_deadline","_bridge_canonical_json","_bridge_canonical_digest","_sha256","MAX_DEADLINE_TEXT_BYTES","_CANONICAL_DEADLINE"})
    sealed=python_calls(".github/scripts/build_sealed_python_environment.py",{"_files_digest","_sha256_bytes"})
    release=python_calls("tobkiri_launcher/scripts/verify_presentation_release.py",{"_release_signature_message","RELEASE_SCHEMA"})
    ui_rows=[];token_rows=[];args_rows=[];sealed_rows=[];release_rows=[];deadline_rows=[];bridge_rows=[]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    private=Ed25519PrivateKey.from_private_bytes(SEED)
    original_files_digest=sealed["_sha256_bytes"]
    captured_file_bytes=[]
    def capture_file_bytes(raw):
        captured_file_bytes[:]=[raw]
        return original_files_digest(raw)
    sealed["_sha256_bytes"]=capture_file_bytes
    for index in range(300):
        common={"now":1750000000+index,"nonce":f"{index:048x}"}
        mode=index%3
        if mode==0:extra={}
        elif mode==1:extra={"browser_audience":{"principal_id":"fixture.あ🚀","device_id":"fixture.device","browser_origin":"https://fixture.invalid","browser_window_id":f"window-{index}","exchange_nonce":"exchange-fixture"}}
        else:extra={"decision":"approve","request_snapshot_digest":"a"*64,"typed_confirmation_digest":"b"*64}
        operator=ui["sign_ui_operator"](f"fixture-request-{index}-あ🚀",**common,**extra)
        ui_rows.append({"input":operator,"message_b64":b64(ui["_operator_message"](operator)),"hmac_hex":operator["signature"],"test_only_key_hex":SEED.hex(),"source_call":"sign_ui_operator"})
        approval["uuid"].uuid4=lambda index=index:uuid.UUID(int=index)
        arguments={"fixture":index,"unicode":"あ🚀","approved":True,"nested":{"approval_token":"untrusted-fixture","x":index/8}}
        hashed=approval["hash_arguments"](arguments)
        args_rows.append({"input":arguments,"hash_untagged":hashed,"source_call":"hash_arguments"})
        token=approval["issue_execution_token"](f"request-{index}-あ🚀",hashed,expires_at=1750000900+index,operation="fixture.op",pack_id="fixture.pack",conversation_id="あ🚀",debug_binding={"lease_epoch":index,"session_id":"fixture-session","run_id":"fixture-run"})
        encoded,signature=token.split(".");body=base64.urlsafe_b64decode(encoded+"="*(-len(encoded)%4))
        token_rows.append({"token":token,"json_b64":b64(body),"mac_input_b64":b64(encoded.encode("ascii")),"test_only_secret_ascii":SEED.hex(),"source_call":"issue_execution_token"})
        records=[{"path":f"a/あ🚀-{index}","size":index,"sha256":"a"*64,"executable":bool(index%2)},{"path":"z","size":0,"sha256":"b"*64,"executable":False}]
        files_digest=sealed["_files_digest"](records)
        sealed_rows.append({"input":records,"bytes_b64":b64(captured_file_bytes[0]),"digest_untagged":files_digest,"source_call":"_files_digest","field_order":["path","size","sha256","executable"]})
        fields={name:f"fixture-{index}-{name}" for name in ["catalog_sha256","artifact_index_sha256","profile_lock_sha256","default_profile_sha256","defaultspack_lock_sha256","source_identity","source_revision","platform","architecture","artifact_id","key_id"]};fields["source_identity"]+="-あ🚀"
        message=release["_release_signature_message"](fields)
        release_rows.append({"input":fields,"message_b64":b64(message),"signature_b64url":base64.urlsafe_b64encode(private.sign(message)).decode().rstrip("="),"test_only_seed_hex":SEED.hex(),"source_call":"_release_signature_message","signature_note":"fixed-key primitive signature fixture of original preimage, no release publication"})
        bridge={"fixture":index,"unicode":"あ🚀","deadline":1750000000.125+index}
        bridge_rows.append({"input":bridge,"bytes_b64":b64(vm["_bridge_canonical_json"](bridge)),"digest":vm["_bridge_canonical_digest"](bridge),"source_call":"_bridge_canonical_digest"})
    rng=random.Random(152017)
    bits=[0,1,0x8000000000000000,0x3fb999999999999a,0x3ee4f8b588e368f1,0x3f1a36e2eb1c432d,0x4340000000000000,0x4376345785d8a000,0x7fefffffffffffff,0x7ff0000000000000,0x7ff8000000000000]+[rng.getrandbits(64) for _ in range(320)]
    for bits_value in bits:
        value=struct.unpack("!d",bits_value.to_bytes(8,"big"))[0]
        row={"input_bits_hex":f"{bits_value:016x}","source_call":"_normalise_bridge_deadline"}
        try:row.update(expect="ok",text=vm["_normalise_bridge_deadline"](value))
        except Exception as e:row.update(expect="error",error_class=type(e).__name__)
        deadline_rows.append(row)
    for name,rows in [("ui-operator",ui_rows),("approval-token",token_rows),("approval-args",args_rows),("sealed-environment",sealed_rows),("release-nul",release_rows),("packvm-deadline",deadline_rows),("packvm-bridge",bridge_rows)]:lines(f"vectors/seals/{name}.jsonl",rows)
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(source/"tobkiri_runtime"))
    pack_signature=importlib.import_module("core_runtime.pack_signature")
    checked("tobkiri_runtime/core_runtime/pack_signature.py")
    negative_rows=[]
    unsigned_reference=json.loads((ROOT/"vectors/legacy-vectors.json").read_bytes())["ed25519_pack"]["expected"]["unsigned_manifest"]
    public=private.public_key().public_bytes_raw()
    for index in range(300):
        unsigned={**unsigned_reference,"version":f"1.2.{index}"}
        signed=pack_signature.sign_manifest(unsigned,private)
        supplied_public=public
        signature=base64.urlsafe_b64decode(signed["signature"]["value"]+"="*(-len(signed["signature"]["value"])%4))
        mode=index%10;weak=mode in {4,5,9}
        if mode==1:signature=bytes([signature[0]^1])+signature[1:]
        elif mode==2:signature=signature[:63]+bytes([signature[63]|0x80])
        elif mode==3:supplied_public=Ed25519PrivateKey.from_private_bytes(bytes([99])*32).public_key().public_bytes_raw()
        elif mode in {4,9}:supplied_public=b"\x01"+bytes(31);signature=supplied_public+(bytes(32) if mode==4 else b"\x01"+bytes(31))
        elif mode==5:supplied_public=bytes(32);signature=bytes(64)
        elif mode==6:signed["version"]+="-changed"
        elif mode==7:signature=hashlib.sha512(f"fixture-signature-{index}".encode()).digest()
        elif mode==8:
            scalar=int.from_bytes(signature[32:],"little")+2**252+27742317777372353535851937790883648493
            signature=signature[:32]+scalar.to_bytes(32,"little")
        signed["signature"]={"algorithm":"Ed25519","key_id":hashlib.sha256(supplied_public).hexdigest()[:32],"value":base64.urlsafe_b64encode(signature).decode().rstrip("=")}
        row={"id":index,"mode":mode,"weak_key_case":weak,"input_b64":b64(pack_signature._canonical_bytes(pack_signature._unsigned_manifest(signed))),"public_key_hex":supplied_public.hex(),"signature_hex":signature.hex(),"source_call":"pack.verify_signed_manifest"}
        try:row.update(oracle_accepts=True,oracle_result=pack_signature.verify_signed_manifest(signed,Ed25519PublicKey.from_public_bytes(supplied_public)))
        except Exception as e:row.update(oracle_accepts=False,error_class=type(e).__name__)
        negative_rows.append(row)
    lines("vectors/seals/pack-ed25519-negative.jsonl",negative_rows)
    provenance["legacy_weak_key_mismatch"]="Original verifier accepts some weak/identity key fixtures that dalek verify_strict rejects; policy pending, expected oracle outcomes unchanged."

    host_path="tobkiri_launcher/src-tauri/src/host_broker.rs";debug_path="tobkiri_launcher/src-tauri/src/debug_approval.rs";release_path="tobkiri_launcher/src-tauri/src/presentation.rs"
    host_text=checked(host_path);debug_text=checked(debug_path);release_text=checked(release_path)
    rust="// Generated verbatim source excerpts; provenance is vectors/seals/extra-provenance.json.\n"
    rust+=rust_item(host_text,"struct ParsedRequest",host_path)+"\n"
    rust+=rust_item(host_text,"pub(crate) struct BrokerAttestationIdentity",host_path)+"\n"
    rust+=rust_item(host_text,"fn attest_response(",host_path)+"\n"
    rust+="#[derive(serde::Deserialize)]\n"+rust_item(debug_text,"pub struct DebugCliOperator",debug_path)+"\n"
    rust+=rust_item(debug_text,"fn canonical_operator_payload(",debug_path)+"\n"
    rust+="impl DebugApprovalManager {\n"+rust_item(debug_text,"fn sign(&self, operator:",debug_path)+"\n}\n"
    rust+="#[derive(serde::Deserialize)]\n"+rust_item(release_text,"struct PresentationReleaseManifest",release_path)+"\n"
    rust+=rust_item(release_text,"fn release_signature_message(",release_path)+"\n"
    source_path=ROOT/"tools/legacy-vectors/launcher_source.rs"
    if args.check:assert source_path.read_text()==rust,"Rust source excerpts changed"
    else:source_path.write_text(rust)
    env=os.environ.copy();env.update(CARGO_TARGET_DIR=str(ROOT.parent/".work/cargo-target"),CARGO_BUILD_JOBS="2",CARGO_INCREMENTAL="0")
    actual=json.loads(subprocess.check_output(["cargo","run","--locked","-q","-p","tobkiri-canon","--example","freeze_launcher_seals"],cwd=ROOT,env=env))
    for name,rows in actual.items():lines(f"vectors/seals/{name}.jsonl",rows)
    # The release preimage is independently checked through both original languages.
    for expected,actual_row in zip(release_rows,actual["release-rust"]):assert expected["message_b64"]==actual_row["message_b64"]
    provenance["generator_sha256_hex"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    provenance["rust_harness_sha256_hex"]=hashlib.sha256((ROOT/"crates/tobkiri-canon/examples/freeze_launcher_seals.rs").read_bytes()).hexdigest()
    provenance["coverage"]={"each_new_seal_family":300,"packvm_deadline_cases":len(deadline_rows),"rust_source_excerpts":str(source_path.relative_to(ROOT)),"still_unverified":["exhaustive isoformat/default=str digest boundary classification","Swift JSONSerialization rejection behavior","live approval/nonce/store enforcement (format vectors exercise no runtime)","C %.17g Rust implementation; deadline oracle frozen only"]}
    output("vectors/seals/extra-provenance.json",provenance)
    for path,raw in outputs.items():
        if args.check:assert path.read_bytes()==raw,f"changed extra vectors: {path}"
        else:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    print(f"{'verified' if args.check else 'wrote'} {len(outputs)} extra vector files; original Python/Rust source, fixed test-only keys")
    return 0

if __name__=="__main__":raise SystemExit(main())
