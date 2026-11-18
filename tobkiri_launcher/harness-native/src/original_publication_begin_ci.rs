//! Exact original journal encoding and persistence on CI-owned state.
use super::super::{publication_begin, *};
use crate::BootstrapActivationCandidate;
use hmac::{Hmac, Mac};
use sha2::{Digest as _, Sha256};
use tobkiri_canon::{AllocationBudget, AllocationError, CoreBuffer, CoreValue, MAX_BYTES};

fn capacity(_: AllocationError) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn begin<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    scope: &RecoveryScope,
    predecessor: Option<&active_profile::LoadedPointer>,
    candidate: &BootstrapActivationCandidate,
    read_published_source: impl FnOnce() -> Result<(CoreValue, Digest)>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    let (source, raw_bundle_digest) = read_published_source()?;
    let definition = source.digest_guest(budget).map_err(capacity)?;
    if &definition != candidate.definition_digest()
        || &raw_bundle_digest != candidate.bundle_digest()
    {
        return Err(raw(
            crate::bootstrap_review::DENIED,
            "bootstrap target changed after confirmation",
        ));
    }
    scope.validate(op)?;
    let directory = &scope.directory;
    drop(source);
    if directory.exists(op, JOURNAL)? {
        return Err(value(
            "Profile publication already has an unfinished intent",
        ));
    }
    let payload = publication_begin::payload(op, predecessor, candidate, budget)?;
    let key_path = directory.publication_key_path();
    let mut generated = Zeroizing::new([0_u8; 64]);
    op.check(OriginalAuthorityStage::Key)?;
    let existing = if key_path.is_symlink() || key_path.exists() {
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
    let intent = encode(op, payload, &key, budget)?;
    directory.create_publication_intent(op, intent.as_bytes())?;
    Ok(intent)
}

fn encode<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    payload: CoreValue,
    key: &[u8],
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
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
    let bytes = fs::read(&path).unwrap();
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
    let checks = imported["publication_checks"].as_array().unwrap();
    let fixture_root = path.with_file_name("inactive-registry-inputs");
    let load_candidate = |case: &str, budget: &Arc<dyn AllocationBudget>| {
        let matched = checks.iter().find(|row| row["case"] == case).unwrap();
        let stored = &matched["outcome"]["candidate"];
        let expected = &stored["intent"]["payload"]["candidate"];
        let definition =
            Digest::parse(expected["profile_definition_digest"].as_str().unwrap()).unwrap();
        let lock = Digest::parse(expected["lock_digest"].as_str().unwrap()).unwrap();
        let bundle = Digest::parse(oracle_digest(matched)).unwrap();
        let plan = Digest::parse(expected["plan_digest"].as_str().unwrap()).unwrap();
        let resolved =
            fs::read(fixture_root.join(format!("publication-{case}/resolved-records.json")))
                .unwrap();
        assert!(resolved.len() <= MAX_BYTES);
        assert_eq!(
            &Digest::hash(&resolved).as_str()[7..],
            stored["resolved_records_sha256"].as_str().unwrap()
        );
        let source = fs::read(fixture_root.join("source-profile.json")).unwrap();
        let source = CoreValue::parse_guest(&source, budget).unwrap();
        let confirmation = matched
            .get("confirmation")
            .unwrap_or(&imported["setup_review"]["confirmation"]);
        let confirmation = serde_json::to_vec(confirmation).unwrap();
        let confirmation = CoreValue::parse_guest(&confirmation, budget).unwrap();
        let records = CoreValue::parse_guest(&resolved, budget).unwrap();
        let (candidate, admitted) = BootstrapActivationCandidate::fixture(
            confirmation,
            source,
            records,
            bundle,
            [definition, plan, lock],
            budget,
        )
        .unwrap();
        let encoded = admitted.encode_guest(budget).unwrap();
        assert_eq!(encoded.as_bytes(), resolved);
        candidate
    };
    let reread = |case: &str, budget: &Arc<dyn AllocationBudget>| -> Result<(CoreValue, Digest)> {
        let dir = fixture_root.join(format!("publication-{case}"));
        let source = fs::read(dir.join("reread-source.json")).unwrap();
        let raw_lock = fs::read(dir.join("reread-bundle.lock.json")).unwrap();
        assert!(source.len() <= MAX_BYTES && raw_lock.len() <= MAX_BYTES);
        Ok((
            CoreValue::parse_guest(&source, budget).map_err(capacity)?,
            Digest::hash(&raw_lock),
        ))
    };

    for case in ["matched", "matched-with-predecessor"] {
        let matched = checks.iter().find(|row| row["case"] == case).unwrap();
        let oracle = &matched["outcome"]["candidate"]["intent"];
        let expected = &oracle["payload"];
        with_source(|source, read, cancel, budget| {
            use std::os::unix::fs::PermissionsExt;

            let profiles = source.root.join("profiles");
            if case == "matched-with-predecessor" {
                let prior = &expected["predecessor"];
                assert_ne!(prior["profile_id"], expected["candidate"]["profile_id"]);
                let snapshot = Path::new(prior["activation_snapshot_path"].as_str().unwrap());
                assert!(snapshot.starts_with("workspaces/prior-profile/activation/activations"));
                assert!(
                    snapshot
                        .components()
                        .all(|segment| matches!(segment, std::path::Component::Normal(_)))
                );
                let input = fixture_root.join(format!("publication-{case}/user-data"));
                for relative in [Path::new("profiles/active.json"), snapshot] {
                    let target = source.root.join(relative);
                    fs::create_dir_all(target.parent().unwrap()).unwrap();
                    fs::copy(input.join(relative), target).unwrap();
                }
            }
            let candidate = load_candidate(case, budget);
            assert_eq!(
                read.begin_confirmed(source, cancel, candidate.confirmation().share(), budget)
                    .unwrap(),
                Prefix::NoIntent
            );
            assert!(!lock_free(&profiles.join(LOCK)));
            if case == "matched-with-predecessor" {
                // A later selection change belongs to the final pointer CAS.
                // Preparation must use the predecessor already verified at entry.
                fs::rename(
                    profiles.join("active.json"),
                    profiles.join("prior-captured.json"),
                )
                .unwrap();
            }
            let payload = read
                .prepare_initial_publication(source, cancel, &candidate, budget)
                .unwrap();
            let compact = payload.encode_guest(budget).unwrap();
            assert_eq!(
                &serde_json::from_slice::<Value>(compact.as_bytes()).unwrap(),
                expected
            );
            assert!(!profiles.join(KEY).exists() && !profiles.join(JOURNAL).exists());
            drop((payload, compact));
            let op = operation(read, source, cancel);
            let confirmed = read.confirmed.as_ref().unwrap();
            let key = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
            fs::write(profiles.join(KEY), key).unwrap();
            fs::set_permissions(profiles.join(KEY), fs::Permissions::from_mode(0o600)).unwrap();
            let encoded = begin(
                &op,
                &confirmed.scope,
                confirmed.predecessor.as_ref(),
                &candidate,
                || {
                    assert!(!lock_free(&profiles.join(LOCK)));
                    reread("matched", budget)
                },
                budget,
            )
            .unwrap();
            assert_eq!(
                &Digest::hash(encoded.as_bytes()).as_str()[7..],
                oracle["encoded_sha256"].as_str().unwrap()
            );
            assert_eq!(
                confirmed
                    .scope
                    .directory
                    .create_publication_intent(&op, b"second intent")
                    .unwrap_err()
                    .message,
                "Profile publication already has an unfinished intent",
            );
            let stored = fs::read(profiles.join(JOURNAL)).unwrap();
            assert_eq!(stored, encoded.as_bytes());
            let envelope: Value = serde_json::from_slice(&stored).unwrap();
            assert_eq!(&envelope["payload"], expected);
            assert!(read_intent(&op, &profiles).unwrap().is_some());
            read.close_continuation().unwrap();
            assert!(lock_free(&profiles.join(LOCK)));
        });
    }

    with_source(|source, read, cancel, budget| {
        let approved = CoreValue::parse_guest(br#"{"different":true}"#, budget).unwrap();
        read.begin_confirmed(source, cancel, approved, budget)
            .unwrap();
        let candidate = load_candidate("matched", budget);
        let error = read
            .prepare_initial_publication(source, cancel, &candidate, budget)
            .unwrap_err();
        assert_eq!(
            error.message,
            "bootstrap activation confirmation is stale or tampered"
        );
        let profiles = source.root.join("profiles");
        assert!(!profiles.join(KEY).exists() && !profiles.join(JOURNAL).exists());
        read.close_continuation().unwrap();
    });

    // An absent key uses the original 64-character format. Reopening a later
    // transaction uses those same persisted bytes and signs the same target.
    with_source(|source, read, cancel, budget| {
        use std::os::unix::fs::{MetadataExt, symlink};

        let op = operation(read, source, cancel);
        let root = &source.root;
        let profiles = root.join("profiles");
        let candidate = load_candidate("matched", budget);
        with_confirmed_predecessor(&op, root, budget, |scope, predecessor| {
            assert!(predecessor.is_none());
            let dir = &scope.directory;
            let key = profiles.join(KEY);
            let journal = profiles.join(JOURNAL);
            let begin = || {
                begin(
                    &op,
                    scope,
                    predecessor,
                    &candidate,
                    || reread("matched", budget),
                    budget,
                )
            };
            symlink("absent-key-target", &key).unwrap();
            assert_eq!(
                begin().unwrap_err().class,
                "core_runtime.hmac_key_manager.SigningKeyError"
            );
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

    for case in ["changed-source", "changed-lock"] {
        let original = imported["publication_checks"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["case"] == case)
            .unwrap();
        with_source(|source, read, cancel, budget| {
            let op = operation(read, source, cancel);
            let root = &source.root;
            let profiles = root.join("profiles");
            let candidate = load_candidate("matched", budget);
            let error = with_confirmed_predecessor(&op, root, budget, |scope, predecessor| {
                assert!(predecessor.is_none());
                begin(
                    &op,
                    scope,
                    predecessor,
                    &candidate,
                    || {
                        assert!(!lock_free(&profiles.join(LOCK)));
                        let (source, lock) = reread(case, budget)?;
                        assert_eq!(lock.as_str(), oracle_digest(original));
                        Ok((source, lock))
                    },
                    budget,
                )
            })
            .unwrap_err();
            assert_eq!(error.class, crate::bootstrap_review::DENIED);
            let original = &original["outcome"]["error"];
            assert_eq!(
                original["type"],
                "ecosystem.defaultspack.domain.runtime_v4.service.ProfileResolutionDenied"
            );
            assert_eq!(error.message, original["message"].as_str().unwrap());
            assert!(error.cause.is_none() && error.context.is_none() && !error.suppressed);
            assert!(
                original["cause"].is_null()
                    && original["context"].is_null()
                    && original["suppressed"] == false
            );
            assert!(!profiles.join(KEY).exists() && !profiles.join(JOURNAL).exists());
            assert!(lock_free(&profiles.join(LOCK)));
        });
    }

    with_source(|source, read, cancel, budget| {
        let op = operation(read, source, cancel);
        let root = &source.root;
        let profiles = root.join("profiles");
        let candidate = load_candidate("matched", budget);
        let moved = root.join("moved-profiles");
        let error = with_confirmed_predecessor(&op, root, budget, |scope, predecessor| {
            assert!(predecessor.is_none());
            begin(
                &op,
                scope,
                predecessor,
                &candidate,
                || {
                    let published = reread("matched", budget)?;
                    fs::rename(&profiles, &moved).unwrap();
                    fs::create_dir(&profiles).unwrap();
                    Ok(published)
                },
                budget,
            )
        })
        .unwrap_err();
        assert_eq!(error.message, "persistence ancestor identity changed");
        assert!(!profiles.join(KEY).exists() && !profiles.join(JOURNAL).exists());
        assert!(!moved.join(KEY).exists() && !moved.join(JOURNAL).exists());
        assert!(lock_free(&moved.join(LOCK)));
    });
}

fn oracle_digest(row: &serde_json::Value) -> &str {
    row["reread_bundle_digest"].as_str().unwrap()
}
