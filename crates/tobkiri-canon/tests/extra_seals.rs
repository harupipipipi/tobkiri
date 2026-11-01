use base64::{
    Engine as _,
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
};
use serde_json::Value;
use std::{collections::BTreeMap, fs, path::PathBuf};
use tobkiri_canon::*;

fn rows(name: &str) -> Vec<Value> {
    fs::read_to_string(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../vectors/seals")
            .join(format!("{name}.jsonl")),
    )
    .unwrap()
    .lines()
    .map(|l| serde_json::from_str(l).unwrap())
    .collect()
}
fn bytes(row: &Value, field: &str) -> Vec<u8> {
    STANDARD.decode(row[field].as_str().unwrap()).unwrap()
}
fn hex(text: &str) -> Vec<u8> {
    text.as_bytes()
        .chunks(2)
        .map(|c| u8::from_str_radix(std::str::from_utf8(c).unwrap(), 16).unwrap())
        .collect()
}

#[test]
fn source_ui_newline_and_rust_debug_json_hmac_families_match_exact_stored_bytes() {
    for family in ["ui-operator", "debug-cli-operator"] {
        let cases = rows(family);
        assert!(cases.len() >= 300);
        for row in cases {
            let payload = bytes(&row, "message_b64");
            let key = hex(row["test_only_key_hex"].as_str().unwrap());
            verify_seal_hex(&payload, row["hmac_hex"].as_str().unwrap(), &key).unwrap();
            let mut changed = payload.clone();
            changed.push(b'\n');
            assert!(verify_seal_hex(&changed, row["hmac_hex"].as_str().unwrap(), &key).is_err());
            if family == "ui-operator" {
                assert!(!payload.ends_with(b"\n"));
            } else {
                let value = parse(&payload, Variant::StrictV1).unwrap();
                assert_eq!(encode(&value, Variant::StrictV1).unwrap(), payload);
            }
        }
    }
}
#[test]
fn launcher_newline_and_release_nul_ed25519_are_distinct_and_tamper_evident() {
    let key = ed25519_dalek::SigningKey::from_bytes(&std::array::from_fn(|i| i as u8))
        .verifying_key()
        .to_bytes();
    for row in rows("launcher-response") {
        let payload = bytes(&row, "message_b64");
        assert!(payload.starts_with(b"tobkiri-launcher-response-v1\n"));
        assert!(!payload.ends_with(b"\n"));
        let signature: [u8; 64] = URL_SAFE_NO_PAD
            .decode(
                row["input"]["_launcher_attestation"]["signature"]
                    .as_str()
                    .unwrap(),
            )
            .unwrap()
            .try_into()
            .unwrap();
        verify_signature_bytes(&payload, &key, &signature).unwrap();
        let mut changed = payload;
        changed.push(b'\n');
        assert!(verify_signature_bytes(&changed, &key, &signature).is_err());
    }
    for row in rows("release-nul") {
        let payload = bytes(&row, "message_b64");
        assert_eq!(payload.iter().filter(|b| **b == 0).count(), 11);
        assert!(!payload.ends_with(b"\0"));
        let signature = URL_SAFE_NO_PAD
            .decode(row["signature_b64url"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        verify_signature_bytes(&payload, &key, &signature).unwrap();
        let changed: Vec<_> = payload
            .iter()
            .map(|b| if *b == 0 { b'\n' } else { *b })
            .collect();
        assert!(verify_signature_bytes(&changed, &key, &signature).is_err());
    }
}
#[test]
fn approval_token_keeps_ascii_escaped_json_and_macs_transport_not_decoded_body() {
    for row in rows("approval-token") {
        let token = row["token"].as_str().unwrap();
        let (encoded, signature) = token.split_once('.').unwrap();
        assert!(!encoded.contains('='));
        assert!(!signature.contains('='));
        let payload = bytes(&row, "json_b64");
        assert_eq!(URL_SAFE_NO_PAD.decode(encoded).unwrap(), payload);
        assert_eq!(bytes(&row, "mac_input_b64"), encoded.as_bytes());
        assert!(
            std::str::from_utf8(&payload)
                .unwrap()
                .contains("\\u3042\\ud83d\\ude80")
        );
        let signature = URL_SAFE_NO_PAD.decode(signature).unwrap();
        let key = row["test_only_secret_ascii"].as_str().unwrap().as_bytes();
        verify_seal_bytes(encoded.as_bytes(), &signature, key).unwrap();
        assert!(verify_seal_bytes(&payload, &signature, key).is_err());
        parse(&payload, Variant::StrictV1).unwrap();
    }
}
fn strip(value: CanonValue) -> CanonValue {
    match value {
        CanonValue::Obj(values) => CanonValue::Obj(
            values
                .into_iter()
                .filter(|(k, _)| {
                    ![
                        "approval_token",
                        "approved",
                        "computer_use_haze_sequence_id",
                        "computer_use_sequence_id",
                        "_headers",
                        "_method",
                        "_raw_body",
                        "_raw_body_base64",
                    ]
                    .contains(&k.as_str())
                })
                .map(|(k, v)| (k, strip(v)))
                .collect(),
        ),
        CanonValue::Arr(values) => CanonValue::Arr(values.into_iter().map(strip).collect()),
        v => v,
    }
}
#[test]
fn legacy_argument_hash_filters_and_packvm_bridge_use_wider_float_domain() {
    for row in rows("approval-args") {
        let value = parse(
            &serde_json::to_vec(&row["input"]).unwrap(),
            Variant::LegacyFloat,
        )
        .unwrap();
        let value = strip(value);
        assert_eq!(
            digest(&value, Variant::LegacyFloat).unwrap().hex(),
            row["hash_untagged"].as_str().unwrap()
        );
    }
    for row in rows("packvm-bridge") {
        let value = parse(
            &serde_json::to_vec(&row["input"]).unwrap(),
            Variant::LegacyFloat,
        )
        .unwrap();
        assert_eq!(
            encode(&value, Variant::LegacyFloat).unwrap(),
            bytes(&row, "bytes_b64")
        );
        assert_eq!(
            digest(&value, Variant::LegacyFloat).unwrap().as_str(),
            row["digest"].as_str().unwrap()
        );
        assert!(encode(&value, Variant::StrictV1).is_err());
    }
}
#[derive(serde::Serialize)]
struct SealedFile<'a> {
    path: &'a str,
    size: u64,
    sha256: &'a str,
    executable: bool,
}
#[test]
fn sealed_environment_retains_insertion_order_and_tagged_digest_boundary() {
    for row in rows("sealed-environment") {
        let payload = bytes(&row, "bytes_b64");
        let identity = hash_bytes(&payload);
        assert_eq!(identity.hex(), row["digest_untagged"].as_str().unwrap());
        let records: Vec<_> = row["input"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| SealedFile {
                path: r["path"].as_str().unwrap(),
                size: r["size"].as_u64().unwrap(),
                sha256: r["sha256"].as_str().unwrap(),
                executable: r["executable"].as_bool().unwrap(),
            })
            .collect();
        assert_eq!(serde_json::to_vec(&records).unwrap(), payload);
        let alphabetized = parse(&payload, Variant::StrictV1).unwrap();
        assert_ne!(
            digest(&alphabetized, Variant::StrictV1).unwrap().hex(),
            row["digest_untagged"].as_str().unwrap()
        );
    }
}
#[test]
fn deadline_oracle_retains_positive_finite_ieee_identity() {
    let cases = rows("packvm-deadline");
    assert!(cases.len() >= 300);
    for row in cases {
        let bits = u64::from_str_radix(row["input_bits_hex"].as_str().unwrap(), 16).unwrap();
        let value = f64::from_bits(bits);
        if row["expect"] == "ok" {
            assert!(value.is_finite() && value > 0.0);
            assert_eq!(
                row["text"]
                    .as_str()
                    .unwrap()
                    .parse::<f64>()
                    .unwrap()
                    .to_bits(),
                bits
            );
        } else {
            assert!(!value.is_finite() || value <= 0.0);
        }
    } // This freezes the original .17g rule; Rust formatting parity remains a migration TODO.
}
#[test]
fn legacy_nonfinite_decode_encode_asymmetry_and_strict_ed_rejections_are_explicit() {
    for text in [b"NaN".as_slice(), b"Infinity", b"-Infinity"] {
        let value = parse(text, Variant::LegacyFloat).unwrap();
        assert!(encode(&value, Variant::LegacyFloat).is_err());
        assert_eq!(encode(&value, Variant::LegacyPackSig).unwrap(), text);
        assert!(parse(text, Variant::StrictV1).is_err());
    }
    assert!(verify_signature_bytes(b"fixture", &[0; 32], &[0; 64]).is_err());
    let value = CanonValue::Obj(BTreeMap::new());
    let signer = ed25519_dalek::SigningKey::from_bytes(&[5; 32]);
    let signature = sign(&value, &signer).unwrap();
    verify_signature(
        &value,
        Variant::StrictV1,
        &signer.verifying_key().to_bytes(),
        &signature,
    )
    .unwrap();
    let mut bad = signature;
    bad[63] |= 0x80;
    assert!(
        verify_signature(
            &value,
            Variant::StrictV1,
            &signer.verifying_key().to_bytes(),
            &bad
        )
        .is_err()
    );
}

#[test]
fn actual_legacy_negative_ed25519_outcomes_and_weak_key_parity_gap_are_frozen() {
    let cases = rows("pack-ed25519-negative");
    assert_eq!(cases.len(), 300);
    let mut gap_count = 0;
    for row in cases {
        let key = hex(row["public_key_hex"].as_str().unwrap())
            .try_into()
            .unwrap();
        let signature = hex(row["signature_hex"].as_str().unwrap())
            .try_into()
            .unwrap();
        let result = verify_signature_bytes(&bytes(&row, "input_b64"), &key, &signature);
        let legacy = row["oracle_accepts"].as_bool().unwrap();
        if legacy && row["weak_key_case"] == true {
            gap_count += 1;
            assert!(
                result.is_err(),
                "strict verification must retain weak-key rejection"
            );
        } else {
            assert_eq!(result.is_ok(), legacy, "oracle case {}", row["id"]);
        }
    }
    assert!(
        gap_count > 0,
        "known crypto acceptance mismatch must remain visible; this is not full legacy parity"
    );
}
