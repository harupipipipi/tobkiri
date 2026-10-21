//! Bounded, read-only Profile inspection for trusted Launcher callers.

use std::collections::BTreeSet;

use serde::Serialize;
use tobkiri_core::{Digest, Variant, parse};

use crate::HostError;

const MAX_CANDIDATE_BYTES: usize = crate::transport::MAX_HOST_RESPONSE_BODY_BYTES;
const MAX_CONFLICT_CODES: usize = 64;
const MAX_CONFLICT_CODE_BYTES: usize = 64;

/// Safe metadata about one persisted Profile revision.
///
/// This deliberately omits the Profile name, Pack sources, declarations, and
/// conflict text. It is an inspection summary, not the full Setup review UI.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProfileInspection {
    pub profile_id: tobkiri_core::ids::ProfileId,
    pub revision: Digest,
    pub pack_count: u32,
    pub publisher_count: u32,
    pub pack_limit_count: u32,
    pub binding_count: u32,
    pub overlay_order_count: u32,
    pub user_overlay_count: u32,
    pub startup_input_count: u32,
    pub has_startup_ui: bool,
}

/// Compact outcome of structural checking for an unsaved Profile candidate.
///
/// `canonical_digest` uses Core's persisted Profile revision recipe. It does
/// not mean that the candidate was saved, approved, or activated.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProfileCandidateCheck {
    pub canonical_digest: Digest,
    pub conflict_count: u32,
    pub conflict_codes: Vec<String>,
    pub conflict_codes_truncated: bool,
}

pub(crate) fn inspect(
    profile: &tobkiri_core::profile::Profile,
    revision: Digest,
) -> Result<ProfileInspection, HostError> {
    Ok(ProfileInspection {
        profile_id: profile.id.clone(),
        revision,
        pack_count: bounded_count(profile.packs.len())?,
        publisher_count: bounded_count(profile.publishers.len())?,
        pack_limit_count: bounded_count(profile.pack_limits.len())?,
        binding_count: bounded_count(profile.bindings.len())?,
        overlay_order_count: bounded_count(profile.overlay_order.len())?,
        user_overlay_count: bounded_count(profile.user_overlays.len())?,
        startup_input_count: bounded_count(profile.startup.inputs.len())?,
        has_startup_ui: profile.startup.ui.is_some(),
    })
}

pub(crate) fn check_candidate(
    canonical_digest: Digest,
    report: tobkiri_core::profile::ConflictReport,
) -> Result<ProfileCandidateCheck, HostError> {
    let conflict_count = bounded_count(report.conflicts.len())?;
    let mut codes = BTreeSet::new();
    let mut conflict_codes_truncated = false;
    for conflict in report.conflicts {
        if conflict.code.len() > MAX_CONFLICT_CODE_BYTES
            || !conflict.code.bytes().all(|byte| byte.is_ascii())
        {
            return Err(HostError::Core);
        }
        if !codes.contains(&conflict.code) {
            if codes.len() == MAX_CONFLICT_CODES {
                conflict_codes_truncated = true;
            } else {
                codes.insert(conflict.code);
            }
        }
    }
    let conflict_codes = codes.into_iter().collect();

    Ok(ProfileCandidateCheck {
        canonical_digest,
        conflict_count,
        conflict_codes,
        conflict_codes_truncated,
    })
}

pub(crate) fn candidate_digest(
    profile: &tobkiri_core::profile::Profile,
) -> Result<Digest, HostError> {
    canonical_profile_digest(profile)
}

/// Enforce the existing bounded host body limit before parsing or Pack reads.
pub(crate) fn check_candidate_size(bytes: &[u8]) -> Result<(), HostError> {
    if bytes.len() > MAX_CANDIDATE_BYTES {
        Err(HostError::Limit)
    } else {
        Ok(())
    }
}

fn bounded_count(count: usize) -> Result<u32, HostError> {
    u32::try_from(count).map_err(|_| HostError::Limit)
}

/// Match Core's `strict_bytes` recipe so this identity agrees with a saved
/// revision without writing a revision or head.
fn canonical_profile_digest(profile: &tobkiri_core::profile::Profile) -> Result<Digest, HostError> {
    let json = serde_json::to_vec(profile).map_err(|_| HostError::Core)?;
    if json.len() > MAX_CANDIDATE_BYTES {
        return Err(HostError::Limit);
    }
    let value = parse(&json, Variant::StrictV1).map_err(|_| HostError::Core)?;
    let canonical = tobkiri_core::encode(&value, Variant::StrictV1).map_err(|_| HostError::Core)?;
    if canonical.len() > MAX_CANDIDATE_BYTES {
        return Err(HostError::Limit);
    }
    Ok(Digest::hash(&canonical))
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use std::{collections::BTreeMap, path::Path};

    use async_trait::async_trait;
    use serde_json::{Value, json};
    use tobkiri_core::{
        ApprovalAnswer, ApprovalPrompt, Core, CoreConfig, HostError as IoError, HostIo,
        TrustedPrompt, UiEmbed,
    };

    use crate::{HostCaller, LauncherCore, TransportTickets};

    use super::*;

    const PROFILE_ID: &str = "prof_inspection_existing";

    #[derive(Default)]
    struct HostCalls {
        approval: AtomicUsize,
        display: AtomicUsize,
        terminal: AtomicUsize,
        embedded_ui: AtomicUsize,
        directory_picker: AtomicUsize,
    }

    impl HostCalls {
        fn snapshot(&self) -> [usize; 5] {
            [
                self.approval.load(Ordering::SeqCst),
                self.display.load(Ordering::SeqCst),
                self.terminal.load(Ordering::SeqCst),
                self.embedded_ui.load(Ordering::SeqCst),
                self.directory_picker.load(Ordering::SeqCst),
            ]
        }
    }

    // Test-only approval lets the fixture use Core::save_profile; it makes no
    // claim about the Launcher GUI approval or review experience.
    struct ApprovingHost(Arc<HostCalls>);

    #[async_trait]
    impl TrustedPrompt for ApprovingHost {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            self.0.approval.fetch_add(1, Ordering::SeqCst);
            ApprovalAnswer::Approve
        }
    }

    #[async_trait]
    impl HostIo for ApprovingHost {
        fn display(&self, _: &str, _: &tobkiri_core::CanonValue) {
            self.0.display.fetch_add(1, Ordering::SeqCst);
        }

        async fn terminal_read_line(&self, _: &str) -> Result<String, IoError> {
            self.0.terminal.fetch_add(1, Ordering::SeqCst);
            Err(IoError("unavailable".into()))
        }

        fn terminal_write(&self, _: &str) {
            self.0.terminal.fetch_add(1, Ordering::SeqCst);
        }

        fn embed_ui(&self, _: UiEmbed) -> Result<tobkiri_core::ids::UiSessionId, IoError> {
            self.0.embedded_ui.fetch_add(1, Ordering::SeqCst);
            Err(IoError("unavailable".into()))
        }

        async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
            self.0.directory_picker.fetch_add(1, Ordering::SeqCst);
            None
        }
    }

    fn trusted_caller() -> HostCaller<'static> {
        HostCaller {
            label: "core-host",
            url: "tauri://localhost/",
        }
    }

    fn untrusted_caller() -> HostCaller<'static> {
        HostCaller {
            label: "pack-ui",
            url: "tauri://localhost/",
        }
    }

    fn open_launcher(directory: &tempfile::TempDir, calls: Arc<HostCalls>) -> LauncherCore {
        LauncherCore::open_with_config_and_tickets(
            Arc::new(ApprovingHost(calls)),
            CoreConfig::for_test_data_root(directory.path().canonicalize().unwrap()),
            TransportTickets::default(),
        )
        .unwrap()
    }

    async fn launcher_fixture() -> (LauncherCore, tempfile::TempDir, Arc<HostCalls>) {
        let directory = tempfile::tempdir().unwrap();
        let calls = Arc::new(HostCalls::default());
        let launcher = open_launcher(&directory, calls.clone());
        (launcher, directory, calls)
    }

    async fn save_profile_with_core(
        directory: &tempfile::TempDir,
        profile: tobkiri_core::profile::Profile,
        calls: Arc<HostCalls>,
    ) -> Digest {
        let core = Core::open(
            CoreConfig::for_test_data_root(directory.path().canonicalize().unwrap()),
            Arc::new(ApprovingHost(calls)),
        )
        .unwrap();
        let revision = core.save_profile(profile, None).await.unwrap();
        drop(core);
        revision
    }

    fn base_profile(id: &str) -> Value {
        json!({
            "format":"tobkiri.profile/1", "id":id, "name":"private profile name",
            "packs":[], "publishers":{},
            "ceiling":{
                "wasm":{"fuel_per_call":100000,"memory_mib":8,"wall_ms":1000},
                "loops":{"max_iterations":8,"max_wall_ms":1000,"max_cost_units":100}
            },
            "pack_limits":{}, "bindings":{}, "overlay_order":[], "user_overlays":[],
            "startup":{"inputs":[],"ui":null}
        })
    }

    fn parsed_profile(document: &Value) -> tobkiri_core::profile::Profile {
        tobkiri_core::profile::parse_profile(&serde_json::to_vec(document).unwrap()).unwrap()
    }

    fn file_snapshot(root: &Path) -> BTreeMap<String, Digest> {
        fn visit(root: &Path, current: &Path, files: &mut BTreeMap<String, Digest>) {
            for entry in std::fs::read_dir(current).unwrap() {
                let entry = entry.unwrap();
                let path = entry.path();
                if entry.file_type().unwrap().is_dir() {
                    visit(root, &path, files);
                } else {
                    let relative = path
                        .strip_prefix(root)
                        .unwrap()
                        .to_string_lossy()
                        .into_owned();
                    files.insert(relative, Digest::hash(&std::fs::read(path).unwrap()));
                }
            }
        }

        let mut files = BTreeMap::new();
        visit(root, root, &mut files);
        files
    }

    fn local_data_pack(root: &std::path::Path) -> (String, String) {
        let pack_root = root.join("candidate-data-pack");
        std::fs::create_dir_all(pack_root.join("data")).unwrap();
        let payload = "candidate data only";
        std::fs::write(pack_root.join("data/value.txt"), payload).unwrap();
        let permissions =
            serde_json::to_value(parsed_profile(&base_profile("prof_permission_template")).ceiling)
                .unwrap();
        let manifest = json!({
            "format":"tobkiri.pack/1", "id":"local.profile-inspection-data",
            "version":"1.0.0", "publisher_key":null,
            "runtime":{"kind":"data"},
            "shapes":[], "ports":{"provides":[],"consumes":[],"untyped":[]},
            "flows":[], "overlays":[], "stores":[],
            "permissions":permissions,
            "files":[{
                "path":"data/value.txt",
                "sha256":Digest::hash(payload.as_bytes()).to_string(),
                "size":payload.len()
            }]
        });
        std::fs::write(
            pack_root.join("pack.json"),
            serde_json::to_vec(&manifest).unwrap(),
        )
        .unwrap();
        let verified =
            tobkiri_core::pack::verify_directory(&pack_root, &Default::default()).unwrap();
        (
            verified.hash.to_string(),
            format!("file:{}", pack_root.display()),
        )
    }

    #[tokio::test]
    async fn inspection_returns_only_bounded_metadata_for_the_current_revision() {
        let directory = tempfile::tempdir().unwrap();
        let calls = Arc::new(HostCalls::default());
        let (pack_hash, pack_source) = local_data_pack(directory.path());
        let mut document = base_profile(PROFILE_ID);
        document["packs"] = json!([{
            "id":"local.profile-inspection-data", "version":"1.0.0",
            "sha256":pack_hash, "source":pack_source.clone(),
        }]);
        document["pack_limits"] = json!({
            "local.profile-inspection-data":document["ceiling"]
        });
        let profile = parsed_profile(&document);
        let expected_revision = save_profile_with_core(&directory, profile, calls.clone()).await;
        let launcher = open_launcher(&directory, calls);

        let inspection = launcher
            .inspect_profile(trusted_caller(), PROFILE_ID)
            .unwrap();
        assert_eq!(inspection.profile_id.as_str(), PROFILE_ID);
        assert_eq!(inspection.revision, expected_revision);
        assert_eq!(inspection.pack_count, 1);
        assert_eq!(inspection.startup_input_count, 0);

        let serialized = serde_json::to_string(&inspection).unwrap();
        assert!(!serialized.contains("private profile name"));
        assert!(!serialized.contains(&pack_source));
        assert!(!serialized.contains("candidate-data-pack"));
    }

    #[tokio::test]
    async fn untrusted_inspection_is_denied_before_profile_load() {
        let (launcher, _, _) = launcher_fixture().await;
        let result = launcher.inspect_profile(untrusted_caller(), "prof_does_not_exist");
        assert!(matches!(result, Err(HostError::Caller)));
    }

    #[tokio::test]
    async fn candidate_check_rejects_malformed_and_oversized_input() {
        let (launcher, _, _) = launcher_fixture().await;
        assert!(matches!(
            launcher.check_profile_candidate(trusted_caller(), b"{"),
            Err(HostError::Invalid)
        ));
        let oversized = vec![b' '; MAX_CANDIDATE_BYTES + 1];
        assert!(matches!(
            launcher.check_profile_candidate(trusted_caller(), &oversized),
            Err(HostError::Limit)
        ));
        assert!(matches!(
            launcher.check_profile_candidate(untrusted_caller(), &oversized),
            Err(HostError::Caller)
        ));
    }

    #[test]
    fn candidate_check_summary_caps_codes_and_omits_conflict_details() {
        let profile = parsed_profile(&base_profile(PROFILE_ID));
        let report: tobkiri_core::profile::ConflictReport = serde_json::from_value(json!({
            "conflicts": (0..70)
                .map(|index| json!({
                    "code":format!("conflict-{index:02}"),
                    "message":"private conflict detail",
                    "packs":["local.private-pack"]
                }))
                .collect::<Vec<_>>(),
            "repair_pack_available":true
        }))
        .unwrap();

        let summary = check_candidate(candidate_digest(&profile).unwrap(), report).unwrap();
        assert_eq!(summary.conflict_count, 70);
        assert_eq!(summary.conflict_codes.len(), MAX_CONFLICT_CODES);
        assert!(summary.conflict_codes_truncated);
        let serialized = serde_json::to_string(&summary).unwrap();
        assert!(!serialized.contains("private conflict detail"));
        assert!(!serialized.contains("local.private-pack"));
    }

    #[tokio::test]
    async fn candidate_check_is_read_only_and_does_not_start_or_approve() {
        let directory = tempfile::tempdir().unwrap();
        let calls = Arc::new(HostCalls::default());
        let (pack_hash, pack_source) = local_data_pack(directory.path());
        let mut candidate_document = base_profile(PROFILE_ID);
        candidate_document["name"] = json!("candidate private name");
        candidate_document["packs"] = json!([{
            "id":"local.profile-inspection-data", "version":"1.0.0",
            "sha256":pack_hash.clone(), "source":pack_source.clone(),
        }]);
        candidate_document["pack_limits"] = json!({
            "local.profile-inspection-data":candidate_document["ceiling"]
        });
        let candidate_json = serde_json::to_vec(&candidate_document).unwrap();
        let candidate_profile = tobkiri_core::profile::parse_profile(&candidate_json).unwrap();
        let expected_candidate_digest = candidate_digest(&candidate_profile).unwrap();
        let saved_revision =
            save_profile_with_core(&directory, candidate_profile.clone(), calls.clone()).await;
        let launcher = open_launcher(&directory, calls.clone());
        let profile_root = directory.path().join("profiles").join(PROFILE_ID);
        let files_before = file_snapshot(&profile_root);
        let host_calls_before = calls.snapshot();

        let result = launcher
            .check_profile_candidate(trusted_caller(), &candidate_json)
            .unwrap();
        assert_eq!(result.canonical_digest, expected_candidate_digest);
        assert_eq!(result.canonical_digest, saved_revision);
        assert_eq!(result.conflict_count, 0);
        assert!(result.conflict_codes.is_empty());

        let after = launcher
            .inspect_profile(trusted_caller(), PROFILE_ID)
            .unwrap();
        assert_eq!(after.revision, saved_revision);
        assert_eq!(after.pack_count, 1);
        assert_eq!(file_snapshot(&profile_root), files_before);
        assert_eq!(calls.snapshot(), host_calls_before);
        assert_eq!(launcher.current_generation(), None);

        let serialized = serde_json::to_string(&result).unwrap();
        assert!(!serialized.contains(&pack_source));
        assert!(!serialized.contains("candidate private name"));
        assert!(!serialized.contains("data/value.txt"));
    }
}
