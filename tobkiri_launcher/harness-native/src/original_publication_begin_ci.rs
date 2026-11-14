//! Exact original journal encoding and persistence on CI-owned state.
use super::super::*;
use hmac::{Hmac, Mac};
use sha2::{Digest as _, Sha256};
use tobkiri_canon::{AllocationBudget, AllocationError, CoreBuffer, CoreValue, MAX_BYTES};

const SCHEMA: &str = "io.tobkiri.profile-publication-intent.v1";

struct Target<'a> {
    confirmation: &'a CoreValue,
    definition: &'a Digest,
    lock: &'a Digest,
}

fn capacity(_: AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn begin<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    directory: &Directory,
    profiles: &Path,
    target: Target<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    if directory.exists(op, JOURNAL)? {
        return Err(value(
            "Profile publication already has an unfinished intent",
        ));
    }
    let key_path = profiles.join(KEY);
    let mut generated = Zeroizing::new([0_u8; 64]);
    let existing = if directory.exists(op, KEY)? {
        Some(signing_key(op, &key_path)?)
    } else {
        let mut random = Zeroizing::new([0_u8; 32]);
        getrandom::fill(&mut random[..]).map_err(|_| OriginalAuthorityError::unavailable())?;
        let mut digest = Sha256::digest(&random[..]);
        for (pair, &byte) in generated.chunks_exact_mut(2).zip(digest.iter()) {
            let hex = b"0123456789abcdef";
            pair[0] = hex[usize::from(byte >> 4)];
            pair[1] = hex[usize::from(byte & 0xf)];
        }
        zeroize::Zeroize::zeroize(&mut digest[..]);
        directory.create_publication_key(op, &generated[..])?;
        None
    };
    let expected = match &existing {
        Some(existing) => existing.as_slice(),
        None => &generated[..],
    };
    directory.sync_publication_key(op)?;
    let key = signing_key(op, &key_path)?;
    if key.as_slice() != expected {
        return Err(value("Profile publication signing key changed"));
    }
    let intent = encode(op, None, target, &key, budget)?;
    directory.create_publication_intent(op, intent.as_bytes())?;
    Ok(intent)
}

fn encode<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    predecessor: Option<&CoreValue>,
    target: Target<'_>,
    key: &[u8],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let required = |name| {
        target
            .confirmation
            .get_key(name)
            .ok_or_else(|| value("confirmed publication identity is missing"))
    };
    let profile = required("profile_id")?;
    let profile_id = profile
        .as_str()
        .ok_or_else(|| value("confirmed publication Profile is invalid"))?;
    if profile_id.is_empty() || profile_id.len() > 128 {
        return Err(value("confirmed publication Profile is invalid"));
    }
    let plan = required("plan_digest")?;
    let digest = plan
        .as_str()
        .and_then(|text| Digest::parse(text).ok())
        .ok_or_else(|| value("confirmed publication Plan is invalid"))?;
    let mut activation = CoreBuffer::builder(budget, 160);
    for part in ["activation:", profile_id, "-", &digest.as_str()[7..23]] {
        activation.append(part.as_bytes()).map_err(capacity)?;
    }
    let activation = activation.finish().map_err(capacity)?;
    let candidate = CoreValue::object_shared(
        [
            ("profile_id", profile),
            ("profile_revision", required("profile_revision")?),
            (
                "activation_id",
                CoreValue::string_from_utf8(&activation, budget).map_err(capacity)?,
            ),
            ("plan_digest", plan),
            (
                "lock_digest",
                CoreValue::string(target.lock.as_str(), budget).map_err(capacity)?,
            ),
            (
                "profile_definition_digest",
                CoreValue::string(target.definition.as_str(), budget).map_err(capacity)?,
            ),
            ("security_epoch", required("security_epoch")?),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)?;
    let confirmed = target.confirmation.digest_guest(budget).map_err(capacity)?;
    let predecessor = match predecessor {
        Some(pointer) => pointer.share(),
        None => CoreValue::null(budget).map_err(capacity)?,
    };
    let payload = CoreValue::object_shared(
        [
            (
                "schema",
                CoreValue::string(SCHEMA, budget).map_err(capacity)?,
            ),
            ("predecessor", predecessor),
            ("candidate", candidate),
            (
                "confirmation_digest",
                CoreValue::string(confirmed.as_str(), budget).map_err(capacity)?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)?;
    let compact = payload.encode_guest(budget).map_err(capacity)?;
    let preimage = python_default_spacing(op, compact.as_bytes())?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(key).map_err(|_| OriginalAuthorityError::unavailable())?;
    mac.update(&preimage);
    let hex = Digest::from_sha256(mac.finalize().into_bytes().into());
    drop((preimage, compact));
    let envelope = CoreValue::object_shared(
        [
            ("payload", payload),
            (
                "hmac",
                CoreValue::string(&hex.as_str()[7..], budget).map_err(capacity)?,
            ),
        ]
        .into_iter()
        .map(Ok),
        budget,
    )
    .map_err(capacity)?;
    let envelope = envelope.encode_guest(budget).map_err(capacity)?;
    let mut result = CoreBuffer::builder(budget, MAX_BYTES);
    result.append(envelope.as_bytes()).map_err(capacity)?;
    result.append(b"\n").map_err(capacity)?;
    result.finish().map_err(capacity)
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "requires the pinned original's real publication writer receipt"]
async fn original_publication_intent_bytes_match() {
    use super::tests::{lock_free, operation, with_source};
    use serde_json::Value;

    assert_eq!(std::env::var("GITHUB_ACTIONS").as_deref(), Ok("true"));
    let path =
        PathBuf::from(std::env::var_os("TOBKIRI_ORIGINAL_INACTIVE_REGISTRY_REFERENCE").unwrap());
    let bytes = fs::read(path).unwrap();
    assert!(bytes.len() <= 2 * 1024 * 1024);
    let fixture: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        fixture["original_head"],
        "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
    );
    let imported = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["case"] == "authenticated-handover")
        .unwrap();
    let matched = imported["publication_checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["case"] == "matched")
        .unwrap();
    let oracle = &matched["outcome"]["candidate"]["intent"];
    let expected = &oracle["payload"];
    let expected_sha256 = oracle["encoded_sha256"].as_str().unwrap();
    let definition = Digest::parse(
        expected["candidate"]["profile_definition_digest"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    let lock = Digest::parse(expected["candidate"]["lock_digest"].as_str().unwrap()).unwrap();

    with_source(|source, read, cancel, budget| {
        use std::os::unix::fs::PermissionsExt;

        let op = operation(read, source, cancel);
        let root = &source.root;
        let profiles = root.join("profiles");
        let confirmation = serde_json::to_vec(&imported["setup_review"]["confirmation"]).unwrap();
        let confirmation = CoreValue::parse_guest(&confirmation, budget).unwrap();
        let key = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        let result = with_confirmed_predecessor(&op, root, budget, |predecessor| {
            assert!(predecessor.is_none());
            let dir = Directory::capture(&op, &profiles, false)?;
            assert!(!lock_free(&profiles.join(LOCK)));
            fs::write(profiles.join(KEY), key).unwrap();
            fs::set_permissions(profiles.join(KEY), fs::Permissions::from_mode(0o600)).unwrap();
            let encoded = begin(
                &op,
                &dir,
                &profiles,
                Target {
                    confirmation: &confirmation,
                    definition: &definition,
                    lock: &lock,
                },
                budget,
            )?;
            assert_eq!(
                &Digest::hash(encoded.as_bytes()).as_str()[7..],
                expected_sha256
            );
            assert_eq!(
                dir.create_publication_intent(&op, b"second intent")
                    .unwrap_err()
                    .message,
                "Profile publication already has an unfinished intent",
            );
            let stored = fs::read(profiles.join(JOURNAL)).unwrap();
            assert_eq!(stored, encoded.as_bytes());
            let envelope: Value = serde_json::from_slice(&stored).unwrap();
            assert_eq!(&envelope["payload"], expected);
            assert!(read_intent(&op, &profiles)?.is_some());
            Ok(())
        });
        result.unwrap();
        assert!(lock_free(&profiles.join(LOCK)));
    });

    // An absent key uses the original 64-character format. Reopening a later
    // transaction uses those same persisted bytes and signs the same target.
    with_source(|source, read, cancel, budget| {
        use std::os::unix::fs::{MetadataExt, symlink};

        let op = operation(read, source, cancel);
        let root = &source.root;
        let profiles = root.join("profiles");
        let confirmation = serde_json::to_vec(&imported["setup_review"]["confirmation"]).unwrap();
        let confirmation = CoreValue::parse_guest(&confirmation, budget).unwrap();
        with_confirmed_predecessor(&op, root, budget, |predecessor| {
            assert!(predecessor.is_none());
            let dir = Directory::capture(&op, &profiles, false)?;
            let key = profiles.join(KEY);
            let journal = profiles.join(JOURNAL);
            let begin = || {
                begin(
                    &op,
                    &dir,
                    &profiles,
                    Target {
                        confirmation: &confirmation,
                        definition: &definition,
                        lock: &lock,
                    },
                    budget,
                )
            };
            symlink("absent-key-target", &key).unwrap();
            assert!(begin().is_err());
            assert!(key.is_symlink());
            assert!(!journal.exists());
            fs::remove_file(&key).unwrap();
            let intent = begin()?;
            let stored = signing_key(&op, &key)?;
            assert_eq!(stored.len(), 64);
            assert!(
                stored
                    .iter()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
            );
            assert_eq!(fs::metadata(&key).unwrap().mode() & 0o777, 0o600);
            assert!(read_intent(&op, &profiles)?.is_some());
            dir.clear_journal(&op)?;
            assert_eq!(begin()?.as_bytes(), intent.as_bytes());
            assert_eq!(signing_key(&op, &key)?.as_slice(), stored.as_slice());
            Ok(())
        })
        .unwrap();
        assert!(lock_free(&profiles.join(LOCK)));
    });
}
