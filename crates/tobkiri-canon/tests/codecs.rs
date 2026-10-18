use tobkiri_canon::{CanonValue, Variant, encode, parse};

#[test]
fn strict_parser_rejects_ambiguity_and_keeps_non_bmp_key_order() {
    for bytes in [
        br#"{"a":1,"\u0061":2}"#.as_slice(),
        b"1.0",
        b"9007199254740992",
        br#""\ud800""#,
        b"[1,]",
    ] {
        assert!(parse(bytes, Variant::StrictV1).is_err());
    }
    let value = parse(br#"{"\ud800\udc00":1,"\ue000":2,"a":3}"#, Variant::StrictV1).unwrap();
    assert_eq!(
        String::from_utf8(encode(&value, Variant::StrictV1).unwrap()).unwrap(),
        "{\"a\":3,\"\u{e000}\":2,\"\u{10000}\":1}"
    );
}
#[test]
fn legacy_duplicates_and_python_float_spelling_are_separate() {
    assert_eq!(
        encode(
            &parse(br#"{"a":1,"a":2}"#, Variant::LegacyFloat).unwrap(),
            Variant::LegacyFloat
        )
        .unwrap(),
        br#"{"a":2}"#
    );
    for (value, expected) in [
        (-0.0, "-0.0"),
        (1.0, "1.0"),
        (1e-5, "1e-05"),
        (1e16, "1e+16"),
        (1e15, "1000000000000000.0"),
        (1e-4, "0.0001"),
        (5e-324, "5e-324"),
    ] {
        assert_eq!(
            encode(&CanonValue::Float(value), Variant::LegacyFloat).unwrap(),
            expected.as_bytes()
        );
    }
    assert!(encode(&CanonValue::Float(f64::NAN), Variant::LegacyFloat).is_err());
    assert_eq!(
        encode(&CanonValue::Float(f64::NAN), Variant::LegacyPackSig).unwrap(),
        b"NaN"
    );
}
#[test]
fn strict_input_and_output_bounds_are_inclusive() {
    let maximum = tobkiri_canon::MAX_BYTES;
    let mut input = vec![b' '; maximum];
    input[0] = b'0';
    assert_eq!(
        parse(&input, Variant::StrictV1).unwrap(),
        CanonValue::Int(0)
    );
    input.push(b' ');
    assert!(parse(&input, Variant::StrictV1).is_err());
    assert_eq!(
        encode(&CanonValue::Str("a".repeat(maximum - 2)), Variant::StrictV1)
            .unwrap()
            .len(),
        maximum
    );
    assert!(encode(&CanonValue::Str("a".repeat(maximum - 1)), Variant::StrictV1).is_err());
    assert!(
        parse(
            format!("{}0{}", "[".repeat(64), "]".repeat(64)).as_bytes(),
            Variant::StrictV1
        )
        .is_ok()
    );
    assert!(
        parse(
            format!("{}0{}", "[".repeat(65), "]".repeat(65)).as_bytes(),
            Variant::StrictV1
        )
        .is_err()
    );
}

#[test]
fn typed_serde_never_rewrites_nonfinite_numbers_to_null_or_accepts_invalid_ids() {
    for value in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
        assert!(serde_json::to_vec(&CanonValue::Float(value)).is_err());
    }
    for value in ["../escape", "C:drive", "CON", "com1", "foo.bar"] {
        assert!(tobkiri_canon::ids::ProfileId::parse(value).is_err());
    }
    assert!(serde_json::from_str::<tobkiri_canon::ids::PackId>("\"not-a-publisher\"").is_err());
    assert!(serde_json::from_str::<tobkiri_canon::Digest>("\"sha256:ABC\"").is_err());
}

#[test]
fn inline_digest_preserves_wire_debug_order_and_string_hash_contracts() {
    use std::hash::{DefaultHasher, Hash, Hasher};
    use tobkiri_canon::Digest;

    const EMPTY: &str = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    let digest = Digest::hash(b"");
    assert_eq!(std::mem::size_of::<Digest>(), 71);
    assert_eq!(digest.as_str(), EMPTY);
    assert_eq!(digest.hex(), &EMPTY[7..]);
    assert_eq!(digest, Digest::parse(EMPTY).unwrap());
    assert_eq!(format!("{digest:?}"), format!("Digest({EMPTY:?})"));
    assert_eq!(
        serde_json::to_string(&digest).unwrap(),
        format!("{EMPTY:?}")
    );
    assert_eq!(
        serde_json::from_str::<Digest>(&format!("{EMPTY:?}")).unwrap(),
        digest
    );
    let mut inline = DefaultHasher::new();
    let mut original_string = DefaultHasher::new();
    digest.hash(&mut inline);
    EMPTY.to_owned().hash(&mut original_string);
    assert_eq!(inline.finish(), original_string.finish());
    let other = Digest::hash(b"other");
    assert_eq!(digest.cmp(&other), digest.as_str().cmp(other.as_str()));
    for invalid in [
        "",
        "sha256:ABC",
        "SHA256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85G",
    ] {
        assert!(Digest::parse(invalid).is_err());
    }
}
