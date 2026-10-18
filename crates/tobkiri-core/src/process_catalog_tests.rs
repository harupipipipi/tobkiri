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
