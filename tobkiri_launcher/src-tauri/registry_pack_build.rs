//! Stage the Registry Pack archives from a pinned CI producer receipt.

use std::ffi::OsString;
use std::io;
use std::path::PathBuf;

use super::registry_pack_receipt::valid_raw_sha256;

pub(super) const SOURCE_ENV: &str = "TOBKIRI_REGISTRY_PACK_SOURCE_DIR";
pub(super) const DIGEST_ENV: &str = "TOBKIRI_REGISTRY_PACK_RECEIPT_SHA256";
pub(super) use super::registry_pack_receipt::DIRECTORY;

#[cfg_attr(not(unix), allow(dead_code))]
pub(super) struct Input {
    source: PathBuf,
    receipt_sha256: String,
}

impl Input {
    pub(super) fn configure(
        source: Option<OsString>,
        digest: Option<OsString>,
        allowed: bool,
    ) -> io::Result<Option<Self>> {
        let (source, digest) = match (source, digest) {
            (None, None) => return Ok(None),
            (Some(source), Some(digest)) => (PathBuf::from(source), digest),
            _ => {
                return Err(invalid(format!(
                    "{SOURCE_ENV} and {DIGEST_ENV} must be paired"
                )))
            }
        };
        if !allowed {
            return Err(invalid(
                "Registry Packs require a macOS native-first-setup CI/E2E release build",
            ));
        }
        let receipt_sha256 = digest
            .into_string()
            .ok()
            .filter(|digest| valid_raw_sha256(digest))
            .ok_or_else(|| invalid(format!("{DIGEST_ENV} must be a lowercase SHA-256")))?;
        if !source.is_absolute()
            || source
                .to_str()
                .is_none_or(|path| path.contains(['\n', '\r']))
        {
            return Err(invalid(format!(
                "{SOURCE_ENV} must be an absolute UTF-8 path"
            )));
        }
        Ok(Some(Self {
            source,
            receipt_sha256,
        }))
    }

    pub(super) fn receipt_sha256(&self) -> &str {
        &self.receipt_sha256
    }

    #[cfg(not(unix))]
    pub(super) fn stage(&self, _stage_root: &std::path::Path) -> io::Result<()> {
        Err(invalid("Registry Pack staging requires a Unix build host"))
    }
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

#[cfg(unix)]
mod unix {
    use super::super::registry_pack_receipt::{open_directory, read_verified_receipt, RECEIPT};
    use super::{invalid, Input, DIRECTORY};
    use std::fs::{self, File, OpenOptions};
    use std::io::{self, Write};
    use std::path::Path;

    use super::super::require_staged_owner;

    fn create_output(path: &Path) -> io::Result<File> {
        OpenOptions::new().write(true).create_new(true).open(path)
    }

    impl Input {
        pub(in super::super) fn stage(&self, staged_root: &Path) -> io::Result<()> {
            if self.source.canonicalize()? != self.source {
                return Err(invalid(
                    "Registry Pack source directory must be a physical path",
                ));
            }
            let directory = open_directory(&self.source)?;
            require_staged_owner(&directory.metadata()?, "Registry Pack source directory")?;
            let (receipt, raw) =
                read_verified_receipt(&directory, &self.receipt_sha256, || Ok(()))?;
            let target = staged_root.join(DIRECTORY);
            fs::create_dir(&target)?;
            let result = (|| {
                receipt.verify_archives(
                    &directory,
                    |name| create_output(&target.join(name)),
                    || Ok(()),
                )?;
                create_output(&target.join(RECEIPT))?.write_all(&raw)
            })();
            if let Err(error) = result {
                return match fs::remove_dir_all(&target) {
                    Ok(()) => Err(error),
                    Err(cleanup) => Err(io::Error::new(
                        error.kind(),
                        format!("{error}; Registry Pack staging cleanup failed: {cleanup}"),
                    )),
                };
            }
            println!("cargo:rerun-if-changed={}", self.source.display());
            Ok(())
        }
    }

    #[cfg(test)]
    mod tests {
        use super::super::super::raw_byte_digest;
        use super::super::super::registry_pack_receipt::{
            verify_installed, verify_installed_with_ai_routing, verify_installed_with_conversation,
            verify_installed_with_model, verify_installed_with_orchestration,
            verify_installed_with_orchestration_archives, verify_installed_with_provider,
            verify_installed_with_provider_adapter, verify_installed_with_provider_and_model,
            verify_installed_with_settlement, Pack, AI_ROUTING_PACK_ID, AI_ROUTING_RECEIPT_FORMAT,
            ARCHIVE_LIMIT, ORCHESTRATION_PACK_ID, ORCHESTRATION_RECEIPT_FORMAT, PACK_IDS,
            PROVIDER_ADAPTER_PACK_ID, PROVIDER_ADAPTER_RECEIPT_FORMAT, RECEIPT_FORMAT,
            SETTLEMENT_PACK_ID, SETTLEMENT_RECEIPT_FORMAT,
        };
        use super::super::super::{write_runtime_resource_manifest, RUNTIME_RESOURCE_MANIFEST};
        use super::*;
        use serde_json::{json, Value};
        use std::ffi::OsString;
        use std::os::unix::fs::symlink;
        use std::path::PathBuf;

        struct Fixture {
            root: PathBuf,
            source: PathBuf,
            stage: PathBuf,
            document: Value,
        }

        impl Fixture {
            fn new() -> io::Result<Self> {
                Self::with_packs(RECEIPT_FORMAT, PACK_IDS)
            }

            fn with_packs(
                format: &str,
                ids: impl IntoIterator<Item = &'static str>,
            ) -> io::Result<Self> {
                let root = std::env::temp_dir().canonicalize()?.join(format!(
                    "tobkiri-registry-pack-{:016x}",
                    rand::random::<u64>()
                ));
                fs::create_dir(&root)?;
                let source = root.join("source");
                let stage = root.join("stage");
                fs::create_dir(&source)?;
                fs::create_dir(&stage)?;
                let mut packs = Vec::new();
                for id in ids {
                    let archive = format!("{id}.tpk");
                    fs::write(source.join(&archive), id)?;
                    packs.push(json!({
                        "id": id,
                        "version": "0.1.0",
                        "archive": archive,
                        "archive_bytes": id.len(),
                        "archive_sha256": format!("sha256:{}", raw_byte_digest(id.as_bytes())),
                        "manifest_sha256": format!("sha256:{}", "1".repeat(64)),
                    }));
                }
                let document = json!({"format": format, "packs": packs});
                Ok(Self {
                    root,
                    source,
                    stage,
                    document,
                })
            }

            fn write_receipt(&self, document: &Value) -> io::Result<Input> {
                let raw = serde_json::to_vec(document)?;
                fs::write(self.source.join(RECEIPT), &raw)?;
                Ok(Input {
                    source: self.source.clone(),
                    receipt_sha256: raw_byte_digest(&raw),
                })
            }
        }

        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.root);
            }
        }

        #[test]
        fn settings_are_optional_paired_and_restricted() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let source = fixture.source.as_os_str().to_owned();
            let pin = OsString::from("a".repeat(64));
            assert!(Input::configure(None, None, false)?.is_none());
            assert!(Input::configure(Some(source.clone()), None, true).is_err());
            assert!(Input::configure(None, Some(pin.clone()), true).is_err());
            assert!(Input::configure(Some(source.clone()), Some(pin.clone()), false).is_err());
            assert!(
                Input::configure(Some(source.clone()), Some("A".repeat(64).into()), true).is_err()
            );
            assert!(Input::configure(Some(source), Some(pin), true)?.is_some());
            Ok(())
        }

        #[test]
        fn provider_visitor_only_returns_data_after_the_entire_archive_is_checked() -> io::Result<()>
        {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let read_prefix = |pack: &Pack, source: &mut dyn std::io::Read| {
                assert_eq!(pack.id(), PACK_IDS[0]);
                assert_eq!(pack.version(), "0.1.0");
                assert_eq!(pack.archive_bytes(), PACK_IDS[0].len() as u64);
                assert_eq!(pack.manifest_sha256(), format!("sha256:{}", "1".repeat(64)));
                let mut first = [0];
                source.read_exact(&mut first)?;
                Ok(first[0])
            };
            let verify = || {
                verify_installed_with_provider(
                    &fixture.stage,
                    input.receipt_sha256(),
                    || Ok(()),
                    read_prefix,
                )
            };
            assert_eq!(verify()?, b'l');
            let archive = fixture
                .stage
                .join(DIRECTORY)
                .join(format!("{}.tpk", PACK_IDS[0]));
            let mut bytes = fs::read(&archive)?;
            *bytes.last_mut().unwrap() ^= 1;
            fs::write(&archive, bytes)?;
            assert!(verify().is_err());
            Ok(())
        }

        #[test]
        fn model_visitor_reads_the_exact_authenticated_receipt_record_and_archive() -> io::Result<()>
        {
            let fixture = Fixture::new()?;
            let mut document = fixture.document.clone();
            let manifest_sha256 = format!("sha256:{}", "2".repeat(64));
            document["packs"][1]["manifest_sha256"] = json!(manifest_sha256);
            document["packs"][1]["version"] = json!("0.2.0");
            let input = fixture.write_receipt(&document)?;
            input.stage(&fixture.stage)?;
            let archived_id = b"local.model-registry";
            let bytes = verify_installed_with_model(
                &fixture.stage,
                input.receipt_sha256(),
                || Ok(()),
                |pack, archive| {
                    assert_eq!(pack.id(), "local.model-registry");
                    assert_eq!(pack.version(), "0.2.0");
                    assert_eq!(pack.archive_bytes(), archived_id.len() as u64);
                    assert_eq!(
                        pack.archive_sha256(),
                        format!("sha256:{}", raw_byte_digest(archived_id))
                    );
                    assert_eq!(pack.manifest_sha256(), manifest_sha256);
                    let mut bytes = Vec::new();
                    archive.read_to_end(&mut bytes)?;
                    Ok(bytes)
                },
            )?;
            assert_eq!(bytes, archived_id);
            Ok(())
        }

        #[test]
        fn model_visitor_runs_after_receipt_authentication_and_requires_all_archive_digests(
        ) -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let pin = input.receipt_sha256();

            let mut visited = false;
            let reject_pin = verify_installed_with_model(
                &fixture.stage,
                &"0".repeat(64),
                || Ok(()),
                |_, _| {
                    visited = true;
                    Ok(())
                },
            );
            assert!(reject_pin.is_err());
            assert!(!visited);

            let read_prefix = |_: &Pack, archive: &mut dyn std::io::Read| {
                let mut prefix = [0; 11];
                archive.read_exact(&mut prefix)?;
                assert_eq!(&prefix, b"local.model");
                Ok(prefix)
            };
            let verify =
                || verify_installed_with_model(&fixture.stage, pin, || Ok(()), read_prefix);
            assert_eq!(&verify()?, b"local.model");
            for id in PACK_IDS {
                let archive = fixture.stage.join(DIRECTORY).join(format!("{id}.tpk"));
                let mut tampered = id.as_bytes().to_vec();
                *tampered.last_mut().unwrap() ^= 1;
                fs::write(&archive, tampered)?;
                let error = verify().unwrap_err();
                assert_eq!(error.kind(), io::ErrorKind::InvalidData);
                assert!(error.to_string().contains(id));
                fs::write(archive, id.as_bytes())?;
            }
            assert_eq!(&verify()?, b"local.model");
            Ok(())
        }

        #[test]
        fn combined_visitors_return_both_results_after_the_conversation_digest() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let verify = || {
                verify_installed_with_provider_and_model(
                    &fixture.stage,
                    input.receipt_sha256(),
                    || Ok(()),
                    |pack, archive| {
                        assert_eq!(pack.id(), "local.provider-registry");
                        let mut prefix = [0; 14];
                        archive.read_exact(&mut prefix)?;
                        assert_eq!(&prefix, b"local.provider");
                        Ok(prefix)
                    },
                    |pack, archive| {
                        assert_eq!(pack.id(), "local.model-registry");
                        let mut id = String::new();
                        archive.read_to_string(&mut id)?;
                        Ok(id)
                    },
                )
            };
            assert_eq!(
                verify()?,
                (*b"local.provider", "local.model-registry".to_owned())
            );
            let conversation = fixture
                .stage
                .join(DIRECTORY)
                .join("local.saved-conversation.tpk");
            fs::write(conversation, b"local.saved-conversatioN")?;
            let error = verify().unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
            assert!(error.to_string().contains("local.saved-conversation"));
            Ok(())
        }

        #[test]
        fn conversation_visitor_uses_the_pinned_archive_and_waits_for_its_complete_digest(
        ) -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let verify = || {
                verify_installed_with_conversation(
                    &fixture.stage,
                    input.receipt_sha256(),
                    || Ok(()),
                    |pack, archive| {
                        assert_eq!(pack.id(), "local.saved-conversation");
                        let mut prefix = [0; 11];
                        archive.read_exact(&mut prefix)?;
                        Ok(prefix)
                    },
                )
            };
            assert_eq!(&verify()?, b"local.saved");
            let archive = fixture
                .stage
                .join(DIRECTORY)
                .join("local.saved-conversation.tpk");
            fs::write(archive, b"local.saved-conversatioN")?;
            let error = verify().unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
            assert!(error.to_string().contains("local.saved-conversation"));
            Ok(())
        }

        #[test]
        fn seals_only_the_pinned_receipt_and_three_archives() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let staged = fixture.stage.join(DIRECTORY);
            assert_eq!(fs::read_dir(&staged)?.count(), PACK_IDS.len() + 1);
            for id in PACK_IDS {
                assert_eq!(fs::read(staged.join(format!("{id}.tpk")))?, id.as_bytes());
            }
            assert_eq!(
                fs::read(staged.join(RECEIPT))?,
                fs::read(fixture.source.join(RECEIPT))?
            );
            write_runtime_resource_manifest(&fixture.stage)?;
            let seal: Value =
                serde_json::from_slice(&fs::read(fixture.stage.join(RUNTIME_RESOURCE_MANIFEST))?)?;
            let entries = seal["entries"].as_array().unwrap();
            assert_eq!(entries.len(), PACK_IDS.len() + 1);
            for entry in entries {
                let filename = entry["path"].as_str().unwrap();
                assert!(filename.starts_with(&format!("{DIRECTORY}/")));
                let bytes = fs::read(fixture.stage.join(filename))?;
                assert_eq!(entry["size"], bytes.len());
                assert_eq!(entry["sha256"], raw_byte_digest(&bytes));
            }
            Ok(())
        }

        #[test]
        fn only_the_original_receipt_format_allows_absent_settlement() -> io::Result<()> {
            let original = Fixture::new()?;
            let original_input = original.write_receipt(&original.document)?;
            original_input.stage(&original.stage)?;
            assert_eq!(
                verify_installed_with_settlement(
                    &original.stage,
                    original_input.receipt_sha256(),
                    || Ok(()),
                    |_, _| panic!("the original receipt has no settlement archive"),
                )?,
                None::<()>,
            );
            let original_settlement = original
                .stage
                .join(DIRECTORY)
                .join("local.saved-turn-runtime.tpk");
            fs::write(original_settlement, SETTLEMENT_PACK_ID)?;
            assert!(
                verify_installed(&original.stage, original_input.receipt_sha256(), || Ok(()))
                    .is_err()
            );
            let mut invalid_upgrade = original.document.clone();
            invalid_upgrade["format"] = json!(SETTLEMENT_RECEIPT_FORMAT);
            let invalid = original.write_receipt(&invalid_upgrade)?;
            let error = invalid.stage(&original.root).unwrap_err();
            assert!(error.to_string().contains("wrong number of records"));

            let current = Fixture::with_packs(
                SETTLEMENT_RECEIPT_FORMAT,
                PACK_IDS
                    .into_iter()
                    .chain(std::iter::once(SETTLEMENT_PACK_ID)),
            )?;
            let current_input = current.write_receipt(&current.document)?;
            current_input.stage(&current.stage)?;
            let pin = current_input.receipt_sha256();
            let verify_settlement = || {
                verify_installed_with_settlement(
                    &current.stage,
                    pin,
                    || Ok(()),
                    |pack, archive| {
                        assert_eq!(pack.id(), SETTLEMENT_PACK_ID);
                        let mut prefix = [0; 11];
                        archive.read_exact(&mut prefix)?;
                        Ok(prefix)
                    },
                )
            };
            assert_eq!(verify_settlement()?, Some(*b"local.saved"));
            let settlement = current
                .stage
                .join(DIRECTORY)
                .join("local.saved-turn-runtime.tpk");
            fs::write(&settlement, b"local.saved-turn-runtimE")?;
            assert!(verify_settlement().is_err());
            assert!(verify_installed(&current.stage, pin, || Ok(())).is_err());
            fs::remove_file(&settlement)?;
            assert!(verify_settlement().is_err());
            assert!(verify_installed(&current.stage, pin, || Ok(())).is_err());
            let mut invalid_downgrade = current.document.clone();
            invalid_downgrade["format"] = json!(RECEIPT_FORMAT);
            let invalid = current.write_receipt(&invalid_downgrade)?;
            let error = invalid.stage(&current.root).unwrap_err();
            assert!(error.to_string().contains("wrong number of records"));
            Ok(())
        }

        #[test]
        fn v3_receipts_require_the_pinned_orchestration_archive() -> io::Result<()> {
            let ids = PACK_IDS
                .into_iter()
                .chain([SETTLEMENT_PACK_ID, ORCHESTRATION_PACK_ID]);
            for (format, count) in [(RECEIPT_FORMAT, 3), (SETTLEMENT_RECEIPT_FORMAT, 4)] {
                let fixture = Fixture::with_packs(format, ids.clone().take(count))?;
                let input = fixture.write_receipt(&fixture.document)?;
                input.stage(&fixture.stage)?;
                assert_eq!(
                    verify_installed_with_orchestration(
                        &fixture.stage,
                        input.receipt_sha256(),
                        || Ok(()),
                        |_, _| panic!("older receipts do not have orchestration"),
                    )?,
                    None::<()>,
                );
            }

            let current = Fixture::with_packs(ORCHESTRATION_RECEIPT_FORMAT, ids)?;
            let input = current.write_receipt(&current.document)?;
            input.stage(&current.stage)?;
            let pin = input.receipt_sha256();
            let inspect_id = |pack: &Pack, _: &mut dyn std::io::Read| Ok(pack.id().to_owned());
            let inspect_orchestration = |pack: &Pack, archive: &mut dyn std::io::Read| {
                assert_eq!(pack.id(), ORCHESTRATION_PACK_ID);
                assert_eq!(pack.version(), "0.1.0");
                let mut prefix = [0; 11];
                archive.read_exact(&mut prefix)?;
                Ok(prefix)
            };
            assert_eq!(
                verify_installed_with_orchestration_archives(
                    &current.stage,
                    pin,
                    || Ok(()),
                    inspect_id,
                    inspect_id,
                    inspect_id,
                    inspect_id,
                    inspect_orchestration,
                )?,
                (
                    PACK_IDS[0].to_owned(),
                    PACK_IDS[1].to_owned(),
                    PACK_IDS[2].to_owned(),
                    Some(SETTLEMENT_PACK_ID.to_owned()),
                    Some(*b"local.saved"),
                ),
            );
            let reacquire = || {
                verify_installed_with_orchestration(
                    &current.stage,
                    pin,
                    || Ok(()),
                    inspect_orchestration,
                )
            };
            assert_eq!(reacquire()?, Some(*b"local.saved"));

            let archive = current
                .stage
                .join(DIRECTORY)
                .join(format!("{ORCHESTRATION_PACK_ID}.tpk"));
            fs::write(&archive, b"local.saved-orchestratioN")?;
            assert!(reacquire().is_err());
            assert!(verify_installed(&current.stage, pin, || Ok(())).is_err());
            fs::remove_file(&archive)?;
            assert!(reacquire().is_err());

            let mut invalid = current.document.clone();
            invalid["packs"][4]["version"] = json!("0.1.1");
            let error = current
                .write_receipt(&invalid)?
                .stage(&current.root)
                .unwrap_err();
            assert!(error.to_string().contains(ORCHESTRATION_PACK_ID));
            Ok(())
        }

        #[test]
        fn v4_receipts_require_the_pinned_provider_adapter_archive() -> io::Result<()> {
            let ids = PACK_IDS.into_iter().chain([
                SETTLEMENT_PACK_ID,
                ORCHESTRATION_PACK_ID,
                PROVIDER_ADAPTER_PACK_ID,
            ]);
            for (format, count) in [
                (RECEIPT_FORMAT, 3),
                (SETTLEMENT_RECEIPT_FORMAT, 4),
                (ORCHESTRATION_RECEIPT_FORMAT, 5),
            ] {
                let fixture = Fixture::with_packs(format, ids.clone().take(count))?;
                let input = fixture.write_receipt(&fixture.document)?;
                input.stage(&fixture.stage)?;
                assert_eq!(
                    verify_installed_with_provider_adapter(
                        &fixture.stage,
                        input.receipt_sha256(),
                        || Ok(()),
                        |_, _| panic!("older receipts do not have the provider adapter"),
                    )?,
                    None::<()>,
                );
            }

            for (approved, unapproved) in
                [("0.1.0", "0.1.1"), ("0.2.0", "0.2.1"), ("0.3.0", "0.3.1")]
            {
                let fixture = Fixture::with_packs(PROVIDER_ADAPTER_RECEIPT_FORMAT, ids.clone())?;
                let mut receipt = fixture.document.clone();
                receipt["packs"][5]["version"] = json!(approved);
                let input = fixture.write_receipt(&receipt)?;
                input.stage(&fixture.stage)?;
                let pin = input.receipt_sha256();
                let inspect = || {
                    verify_installed_with_provider_adapter(
                        &fixture.stage,
                        pin,
                        || Ok(()),
                        |pack, archive| {
                            assert_eq!(pack.id(), PROVIDER_ADAPTER_PACK_ID);
                            assert_eq!(pack.version(), approved);
                            let mut prefix = [0; 6];
                            archive.read_exact(&mut prefix)?;
                            Ok(prefix)
                        },
                    )
                };
                assert_eq!(inspect()?, Some(*b"local."));

                let archive = fixture
                    .stage
                    .join(DIRECTORY)
                    .join(format!("{PROVIDER_ADAPTER_PACK_ID}.tpk"));
                fs::write(&archive, b"local.provider-adapteR")?;
                assert!(inspect().is_err());
                assert!(verify_installed(&fixture.stage, pin, || Ok(())).is_err());
                fs::remove_file(&archive)?;
                assert!(inspect().is_err());

                receipt["packs"][5]["version"] = json!(unapproved);
                let error = fixture
                    .write_receipt(&receipt)?
                    .stage(&fixture.root)
                    .unwrap_err();
                assert!(error.to_string().contains(PROVIDER_ADAPTER_PACK_ID));
            }
            Ok(())
        }

        #[test]
        fn v5_receipts_require_the_pinned_ai_routing_archive() -> io::Result<()> {
            let ids = PACK_IDS.into_iter().chain([
                SETTLEMENT_PACK_ID,
                ORCHESTRATION_PACK_ID,
                PROVIDER_ADAPTER_PACK_ID,
                AI_ROUTING_PACK_ID,
            ]);
            for (format, count) in [
                (RECEIPT_FORMAT, 3),
                (SETTLEMENT_RECEIPT_FORMAT, 4),
                (ORCHESTRATION_RECEIPT_FORMAT, 5),
                (PROVIDER_ADAPTER_RECEIPT_FORMAT, 6),
            ] {
                let fixture = Fixture::with_packs(format, ids.clone().take(count))?;
                let input = fixture.write_receipt(&fixture.document)?;
                input.stage(&fixture.stage)?;
                assert_eq!(
                    verify_installed_with_ai_routing(
                        &fixture.stage,
                        input.receipt_sha256(),
                        || Ok(()),
                        |_, _| panic!("the receipt has no AI Routing archive"),
                    )?,
                    None::<()>,
                );
            }

            for version in ["0.1.0", "0.2.0", "0.3.0"] {
                let legacy = Fixture::with_packs(AI_ROUTING_RECEIPT_FORMAT, ids.clone())?;
                let mut receipt = legacy.document.clone();
                receipt["packs"][6]["version"] = json!(version);
                let input = legacy.write_receipt(&receipt)?;
                input.stage(&legacy.stage)?;
                assert_eq!(
                    verify_installed_with_ai_routing(
                        &legacy.stage,
                        input.receipt_sha256(),
                        || Ok(()),
                        |pack, _| Ok(pack.version().to_owned()),
                    )?
                    .as_deref(),
                    Some(version),
                );
            }
            let fixture = Fixture::with_packs(AI_ROUTING_RECEIPT_FORMAT, ids)?;
            let mut receipt = fixture.document.clone();
            receipt["packs"][5]["version"] = json!("0.3.0");
            receipt["packs"][6]["version"] = json!("0.4.0");
            let input = fixture.write_receipt(&receipt)?;
            input.stage(&fixture.stage)?;
            let pin = input.receipt_sha256();
            let inspect = || {
                verify_installed_with_ai_routing(
                    &fixture.stage,
                    pin,
                    || Ok(()),
                    |pack, archive| {
                        assert_eq!(pack.id(), AI_ROUTING_PACK_ID);
                        assert_eq!(pack.version(), "0.4.0");
                        let mut prefix = [0; 6];
                        archive.read_exact(&mut prefix)?;
                        Ok(prefix)
                    },
                )
            };
            assert_eq!(inspect()?, Some(*b"local."));

            let archive = fixture
                .stage
                .join(DIRECTORY)
                .join(format!("{AI_ROUTING_PACK_ID}.tpk"));
            fs::write(&archive, b"local.ai-routinG")?;
            assert!(inspect().is_err());
            assert!(verify_installed(&fixture.stage, pin, || Ok(())).is_err());
            fs::remove_file(&archive)?;
            assert!(inspect().is_err());

            let mut downgraded = receipt.clone();
            downgraded["format"] = json!(PROVIDER_ADAPTER_RECEIPT_FORMAT);
            assert!(fixture
                .write_receipt(&downgraded)?
                .stage(&fixture.root)
                .is_err());
            let mut reordered = receipt.clone();
            reordered["packs"].as_array_mut().unwrap().swap(5, 6);
            assert!(fixture
                .write_receipt(&reordered)?
                .stage(&fixture.root)
                .is_err());
            receipt["packs"][6]["version"] = json!("0.4.1");
            let error = fixture
                .write_receipt(&receipt)?
                .stage(&fixture.root)
                .unwrap_err();
            assert!(error.to_string().contains(AI_ROUTING_PACK_ID));
            Ok(())
        }

        #[test]
        fn verifies_the_installed_receipt_and_exact_inventory() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            assert!(verify_installed(&fixture.stage, input.receipt_sha256(), || Ok(())).is_err());
            input.stage(&fixture.stage)?;
            let verify = || verify_installed(&fixture.stage, input.receipt_sha256(), || Ok(()));
            verify()?;
            assert!(verify_installed(&fixture.stage, &"2".repeat(64), || Ok(())).is_err());

            let directory = fixture.stage.join(DIRECTORY);
            let receipt_path = directory.join(RECEIPT);
            let original_receipt = fs::read(&receipt_path)?;
            let mut oversized_receipt = original_receipt.clone();
            oversized_receipt.extend_from_slice(&[b' '; 16 * 1024]);
            let oversized_pin = raw_byte_digest(&oversized_receipt);
            fs::write(&receipt_path, &oversized_receipt)?;
            assert!(verify_installed(&fixture.stage, &oversized_pin, || Ok(())).is_err());
            fs::write(&receipt_path, original_receipt)?;

            let archive = directory.join(format!("{}.tpk", PACK_IDS[0]));
            fs::write(&archive, vec![0; PACK_IDS[0].len()])?;
            assert!(verify().is_err());
            fs::write(&archive, PACK_IDS[0].as_bytes())?;
            fs::write(directory.join("unexpected"), b"extra")?;
            assert!(verify().is_err());
            fs::remove_file(directory.join("unexpected"))?;
            fs::remove_file(&archive)?;
            assert!(verify().is_err());
            fs::write(&archive, PACK_IDS[0].as_bytes())?;
            verify()?;
            Ok(())
        }

        #[test]
        fn rejects_installed_symlinks_and_hard_links() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let input = fixture.write_receipt(&fixture.document)?;
            input.stage(&fixture.stage)?;
            let pin = input.receipt_sha256();

            let alias = fixture.root.join("alias");
            symlink(&fixture.root, &alias)?;
            assert!(verify_installed(&alias.join("stage"), pin, || Ok(())).is_err());
            fs::remove_file(&alias)?;
            symlink(&fixture.stage, &alias)?;
            assert!(verify_installed(&alias, pin, || Ok(())).is_err());

            let directory = fixture.stage.join(DIRECTORY);
            let outside = fixture.root.join("outside");
            fs::rename(&directory, &outside)?;
            symlink(&outside, &directory)?;
            assert!(verify_installed(&fixture.stage, pin, || Ok(())).is_err());
            fs::remove_file(&directory)?;
            fs::rename(&outside, &directory)?;

            for filename in [RECEIPT.to_owned(), format!("{}.tpk", PACK_IDS[0])] {
                let current = directory.join(filename);
                fs::rename(&current, &outside)?;
                symlink(&outside, &current)?;
                assert!(verify_installed(&fixture.stage, pin, || Ok(())).is_err());
                fs::remove_file(&current)?;
                fs::hard_link(&outside, &current)?;
                assert!(verify_installed(&fixture.stage, pin, || Ok(())).is_err());
                fs::remove_file(&outside)?;
            }
            verify_installed(&fixture.stage, pin, || Ok(()))
        }

        #[test]
        fn checkpoints_before_access_and_between_archive_chunks() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let interrupted = || io::Error::new(io::ErrorKind::Interrupted, "Launch cancelled");
            let error = verify_installed(&fixture.stage, "", || Err(interrupted())).unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::Interrupted);

            let archive = vec![7; 3 * 64 * 1024];
            let filename = format!("{}.tpk", PACK_IDS[0]);
            fs::write(fixture.source.join(&filename), &archive)?;
            let mut receipt = fixture.document.clone();
            receipt["packs"][0]["archive_bytes"] = json!(archive.len());
            receipt["packs"][0]["archive_sha256"] =
                json!(format!("sha256:{}", raw_byte_digest(&archive)));
            let input = fixture.write_receipt(&receipt)?;
            input.stage(&fixture.stage)?;
            let pin = input.receipt_sha256();

            // Cancel on the checkpoint after hashing the first chunk. The invalid tail
            // would otherwise make the archive fail its digest check.
            let mut invalid_archive = archive;
            *invalid_archive.last_mut().unwrap() ^= 1;
            fs::write(
                fixture.stage.join(DIRECTORY).join(filename),
                &invalid_archive,
            )?;
            let mut checkpoints = 0;
            let error = verify_installed(&fixture.stage, pin, || {
                checkpoints += 1;
                if checkpoints == 5 {
                    return Err(interrupted());
                }
                Ok(())
            })
            .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::Interrupted);
            assert!(verify_installed(&fixture.stage, pin, || Ok(())).is_err());
            Ok(())
        }

        #[test]
        fn rejects_receipt_archive_and_filesystem_substitutions() -> io::Result<()> {
            let fixture = Fixture::new()?;
            let document = &fixture.document;
            let input = fixture.write_receipt(document)?;
            let receipt_path = fixture.source.join(RECEIPT);
            let raw = fs::read(&receipt_path)?;
            fs::write(&receipt_path, [&raw[..], b"\n"].concat())?;
            assert!(input.stage(&fixture.stage).is_err());
            for (key, value) in [
                ("archive", json!("../local.provider-registry.tpk")),
                ("archive_bytes", json!(ARCHIVE_LIMIT + 1)),
                ("manifest_sha256", json!("f".repeat(64))),
                ("version", json!("01.0.0")),
            ] {
                let mut malformed = document.clone();
                malformed["packs"][0][key] = value;
                assert!(fixture
                    .write_receipt(&malformed)?
                    .stage(&fixture.stage)
                    .is_err());
            }
            let mut reordered = document.clone();
            reordered["packs"].as_array_mut().unwrap().swap(0, 1);
            assert!(fixture
                .write_receipt(&reordered)?
                .stage(&fixture.stage)
                .is_err());
            fixture.write_receipt(document)?;

            let archive = fixture.source.join(format!("{}.tpk", PACK_IDS[1]));
            let original = fs::read(&archive)?;
            fs::write(&archive, vec![0; original.len()])?;
            assert!(input.stage(&fixture.stage).is_err());
            assert!(!fixture.stage.join(DIRECTORY).exists());
            fs::remove_file(&archive)?;
            let external = fixture.root.join("original.tpk");
            fs::write(&external, &original)?;
            symlink(&external, &archive)?;
            assert!(input.stage(&fixture.stage).is_err());
            fs::remove_file(&archive)?;
            fs::hard_link(&external, &archive)?;
            assert!(input.stage(&fixture.stage).is_err());
            fs::remove_file(&archive)?;
            fs::rename(&external, &archive)?;

            let extra = fixture.source.join("extra");
            fs::write(&extra, b"unexpected")?;
            assert!(input.stage(&fixture.stage).is_err());
            fs::remove_file(extra)?;
            let linked = fixture.root.join("source-link");
            symlink(&fixture.source, &linked)?;
            let linked_input = Input {
                source: linked,
                receipt_sha256: input.receipt_sha256.clone(),
            };
            assert!(linked_input.stage(&fixture.stage).is_err());
            input.stage(&fixture.stage)?;
            Ok(())
        }
    }
}
