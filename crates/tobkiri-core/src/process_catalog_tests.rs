//! Real private descriptors only; fixtures are never executed.
//!
//! There is no safe metadata/entropy-failure injection seam in this slice.
//! We do not manufacture invalid or externally closed owned descriptors.
//! Control usage after owner drop observes permit lifetime, not an independent
//! kernel-FD-close or physical-process-completion receipt.

use super::*;
use std::{
    cell::Cell,
    fs::OpenOptions,
    io::{Seek, SeekFrom},
    os::unix::fs::{FileExt, MetadataExt, PermissionsExt},
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
use tobkiri_gate::CoreControlLimits;

const DATA: &[u8] = b"data";
const WORK: u64 = 5; // Four content bytes plus the admitted EOF-detection byte.

struct Fixture {
    // Destroy the catalog before removing the paths retained by its Files.
    catalog: ProcessCatalog,
    executable: PathBuf,
    cwd: PathBuf,
    temp: tempfile::TempDir,
}
impl Fixture {
    fn new() -> Self {
        Self::with_control(CoreControl::default())
    }
    fn with_control(control: CoreControl) -> Self {
        let temp = tempfile::tempdir().unwrap();
        let executable = temp.path().join("executable");
        std::fs::write(&executable, DATA).unwrap();
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o755)).unwrap();
        let cwd = temp.path().join("cwd");
        std::fs::create_dir(&cwd).unwrap();
        std::fs::set_permissions(&cwd, std::fs::Permissions::from_mode(0o755)).unwrap();
        Self {
            catalog: ProcessCatalog::new(control).unwrap(),
            executable,
            cwd,
            temp,
        }
    }
    fn exe(&self) -> File {
        File::open(&self.executable).unwrap()
    }
    fn dir(&self) -> File {
        File::open(&self.cwd).unwrap()
    }
    fn register(&self, id: &str) -> Result<(), ProcessRegistrationError> {
        self.catalog.register(
            id,
            self.exe(),
            &Digest::hash(DATA),
            "Tool",
            self.dir(),
            "Work",
        )
    }
    fn stats(&self) -> (usize, u64, u64) {
        let state = self.catalog.state.lock().unwrap();
        (
            state.entries.iter().flatten().count(),
            state.attempts,
            state.hash_work_reserved,
        )
    }
    fn retained_data(&self, id: &str) {
        let state = self.catalog.state.lock().unwrap();
        let entry = state
            .entries
            .iter()
            .flatten()
            .find(|entry| entry.program_id.as_str() == id)
            .unwrap();
        let mut bytes = [0; 8];
        let length = entry.executable.read_at(&mut bytes, 0).unwrap();
        assert_eq!(&bytes[..length], DATA);
    }
}

#[test]
fn positional_hash_ignores_shared_cursor_and_retains_renamed_descriptor() {
    let fixture = Fixture::new();
    let mut executable = fixture.exe();
    executable.seek(SeekFrom::Start(2)).unwrap();
    // A legitimate test alias shares the seek cursor with the retained File.
    let mut alias = executable.try_clone().unwrap();
    let reads = Cell::new(0);
    fixture
        .catalog
        .register_inner(
            "tool",
            executable,
            &Digest::hash(DATA),
            "Tool",
            fixture.dir(),
            "Work",
            1,
            &mut || {
                reads.set(reads.get() + 1);
                alias.seek(SeekFrom::Start(3)).unwrap();
                Ok(())
            },
        )
        .unwrap();
    assert!(reads.get() > 1);
    assert_eq!(alias.stream_position().unwrap(), 3);
    let original = alias.metadata().unwrap();
    std::fs::rename(&fixture.executable, fixture.temp.path().join("renamed")).unwrap();
    std::fs::write(&fixture.executable, b"replacement").unwrap();
    let replacement = fixture.exe().metadata().unwrap();
    assert_ne!(
        (original.dev(), original.ino()),
        (replacement.dev(), replacement.ino())
    );
    fixture.retained_data("tool");
    let state = fixture.catalog.state.lock().unwrap();
    let entry = state.entries[0].as_ref().unwrap();
    assert_eq!(entry.executable_digest, Digest::hash(DATA));
    let mut retained = &entry.executable;
    assert_eq!(retained.stream_position().unwrap(), 3);
    let cwd = fixture.dir().metadata().unwrap();
    let retained_cwd = entry.cwd.metadata().unwrap();
    assert!(retained_cwd.is_dir());
    assert_eq!(
        (cwd.dev(), cwd.ino()),
        (retained_cwd.dev(), retained_cwd.ino())
    );
}

#[test]
fn identical_replacement_bytes_do_not_replace_recorded_descriptor_identity() {
    let fixture = Fixture::new();
    fixture.register("original").unwrap();
    std::fs::rename(&fixture.executable, fixture.temp.path().join("old")).unwrap();
    std::fs::write(&fixture.executable, DATA).unwrap();
    std::fs::set_permissions(&fixture.executable, std::fs::Permissions::from_mode(0o755)).unwrap();
    fixture.register("replacement").unwrap();
    let state = fixture.catalog.state.lock().unwrap();
    let first = state.entries[0].as_ref().unwrap();
    let second = state.entries[1].as_ref().unwrap();
    assert_eq!(first.executable_digest, second.executable_digest);
    let first_stat = first.executable.metadata().unwrap();
    let second_stat = second.executable.metadata().unwrap();
    assert_ne!(
        (first_stat.dev(), first_stat.ino()),
        (second_stat.dev(), second_stat.ino())
    );
    assert_ne!(
        first.executable_identity.bytes(),
        second.executable_identity.bytes()
    );
}

#[test]
fn program_and_both_label_bounds_use_utf8_bytes_and_preserve_prior_entries() {
    let fixture = Fixture::new();
    let boundary = "é".repeat(64);
    fixture
        .catalog
        .register(
            &boundary,
            fixture.exe(),
            &Digest::hash(DATA),
            &boundary,
            fixture.dir(),
            &boundary,
        )
        .unwrap();
    let invalid = [
        String::new(),
        "line\nbreak".into(),
        "nul\0byte".into(),
        "unicode\u{0085}control".into(),
        "x".repeat(129),
        "é".repeat(65),
    ];
    for text in &invalid {
        assert_eq!(
            fixture.register(text),
            Err(ProcessRegistrationError::InvalidProgramId)
        );
        for (catalog_label, cwd_label) in [(text.as_str(), "Work"), ("Tool", text.as_str())] {
            assert_eq!(
                fixture.catalog.register(
                    "valid",
                    fixture.exe(),
                    &Digest::hash(DATA),
                    catalog_label,
                    fixture.dir(),
                    cwd_label
                ),
                Err(ProcessRegistrationError::InvalidLabel)
            );
        }
    }
    assert_eq!(fixture.stats(), (1, 19, WORK));
    fixture.retained_data(&boundary);
}

#[test]
fn actual_descriptor_type_mode_read_and_length_failures_publish_nothing() {
    for case in 0..5 {
        let fixture = Fixture::new();
        let (executable, cwd, expected) = match case {
            0 => (
                fixture.dir(),
                fixture.dir(),
                ProcessRegistrationError::NotRegularFile,
            ),
            1 => {
                std::fs::set_permissions(
                    &fixture.executable,
                    std::fs::Permissions::from_mode(0o644),
                )
                .unwrap();
                (
                    fixture.exe(),
                    fixture.dir(),
                    ProcessRegistrationError::NotExecutable,
                )
            }
            2 => (
                fixture.exe(),
                fixture.exe(),
                ProcessRegistrationError::NotDirectory,
            ),
            3 => (
                OpenOptions::new()
                    .write(true)
                    .open(&fixture.executable)
                    .unwrap(),
                fixture.dir(),
                ProcessRegistrationError::ReadFailed,
            ),
            _ => {
                OpenOptions::new()
                    .write(true)
                    .open(&fixture.executable)
                    .unwrap()
                    .set_len(MAX_EXECUTABLE_BYTES + 1)
                    .unwrap();
                (
                    fixture.exe(),
                    fixture.dir(),
                    ProcessRegistrationError::ExecutableTooLarge,
                )
            }
        };
        let reads = Cell::new(0);
        assert_eq!(
            fixture.catalog.register_inner(
                "tool",
                executable,
                &Digest::hash(DATA),
                "Tool",
                cwd,
                "Work",
                1,
                &mut || {
                    reads.set(reads.get() + 1);
                    Ok(())
                }
            ),
            Err(expected)
        );
        assert_eq!(reads.get(), 0);
        assert_eq!(fixture.stats(), (0, 1, if case == 3 { WORK } else { 0 }));
    }
}

#[test]
fn failed_digest_keeps_work_consumed_but_does_not_reserve_failed_id() {
    let fixture = Fixture::new();
    fixture.register("prior").unwrap();
    assert_eq!(
        fixture.catalog.register(
            "retry",
            fixture.exe(),
            &Digest::hash(b"different"),
            "Tool",
            fixture.dir(),
            "Work"
        ),
        Err(ProcessRegistrationError::DigestMismatch)
    );
    assert_eq!(fixture.stats(), (1, 2, 2 * WORK));
    fixture.retained_data("prior");
    fixture.register("retry").unwrap();
    assert_eq!(fixture.stats(), (2, 3, 3 * WORK));
}

#[test]
fn actual_growth_and_truncation_during_positional_hash_are_rejected() {
    for grow in [true, false] {
        let fixture = Fixture::new();
        let writer = OpenOptions::new()
            .write(true)
            .open(&fixture.executable)
            .unwrap();
        let reads = Cell::new(0);
        assert_eq!(
            fixture.catalog.register_inner(
                "tool",
                fixture.exe(),
                &Digest::hash(DATA),
                "Tool",
                fixture.dir(),
                "Work",
                1,
                &mut || {
                    reads.set(reads.get() + 1);
                    if reads.get() == 1 {
                        if grow {
                            assert_eq!(writer.write_at(b"x", 4).unwrap(), 1);
                        } else {
                            writer.set_len(1).unwrap();
                        }
                    }
                    Ok(())
                }
            ),
            Err(ProcessRegistrationError::ContentChanged)
        );
        assert!(reads.get() > 1);
        assert_eq!(fixture.stats(), (0, 1, WORK));
    }
}

#[test]
fn executable_and_cwd_mode_drift_reject_without_timestamp_assumptions() {
    for executable in [true, false] {
        let fixture = Fixture::new();
        let path = if executable {
            &fixture.executable
        } else {
            &fixture.cwd
        };
        let reads = Cell::new(0);
        assert_eq!(
            fixture.catalog.register_inner(
                "tool",
                fixture.exe(),
                &Digest::hash(DATA),
                "Tool",
                fixture.dir(),
                "Work",
                1,
                &mut || {
                    reads.set(reads.get() + 1);
                    if reads.get() == 1 {
                        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
                            .unwrap();
                    }
                    Ok(())
                }
            ),
            Err(ProcessRegistrationError::ContentChanged)
        );
        assert_eq!(fixture.stats(), (0, 1, WORK));
    }
}

#[test]
fn duplicate_and_full_catalog_never_rebind_or_evict_published_entries() {
    let fixture = Fixture::new();
    fixture.register("p0").unwrap();
    let other = fixture.temp.path().join("different");
    std::fs::write(&other, b"different").unwrap();
    std::fs::set_permissions(&other, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(
        fixture.catalog.register(
            "p0",
            File::open(other).unwrap(),
            &Digest::hash(b"different"),
            "Other",
            fixture.dir(),
            "Elsewhere"
        ),
        Err(ProcessRegistrationError::DuplicateProgramId)
    );
    assert_eq!(fixture.stats(), (1, 2, WORK));
    for index in 1..16 {
        fixture.register(&format!("p{index}")).unwrap();
    }
    assert_eq!(
        fixture.register("overflow"),
        Err(ProcessRegistrationError::CatalogFull)
    );
    assert_eq!(fixture.stats(), (16, 18, 16 * WORK));
    for index in 0..16 {
        fixture.retained_data(&format!("p{index}"));
    }
}

#[test]
fn failed_attempts_exhaust_at_thirty_two_without_reading_or_refunding() {
    let fixture = Fixture::new();
    for _ in 0..32 {
        assert_eq!(
            fixture.register(""),
            Err(ProcessRegistrationError::InvalidProgramId)
        );
    }
    let reads = Cell::new(0);
    assert_eq!(
        fixture.catalog.register_inner(
            "valid",
            fixture.exe(),
            &Digest::hash(DATA),
            "Tool",
            fixture.dir(),
            "Work",
            1,
            &mut || {
                reads.set(reads.get() + 1);
                Ok(())
            }
        ),
        Err(ProcessRegistrationError::AttemptsExhausted)
    );
    assert_eq!(reads.get(), 0);
    assert_eq!(fixture.stats(), (0, 32, 0));
    let independent = Fixture::new();
    independent.register("valid").unwrap();
    assert_eq!(independent.stats(), (1, 1, WORK));
    assert_eq!(fixture.stats(), (0, 32, 0));
}

#[test]
fn lifetime_hash_work_includes_failed_and_unused_detection_bytes() {
    let fixture = Fixture::new();
    // Lower ONLY the existing private work limit; actual input remains tiny.
    fixture.catalog.state.lock().unwrap().hash_work_limit = 2 * WORK;
    assert_eq!(
        fixture.catalog.register(
            "bad",
            fixture.exe(),
            &Digest::hash(b"wrong"),
            "Tool",
            fixture.dir(),
            "Work"
        ),
        Err(ProcessRegistrationError::DigestMismatch)
    );
    fixture.register("good").unwrap();
    assert_eq!(fixture.stats(), (1, 2, 2 * WORK));
    let reads = Cell::new(0);
    assert_eq!(
        fixture.catalog.register_inner(
            "denied",
            fixture.exe(),
            &Digest::hash(DATA),
            "Tool",
            fixture.dir(),
            "Work",
            1,
            &mut || {
                reads.set(reads.get() + 1);
                Ok(())
            }
        ),
        Err(ProcessRegistrationError::HashWorkExhausted)
    );
    assert_eq!(reads.get(), 0);
    assert_eq!(fixture.stats(), (1, 3, 2 * WORK));
    fixture.retained_data("good");
}

#[test]
fn shared_control_admits_memory_before_reads_and_retains_only_live_storage() {
    let control = CoreControl::new(CoreControlLimits {
        max_registrations: 2,
        ..CoreControlLimits::default()
    })
    .unwrap();
    let fixture = Fixture::with_control(control.clone());
    let owner_bytes = control.usage().bytes;
    assert_eq!(control.usage().count(ControlKind::Catalog), 1);
    fixture
        .catalog
        .register_inner(
            "prior",
            fixture.exe(),
            &Digest::hash(DATA),
            "Tool",
            fixture.dir(),
            "Work",
            1,
            &mut || {
                // Both memory reservations already exist at every actual read callback.
                assert_eq!(control.usage().count(ControlKind::Registration), 2);
                assert!(
                    control.usage().bytes >= owner_bytes + u64::try_from(SCRATCH_BYTES).unwrap()
                );
                Ok(())
            },
        )
        .unwrap();
    let retained = control.usage();
    assert_eq!(retained.count(ControlKind::Registration), 1);
    assert!(retained.bytes > owner_bytes);
    let reads = Cell::new(0);
    let mut observer = || {
        reads.set(reads.get() + 1);
        Ok(())
    };
    assert_eq!(
        fixture.catalog.register_inner(
            "scratch-denied",
            fixture.exe(),
            &Digest::hash(DATA),
            "Tool",
            fixture.dir(),
            "Work",
            1,
            &mut observer
        ),
        Err(ProcessRegistrationError::Capacity)
    );
    assert_eq!(control.usage(), retained);
    let held = control.reserve(ControlKind::Registration, 1).unwrap();
    assert_eq!(
        fixture.catalog.register_inner(
            "entry-denied",
            fixture.exe(),
            &Digest::hash(DATA),
            "Tool",
            fixture.dir(),
            "Work",
            1,
            &mut observer
        ),
        Err(ProcessRegistrationError::Capacity)
    );
    assert_eq!(reads.get(), 0);
    assert_eq!(fixture.stats(), (1, 3, 3 * WORK));
    fixture.retained_data("prior");
    drop(held);
    assert_eq!(control.usage(), retained);
    drop(fixture);
    assert_eq!(control.usage().bytes, 0);
    assert_eq!(control.usage().count(ControlKind::Catalog), 0);
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
}

#[test]
fn catalog_initialization_capacity_denial_does_not_spend_other_accounts() {
    let denied = CoreControl::new(CoreControlLimits {
        max_catalogs: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    assert!(matches!(
        ProcessCatalog::new(denied.clone()),
        Err(ProcessRegistrationError::Capacity)
    ));
    assert_eq!(denied.usage().bytes, 0);
    let independent = Fixture::new();
    independent.register("tool").unwrap();
    assert_eq!(denied.usage().bytes, 0);
}

#[test]
fn recorded_binding_is_owner_specific_and_commits_labels_arguments_and_limits() {
    let fixture = Fixture::new();
    let foreign = Fixture::new();
    fixture.register("tool").unwrap();
    foreign.register("tool").unwrap();
    let argv = vec!["TEST_SECRET_ARG_VALUE".to_owned()];
    let env = vec![("TEST_VAR".to_owned(), "TEST_SECRET_ENV_VALUE".to_owned())];
    let limits = ResourceLimits {
        fuel_per_call: 2_000,
        memory_mib: 32,
        wall_ms: 1_000,
        table_elements: 512,
        output_bytes: 2_048,
    };
    let binding = fixture
        .catalog
        .commit_launch("tool", &argv, &env, &limits, None)
        .unwrap();
    let state = fixture.catalog.state.lock().unwrap();
    let entry = state.entries[0].as_ref().unwrap();
    let snapshot = launch_snapshot(entry, &argv, &env, &limits, None);
    assert_eq!(binding.verify(&fixture.catalog.key, &snapshot), Ok(()));
    assert_eq!(
        binding.verify(&foreign.catalog.key, &snapshot),
        Err(LaunchBindingError::Mismatch)
    );
    let different_argv = vec!["different".to_owned()];
    let different_env = vec![("TEST_VAR".to_owned(), "different".to_owned())];
    let mut different_limits = limits.clone();
    different_limits.wall_ms += 1;
    for change in 0..6 {
        let mut altered = launch_snapshot(entry, &argv, &env, &limits, None);
        match change {
            0 => altered.catalog_label = "Other tool",
            1 => altered.cwd_label = "Other workspace",
            2 => altered.argv = &different_argv,
            3 => altered.env = &different_env,
            4 => altered.limits = &different_limits,
            _ => altered.pty = Some(PtyDimensions::new(80, 24).unwrap()),
        }
        assert_eq!(
            binding.verify(&fixture.catalog.key, &altered),
            Err(LaunchBindingError::Mismatch)
        );
    }
    let rendered = format!("{binding:?} {binding}");
    for secret in [&argv[0], &env[0].1] {
        assert!(!rendered.contains(secret));
        let failure = ProcessRegistrationError::InvalidProgramId;
        assert!(!format!("{failure} {failure:?}").contains(secret));
    }
}

struct SnapshotBudget {
    used: AtomicU64,
    calls: AtomicU64,
    limit: u64,
    requests: Mutex<Vec<AllocationRequest>>,
}
impl SnapshotBudget {
    fn new(limit: u64) -> Self {
        Self {
            used: AtomicU64::new(0),
            calls: AtomicU64::new(0),
            limit,
            requests: Mutex::new(Vec::new()),
        }
    }
    fn used(&self) -> u64 {
        self.used.load(Ordering::SeqCst)
    }
    fn calls(&self) -> u64 {
        self.calls.load(Ordering::SeqCst)
    }
}
impl AllocationBudget for SnapshotBudget {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.requests.lock().unwrap().push(request);
        self.used
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= self.limit)
            })
            .map(|_| ())
            .map_err(|_| AllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        let prior = self.used.fetch_sub(bytes, Ordering::SeqCst);
        assert!(prior >= bytes, "snapshot allocation refunded twice");
    }
}

fn snapshot_limits() -> ResourceLimits {
    ResourceLimits {
        fuel_per_call: 2_000,
        memory_mib: 32,
        wall_ms: 1_000,
        table_elements: 512,
        output_bytes: 2_048,
    }
}

#[test]
fn prepared_snapshot_owns_args_files_and_charges_after_catalog_drop() {
    let control = CoreControl::default();
    let fixture = Fixture::with_control(control.clone());
    fixture.register("tool").unwrap();
    let budget = Arc::new(SnapshotBudget::new(2 * 1024 * 1024));
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let mut argv = vec!["SECRET_FIRST".to_owned(), "SECRET_SECOND".to_owned()];
    // Duplicated names and order must survive exactly; no normalization occurs.
    let mut env = vec![
        ("SECRET_NAME".to_owned(), "SECRET_VALUE_1".to_owned()),
        ("SECRET_NAME".to_owned(), "SECRET_VALUE_2".to_owned()),
    ];
    let mut limits = snapshot_limits();
    let snapshot = fixture
        .catalog
        .prepare_snapshot("tool", &argv, &env, &limits, None, &owner)
        .unwrap();
    assert_eq!(fixture.catalog.verify_snapshot(&snapshot), Ok(()));
    let charged = budget.used();
    let data = snapshot.data.get();
    let actual_allocated = data.argv.capacity() * std::mem::size_of::<String>()
        + data.env.capacity() * std::mem::size_of::<(String, String)>()
        + data.argv.iter().map(String::capacity).sum::<usize>()
        + data
            .env
            .iter()
            .map(|(name, value)| name.capacity() + value.capacity())
            .sum::<usize>();
    assert_eq!(charged, u64::try_from(actual_allocated).unwrap());
    assert_eq!(budget.requests.lock().unwrap().len(), 1);
    assert_eq!(
        budget.requests.lock().unwrap()[0].kind,
        AllocationKind::Clone
    );
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(snapshot.binding().argv_count(), 2);
    assert_eq!(snapshot.binding().env_count(), 2);
    let cwd = fixture.dir().metadata().unwrap();
    argv[0] = "CALLER_CHANGED_ARG".to_owned();
    env.reverse();
    env[0].1 = "CALLER_CHANGED_ENV".to_owned();
    limits.wall_ms += 1;
    assert_eq!(fixture.catalog.verify_snapshot(&snapshot), Ok(()));
    let data = snapshot.data.get();
    assert_eq!(data.argv[0], "SECRET_FIRST");
    assert_eq!(data.env[0].1, "SECRET_VALUE_1");
    assert_eq!(data.env[1].1, "SECRET_VALUE_2");
    assert_eq!(data.limits.wall_ms, snapshot_limits().wall_ms);
    assert_ne!(data.limits.wall_ms, limits.wall_ms);
    std::fs::rename(
        &fixture.executable,
        fixture.temp.path().join("old-executable"),
    )
    .unwrap();
    std::fs::write(&fixture.executable, b"unapproved replacement").unwrap();
    std::fs::rename(&fixture.cwd, fixture.temp.path().join("old-cwd")).unwrap();
    std::fs::create_dir(&fixture.cwd).unwrap();
    // The fixture drops the catalog and removes every pathname. The snapshot's
    // duplicated actual open objects and their own original charges survive.
    drop(fixture);
    assert_eq!(control.usage().count(ControlKind::Catalog), 0);
    assert_eq!(control.usage().count(ControlKind::Registration), 0);
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    assert_eq!(control.usage().bytes, snapshot._control.bytes());
    assert_eq!(budget.used(), charged);
    let mut bytes = [0u8; 16];
    let count = snapshot
        .data
        .get()
        .executable
        .read_at(&mut bytes, 0)
        .unwrap();
    assert_eq!(&bytes[..count], DATA);
    let retained_cwd = snapshot.data.get().cwd.metadata().unwrap();
    assert!(retained_cwd.is_dir());
    assert_eq!(
        (retained_cwd.dev(), retained_cwd.ino()),
        (cwd.dev(), cwd.ino())
    );
    let rendered = format!("{snapshot:?}");
    for secret in [
        "SECRET_FIRST",
        "SECRET_SECOND",
        "SECRET_NAME",
        "SECRET_VALUE_1",
        "SECRET_VALUE_2",
    ] {
        assert!(!rendered.contains(secret));
    }
    drop(snapshot);
    assert_eq!(budget.used(), 0);
    assert_eq!(control.usage().bytes, 0);
    assert_eq!(control.usage().count(ControlKind::Task), 0);
}

#[test]
fn prepared_snapshot_rejects_foreign_catalog_key_with_identical_descriptor_facts() {
    let fixture = Fixture::new();
    fixture.register("tool").unwrap();
    let foreign = ProcessCatalog::new(CoreControl::default()).unwrap();
    // Both catalogs register clones of the same underlying executable and cwd,
    // the same required digest, id and labels. Only their private keys differ.
    let (executable, cwd) = {
        let state = fixture.catalog.state.lock().unwrap();
        let entry = state.entries[0].as_ref().unwrap();
        (
            entry.executable.try_clone().unwrap(),
            entry.cwd.try_clone().unwrap(),
        )
    };
    foreign
        .register("tool", executable, &Digest::hash(DATA), "Tool", cwd, "Work")
        .unwrap();
    {
        let first = fixture.catalog.state.lock().unwrap();
        let second = foreign.state.lock().unwrap();
        let first = first.entries[0].as_ref().unwrap();
        let second = second.entries[0].as_ref().unwrap();
        assert_eq!(
            first.executable_identity.bytes(),
            second.executable_identity.bytes()
        );
        assert_eq!(first.cwd_identity.bytes(), second.cwd_identity.bytes());
        assert_eq!(first.executable_digest, second.executable_digest);
    }
    let budget = Arc::new(SnapshotBudget::new(4096));
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let snapshot = fixture
        .catalog
        .prepare_snapshot(
            "tool",
            &["private".to_owned()],
            &[],
            &snapshot_limits(),
            None,
            &owner,
        )
        .unwrap();
    assert_eq!(fixture.catalog.verify_snapshot(&snapshot), Ok(()));
    assert_eq!(
        foreign.verify_snapshot(&snapshot),
        Err(LaunchBindingError::Mismatch)
    );
    drop(snapshot);
    assert_eq!(budget.used(), 0);
}

#[test]
fn snapshot_input_bounds_reject_before_owned_copy_budget_or_descriptor_duplication() {
    let control = CoreControl::default();
    let fixture = Fixture::with_control(control.clone());
    fixture.register("tool").unwrap();
    let baseline = control.usage();
    let budget = Arc::new(SnapshotBudget::new(2 * 1024 * 1024));
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let cases = [
        (
            vec![String::new(); tobkiri_gate::MAX_ARGV_ENTRIES + 1],
            vec![],
            LaunchBindingError::ArgvCount,
        ),
        (
            vec![],
            vec![("N".to_owned(), String::new()); tobkiri_gate::MAX_ENV_ENTRIES + 1],
            LaunchBindingError::EnvCount,
        ),
        (
            vec!["x".repeat(tobkiri_gate::MAX_STRING_BYTES + 1)],
            vec![],
            LaunchBindingError::StringBytes,
        ),
        (
            vec![
                "x".repeat(tobkiri_gate::MAX_STRING_BYTES);
                tobkiri_gate::MAX_TOTAL_BYTES / tobkiri_gate::MAX_STRING_BYTES + 1
            ],
            vec![],
            LaunchBindingError::TotalBytes,
        ),
        (
            vec!["ARG_SECRET\0TAIL".to_owned()],
            vec![],
            LaunchBindingError::NulByte,
        ),
        (
            vec![],
            vec![("N".to_owned(), "ENV_SECRET\0TAIL".to_owned())],
            LaunchBindingError::NulByte,
        ),
        (
            vec![],
            vec![("SECRET=NAME".to_owned(), "value".to_owned())],
            LaunchBindingError::EnvName,
        ),
        (
            vec![],
            vec![(
                "x".repeat(tobkiri_gate::MAX_STRING_BYTES + 1),
                "value".to_owned(),
            )],
            LaunchBindingError::StringBytes,
        ),
    ];
    for (argv, env, expected) in cases {
        let duplicates = Cell::new(0);
        let result = fixture.catalog.prepare_snapshot_inner(
            "tool",
            &argv,
            &env,
            &snapshot_limits(),
            None,
            &owner,
            &mut |file| {
                duplicates.set(duplicates.get() + 1);
                file.try_clone()
            },
        );
        let error = result.unwrap_err();
        assert_eq!(error, ProcessSnapshotError::Binding(expected));
        let rendered = format!("{error:?} {error}");
        for secret in ["ARG_SECRET", "ENV_SECRET", "SECRET=NAME"] {
            assert!(!rendered.contains(secret));
        }
        assert_eq!(duplicates.get(), 0);
        assert_eq!(budget.calls(), 0);
        assert_eq!(budget.used(), 0);
        assert_eq!(control.usage(), baseline);
    }
    assert!(matches!(
        fixture
            .catalog
            .prepare_snapshot("missing", &[], &[], &snapshot_limits(), None, &owner),
        Err(ProcessSnapshotError::UnknownProgram)
    ));
    assert_eq!(budget.calls(), 0);
    assert_eq!(control.usage(), baseline);
}

#[test]
fn snapshot_existing_count_string_and_total_boundaries_are_not_narrowed() {
    let fixture = Fixture::new();
    fixture.register("tool").unwrap();
    let budget = Arc::new(SnapshotBudget::new(2 * 1024 * 1024));
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let mut argv = vec![String::new(); tobkiri_gate::MAX_ARGV_ENTRIES];
    argv[0] = "x".repeat(tobkiri_gate::MAX_STRING_BYTES);
    let env = vec![("N".to_owned(), String::new()); tobkiri_gate::MAX_ENV_ENTRIES];
    let snapshot = fixture
        .catalog
        .prepare_snapshot(
            "tool",
            &argv,
            &env,
            &snapshot_limits(),
            Some(PtyDimensions::new(80, 24).unwrap()),
            &owner,
        )
        .unwrap();
    assert_eq!(fixture.catalog.verify_snapshot(&snapshot), Ok(()));
    assert_eq!(
        snapshot.binding().argv_count(),
        tobkiri_gate::MAX_ARGV_ENTRIES
    );
    assert_eq!(
        snapshot.binding().env_count(),
        tobkiri_gate::MAX_ENV_ENTRIES
    );
    drop(snapshot);
    assert_eq!(budget.used(), 0);
    let argv = vec![
        "x".repeat(tobkiri_gate::MAX_STRING_BYTES);
        tobkiri_gate::MAX_TOTAL_BYTES / tobkiri_gate::MAX_STRING_BYTES
    ];
    assert_eq!(
        argv.iter().map(String::len).sum::<usize>(),
        tobkiri_gate::MAX_TOTAL_BYTES
    );
    let snapshot = fixture
        .catalog
        .prepare_snapshot("tool", &argv, &[], &snapshot_limits(), None, &owner)
        .unwrap();
    assert_eq!(fixture.catalog.verify_snapshot(&snapshot), Ok(()));
    drop(snapshot);
    assert_eq!(budget.used(), 0);
}

#[test]
fn snapshot_layout_rejects_overflow_without_allocation_or_fake_input_slices() {
    for (argv, env, raw) in [
        (usize::MAX, 0, 0),
        (0, usize::MAX, 0),
        (1, 0, usize::MAX),
        (0, 0, usize::MAX),
    ] {
        assert_eq!(
            snapshot_payload_layout(argv, env, raw),
            Err(AllocationError::Overflow)
        );
    }
    assert_eq!(snapshot_payload_layout(0, 0, 0).unwrap().bytes, 0);
}

#[test]
fn snapshot_budget_and_real_descriptor_failures_rollback_all_new_storage() {
    let control = CoreControl::default();
    let fixture = Fixture::with_control(control.clone());
    fixture.register("tool").unwrap();
    let baseline = control.usage();
    let denied = Arc::new(SnapshotBudget::new(0));
    let denied_owner: Arc<dyn AllocationBudget> = denied.clone();
    let argv = vec!["SECRET_ARG".to_owned()];
    let env = vec![("SECRET_NAME".to_owned(), "SECRET_ENV".to_owned())];
    let duplicates = Cell::new(0);
    let result = fixture.catalog.prepare_snapshot_inner(
        "tool",
        &argv,
        &env,
        &snapshot_limits(),
        None,
        &denied_owner,
        &mut |file| {
            duplicates.set(duplicates.get() + 1);
            file.try_clone()
        },
    );
    assert!(matches!(
        result,
        Err(ProcessSnapshotError::Allocation(AllocationError::Limit))
    ));
    assert_eq!(duplicates.get(), 0);
    assert_eq!(denied.used(), 0);
    assert_eq!(control.usage(), baseline);

    let budget = Arc::new(SnapshotBudget::new(4096));
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let result = fixture.catalog.prepare_snapshot_inner(
        "tool",
        &argv,
        &env,
        &snapshot_limits(),
        None,
        &owner,
        &mut |file| {
            duplicates.set(duplicates.get() + 1);
            assert!(budget.used() > 0);
            assert_eq!(control.usage().count(ControlKind::Task), 1);
            if duplicates.get() == 2 {
                // Fail the cwd duplication after argv/env were copied and an
                // actual executable duplicate was acquired; no invalid FD exists.
                Err(std::io::Error::other("PRIVATE_OS_ERROR"))
            } else {
                file.try_clone()
            }
        },
    );
    assert_eq!(result.unwrap_err(), ProcessSnapshotError::Descriptor);
    assert_eq!(duplicates.get(), 2);
    assert_eq!(budget.used(), 0);
    assert_eq!(control.usage(), baseline);
    fixture.retained_data("tool");
    let successful = fixture
        .catalog
        .prepare_snapshot("tool", &argv, &env, &snapshot_limits(), None, &owner)
        .unwrap();
    assert_eq!(fixture.catalog.verify_snapshot(&successful), Ok(()));
    drop(successful);
    assert_eq!(budget.used(), 0);
    assert_eq!(control.usage(), baseline);
}

#[test]
fn snapshot_control_denial_precedes_argument_budget_and_file_duplicates() {
    let control = CoreControl::new(CoreControlLimits {
        max_tasks: 0,
        ..CoreControlLimits::default()
    })
    .unwrap();
    let fixture = Fixture::with_control(control.clone());
    fixture.register("tool").unwrap();
    let baseline = control.usage();
    let budget = Arc::new(SnapshotBudget::new(4096));
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let duplicates = Cell::new(0);
    let result = fixture.catalog.prepare_snapshot_inner(
        "tool",
        &["private".to_owned()],
        &[],
        &snapshot_limits(),
        None,
        &owner,
        &mut |file| {
            duplicates.set(duplicates.get() + 1);
            file.try_clone()
        },
    );
    assert_eq!(result.unwrap_err(), ProcessSnapshotError::Capacity);
    assert_eq!(duplicates.get(), 0);
    assert_eq!(budget.calls(), 0);
    assert_eq!(control.usage(), baseline);
}

#[test]
fn empty_snapshot_retains_the_original_budget_owner_until_unused_drop() {
    let control = CoreControl::default();
    let fixture = Fixture::with_control(control.clone());
    fixture.register("tool").unwrap();
    let baseline = control.usage();
    let budget = Arc::new(SnapshotBudget::new(0));
    let weak_budget = Arc::downgrade(&budget);
    let owner: Arc<dyn AllocationBudget> = budget.clone();
    let snapshot = fixture
        .catalog
        .prepare_snapshot("tool", &[], &[], &snapshot_limits(), None, &owner)
        .unwrap();
    assert_eq!(fixture.catalog.verify_snapshot(&snapshot), Ok(()));
    assert_eq!(snapshot.binding().argv_count(), 0);
    assert_eq!(snapshot.binding().env_count(), 0);
    assert_eq!(budget.used(), 0);
    assert_eq!(budget.calls(), 1);
    drop(owner);
    drop(budget);
    assert!(weak_budget.upgrade().is_some());
    assert_eq!(control.usage().count(ControlKind::Task), 1);
    drop(snapshot);
    assert!(weak_budget.upgrade().is_none());
    assert_eq!(control.usage(), baseline);
}
