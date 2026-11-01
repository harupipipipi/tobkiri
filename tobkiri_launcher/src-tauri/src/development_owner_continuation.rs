//! Pure admission policy for one explicitly consented developer Native transition.
//!
//! This module performs no I/O and grants no access by itself. The Native adapter
//! must capture facts with no-follow handles, keep its exclusive ownership guard
//! across validation and use, and authenticate the exact payload with a private
//! current-session Native permit or a proven OS-backed durable authorization.
//! Renderer/Python values and unsealed receipts are never authentication. Existing Authority, VM, key and
//! budget checks still apply after selecting the existing development root.

use serde::{Deserialize, Serialize};
use std::path::{Component, Path, PathBuf};

/// Closed protocol version; unknown versions fail before seal verification.
pub const PROTOCOL_VERSION: u32 = 1;
const DOMAIN: &[u8] = b"Tobkiri/development-owner-continuation/v1\0";
const MAX_PATH_BYTES: usize = 4096;
const MAX_ANCESTORS: usize = 128;
const MAX_SEAL_BYTES: usize = 64 * 1024;
const MAX_PAYLOAD_BYTES: usize = 2 * 1024 * 1024;

/// Immutable identity captured from a no-follow filesystem handle.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
    pub uid: u32,
    /// Full native mode, including file type bits, captured without rewriting.
    pub mode: u32,
}

/// Only directories and ordinary executable files are admitted.
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub enum FileKind {
    Directory,
    RegularFile,
}

/// One member of a complete absolute root-to-leaf ancestry capture.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PathFact {
    pub path: PathBuf,
    /// Native resolution must equal the original path, including every ancestor.
    pub resolved_path: PathBuf,
    pub identity: FileIdentity,
    pub kind: FileKind,
    /// Includes symlinks, reparse points, and any other platform redirection.
    pub redirected: bool,
}

/// Native executable bytes and the immutable identity of their complete path.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecutableFacts {
    pub sha256: [u8; 32],
    pub ancestry: Vec<PathFact>,
}

/// Exact facts shown by Native and authenticated by its consent authorization.
///
/// The adapter must create a fresh random nonce only after explicit Native user
/// consent. A private in-memory permit expires with this Native process. Reuse
/// across launches requires separately proven durable OS authentication for the
/// exact new executable and scope. Another Native change needs another ceremony;
/// Pack changes within the authorized lifetime need no update.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ConsentFacts {
    pub version: u32,
    pub nonce: [u8; 32],
    pub workspace_root: PathBuf,
    pub effective_uid: u32,
    pub old_native: ExecutableFacts,
    pub new_native: ExecutableFacts,
    pub state_root: Vec<PathFact>,
    pub user_data: Vec<PathFact>,
}

/// Fresh identity of the currently executing Native, including PID reuse defense.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProcessFacts {
    pub pid: u32,
    /// Nonzero native process start identity; must not be a caller timestamp.
    pub start_identity: u64,
    pub uid: u32,
    pub executable_path: PathBuf,
    /// Running-code identity matched to the captured executable, not only its path.
    pub executable_sha256: [u8; 32],
    pub alive: bool,
}

/// Result of a fresh Native ownership scan while holding the ownership guard.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OwnershipState {
    /// Old Native and its Kernel/VM are stopped; no competing owner exists.
    QuiescentExclusive {
        user_data: PathBuf,
    },
    Busy,
    Uncertain,
}

/// Facts recaptured immediately before opening the continued development state.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FreshObservation {
    /// This is a compile/build provenance gate, never an environment override.
    pub development_build: bool,
    pub parallel_debug_instance: bool,
    pub facts: ConsentFacts,
    pub current_process: ProcessFacts,
    pub ownership: OwnershipState,
}

/// Exact scope supplied separately to the private Native authenticator.
#[derive(Clone, Copy, Debug)]
pub struct SealScope<'a> {
    pub new_native_sha256: &'a [u8; 32],
    pub state_root: &'a Path,
    pub user_data: &'a Path,
    pub current_pid: u32,
    pub current_start_identity: u64,
}

/// Native authenticator of explicit consent, never a client-provided flag.
///
/// A current-session implementation must hold a private permit created only by
/// the explicit Native confirmation path, match every `payload` byte, and bind
/// its permit to `scope.current_pid` and `scope.current_start_identity`. The permit
/// must never be serialized or exported to Python, renderer, environment, or IPC.
///
/// Durable implementations additionally need proven creator authentication and
/// exact-code access control from the OS, fresh scoped bookmarks, and revocation
/// checks. An exact-code Keychain read ACL alone does not authenticate who created
/// its bytes. Checksums, unsigned JSON, ambient secrets, and same-user readable
/// keys are insufficient. A successful call attests explicit Native consent for
/// the exact executable and scope; unavailable authentication must fail closed.
pub trait NativeSealVerifier {
    fn authenticate(
        &self,
        payload: &[u8],
        sealed_authorization: &[u8],
        scope: SealScope<'_>,
    ) -> Result<(), SealRejected>;
}

/// Authentication failed; details stay inside the Native adapter.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SealRejected;

/// Finite failure codes; none includes credentials, state contents, or seal bytes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reject {
    UnsupportedVersion,
    NotDevelopment,
    ParallelDebugInstance,
    InvalidNonce,
    InvalidDigest,
    UnchangedNative,
    InvalidPath,
    InvalidAncestry,
    RedirectedPath,
    InvalidFileIdentity,
    UnsafePermissions,
    WrongOwner,
    WrongDevelopmentLayout,
    FactsChanged,
    ProcessNotCurrent,
    OwnershipNotExclusive,
    InvalidSeal,
    Unauthenticated,
    PayloadTooLarge,
}

/// Narrow root-selection result, without VM/key/Authority/budget mutation rights.
#[derive(Debug, PartialEq, Eq)]
pub struct AuthorizedContinuation {
    state_root: PathBuf,
    user_data: PathBuf,
    new_native_sha256: [u8; 32],
}

impl AuthorizedContinuation {
    /// Existing development build root containing user_data and sibling setup data.
    pub fn state_root(&self) -> &Path {
        &self.state_root
    }

    /// Existing exact user_data directory; callers must retain the ownership guard.
    pub fn user_data(&self) -> &Path {
        &self.user_data
    }

    /// The only Native executable digest authorized by this consent.
    pub fn new_native_sha256(&self) -> &[u8; 32] {
        &self.new_native_sha256
    }
}

/// Encode the entire validated consent using bounded, unambiguous binary fields.
///
/// Integer fields use big endian; byte strings have an unsigned 32-bit byte length.
/// Arrays have a count followed by each encoded element. No parser or loose JSON
/// receipt participates in authorization; Native supplies independently captured
/// facts and verifies these exact bytes against its sealed authorization.
pub fn canonical_payload(facts: &ConsentFacts) -> Result<Vec<u8>, Reject> {
    validate_facts(facts)?;
    let mut out = Vec::new();
    out.extend_from_slice(DOMAIN);
    out.extend_from_slice(&facts.version.to_be_bytes());
    out.extend_from_slice(&facts.nonce);
    encode_path(&mut out, &facts.workspace_root)?;
    out.extend_from_slice(&facts.effective_uid.to_be_bytes());
    encode_executable(&mut out, &facts.old_native)?;
    encode_executable(&mut out, &facts.new_native)?;
    encode_ancestry(&mut out, &facts.state_root)?;
    encode_ancestry(&mut out, &facts.user_data)?;
    if out.len() > MAX_PAYLOAD_BYTES {
        return Err(Reject::PayloadTooLarge);
    }
    Ok(out)
}

/// Fail closed unless fresh Native observations and authenticated consent agree.
///
/// The adapter must hold the same exclusive ownership guard through subsequent
/// state access. `fresh` must come from Native OS observation immediately before
/// this call, including process liveness, file handles, and complete ancestry.
pub fn validate_authorization(
    consent: &ConsentFacts,
    sealed_authorization: &[u8],
    fresh: &FreshObservation,
    verifier: &impl NativeSealVerifier,
) -> Result<AuthorizedContinuation, Reject> {
    if !fresh.development_build {
        return Err(Reject::NotDevelopment);
    }
    if fresh.parallel_debug_instance {
        return Err(Reject::ParallelDebugInstance);
    }
    let payload = canonical_payload(consent)?;
    validate_facts(&fresh.facts)?;
    if fresh.facts != *consent {
        return Err(Reject::FactsChanged);
    }
    let current = &fresh.current_process;
    let executable_path = leaf(&consent.new_native.ancestry)?;
    if !current.alive
        || current.pid == 0
        || current.start_identity == 0
        || current.uid != consent.effective_uid
        || current.executable_path != executable_path
        || current.executable_sha256 != consent.new_native.sha256
    {
        return Err(Reject::ProcessNotCurrent);
    }
    let user_data = leaf(&consent.user_data)?;
    match &fresh.ownership {
        OwnershipState::QuiescentExclusive { user_data: locked } if locked == user_data => {}
        _ => return Err(Reject::OwnershipNotExclusive),
    }
    if sealed_authorization.is_empty() || sealed_authorization.len() > MAX_SEAL_BYTES {
        return Err(Reject::InvalidSeal);
    }
    verifier
        .authenticate(
            &payload,
            sealed_authorization,
            SealScope {
                new_native_sha256: &consent.new_native.sha256,
                state_root: leaf(&consent.state_root)?,
                user_data,
                current_pid: current.pid,
                current_start_identity: current.start_identity,
            },
        )
        .map_err(|_| Reject::Unauthenticated)?;
    Ok(AuthorizedContinuation {
        state_root: leaf(&consent.state_root)?.to_path_buf(),
        user_data: user_data.to_path_buf(),
        new_native_sha256: consent.new_native.sha256,
    })
}

fn validate_facts(facts: &ConsentFacts) -> Result<(), Reject> {
    if facts.version != PROTOCOL_VERSION {
        return Err(Reject::UnsupportedVersion);
    }
    if facts.nonce == [0; 32] {
        return Err(Reject::InvalidNonce);
    }
    if facts.old_native.sha256 == [0; 32] || facts.new_native.sha256 == [0; 32] {
        return Err(Reject::InvalidDigest);
    }
    if facts.old_native.sha256 == facts.new_native.sha256 {
        return Err(Reject::UnchangedNative);
    }
    clean_path(&facts.workspace_root)?;
    validate_ancestry(
        &facts.old_native.ancestry,
        FileKind::RegularFile,
        facts.effective_uid,
    )?;
    validate_ancestry(
        &facts.new_native.ancestry,
        FileKind::RegularFile,
        facts.effective_uid,
    )?;
    validate_ancestry(&facts.state_root, FileKind::Directory, facts.effective_uid)?;
    validate_ancestry(&facts.user_data, FileKind::Directory, facts.effective_uid)?;
    let expected = facts
        .workspace_root
        .join("tobkiri_launcher/src-tauri/target/dev-state/builds")
        .join(hex_digest(&facts.old_native.sha256))
        .join("user_data");
    if leaf(&facts.user_data)? != expected || Some(leaf(&facts.state_root)?) != expected.parent() {
        return Err(Reject::WrongDevelopmentLayout);
    }
    // The workspace must itself be present in the sealed state ancestry.
    if !facts
        .user_data
        .iter()
        .any(|fact| fact.path == facts.workspace_root)
    {
        return Err(Reject::WrongDevelopmentLayout);
    }
    // Shared path names cannot carry contradictory immutable observations.
    for left in [
        &facts.state_root,
        &facts.user_data,
        &facts.old_native.ancestry,
        &facts.new_native.ancestry,
    ] {
        for fact in left {
            for right in [
                &facts.state_root,
                &facts.user_data,
                &facts.old_native.ancestry,
                &facts.new_native.ancestry,
            ] {
                if right
                    .iter()
                    .any(|other| other.path == fact.path && other != fact)
                {
                    return Err(Reject::FactsChanged);
                }
            }
        }
    }
    Ok(())
}

fn validate_ancestry(
    ancestry: &[PathFact],
    leaf_kind: FileKind,
    effective_uid: u32,
) -> Result<(), Reject> {
    if ancestry.is_empty() || ancestry.len() > MAX_ANCESTORS {
        return Err(Reject::InvalidAncestry);
    }
    let path = leaf(ancestry)?;
    clean_path(path)?;
    let expected: Vec<_> = path.ancestors().collect();
    if expected.len() != ancestry.len() {
        return Err(Reject::InvalidAncestry);
    }
    for (index, (fact, expected_path)) in ancestry.iter().zip(expected.iter().rev()).enumerate() {
        clean_path(&fact.path)?;
        clean_path(&fact.resolved_path)?;
        if fact.path != *expected_path {
            return Err(Reject::InvalidAncestry);
        }
        if fact.redirected || fact.resolved_path != fact.path {
            return Err(Reject::RedirectedPath);
        }
        let is_leaf = index + 1 == ancestry.len();
        let kind = if is_leaf {
            leaf_kind
        } else {
            FileKind::Directory
        };
        let type_bits = match kind {
            FileKind::Directory => 0o040000,
            FileKind::RegularFile => 0o100000,
        };
        if fact.kind != kind
            || fact.identity.inode == 0
            || fact.identity.mode & 0o170000 != type_bits
        {
            return Err(Reject::InvalidFileIdentity);
        }
        if fact.identity.mode & 0o022 != 0 || fact.identity.mode & 0o7000 != 0 {
            return Err(Reject::UnsafePermissions);
        }
        if fact.identity.uid != effective_uid && (is_leaf || fact.identity.uid != 0) {
            return Err(Reject::WrongOwner);
        }
        if is_leaf && leaf_kind == FileKind::RegularFile && fact.identity.mode & 0o100 == 0 {
            return Err(Reject::UnsafePermissions);
        }
    }
    Ok(())
}

fn leaf(ancestry: &[PathFact]) -> Result<&Path, Reject> {
    ancestry
        .last()
        .map(|fact| fact.path.as_path())
        .ok_or(Reject::InvalidAncestry)
}

fn clean_path(path: &Path) -> Result<(), Reject> {
    let text = path.to_str().ok_or(Reject::InvalidPath)?;
    if !path.is_absolute() || text.len() > MAX_PATH_BYTES || text.contains('\0') {
        return Err(Reject::InvalidPath);
    }
    let mut rebuilt = PathBuf::new();
    for component in path.components() {
        match component {
            Component::RootDir | Component::Normal(_) => rebuilt.push(component.as_os_str()),
            _ => return Err(Reject::InvalidPath),
        }
    }
    if rebuilt.as_os_str() != path.as_os_str() {
        return Err(Reject::InvalidPath);
    }
    Ok(())
}

fn hex_digest(digest: &[u8; 32]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(64);
    for byte in digest {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 15) as usize] as char);
    }
    out
}

fn encode_path(out: &mut Vec<u8>, path: &Path) -> Result<(), Reject> {
    let bytes = path.to_str().ok_or(Reject::InvalidPath)?.as_bytes();
    out.extend_from_slice(&(bytes.len() as u32).to_be_bytes());
    out.extend_from_slice(bytes);
    Ok(())
}

fn encode_executable(out: &mut Vec<u8>, executable: &ExecutableFacts) -> Result<(), Reject> {
    out.extend_from_slice(&executable.sha256);
    encode_ancestry(out, &executable.ancestry)
}

fn encode_ancestry(out: &mut Vec<u8>, ancestry: &[PathFact]) -> Result<(), Reject> {
    out.extend_from_slice(&(ancestry.len() as u32).to_be_bytes());
    for fact in ancestry {
        encode_path(out, &fact.path)?;
        encode_path(out, &fact.resolved_path)?;
        out.extend_from_slice(&fact.identity.device.to_be_bytes());
        out.extend_from_slice(&fact.identity.inode.to_be_bytes());
        out.extend_from_slice(&fact.identity.uid.to_be_bytes());
        out.extend_from_slice(&fact.identity.mode.to_be_bytes());
        out.push(match fact.kind {
            FileKind::Directory => 1,
            FileKind::RegularFile => 2,
        });
        out.push(u8::from(fact.redirected));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    fn ancestry(path: &Path, kind: FileKind) -> Vec<PathFact> {
        let mut paths: Vec<_> = path.ancestors().collect();
        paths.reverse();
        paths
            .iter()
            .enumerate()
            .map(|(index, path)| {
                let leaf_kind = if index + 1 == paths.len() {
                    kind
                } else {
                    FileKind::Directory
                };
                // Every identical path has the same deterministic identity.
                let inode = path.to_str().unwrap().bytes().fold(1u64, |sum, byte| {
                    sum.wrapping_mul(31).wrapping_add(byte as u64)
                });
                PathFact {
                    path: path.to_path_buf(),
                    resolved_path: path.to_path_buf(),
                    identity: FileIdentity {
                        device: 1,
                        inode,
                        uid: if index < 2 { 0 } else { 501 },
                        mode: match leaf_kind {
                            FileKind::Directory => 0o040755,
                            FileKind::RegularFile => 0o100755,
                        },
                    },
                    kind: leaf_kind,
                    redirected: false,
                }
            })
            .collect()
    }

    fn fixture() -> (ConsentFacts, FreshObservation) {
        let workspace_root = PathBuf::from("/Users/developer/checkout");
        let state_root = workspace_root
            .join("tobkiri_launcher/src-tauri/target/dev-state/builds")
            .join(hex_digest(&[1; 32]));
        let facts = ConsentFacts {
            version: PROTOCOL_VERSION,
            nonce: [7; 32],
            workspace_root: workspace_root.clone(),
            effective_uid: 501,
            old_native: ExecutableFacts {
                sha256: [1; 32],
                ancestry: ancestry(
                    &workspace_root.join("old-target/debug/Tobkiri"),
                    FileKind::RegularFile,
                ),
            },
            new_native: ExecutableFacts {
                sha256: [2; 32],
                ancestry: ancestry(
                    &workspace_root.join("new-target/debug/Tobkiri"),
                    FileKind::RegularFile,
                ),
            },
            state_root: ancestry(&state_root, FileKind::Directory),
            user_data: ancestry(&state_root.join("user_data"), FileKind::Directory),
        };
        let fresh = FreshObservation {
            development_build: true,
            parallel_debug_instance: false,
            facts: facts.clone(),
            current_process: ProcessFacts {
                pid: 42,
                start_identity: 123456,
                uid: 501,
                executable_path: leaf(&facts.new_native.ancestry).unwrap().to_path_buf(),
                executable_sha256: facts.new_native.sha256,
                alive: true,
            },
            ownership: OwnershipState::QuiescentExclusive {
                user_data: leaf(&facts.user_data).unwrap().to_path_buf(),
            },
        };
        (facts, fresh)
    }

    struct ExactNativeVerifier {
        expected_payload: Vec<u8>,
        expected_sha: [u8; 32],
        expected_state: PathBuf,
        expected_data: PathBuf,
        calls: Cell<usize>,
    }

    impl ExactNativeVerifier {
        fn for_facts(facts: &ConsentFacts) -> Self {
            Self {
                expected_payload: canonical_payload(facts).unwrap(),
                expected_sha: facts.new_native.sha256,
                expected_state: leaf(&facts.state_root).unwrap().to_path_buf(),
                expected_data: leaf(&facts.user_data).unwrap().to_path_buf(),
                calls: Cell::new(0),
            }
        }
    }

    impl NativeSealVerifier for ExactNativeVerifier {
        fn authenticate(
            &self,
            payload: &[u8],
            seal: &[u8],
            scope: SealScope<'_>,
        ) -> Result<(), SealRejected> {
            self.calls.set(self.calls.get() + 1);
            if payload == self.expected_payload
                && seal == b"native-test-seal"
                && scope.new_native_sha256 == &self.expected_sha
                && scope.state_root == self.expected_state
                && scope.user_data == self.expected_data
            {
                Ok(())
            } else {
                Err(SealRejected)
            }
        }
    }

    fn admit(
        facts: &ConsentFacts,
        fresh: &FreshObservation,
    ) -> Result<AuthorizedContinuation, Reject> {
        validate_authorization(
            facts,
            b"native-test-seal",
            fresh,
            &ExactNativeVerifier::for_facts(&fixture().0),
        )
    }

    #[test]
    fn proven_durable_authorization_can_continue_same_state_across_restarts() {
        let (facts, mut fresh) = fixture();
        let first = admit(&facts, &fresh).unwrap();
        fresh.current_process.pid += 1;
        fresh.current_process.start_identity += 1;
        let restarted = admit(&facts, &fresh).unwrap();
        assert_eq!(first, restarted);
        assert_eq!(first.user_data(), leaf(&facts.user_data).unwrap());
        assert_eq!(first.state_root(), leaf(&facts.state_root).unwrap());
        assert_eq!(first.new_native_sha256(), &[2; 32]);
    }

    #[test]
    fn private_session_permit_cannot_replay_in_another_process_or_session() {
        struct SessionVerifier {
            native: ExactNativeVerifier,
            pid: u32,
            start_identity: u64,
            private_permit: [u8; 32],
        }
        impl NativeSealVerifier for SessionVerifier {
            fn authenticate(
                &self,
                payload: &[u8],
                permit: &[u8],
                scope: SealScope<'_>,
            ) -> Result<(), SealRejected> {
                if scope.current_pid != self.pid
                    || scope.current_start_identity != self.start_identity
                    || permit != self.private_permit
                {
                    return Err(SealRejected);
                }
                self.native
                    .authenticate(payload, b"native-test-seal", scope)
            }
        }
        let (facts, mut fresh) = fixture();
        let session = SessionVerifier {
            native: ExactNativeVerifier::for_facts(&facts),
            pid: fresh.current_process.pid,
            start_identity: fresh.current_process.start_identity,
            private_permit: [8; 32],
        };
        assert!(validate_authorization(&facts, &[8; 32], &fresh, &session).is_ok());
        assert_eq!(
            validate_authorization(&facts, &[9; 32], &fresh, &session),
            Err(Reject::Unauthenticated)
        );
        fresh.current_process.pid += 1;
        assert_eq!(
            validate_authorization(&facts, &[8; 32], &fresh, &session),
            Err(Reject::Unauthenticated)
        );
        fresh.current_process.pid = session.pid;
        fresh.current_process.start_identity += 1;
        assert_eq!(
            validate_authorization(&facts, &[8; 32], &fresh, &session),
            Err(Reject::Unauthenticated)
        );
    }

    #[test]
    fn canonical_encoding_is_stable_domain_separated_and_binds_nonce() {
        let (facts, _) = fixture();
        let payload = canonical_payload(&facts).unwrap();
        assert!(payload.starts_with(DOMAIN));
        assert_eq!(payload, canonical_payload(&facts).unwrap());
        assert_eq!(&payload[DOMAIN.len()..DOMAIN.len() + 4], &[0, 0, 0, 1]);
        let mut changed = facts.clone();
        changed.nonce[0] ^= 1;
        assert_ne!(payload, canonical_payload(&changed).unwrap());
    }

    #[test]
    fn unknown_receipt_fields_and_versions_are_rejected() {
        let (mut facts, _) = fixture();
        let mut json = serde_json::to_value(&facts).unwrap();
        json["approved"] = serde_json::Value::Bool(true);
        assert!(serde_json::from_value::<ConsentFacts>(json).is_err());
        let mut nested = serde_json::to_value(&facts).unwrap();
        nested["new_native"]["approved"] = serde_json::Value::Bool(true);
        assert!(serde_json::from_value::<ConsentFacts>(nested).is_err());
        facts.version += 1;
        assert_eq!(canonical_payload(&facts), Err(Reject::UnsupportedVersion));
    }

    #[test]
    fn non_development_and_debug_parallel_never_call_authenticator() {
        let (facts, mut fresh) = fixture();
        let verifier = ExactNativeVerifier::for_facts(&facts);
        fresh.development_build = false;
        assert_eq!(
            validate_authorization(&facts, b"seal", &fresh, &verifier),
            Err(Reject::NotDevelopment)
        );
        fresh.development_build = true;
        fresh.parallel_debug_instance = true;
        assert_eq!(
            validate_authorization(&facts, b"seal", &fresh, &verifier),
            Err(Reject::ParallelDebugInstance)
        );
        assert_eq!(verifier.calls.get(), 0);
    }

    #[test]
    fn unauthenticated_or_tampered_seal_cannot_authorize() {
        let (facts, fresh) = fixture();
        let verifier = ExactNativeVerifier::for_facts(&facts);
        for seal in [
            b"".as_slice(),
            b"approved=true",
            b"native-test-seal-changed",
        ] {
            assert!(validate_authorization(&facts, seal, &fresh, &verifier).is_err());
        }
        assert_eq!(
            validate_authorization(&facts, &vec![0; MAX_SEAL_BYTES + 1], &fresh, &verifier),
            Err(Reject::InvalidSeal)
        );
        let mut changed = facts.clone();
        changed.nonce[0] ^= 1;
        let mut changed_fresh = fresh.clone();
        changed_fresh.facts = changed.clone();
        assert_eq!(
            validate_authorization(&changed, b"native-test-seal", &changed_fresh, &verifier),
            Err(Reject::Unauthenticated)
        );
    }

    #[test]
    fn immutable_identity_changes_fail_before_seal_verification() {
        let (facts, original) = fixture();
        for field in 0..4 {
            let mut fresh = original.clone();
            let identity = &mut fresh.facts.user_data.last_mut().unwrap().identity;
            match field {
                0 => identity.device += 1,
                1 => identity.inode += 1,
                2 => identity.uid += 1,
                _ => identity.mode = 0o040700,
            }
            assert!(admit(&facts, &fresh).is_err());
        }
        let mut fresh = original;
        fresh.facts.new_native.sha256[0] ^= 1;
        assert_eq!(admit(&facts, &fresh), Err(Reject::FactsChanged));
    }

    #[test]
    fn redirected_or_missing_ancestors_are_rejected() {
        let (facts, _) = fixture();
        for index in 0..facts.user_data.len() {
            let mut changed = facts.clone();
            changed.user_data[index].redirected = true;
            assert_eq!(canonical_payload(&changed), Err(Reject::RedirectedPath));
            let mut changed = facts.clone();
            changed.user_data[index].resolved_path = PathBuf::from("/redirected");
            assert_eq!(canonical_payload(&changed), Err(Reject::RedirectedPath));
        }
        let mut changed = facts;
        changed.user_data.remove(1);
        assert_eq!(canonical_payload(&changed), Err(Reject::InvalidAncestry));
    }

    #[test]
    fn unsafe_owner_modes_and_nonregular_executables_are_rejected() {
        let (facts, _) = fixture();
        for mode in [0o100777, 0o104755, 0o100644] {
            let mut changed = facts.clone();
            changed
                .new_native
                .ancestry
                .last_mut()
                .unwrap()
                .identity
                .mode = mode;
            assert_eq!(canonical_payload(&changed), Err(Reject::UnsafePermissions));
        }
        let mut changed = facts.clone();
        changed.new_native.ancestry.last_mut().unwrap().identity.uid = 502;
        assert_eq!(canonical_payload(&changed), Err(Reject::WrongOwner));
        let mut changed = facts;
        changed.new_native.ancestry.last_mut().unwrap().kind = FileKind::Directory;
        assert_eq!(
            canonical_payload(&changed),
            Err(Reject::InvalidFileIdentity)
        );
    }

    #[test]
    fn origin_sha_layout_is_exact_and_installed_state_is_rejected() {
        let (facts, _) = fixture();
        for path in [
            PathBuf::from("/Users/developer/Library/Application Support/dev.rumiai.app/user_data"),
            leaf(&facts.state_root)
                .unwrap()
                .parent()
                .unwrap()
                .join(hex_digest(&[2; 32]))
                .join("user_data"),
        ] {
            let mut changed = facts.clone();
            changed.user_data = ancestry(&path, FileKind::Directory);
            changed.state_root = ancestry(path.parent().unwrap(), FileKind::Directory);
            assert_eq!(
                canonical_payload(&changed),
                Err(Reject::WrongDevelopmentLayout)
            );
        }
        let mut changed = facts;
        changed.state_root = ancestry(
            Path::new("/Users/developer/another-root"),
            FileKind::Directory,
        );
        assert_eq!(
            canonical_payload(&changed),
            Err(Reject::WrongDevelopmentLayout)
        );
    }

    #[test]
    fn path_spellings_and_unbounded_capture_are_rejected() {
        for path in [
            "relative",
            "/Users/../etc",
            "/Users/./developer",
            "//Users/developer",
            "/Users//developer",
            "/Users/developer/",
        ] {
            assert_eq!(
                clean_path(Path::new(path)),
                Err(Reject::InvalidPath),
                "{path}"
            );
        }
        let (mut facts, _) = fixture();
        facts.new_native.ancestry = vec![facts.user_data[0].clone(); MAX_ANCESTORS + 1];
        assert_eq!(canonical_payload(&facts), Err(Reject::InvalidAncestry));
    }

    #[test]
    fn invalid_nonce_digest_and_no_native_change_fail() {
        let (facts, _) = fixture();
        let mut changed = facts.clone();
        changed.nonce = [0; 32];
        assert_eq!(canonical_payload(&changed), Err(Reject::InvalidNonce));
        let mut changed = facts.clone();
        changed.new_native.sha256 = [0; 32];
        assert_eq!(canonical_payload(&changed), Err(Reject::InvalidDigest));
        let mut changed = facts;
        changed.new_native.sha256 = changed.old_native.sha256;
        assert_eq!(canonical_payload(&changed), Err(Reject::UnchangedNative));
    }

    #[test]
    fn current_process_liveness_identity_and_exact_binary_are_required() {
        let (facts, original) = fixture();
        for field in 0..6 {
            let mut fresh = original.clone();
            match field {
                0 => fresh.current_process.alive = false,
                1 => fresh.current_process.pid = 0,
                2 => fresh.current_process.start_identity = 0,
                3 => fresh.current_process.uid += 1,
                4 => fresh.current_process.executable_path = PathBuf::from("/other"),
                _ => fresh.current_process.executable_sha256 = [3; 32],
            }
            assert_eq!(admit(&facts, &fresh), Err(Reject::ProcessNotCurrent));
        }
    }

    #[test]
    fn busy_uncertain_and_different_scope_ownership_fail_closed() {
        let (facts, mut fresh) = fixture();
        for ownership in [
            OwnershipState::Busy,
            OwnershipState::Uncertain,
            OwnershipState::QuiescentExclusive {
                user_data: PathBuf::from("/another/user_data"),
            },
        ] {
            fresh.ownership = ownership;
            assert_eq!(admit(&facts, &fresh), Err(Reject::OwnershipNotExclusive));
        }
    }
}
