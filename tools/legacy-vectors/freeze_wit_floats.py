#!/usr/bin/env python3
"""Copy/check actual Rust SDK width vectors, never a JS-derived formatter."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("--check",action="store_true")
args=parser.parse_args()
source=ROOT/"sdk/js/tests/float-golden.json"
raw=source.read_bytes()
cases=json.loads(raw)
assert len(cases)>=1000 and all(c["width"] in {"f32","f64"} for c in cases)
provenance={"test_only":True,"cases":len(cases),"source":str(source.relative_to(ROOT)),"golden_sha256_hex":hashlib.sha256(raw).hexdigest(),"authority":"Rust std format!(\"{value:e}\") at declared width, final opus-float-policy","reproduce":"cargo run --manifest-path packs/Cargo.toml --locked -p tobkiri-guest-sdk --example float_vectors","sources":{}}
for relative in ["packs/guest-sdk/examples/float_vectors.rs","packs/guest-sdk/src/lib.rs"]:
    provenance["sources"][relative]={"sha256_hex":hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()}
outputs={ROOT/"vectors/wit-floats.json":raw,ROOT/"vectors/wit-floats-provenance.json":(json.dumps(provenance,sort_keys=True,indent=2)+"\n").encode()}
for path,content in outputs.items():
    if args.check:assert path.read_bytes()==content,str(path)
    else:path.write_bytes(content)
print(f"{'verified' if args.check else 'froze'} {len(cases)} actual Rust-width float vectors")
