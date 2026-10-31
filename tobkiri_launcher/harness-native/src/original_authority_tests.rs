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

// Native gaps are not original exceptions. Keep every case observable, then
// let the unchanged strict comparison fail on the different outcome kind.
fn native_issue(
    value: &OriginalAuthorityError,
    depth: usize,
) -> Option<(OriginalAuthorityStage, &'static str)> {
    fn cause(value: &OriginalAuthorityCause, depth: usize) -> Option<&'static str> {
        if depth > 32 {
            return Some("native-error-record-depth");
        }
        match value {
            OriginalAuthorityCause::Unsupported(reason) => Some(reason),
            OriginalAuthorityCause::Capacity => Some("native-capacity"),
            OriginalAuthorityCause::Cancelled => Some("native-cancelled"),
            OriginalAuthorityCause::Deadline => Some("native-deadline"),
            OriginalAuthorityCause::SourceUnavailable => Some("native-source-unavailable"),
            OriginalAuthorityCause::InvalidRecord(_) => Some("native-record-representation"),
            OriginalAuthorityCause::Python {
                cause: source,
                context,
                ..
            } => source
                .as_ref()
                .and_then(|value| cause(value, depth + 1))
                .or_else(|| context.as_ref().and_then(|value| cause(value, depth + 1))),
            OriginalAuthorityCause::Io(value) => value
                .context
                .as_ref()
                .and_then(|value| cause(value, depth + 1)),
            OriginalAuthorityCause::SecurePath { context, .. } => {
                context.as_ref().and_then(|value| cause(value, depth + 1))
            }
            _ => None,
        }
    }
    if depth > 32 {
        return Some((value.stage, "native-error-record-depth"));
    }
    value
        .cause
        .as_ref()
        .and_then(|value| cause(value, depth + 1))
        .map(|reason| (value.stage, reason))
        .or_else(|| {
            value
                .context
                .as_ref()
                .and_then(|value| native_issue(value, depth + 1))
        })
}
fn stage_name(value: OriginalAuthorityStage) -> &'static str {
    match value {
        OriginalAuthorityStage::Source => "source",
        OriginalAuthorityStage::Process => "process",
        OriginalAuthorityStage::Path => "path",
        OriginalAuthorityStage::Key => "key",
        OriginalAuthorityStage::Guard => "guard",
        OriginalAuthorityStage::Connect => "connect",
        OriginalAuthorityStage::Initialize => "initialize",
        OriginalAuthorityStage::Audit => "audit",
        OriginalAuthorityStage::Migration => "migration",
        OriginalAuthorityStage::Epoch => "epoch",
        OriginalAuthorityStage::EpochRecord => "epoch-record",
        OriginalAuthorityStage::Close => "close",
    }
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
            Err(error) => match native_issue(error, 0) {
                Some((stage, reason)) => {
                    json!({"outcome":"native-integration-failure", "stage":stage_name(stage), "reason":reason})
                }
                None => json!({"outcome":"error", "error":graph(error, &mut 0)}),
            },
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
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
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
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn decoded_data_preserves_source_order_depth_and_releases_actual_capacity() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let selected = root.join("selected");
    std::fs::create_dir(&selected)?;
    let source = Source {
        root: selected.clone(),
        _selected: File::open(&selected)?,
        live: Arc::new(AtomicBool::new(true)),
    };
    let owner = RuntimeOwner::open(root.join("core"))?;
    let invocation = owner.begin_native_setup()?;
    let cancellation = CoreCancellation::new_with_control(invocation.control())?;
    let cleanup = Arc::new(io::CleanupState::new(
        invocation
            .control()
            .reserve(tobkiri_gate::ControlKind::Context, 4096)?,
    ));
    let op = Operation {
        owner: &source,
        invocation: &invocation,
        cancellation: &cancellation,
        deadline: invocation.deadline()?,
        pid: std::process::id(),
        cleanup,
        fixture: None,
    };
    // Production keeps OriginalAuthorityRead's diagnostic scratch through the
    // read-to-detach interval. This private decoder test prepays its own finite
    // diagnostic storage until the actual errors below have been dropped.
    let diagnostics = op.reserve(OriginalAuthorityStage::Audit, 8192)?;
    let empty = super::json::decode(b"", &op)
        .err()
        .expect("actual decode failure");
    match empty.cause.as_ref().expect("decode cause") {
        OriginalAuthorityCause::Python {
            class,
            context,
            suppressed,
            ..
        } => {
            assert_eq!(*class, "json.decoder.JSONDecodeError");
            assert!(*suppressed);
            assert!(
                matches!(context.as_deref(),Some(OriginalAuthorityCause::Python {class:"builtins.StopIteration",message,..}) if message=="0")
            );
        }
        _ => panic!("unexpected decode cause"),
    }
    drop(empty);
    let decoded = super::json::decode(br#"{"z":"\ud800","a":1,"z":"ok"}"#, &op)?;
    let names: Vec<_> = decoded
        .entries(decoded.root)
        .unwrap()
        .iter()
        .map(|(name, _)| name.utf8().unwrap())
        .collect();
    assert_eq!(names, ["z", "a"]);
    assert_eq!(
        &**super::json::canonical(&decoded, None, &op)?,
        br#"{"a":1,"z":"ok"}"#
    );
    drop(decoded);
    let decoded = super::json::decode(br#"{"a":"\ud800","b":NaN}"#, &op)?;
    let failure = super::json::canonical(&decoded, None, &op)
        .err()
        .expect("nonfinite failure precedes UTF8 encode");
    assert_eq!(failure.class, "builtins.ValueError");
    assert_eq!(
        failure.message,
        "Out of range float values are not JSON compliant: nan"
    );
    drop(failure);
    drop(decoded);
    let deep = format!("{{\"nested\":{}0{}}}", "[".repeat(2000), "]".repeat(2000));
    let decoded = super::json::decode(deep.as_bytes(), &op)?;
    let encoded = super::json::canonical(&decoded, None, &op)?;
    assert_eq!(&**encoded, deep.as_bytes());
    assert!(invocation.capacity_snapshot()?.charged_bytes > 0);
    drop(encoded);
    drop(decoded);
    assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 8192);

    let digest = "a".repeat(64);
    let principal = json!({"parent_artifact_digest":digest,"function_implementation_digest":digest,
        "function_id":"function","contract_revision_digest":digest,"operation_id":"operation"});
    let mut model = json!({"lease_id":"lease","request_id":"request","caller":principal,"target":principal,
        "caller_domain_id":true,"caller_boot_epoch":1,"target_domain_id":"target","target_boot_epoch":1,
        "request_digest":digest,"effect_digest":digest,"authorized_scope":{"capability":true,"semantics_digest":digest},
        "resource_namespace":"resource","profile_id":"profile","activation_id":"activation","activation_digest":digest,
        "plan_digest":digest,"profile_authority_digest":digest,"fencing_token":1,"caller_publisher_lineage":"caller",
        "target_publisher_lineage":"target","host_extension_id":"extension","provider_authority_id":"provider",
        "provider_authority_digest":digest,"grant_id":"grant","audit_reservation_id":"audit","security_epoch":1,
        "issued_at":9007199254740992i64,"expires_at":9007199254740993i64});
    let input = serde_json::to_vec(&model)?;
    let decoded = super::json::decode(&input, &op)?;
    let normalized = super::lease::normalize(&decoded, &op)?;
    assert_eq!(
        normalized.get("caller_domain_id"),
        Some(&CanonValue::Bool(true))
    );
    assert_eq!(
        normalized
            .get("authorized_scope")
            .unwrap()
            .get("capability")
            .and_then(CanonValue::as_str),
        Some("True")
    );
    drop(normalized);
    drop(decoded);
    model["call_chain"] = json!([true, 1, []]);
    let input = serde_json::to_vec(&model)?;
    let decoded = super::json::decode(&input, &op)?;
    let failure = super::lease::normalize(&decoded, &op)
        .err()
        .expect("set reaches later unhashable item");
    assert_eq!(failure.class, "builtins.TypeError");
    assert_eq!(failure.message, "unhashable type: 'list'");
    drop(failure);
    drop(decoded);
    model["call_chain"] = json!([true, 1.0]);
    let input = serde_json::to_vec(&model)?;
    let decoded = super::json::decode(&input, &op)?;
    assert_eq!(
        super::lease::normalize(&decoded, &op)
            .err()
            .expect("Python numeric duplicate")
            .message,
        "lease call chain is cyclic or too deep"
    );
    drop(decoded);
    model["call_chain"] = json!([[], 0, 1, 2, 3]);
    let input = serde_json::to_vec(&model)?;
    let decoded = super::json::decode(&input, &op)?;
    assert_eq!(
        super::lease::normalize(&decoded, &op)
            .err()
            .expect("length short-circuits set")
            .message,
        "lease call chain is cyclic or too deep"
    );
    drop(decoded);
    assert!(!selected.join("authority").exists());
    drop(diagnostics);
    assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
    assert_eq!(invocation.capacity_snapshot()?.reservations, 0);
    Ok(())
}

#[test]
#[cfg(target_os = "linux")]
fn unsupported_owner_refuses_before_authority_admission() -> anyhow::Result<()> {
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let user_data = root.join("selected");
    std::fs::create_dir(&user_data)?;
    let error =
        match RuntimeOwner::open(root.join("core")).and_then(|owner| owner.begin_native_setup()) {
            Ok(_) => anyhow::bail!("unsupported platform admitted native authority funding"),
            Err(error) => error,
        };
    match error {
        tobkiri_core::CoreError::Store(source) => assert_eq!(
            source.to_string(),
            "storage primitive unavailable: allocation owner OS/filesystem proof unavailable"
        ),
        other => anyhow::bail!("owner rejected for an unrelated reason: {other:?}"),
    }
    assert!(!user_data.join("authority").exists());
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
