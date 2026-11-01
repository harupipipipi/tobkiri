use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use serde_json::Value;
use std::{fs, path::PathBuf};
use tobkiri_canon::{
    Digest, Variant, artifact_tree_digest, derive_lease_mac_key, digest, encode, lease_token,
    legacy_key_id, parse, seal, verify_lease_token, verify_seal_hex, verify_signature,
};

fn rows(name: &str) -> Vec<Value> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../vectors")
        .join(name);
    fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}
fn bytes(value: &Value, field: &str) -> Vec<u8> {
    STANDARD.decode(value[field].as_str().unwrap()).unwrap()
}
fn hex(value: &str) -> Vec<u8> {
    value
        .as_bytes()
        .chunks(2)
        .map(|c| u8::from_str_radix(std::str::from_utf8(c).unwrap(), 16).unwrap())
        .collect()
}

#[test]
fn all_three_python_codec_corpora_match_bytes_and_failures() {
    for (name, variant) in [
        ("StrictV1", Variant::StrictV1),
        ("LegacyFloat", Variant::LegacyFloat),
        ("LegacyPackSig", Variant::LegacyPackSig),
    ] {
        let cases = rows(&format!("canon/{name}.jsonl"));
        assert!(cases.len() >= 300);
        for case in cases {
            let actual = parse(&bytes(&case, "input_b64"), variant)
                .and_then(|v| encode(&v, variant).map(|b| (v, b)));
            if case["expect"] == "error" {
                assert!(
                    actual.is_err(),
                    "{name} case {} unexpectedly accepted",
                    case["id"]
                );
            } else {
                let (value, actual) =
                    actual.unwrap_or_else(|e| panic!("{name} case {} failed: {e}", case["id"]));
                assert_eq!(
                    actual,
                    bytes(&case, "output_b64"),
                    "{name} case {}",
                    case["id"]
                );
                assert_eq!(
                    digest(&value, variant).unwrap().as_str(),
                    case["digest"].as_str().unwrap()
                );
            }
        }
    }
}
#[test]
fn workflow_seals_and_authority_lease_tokens_match_distinct_wire_formats() {
    for case in rows("seals/workflow-hmac.jsonl") {
        let payload = bytes(&case, "input_b64");
        let key = hex(case["key_hex"].as_str().unwrap());
        let value = parse(&payload, Variant::StrictV1).unwrap();
        assert_eq!(seal(&value, Variant::StrictV1, &key).unwrap(), case["seal"]);
        verify_seal_hex(&payload, case["seal"].as_str().unwrap(), &key).unwrap();
        let mut changed = payload.clone();
        changed.push(b' ');
        assert!(verify_seal_hex(&changed, case["seal"].as_str().unwrap(), &key).is_err());
    }
    for case in rows("seals/authority-lease.jsonl") {
        let key = derive_lease_mac_key(case["fernet_key_ascii"].as_str().unwrap().as_bytes());
        assert_eq!(
            key.as_slice(),
            hex(case["derived_key_hex"].as_str().unwrap())
        );
        let value = parse(&bytes(&case, "input_b64"), Variant::LegacyFloat).unwrap();
        let identity = digest(&value, Variant::LegacyFloat).unwrap();
        assert_eq!(identity.as_str(), case["lease_digest"]);
        let token = lease_token(case["lease_id"].as_str().unwrap(), &identity, &key).unwrap();
        assert_eq!(token, case["token"]);
        assert_eq!(
            verify_lease_token(&token, &key).unwrap(),
            (case["lease_id"].as_str().unwrap().into(), identity)
        );
    }
}
#[test]
fn audit_continuation_and_pack_signature_oracles_match() {
    for (name, variant) in [
        ("authority-audit", Variant::LegacyFloat),
        ("continuation-frame", Variant::StrictV1),
    ] {
        for case in rows(&format!("seals/{name}.jsonl")) {
            let value = parse(&bytes(&case, "input_b64"), variant).unwrap();
            assert_eq!(digest(&value, variant).unwrap().as_str(), case["digest"]);
        }
    }
    for case in rows("seals/pack-ed25519.jsonl") {
        let key: [u8; 32] = hex(case["public_key_hex"].as_str().unwrap())
            .try_into()
            .unwrap();
        let signature: [u8; 64] = URL_SAFE_NO_PAD
            .decode(case["signature_b64url"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let value = parse(&bytes(&case, "input_b64"), Variant::LegacyPackSig).unwrap();
        assert_eq!(legacy_key_id(&key), case["key_id"]);
        verify_signature(&value, Variant::LegacyPackSig, &key, &signature).unwrap();
        let mut changed = signature;
        changed[0] ^= 1;
        assert!(verify_signature(&value, Variant::LegacyPackSig, &key, &changed).is_err());
    }
}
#[test]
fn artifact_tree_corpus_is_shared_with_python_oracle() {
    for case in rows("seals/artifact-tree.jsonl") {
        let directory = tempfile::tempdir().unwrap();
        for (name, value) in case["files"].as_object().unwrap() {
            let path = directory.path().join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, value.as_str().unwrap()).unwrap();
        }
        assert_eq!(
            artifact_tree_digest(directory.path()).unwrap(),
            Digest::parse(case["digest"].as_str().unwrap()).unwrap()
        );
    }
}
