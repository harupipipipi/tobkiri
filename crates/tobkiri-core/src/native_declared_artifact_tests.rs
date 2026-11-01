//! Actual macOS-root/kernel/file tests for the generic Core action. The source
//! here is fixture DATA; Launcher separately proves its real resolver linkage.
use super::*;
use crate::{
    ApprovalAnswer, ApprovalPrompt, CanonValue, CoreConfig, HostError, HostIo, TrustedPrompt,
    UiEmbed, ids::UiSessionId,
};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};

struct NoEffects;
#[async_trait::async_trait]
impl TrustedPrompt for NoEffects {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        panic!("declared-artifact inspection must not invent an approval");
    }
}
#[async_trait::async_trait]
impl HostIo for NoEffects {
    fn display(&self, _: &str, _: &CanonValue) {
        panic!("no host effect during inspection");
    }
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        panic!("no Terminal during inspection");
    }
    fn terminal_write(&self, _: &str) {
        panic!("no Terminal during inspection");
    }
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        panic!("no UI during inspection");
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        panic!("no new directory selection during inspection");
    }
}

struct Source {
    root: PathBuf,
    declarations: BTreeMap<String, Digest>,
    dropped: Arc<AtomicUsize>,
}
impl NativeArtifactDeclaration for Digest {
    fn digest(&self) -> &str {
        self.as_str()
    }
}
impl NativeDeclaredArtifactSource for Source {
    type Declaration = Digest;
    fn root(&self) -> &Path {
        &self.root
    }
    fn declarations(&self) -> &BTreeMap<String, Digest> {
        &self.declarations
    }
}
impl Drop for Source {
    fn drop(&mut self) {
        self.dropped.fetch_add(1, Ordering::SeqCst);
    }
}
fn fixture() -> (tempfile::TempDir, Arc<Core>, Source) {
    let temp = tempfile::tempdir().unwrap();
    let physical = fs::canonicalize(temp.path()).unwrap();
    let core = Arc::new(
        Core::open(
            CoreConfig::for_test_data_root(physical.join("data")),
            Arc::new(NoEffects),
        )
        .unwrap(),
    );
    assert!(core.profile_ids().unwrap().is_empty());
    let root = physical.join("source");
    fs::create_dir(&root).unwrap();
    let source = Source {
        root,
        declarations: BTreeMap::new(),
        dropped: Arc::new(AtomicUsize::new(0)),
    };
    (temp, core, source)
}

#[tokio::test]
async fn reads_declared_member_and_returns_identical_source_without_any_profile() {
    let (_temp, core, mut source) = fixture();
    fs::create_dir(source.root.join("nested")).unwrap();
    fs::write(source.root.join("nested/member.json"), br#"{"value":7}"#).unwrap();
    source
        .declarations
        .insert("nested/member.json".into(), Digest::hash(br#"{"value":7}"#));
    let root_pointer = source.root.as_os_str().as_encoded_bytes().as_ptr();
    let key_pointer = source.declarations.first_key_value().unwrap().0.as_ptr();
    let mut inspection = core.inspect_declared_artifacts(source).unwrap();
    let output = inspection.read("nested/member.json").await.unwrap();
    assert_eq!(output.as_bytes(), br#"{"value":7}"#);
    let source = inspection.finish().await.unwrap();
    assert_eq!(
        source.root.as_os_str().as_encoded_bytes().as_ptr(),
        root_pointer
    );
    assert_eq!(
        source.declarations.first_key_value().unwrap().0.as_ptr(),
        key_pointer
    );
    assert!(core.profile_ids().unwrap().is_empty());
}

#[tokio::test]
async fn undeclared_sibling_traversal_and_changed_digest_cannot_disclose_bytes() {
    let (_temp, core, mut source) = fixture();
    fs::write(source.root.join("member"), b"changed").unwrap();
    fs::write(source.root.join("sibling"), b"private").unwrap();
    source
        .declarations
        .insert("member".into(), Digest::hash(b"expected"));
    source
        .declarations
        .insert("../outside".into(), Digest::hash(b"private"));
    let mut inspection = core.inspect_declared_artifacts(source).unwrap();
    assert!(matches!(
        inspection.read("sibling").await,
        Err(NativeArtifactError::Denied)
    ));
    assert!(matches!(
        inspection.read("../outside").await,
        Err(NativeArtifactError::Denied)
    ));
    assert!(matches!(
        inspection.read("member").await,
        Err(NativeArtifactError::Integrity)
    ));
    inspection.finish().await.unwrap();
}

#[tokio::test]
async fn declared_parent_and_leaf_symlinks_and_fifo_fail_without_hanging() {
    use std::os::unix::fs::symlink;
    let (_temp, core, mut source) = fixture();
    let outside = source.root.parent().unwrap().join("outside");
    fs::create_dir(&outside).unwrap();
    fs::write(outside.join("secret"), b"secret").unwrap();
    symlink(&outside, source.root.join("parent-link")).unwrap();
    symlink(outside.join("secret"), source.root.join("leaf-link")).unwrap();
    let fifo =
        std::ffi::CString::new(source.root.join("fifo").as_os_str().as_encoded_bytes()).unwrap();
    // SAFETY: fixture-owned valid C pathname and ordinary owner-only FIFO mode.
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
    for name in ["parent-link/secret", "leaf-link", "fifo"] {
        source
            .declarations
            .insert(name.into(), Digest::hash(b"secret"));
    }
    let mut inspection = core.inspect_declared_artifacts(source).unwrap();
    for name in ["parent-link/secret", "leaf-link", "fifo"] {
        let result = tokio::time::timeout(std::time::Duration::from_secs(2), inspection.read(name))
            .await
            .expect("rejected special file must not block in open");
        assert!(matches!(
            result,
            Err(NativeArtifactError::Denied | NativeArtifactError::SourceIo(_))
        ));
    }
    inspection.finish().await.unwrap();
}

#[tokio::test]
async fn legitimate_hardlinked_declared_file_keeps_existing_read_semantics() {
    let (_temp, core, mut source) = fixture();
    fs::write(source.root.join("original"), b"shared").unwrap();
    fs::hard_link(source.root.join("original"), source.root.join("declared")).unwrap();
    source
        .declarations
        .insert("declared".into(), Digest::hash(b"shared"));
    let mut inspection = core.inspect_declared_artifacts(source).unwrap();
    assert_eq!(
        inspection.read("declared").await.unwrap().as_bytes(),
        b"shared"
    );
    inspection.finish().await.unwrap();
}

#[tokio::test]
async fn original_envelope_bound_rejects_before_allocating_a_large_read_tail() {
    let (_temp, core, mut source) = fixture();
    fs::File::create(source.root.join("large"))
        .unwrap()
        .set_len(MAX_BYTES as u64 + 1)
        .unwrap();
    source
        .declarations
        .insert("large".into(), Digest::hash(b""));
    let mut inspection = core.inspect_declared_artifacts(source).unwrap();
    assert!(matches!(
        inspection.read("large").await,
        Err(NativeArtifactError::TooLarge)
    ));
    inspection.finish().await.unwrap();
}

#[tokio::test]
async fn abandoned_observer_keeps_actual_source_until_original_worker_joins() {
    let (_temp, core, source) = fixture();
    let dropped = source.dropped.clone();
    let inspection = core.inspect_declared_artifacts(source).unwrap();
    let held = inspection.work.clone();
    let cleanup = inspection.io.clone();
    let io = inspection.io.clone();
    let (entered, ready) = tokio::sync::oneshot::channel();
    let (release, released) = std::sync::mpsc::channel();
    let task = tokio::spawn(async move {
        io.inspect_retaining(
            held.project(|work| work as &(dyn std::any::Any + Send + Sync)),
            move |output| {
                let _source_custody = held;
                let _ = entered.send(());
                released.recv().unwrap();
                output.append(b"late")
            },
        )
        .await
    });
    ready.await.unwrap();
    task.abort();
    drop(inspection);
    assert_eq!(
        dropped.load(Ordering::SeqCst),
        0,
        "actual blocked worker still owns source"
    );
    release.send(()).unwrap();
    cleanup.retire_and_wait().await.unwrap();
    assert_eq!(
        dropped.load(Ordering::SeqCst),
        1,
        "only physical completion releases original source"
    );
}
