//! Recover the same real Conversation Store from either Host implementation.

#![cfg(any(target_os = "macos", target_os = "linux"))]

use std::{
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus},
    thread,
    time::{Duration, Instant},
};
use tobkiri_canon::ids::ProfileId;
use tobkiri_store::{DataRoot, StoreError};

const PYTHON_WRITER: &str = r#"
from pathlib import Path
import sys
import time

runtime, scratch, conversation = sys.argv[1:]
sys.path.insert(0, runtime)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

root = Path(scratch)
store = ConversationStore('defaults', user_data_root=root)
if conversation == 'python-committed':
    write = store._write

    def pause_after_commit(value):
        write(value)
        (root / 'python-ready').write_text('committed')
        while True:
            time.sleep(1)

    store._write = pause_after_commit
    store.create({'id': conversation}, expected_revision=0)
else:
    store.create({'id': conversation}, expected_revision=1)
"#;

struct Writer(Child);

impl Writer {
    fn start(command: &mut Command) -> Self {
        Self(command.spawn().expect("start test writer"))
    }

    fn await_marker(&mut self, marker: &Path) {
        let deadline = Instant::now() + Duration::from_secs(20);
        while !marker.exists() {
            assert!(self.0.try_wait().unwrap().is_none(), "writer exited early");
            assert!(Instant::now() < deadline, "writer did not reach its lock");
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn kill(&mut self) {
        self.0.kill().expect("terminate the fixture writer");
        assert!(!self.0.wait().unwrap().success());
    }

    fn wait(&mut self) -> ExitStatus {
        let deadline = Instant::now() + Duration::from_secs(20);
        loop {
            if let Some(status) = self.0.try_wait().unwrap() {
                return status;
            }
            assert!(Instant::now() < deadline, "writer did not recover the lock");
            thread::sleep(Duration::from_millis(10));
        }
    }
}

impl Drop for Writer {
    fn drop(&mut self) {
        if self.0.try_wait().is_ok_and(|status| status.is_none()) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

fn python(root: &Path, conversation: &str) -> Writer {
    let interpreter = std::env::var_os("TOBKIRI_LEGACY_CONVERSATION_TEST_PYTHON")
        .expect("set TOBKIRI_LEGACY_CONVERSATION_TEST_PYTHON");
    let runtime = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tobkiri_runtime");
    Writer::start(
        Command::new(interpreter)
            .args(["-B", "-I", "-c", PYTHON_WRITER])
            .arg(runtime)
            .arg(root)
            .arg(conversation),
    )
}

#[test]
#[ignore = "requires the pinned CPython runtime"]
fn conversation_lock_recovers_between_python_and_rust() {
    let fixture = tempfile::tempdir().unwrap();
    let directory = fixture.path().canonicalize().unwrap();
    let profile = ProfileId::parse("defaults").unwrap();

    let mut python_writer = python(&directory, "python-committed");
    python_writer.await_marker(&directory.join("python-ready"));
    let root = DataRoot::open_legacy_registry(&directory).unwrap();
    let busy = root.lock_legacy_conversation_store(&profile).unwrap_err();
    assert!(matches!(busy, StoreError::Io(error) if error.kind() == ErrorKind::AlreadyExists));
    python_writer.kill();
    let recovered = root.lock_legacy_conversation_store(&profile).unwrap();
    let stored = recovered.read(1024 * 1024).unwrap().unwrap();
    let first: serde_json::Value = serde_json::from_slice(&stored).unwrap();
    assert_eq!(
        first["conversations"]["python-committed"]["id"],
        "python-committed"
    );
    drop(recovered);

    let mut rust_writer = Writer::start(
        Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "hold_conversation_lock_in_child"])
            .env("TOBKIRI_TEST_CONVERSATION_LOCK_ROOT", &directory),
    );
    rust_writer.await_marker(&directory.join("rust-ready"));
    rust_writer.kill();
    assert!(python(&directory, "python-recovered").wait().success());
    let bytes = root
        .read_legacy_conversation_store(&profile, 1024 * 1024)
        .unwrap()
        .unwrap();
    let second: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(second["revision"], 2);
    assert_eq!(
        second["conversations"]["python-committed"],
        first["conversations"]["python-committed"]
    );
    assert_eq!(
        second["conversations"]["python-recovered"]["id"],
        "python-recovered"
    );
}

#[test]
#[ignore = "subprocess fixture for the lock recovery test"]
fn hold_conversation_lock_in_child() {
    let directory: PathBuf = std::env::var_os("TOBKIRI_TEST_CONVERSATION_LOCK_ROOT")
        .expect("start this fixture from the parent test")
        .into();
    let root = DataRoot::open_legacy_registry(&directory).unwrap();
    let profile = ProfileId::parse("defaults").unwrap();
    let _writer = root.lock_legacy_conversation_store(&profile).unwrap();
    fs::write(directory.join("rust-ready"), "locked").unwrap();
    loop {
        thread::park();
    }
}
