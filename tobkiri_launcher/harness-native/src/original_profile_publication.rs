//! The reached prefix of the OLD publication recovery, under its real owner.
//! No intent/key/path/SQL/callback is supplied by a guest. A verified intent is
//! a continuation boundary, never authority to publish or a successful review.
use super::*;
use crate::original_authority::json::{self, Document, Node, Text};
use tobkiri_canon::verify_seal_hex;
use zeroize::Zeroizing;

#[cfg(unix)]
#[path = "original_profile_publication_io.rs"]
mod publication_io;
#[cfg(unix)]
use publication_io::{Directory, PublicationLock, read_stream};
#[path = "original_profile_publication_model.rs"]
mod model;
use model::validate_predecessor;

const JOURNAL: &str = "bootstrap-publication.json";
const LOCK: &str = ".bootstrap-publication.lock";
const KEY: &str = ".publication-key";
const RAW_IO: &str = "tobkiri.native.PublicationRawIo";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Prefix {
    NoJournal,
    NoIntent,
    VerifiedIntent,
}

/// This carrier stays in Bootstrap, outside the caught execution future. Thus
/// panic/drop cannot hide its actual FD receipt or release uncertain custody.
pub(crate) struct PublicationRead {
    invocation: NativeSetupInvocation,
    cleanup: Arc<CleanupState>,
    calls: u8,
    _capacity: NativeSetupReservation,
    #[cfg(test)]
    remove_intent_after_lock: bool,
}
impl PublicationRead {
    pub(crate) fn new(
        invocation: &NativeSetupInvocation,
    ) -> std::result::Result<Self, OriginalAuthorityAdmission> {
        let control = invocation
            .control()
            .reserve(ControlKind::Context, 4096)
            .map_err(|_| OriginalAuthorityAdmission::Capacity {
                code: "native-control",
            })?;
        let capacity = invocation
            .reserve(CapacityKind::VfsBuffer, 2 * 1024 * 1024 + 16384)
            .map_err(|error| OriginalAuthorityAdmission::Capacity { code: error.code() })?;
        Ok(Self {
            invocation: invocation.clone(),
            cleanup: Arc::new(CleanupState::new(control)),
            calls: 0,
            _capacity: capacity,
            #[cfg(test)]
            remove_intent_after_lock: false,
        })
    }
    pub(crate) fn run<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        discard_superseded: bool,
    ) -> Result<Prefix> {
        if self.calls > 1 || discard_superseded != (self.calls == 0) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "publication recovery occurrence",
            ));
        }
        self.calls += 1;
        #[cfg(test)]
        let remove_intent_after_lock = std::mem::take(&mut self.remove_intent_after_lock);
        let op = Operation {
            owner,
            invocation: &self.invocation,
            cancellation,
            deadline: self
                .invocation
                .deadline()
                .map_err(|_| OriginalAuthorityError::unavailable())?,
            pid: std::process::id(),
            cleanup: self.cleanup.clone(),
            #[cfg(test)]
            fixture: None,
        };
        op.check(OriginalAuthorityStage::Source)?;
        // Retirement is applied after the actual body/close by the owning job.
        // Do not replace a prior original failure with a later shutdown flag.
        owner
            .with_original_review_user_data(|root| {
                prefix(
                    &op,
                    root,
                    #[cfg(test)]
                    remove_intent_after_lock,
                )
            })?
            .map_err(recovery_error)
    }
    #[cfg(test)]
    pub(crate) fn remove_intent_after_lock_for_fixture(&mut self) {
        self.remove_intent_after_lock = true;
    }
    pub(crate) fn receipt(&self) -> OriginalAuthorityCloseReceipt {
        self.cleanup.receipt()
    }
    pub(crate) fn uncertain_cleanup(&self) -> Option<OriginalAuthorityCleanup> {
        (!self.cleanup.verified()).then(|| OriginalAuthorityCleanup(self.cleanup.clone()))
    }
}

fn raw(class: &'static str, message: impl Into<String>) -> OriginalAuthorityError {
    let mut error = json::raw_error(class, message);
    error.stage = OriginalAuthorityStage::Source;
    error
}
fn value(message: &'static str) -> OriginalAuthorityError {
    raw("builtins.ValueError", message)
}
fn secure_error(message: &'static str) -> OriginalAuthorityError {
    raw(
        "tobkiri_protocol.secure_persistence.SecurePersistenceError",
        message,
    )
}
fn into_cause(mut error: OriginalAuthorityError) -> OriginalAuthorityCause {
    if error.class == RAW_IO {
        if let Some(OriginalAuthorityCause::Io(mut io)) = error.cause.take() {
            io.context = error.context.map(|error| Box::new(into_cause(*error)));
            return OriginalAuthorityCause::Io(io);
        }
    }
    OriginalAuthorityCause::Python {
        class: error.class,
        message: error.message,
        cause: error.cause.map(Box::new),
        context: error.context.map(|error| Box::new(into_cause(*error))),
        suppressed: error.suppressed,
    }
}
fn caused(
    mut error: OriginalAuthorityError,
    cause: OriginalAuthorityError,
) -> OriginalAuthorityError {
    error.cause = Some(into_cause(cause));
    error.suppressed = true;
    error
}
fn raw_io(error: std::io::Error, filename: Option<&str>) -> OriginalAuthorityError {
    OriginalAuthorityError {
        stage: OriginalAuthorityStage::Source,
        class: RAW_IO,
        message: String::new(),
        cause: Some(OriginalAuthorityCause::Io(OriginalAuthorityIoError {
            kind: error.kind(),
            errno: error.raw_os_error(),
            filename: filename.map(str::to_owned),
            context: None,
        })),
        context: None,
        suppressed: false,
    }
}
fn recovery_error(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == RAW_IO
        || matches!(
            error.class,
            "builtins.ValueError"
                | "tobkiri_protocol.errors.CanonicalizationError"
                | "tobkiri_protocol.secure_persistence.SecurePersistenceError"
                | "core_runtime.hmac_key_manager.SigningKeyError"
                | "core_runtime.active_profile_store_v4.ActiveProfileStoreError"
                | "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError"
                | "core_runtime.active_profile_store_v4.ActiveProfileStoreLockTimeout"
        )
    {
        caused(
            raw(
                crate::bootstrap_review::DENIED,
                "Profile publication recovery failed closed",
            ),
            error,
        )
    } else {
        error
    }
}

fn prefix<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    #[cfg(test)] remove_intent_after_lock: bool,
) -> Result<Prefix> {
    let _paths = op.reserve(
        OriginalAuthorityStage::Source,
        root.as_os_str()
            .len()
            .saturating_mul(16)
            .saturating_add(8192),
    )?;
    let profiles = root.join("profiles");
    let path = profiles.join(JOURNAL);
    // Original exists() suppresses inaccessible/non-directory branches; a
    // dangling symlink is included by the separate is_symlink() operation.
    op.check(OriginalAuthorityStage::Source)?;
    let exists = path.exists() || path.is_symlink();
    op.check(OriginalAuthorityStage::Source)?;
    if !exists {
        return Ok(Prefix::NoJournal);
    }
    #[cfg(unix)]
    {
        let directory = Directory::capture(op, &profiles, true)?;
        let mut guard = PublicationLock::acquire(op, &directory)?;
        #[cfg(test)]
        if remove_intent_after_lock {
            fs::remove_file(&path).map_err(|error| raw_io(error, path.to_str()))?;
        }
        let result = read_intent(op, &profiles);
        // Python skips post-yield validation for a body exception. The current
        // verified-intent continuation likewise stops at an unbound constructor.
        let result = if matches!(result, Ok(Prefix::NoIntent)) {
            finish_scope(
                result,
                directory.validate_file(op, LOCK, guard.file.as_ref().expect("publication lock")),
            )
        } else {
            result
        };
        let closed = guard.close();
        finish_scope(result, closed)
    }
    #[cfg(not(unix))]
    {
        Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "Windows publication persistence/ACL",
        ))
    }
}

#[cfg(unix)]
fn read_intent<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    profiles: &Path,
) -> Result<Prefix> {
    // read() independently constructs/captures its SecureDirectory, under lock.
    let directory = Directory::capture(op, profiles, true)?;
    if !directory.exists(op, JOURNAL)? {
        return Ok(Prefix::NoIntent);
    }
    let bytes = directory.read(op, JOURNAL)?;
    let mut doc = json::decode_strict(&bytes, op)?;
    let key_path = profiles.join(KEY);
    op.check(OriginalAuthorityStage::Key)?;
    let key_exists = key_path.exists();
    op.check(OriginalAuthorityStage::Key)?;
    if !key_exists {
        return Err(value("Profile publication signing key is unavailable"));
    }
    if !exact(&doc, doc.root, &["payload", "hmac"]) {
        return Err(value("Profile publication intent is invalid"));
    }
    let payload = doc.get(doc.root, "payload").expect("exact envelope");
    let hmac = doc.get(doc.root, "hmac").expect("exact envelope");
    if !exact(
        &doc,
        payload,
        &["schema", "predecessor", "candidate", "confirmation_digest"],
    ) || !doc
        .get(payload, "schema")
        .is_some_and(|id| doc.string_is(id, "io.tobkiri.profile-publication-intent.v1"))
        || !matches!(doc.node(hmac), Node::String(_))
    {
        return Err(value("Profile publication intent authentication failed"));
    }
    let key = signing_key(op, &key_path)?;
    let old_root = doc.root;
    doc.root = payload;
    let canonical = json::canonical(&doc, None, op)?;
    doc.root = old_root;
    let preimage = python_default_spacing(op, &canonical)?;
    let Node::String(signature) = doc.node(hmac) else {
        unreachable!("checked HMAC string")
    };
    let ascii = signature.0.iter().all(|&point| point <= 127);
    let signature_text = signature.utf8()?;
    let lowercase_hex = ascii
        && signature_text.len() == 64
        && signature_text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    // compute_data_hmac precedes compare_digest even for malformed/non-ASCII
    // expected text. A fixed well-formed mismatch lets the existing primitive
    // do that same HMAC once; invalid expected text can never become accepted.
    let expected = if lowercase_hex {
        &signature_text
    } else {
        "0000000000000000000000000000000000000000000000000000000000000000"
    };
    let matched = verify_seal_hex(&preimage, expected, &key).is_ok();
    if !ascii {
        return Err(raw(
            "builtins.TypeError",
            "comparing strings with non-ASCII characters is not supported",
        ));
    }
    // The existing verifier accepts exactly lowercase hex, like comparison
    // against compute_data_hmac's lowercase text, and uses constant-time HMAC.
    if !lowercase_hex || !matched {
        return Err(value("Profile publication intent authentication failed"));
    }
    let predecessor = doc.get(payload, "predecessor").expect("exact payload");
    if !matches!(doc.node(predecessor), Node::Scalar(CanonValue::Null)) {
        validate_predecessor(op, &mut doc, predecessor)?;
    }
    // candidate and confirmation_digest are deliberately not inspected here.
    Ok(Prefix::VerifiedIntent)
}

fn exact(doc: &Document, id: usize, names: &[&str]) -> bool {
    doc.entries(id).is_some_and(|pairs| {
        pairs.len() == names.len()
            && names
                .iter()
                .all(|name| pairs.iter().any(|(key, _)| key.is(name)))
    })
}
fn python_default_spacing<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    compact: &[u8],
) -> Result<Funded<Vec<u8>>> {
    let bound = compact
        .len()
        .checked_mul(2)
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
    let capacity = op.reserve(OriginalAuthorityStage::Key, bound)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(bound)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
    let mut string = false;
    let mut escaped = false;
    for &byte in compact {
        output.push(byte);
        if string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                string = false;
            }
        } else if byte == b'"' {
            string = true;
        } else if matches!(byte, b':' | b',') {
            output.push(b' ');
        }
    }
    Ok(Funded {
        value: output,
        _capacity: capacity,
    })
}

#[cfg(unix)]
fn signing_key<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    let fail = |message: &str| raw("core_runtime.hmac_key_manager.SigningKeyError", message);
    if path.is_symlink() {
        return Err(fail(&format!(
            "signing-key path is symlinked: {}",
            path.display()
        )));
    }
    let name = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    let descriptor = unsafe {
        libc::open(
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if descriptor < 0 {
        return Err(caused(
            fail("signing-key file is unreadable"),
            raw_io(std::io::Error::last_os_error(), path.to_str()),
        ));
    }
    let mut file = TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
    let result = (|| {
        let metadata = file.metadata().map_err(|error| raw_io(error, None))?;
        if !metadata.is_file() {
            return Err(fail("signing-key path is not a regular file"));
        }
        if metadata.uid() != unsafe { libc::getuid() } {
            return Err(fail("signing-key file is not owned by the Host user"));
        }
        if metadata.mode() & 0o077 != 0 {
            return Err(fail("signing-key file must have mode 0600"));
        }
        let data = read_stream(op, &mut file, 4096)?;
        let text = std::str::from_utf8(&data).map_err(|error| {
            let cause = json::decode(&data, op)
                .err()
                .and_then(|mut error| error.cause.take());
            let mut outer = fail("signing-key file is unreadable");
            outer.cause = cause.or_else(|| {
                Some(OriginalAuthorityCause::Unsupported(
                    "signing key Unicode projection",
                ))
            });
            outer.suppressed = true;
            let _ = error;
            outer
        })?;
        let text = text.trim_matches(python_whitespace);
        if text.chars().count() < 32 {
            return Err(fail("signing-key material is missing or weak"));
        }
        let capacity = op.reserve(OriginalAuthorityStage::Key, text.len())?;
        let mut key = Zeroizing::new(Vec::new());
        key.try_reserve_exact(text.len())
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
        key.extend_from_slice(text.as_bytes());
        Ok(Funded {
            value: key,
            _capacity: capacity,
        })
    })();
    finish_scope(result, file.close().map_err(|error| raw_io(error, None)))
}
fn python_whitespace(ch: char) -> bool {
    python_space(ch) || matches!(ch, '\u{001c}'..='\u{001f}')
}
