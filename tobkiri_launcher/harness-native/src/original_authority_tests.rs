//! Remote-CI fixtures run the SAME finite operation; no replacement store.
use super::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::File,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_core::RuntimeOwner;

pub(super) struct Fixture {
    pub clock: f64,
    pub mutation: BTreeMap<String, Option<String>>,
}
impl Fixture {
    pub fn after_epoch(&self, path: &Path) -> Result<()> {
        if self.mutation.is_empty() {
            return Ok(());
        }
        let mut connection = rusqlite::Connection::open(path).map_err(|e| {
            OriginalAuthorityError::sql(
                OriginalAuthorityStage::EpochRecord,
                "fixture mutation failed",
                e,
            )
        })?;
        let transaction = connection.transaction().map_err(|e| {
            OriginalAuthorityError::sql(
                OriginalAuthorityStage::EpochRecord,
                "fixture mutation failed",
                e,
            )
        })?;
        for (key, value) in &self.mutation {
            assert!(
                [
                    "security_epoch",
                    "security_epoch_advanced_at",
                    "security_epoch_reason_digest"
                ]
                .contains(&key.as_str())
            );
            match value {
                Some(value) => {
                    transaction
                        .execute(
                            "UPDATE authority_meta SET value=? WHERE key=?",
                            rusqlite::params![value, key],
                        )
                        .map_err(|e| {
                            OriginalAuthorityError::sql(
                                OriginalAuthorityStage::EpochRecord,
                                "fixture mutation failed",
                                e,
                            )
                        })?;
                }
                None => {
                    transaction
                        .execute(
                            "DELETE FROM authority_meta WHERE key=?",
                            rusqlite::params![key],
                        )
                        .map_err(|e| {
                            OriginalAuthorityError::sql(
                                OriginalAuthorityStage::EpochRecord,
                                "fixture mutation failed",
                                e,
                            )
                        })?;
                }
            }
        }
        transaction.commit().map_err(|e| {
            OriginalAuthorityError::sql(
                OriginalAuthorityStage::EpochRecord,
                "fixture mutation failed",
                e,
            )
        })?;
        connection.close().map_err(|(_, e)| {
            OriginalAuthorityError::sql(OriginalAuthorityStage::Close, "fixture close failed", e)
        })
    }
}

struct Source {
    root: PathBuf,
    live: Arc<AtomicBool>,
    _selected: File,
}
impl OriginalAuthorityOwner for Source {
    fn with_original_user_data<R>(&self, operation: impl FnOnce(&Path) -> R) -> Result<R> {
        self.check_original_owner()?;
        Ok(operation(&self.root))
    }
    fn check_original_owner(&self) -> Result<()> {
        if !self.live.load(Ordering::Acquire) {
            return Err(OriginalAuthorityError::unavailable());
        }
        Ok(())
    }
}

fn material(value: &OriginalAuthorityMaterial) -> Value {
    match value {
        OriginalAuthorityMaterial::Genesis { epoch, .. } => {
            json!({"outcome":"genesis", "security_epoch":epoch.decimal(), "epoch_record":null})
        }
        OriginalAuthorityMaterial::Existing {
            epoch,
            record_epoch,
            advanced_at,
            reason_digest,
        } => json!({
            "outcome":"material", "security_epoch":epoch.decimal(), "epoch_record":{
                "value":record_epoch.decimal(), "advanced_at":advanced_at, "reason_digest":reason_digest}}),
    }
}

fn class_for_sqlite(code: i32) -> &'static str {
    match code & 255 {
        2 | 12 => "sqlite3.InternalError",
        7 => "builtins.MemoryError",
        18 => "sqlite3.DataError",
        19 | 20 => "sqlite3.IntegrityError",
        21 | 25 => "sqlite3.InterfaceError",
        1 | 3 | 4 | 5 | 6 | 8 | 9 | 10 | 13 | 14 | 15 | 16 | 17 => "sqlite3.OperationalError",
        _ => "sqlite3.DatabaseError",
    }
}
fn cause(value: &OriginalAuthorityCause, next: &mut usize) -> Value {
    let id = *next;
    *next += 1;
    let (class, message, suppressed, source, context) = match value {
        OriginalAuthorityCause::Python {
            class,
            message,
            cause: original,
            context,
            suppressed,
        } => {
            let source = original.as_ref().map(|source| cause(source, next));
            let context = context
                .as_ref()
                .map(|context| cause(context, next))
                .or_else(|| source.as_ref().map(|value| json!({"ref":value["id"]})));
            (*class, message.clone(), *suppressed, source, context)
        }
        OriginalAuthorityCause::Validation(message) => (
            "core_runtime.authority.v4_models.AuthorityValidationError",
            (*message).into(),
            false,
            None,
            None,
        ),
        OriginalAuthorityCause::SecurePath { message, context } => (
            "core_runtime.secure_sqlite_path.SecurePathError",
            (*message).into(),
            false,
            None,
            context.as_ref().map(|value| cause(value, next)),
        ),
        OriginalAuthorityCause::Sqlite { code, message } => {
            (class_for_sqlite(*code), message.clone(), false, None, None)
        }
        OriginalAuthorityCause::InvalidKey => (
            "builtins.ValueError",
            "Fernet key must be 32 url-safe base64-encoded bytes.".into(),
            false,
            None,
            None,
        ),
        OriginalAuthorityCause::InvalidToken => (
            "cryptography.fernet.InvalidToken",
            String::new(),
            false,
            None,
            None,
        ),
        OriginalAuthorityCause::Io(error) => {
            let class = match error.kind {
                std::io::ErrorKind::NotFound => "builtins.FileNotFoundError",
                std::io::ErrorKind::PermissionDenied => "builtins.PermissionError",
                std::io::ErrorKind::AlreadyExists => "builtins.FileExistsError",
                std::io::ErrorKind::NotADirectory => "builtins.NotADirectoryError",
                std::io::ErrorKind::IsADirectory => "builtins.IsADirectoryError",
                _ => "builtins.OSError",
            };
            let message = error
                .errno
                .map(|errno| {
                    // Error text from the actual C errno table, not Debug output.
                    #[cfg(unix)]
                    let text = unsafe { std::ffi::CStr::from_ptr(libc::strerror(errno)) }
                        .to_str()
                        .unwrap()
                        .to_owned();
                    #[cfg(not(unix))]
                    let text = "unsupported fixture OS".to_owned();
                    match &error.filename {
                        Some(filename) => {
                            format!("[Errno {errno}] {text}: {}", python_repr(filename))
                        }
                        None => format!("[Errno {errno}] {text}"),
                    }
                })
                .unwrap_or_default();
            let context = error.context.as_ref().map(|value| cause(value, next));
            return json!({"id":id,"type":class,"message":message,"suppressed":false,
                "cause":null,"context":context,"errno":error.errno,"filename":error.filename});
        }
        other => panic!("unqualified native exception representation: {other:?}"),
    };
    json!({"id":id,"type":class,"message":message,"suppressed":suppressed,"cause":source,"context":context})
}
fn graph(value: &OriginalAuthorityError, next: &mut usize) -> Value {
    let id = *next;
    *next += 1;
    let source = value.cause.as_ref().map(|value| cause(value, next));
    let context = value
        .context
        .as_ref()
        .map(|value| graph(value, next))
        .or_else(|| source.as_ref().map(|value| json!({"ref":value["id"]})));
    json!({"id":id,"type":value.class,"message":value.message,"suppressed":value.suppressed,
        "cause":source,"context":context})
}

/// CI sets the exact fresh manifest prepared by the independent pinned source.
/// Without it, this test cannot report qualification or a native comparison.
#[test]
#[ignore = "requires fresh independent original Authority fixtures"]
fn original_reference_driver() -> anyhow::Result<()> {
    let manifest = std::env::var_os("TOBKIRI_ORIGINAL_AUTHORITY_FIXTURES")
        .ok_or_else(|| anyhow::anyhow!("fresh independent original Authority fixtures required"))?;
    let manifest = PathBuf::from(manifest);
    let bytes = std::fs::read(&manifest)?;
    anyhow::ensure!(bytes.len() <= 2 * 1024 * 1024);
    let fixtures: Value = serde_json::from_slice(&bytes)?;
    anyhow::ensure!(fixtures["format"] == "io.tobkiri.original-authority-fixtures.v1");
    anyhow::ensure!(fixtures["python"] == "3.13.16");
    let cases = fixtures["cases"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("fixture cases missing"))?;
    anyhow::ensure!(cases.len() >= 32);
    let storage = tempfile::tempdir()?;
    let storage_root = storage.path().canonicalize()?;
    let mut observations = Vec::new();
    let mut all_match = true;
    for (index, case) in cases.iter().enumerate() {
        let root = PathBuf::from(
            case["user_data_dir"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("fixture source missing"))?,
        );
        let source = Source {
            _selected: File::open(&root)?,
            root,
            live: Arc::new(AtomicBool::new(true)),
        };
        let owner = RuntimeOwner::open(storage_root.join(format!("owner-{index}")))?;
        let invocation = owner.begin_native_setup()?;
        let cancellation = CoreCancellation::new_with_control(invocation.control())?;
        let fixture = Fixture {
            clock: case["clock"]
                .as_f64()
                .ok_or_else(|| anyhow::anyhow!("fixture clock missing"))?,
            mutation: case["after_epoch_mutation"]
                .as_object()
                .map(|items| {
                    items
                        .iter()
                        .map(|(key, value)| (key.clone(), value.as_str().map(str::to_owned)))
                        .collect()
                })
                .unwrap_or_default(),
        };
        let mut read = OriginalAuthorityRead::new(&invocation)?;
        read.run_inner(&source, &cancellation, Some(&fixture))?;
        let actual = read.result().expect("actual original operation result");
        let observed = match actual {
            Ok(value) => material(value),
            Err(error)
                if error.class
                    == "core_runtime.bootstrap.profile_capture.ProfileResolutionDenied" =>
            {
                json!({"outcome":"non-regular-path"})
            }
            Err(error) => json!({"outcome":"error", "error":graph(error, &mut 0)}),
        };
        let mut expected = case["expected"].clone();
        let expected_object = expected.as_object_mut().unwrap();
        // This operation returns materials. The original later protocol digest
        // gate remains the Python caller's separate checked responsibility.
        expected_object.remove("protocol_snapshot");
        expected_object.remove("protocol_snapshot_error");
        let mut flat = observed.clone();
        flat.as_object_mut()
            .unwrap()
            .insert("name".into(), case["name"].clone());
        observations.push(flat);
        all_match &= observed == expected;
        // Persist all reached observations before asserting, for concrete CI
        // differences. Never include key/database/encrypted/raw record bytes.
        let output = serde_json::to_vec_pretty(
            &json!({"format":"io.tobkiri.original-authority-native-observations.v1",
            "native_comparison":true,"qualification":false,"production_enabled":false,"cases":observations}),
        )?;
        anyhow::ensure!(output.len() <= 2 * 1024 * 1024);
        std::fs::write(manifest.with_file_name("native-observations.json"), output)?;
        assert!(
            read.cleanup_verified(),
            "actual IO cleanup was not verified"
        );
        drop(read);
        let snapshot = invocation.capacity_snapshot()?;
        assert_eq!(
            snapshot.charged_bytes, 0,
            "held native DATA after operation"
        );
        assert_eq!(
            snapshot.reservations, 0,
            "held native reservation after operation"
        );
        drop(source);
        drop(cancellation);
        drop(invocation);
        drop(owner);
        // A real exclusive root reopen verifies release of the actual owner.
        drop(RuntimeOwner::open(
            storage_root.join(format!("owner-{index}")),
        )?);
    }
    assert!(
        all_match,
        "actual original/native mismatch; inspect native-observations.json"
    );
    Ok(())
}

#[test]
fn cancelled_absent_operation_does_not_create_authority_state() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let user_data = root.join("selected");
    std::fs::create_dir(&user_data)?;
    let source = Source {
        _selected: File::open(&user_data)?,
        root: user_data.clone(),
        live: Arc::new(AtomicBool::new(true)),
    };
    let owner = RuntimeOwner::open(root.join("core"))?;
    let invocation = owner.begin_native_setup()?;
    let cancellation = CoreCancellation::new_with_control(invocation.control())?;
    cancellation.cancel();
    let read = read_original_authority(&source, &invocation, &cancellation)?;
    let failure = read
        .result()
        .expect("cancelled operation result")
        .unwrap_err();
    assert!(matches!(
        failure.cause,
        Some(OriginalAuthorityCause::Cancelled)
    ));
    assert!(read.cleanup_verified());
    drop(read);
    assert!(!user_data.join("authority").exists());
    assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
    Ok(())
}

#[test]
fn original_integer_lexical_range_is_distinct_from_protocol_snapshot_range() {
    for (text, expected) in [
        (" +0001 ", "1"),
        ("1_0", "10"),
        ("\u{0661}\u{0662}", "12"),
        ("\u{2003}1\u{2003}", "1"),
        ("18446744073709551616", "18446744073709551616"),
    ] {
        assert_eq!(
            integer(text, OriginalAuthorityStage::Epoch)
                .unwrap()
                .decimal(),
            expected
        );
    }
    assert_eq!(
        integer(&"9".repeat(4301), OriginalAuthorityStage::Epoch)
            .unwrap_err()
            .class,
        "builtins.ValueError"
    );
}
