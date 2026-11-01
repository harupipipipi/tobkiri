//! Explicit Native selection of an existing local development Host owner.
//!
//! An ad-hoc build has no provisioned Keychain access group. Its consent stays
//! inside this process: no filesystem flag or legacy Keychain item can silently
//! authorize the next executable or a later launch. Pack replacements during the
//! launch retain this owner and do not repeat this bootstrap ceremony.

use crate::config::AppConfig;
use anyhow::{bail, Context, Result};

pub(crate) const CONTINUE_ARGUMENT: &str = "--tobkiri-continue-development-owner";

/// Refuse unsupported requests before caches or single-instance handling run.
pub(crate) fn preflight_requested_mode(
    app_identifier: &str,
    parallel_debug_instance: bool,
) -> Result<()> {
    if std::env::args_os().any(|arg| arg == CONTINUE_ARGUMENT)
        && (!cfg!(all(target_os = "macos", debug_assertions))
            || option_env!("TOBKIRI_LOCAL_DEV_WORKSPACE") != Some("1")
            || app_identifier != "dev.tobkiri.local-launcher"
            || parallel_debug_instance)
    {
        bail!("Existing Host continuation requires a single local macOS development Launcher");
    }
    Ok(())
}

#[cfg(all(target_os = "macos", debug_assertions))]
pub(crate) use macos::OwnerLease;

#[cfg(not(all(target_os = "macos", debug_assertions)))]
pub(crate) struct OwnerLease;

/// Resolve an explicitly selected owner before any Host state is created.
pub(crate) fn resolve_before_state_writes(
    config: &mut AppConfig,
    app_identifier: &str,
    parallel_debug_instance: bool,
) -> Result<Option<OwnerLease>> {
    preflight_requested_mode(app_identifier, parallel_debug_instance)?;
    let requested = std::env::args_os().any(|arg| arg == CONTINUE_ARGUMENT);
    if !requested {
        return Ok(None);
    }
    if parallel_debug_instance
        || !config.is_dev_workspace()
        || option_env!("TOBKIRI_LOCAL_DEV_WORKSPACE") != Some("1")
    {
        bail!("Existing Host continuation requires a single local development Launcher");
    }
    #[cfg(all(target_os = "macos", debug_assertions))]
    {
        macos::continue_owner(config).map(Some)
    }
    #[cfg(not(all(target_os = "macos", debug_assertions)))]
    {
        bail!("Existing development Host continuation is available only on macOS");
    }
}

#[cfg(all(target_os = "macos", debug_assertions))]
mod macos {
    use super::*;
    use crate::development_owner_continuation::{
        canonical_payload, validate_authorization, ConsentFacts, ExecutableFacts, FileIdentity,
        FileKind, FreshObservation, NativeSealVerifier, OwnershipState, PathFact, ProcessFacts,
        SealRejected, SealScope, PROTOCOL_VERSION,
    };
    use crate::process_utils;
    use objc2::rc::Retained;
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSAlertFirstButtonReturn, NSModalResponseOK, NSOpenPanel, NSScreen};
    use objc2_foundation::{NSArray, NSString, NSURL};
    use rand::RngCore;
    use sha2::{Digest, Sha256};
    use std::ffi::CString;
    use std::fs::File;
    use std::io::Read;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::MetadataExt;
    use std::path::{Path, PathBuf};
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };
    use std::time::{Duration, Instant};

    const MAX_EXECUTABLE_BYTES: u64 = 128 * 1024 * 1024;
    const REVIEW_LIFETIME: Duration = Duration::from_secs(240);

    /// Keep the selected OS URLs, no-follow handles and exclusive owner lock alive.
    pub(crate) struct OwnerLease {
        _selected_access: Vec<crate::development_owner_os::PickerSessionAccess>,
        _path_handles: Vec<File>,
        _ownership_guard: File,
    }

    /// A permit created only by the Native button handler in this process.
    struct ConfirmedSession {
        payload: Vec<u8>,
        nonce: [u8; 32],
        process: ProcessFacts,
        state_root: PathBuf,
        user_data: PathBuf,
    }

    impl NativeSealVerifier for ConfirmedSession {
        fn authenticate(
            &self,
            payload: &[u8],
            sealed_authorization: &[u8],
            scope: SealScope<'_>,
        ) -> std::result::Result<(), SealRejected> {
            if payload != self.payload
                || sealed_authorization != self.nonce
                || scope.new_native_sha256 != &self.process.executable_sha256
                || scope.state_root != self.state_root
                || scope.user_data != self.user_data
                || scope.current_pid != self.process.pid
                || scope.current_start_identity != self.process.start_identity
                || current_process(
                    &self.process.executable_path,
                    self.process.executable_sha256,
                )
                .ok()
                .as_ref()
                    != Some(&self.process)
            {
                return Err(SealRejected);
            }
            Ok(())
        }
    }

    pub(super) fn continue_owner(config: &mut AppConfig) -> Result<OwnerLease> {
        let mtm = MainThreadMarker::new()
            .context("Existing Host review must run on the Native main thread")?;
        let workspace = config
            .dev_workspace_root
            .as_ref()
            .context("Existing Host review requires a development workspace")?
            .clone();
        // These Native pickers run directly on the Ready callback's main thread.
        // Dispatching to that same thread and waiting would deadlock startup.
        let state_url = pick_directory(mtm,
            "Continue an existing Tobkiri development Host",
            "Choose its build folder containing user_data and logs. The previous Launcher and its managed runtime must be stopped.",
            None)?;
        let state_root = selected_path(&state_url)?;
        let state_access = crate::development_owner_os::retain_picker_url_for_session(&state_url)
            .map_err(anyhow::Error::msg)?;
        let user_data = state_root.join("user_data");
        let bundle_url = pick_directory(mtm,
            "Select the previous Tobkiri Launcher",
            "Choose the preserved previous Developer.app. Its executable must match the selected Host folder's SHA256 name.",
            Some("app"))?;
        let bundle_root = selected_path(&bundle_url)?;
        let bundle_access = crate::development_owner_os::retain_picker_url_for_session(&bundle_url)
            .map_err(anyhow::Error::msg)?;
        if bundle_root.extension().and_then(|x| x.to_str()) != Some("app") {
            bail!("Previous Launcher selection must be an app bundle");
        }
        let old_executable = bundle_root.join("Contents/MacOS/tobkiri-launcher");
        let new_executable = std::env::current_exe()?;
        let mut handles = Vec::new();
        let mut nonce = [0_u8; 32];
        rand::thread_rng().fill_bytes(&mut nonce);
        let facts = capture_facts(
            &workspace,
            &state_root,
            &user_data,
            &old_executable,
            &new_executable,
            nonce,
            &mut handles,
        )?;
        let payload = canonical_payload(&facts)
            .map_err(|reason| anyhow::anyhow!("Existing Host facts were rejected: {reason:?}"))?;
        verify_previous_bundle(&bundle_root)?;
        crate::development_owner_os::verify_current_executable(&hex::encode(
            facts.new_native.sha256,
        ))
        .map_err(anyhow::Error::msg)?;
        let state_identity = &facts
            .state_root
            .last()
            .context("Host root identity is missing")?
            .identity;
        // The retained inode lock excludes cooperating continuation attempts.
        // Tauri's single-instance plugin can fail open and is not ownership
        // evidence: require stopped predecessor processes independently, and
        // reject parallel debug mode before entering this review.
        let guard = acquire_owner_guard(&handles, state_identity)?;
        require_quiescent_owner(&state_root, &old_executable)?;
        let process = current_process(&new_executable, facts.new_native.sha256)?;
        let started = Instant::now();
        let detail = serde_json::to_string_pretty(&facts)?;
        let screen = NSScreen::mainScreen(mtm).context("Native review screen is unavailable")?;
        let alert = crate::native_plan_review::build_alert(
            mtm, screen.visibleFrame().size,
            "Continue this Tobkiri Host for this launch",
            "This exact Launcher will use the selected existing VM, keys, conversations, permissions and remaining budget in place. Nothing is copied or reset. Pack updates during this launch keep that owner. This ad-hoc development build cannot securely remember consent across Launcher restarts; a later launch must be confirmed again. Cancel leaves the Host untouched.",
            "Continue this Host", &detail).map_err(anyhow::Error::msg)?;
        if !review_confirmed(mtm, alert)? || started.elapsed() > REVIEW_LIFETIME {
            bail!("Existing Host continuation was cancelled or expired");
        }
        // The permit exists only after this Native response, never from a flag,
        // renderer payload, Python claim, disk receipt or selected URL alone.
        let permit = ConfirmedSession {
            payload,
            nonce,
            process,
            state_root: state_root.clone(),
            user_data: user_data.clone(),
        };
        let fresh = capture_facts(
            &workspace,
            &state_root,
            &user_data,
            &old_executable,
            &new_executable,
            nonce,
            &mut handles,
        )?;
        verify_previous_bundle(&bundle_root)?;
        crate::development_owner_os::verify_current_executable(&hex::encode(
            fresh.new_native.sha256,
        ))
        .map_err(anyhow::Error::msg)?;
        require_quiescent_owner(&state_root, &old_executable)?;
        match crate::development_owner_process::inspect_owner_processes(
            &state_root,
            &user_data,
            &old_executable,
        ) {
            crate::development_owner_process::OwnerProcessState::Clear => {}
            crate::development_owner_process::OwnerProcessState::Busy => {
                bail!("A previous managed Host process is still running")
            }
            crate::development_owner_process::OwnerProcessState::Uncertain => {
                bail!("Previous managed Host processes could not be verified")
            }
        }
        let observed = FreshObservation {
            development_build: true,
            parallel_debug_instance: false,
            current_process: current_process(&new_executable, fresh.new_native.sha256)?,
            ownership: OwnershipState::QuiescentExclusive {
                user_data: user_data.clone(),
            },
            facts: fresh,
        };
        let authorized =
            validate_authorization(&facts, &nonce, &observed, &permit).map_err(|reason| {
                anyhow::anyhow!("Existing Host authorization was rejected: {reason:?}")
            })?;
        if started.elapsed() > REVIEW_LIFETIME {
            bail!("Existing Host review expired before continuation could be committed");
        }
        validate_named_paths(&observed.facts, &guard)?;
        config.continue_development_owner(&authorized)?;
        Ok(OwnerLease {
            _selected_access: vec![state_access, bundle_access],
            _path_handles: handles,
            _ownership_guard: guard,
        })
    }

    fn pick_directory(
        mtm: MainThreadMarker,
        title: &str,
        message: &str,
        file_type: Option<&str>,
    ) -> Result<Retained<NSURL>> {
        let panel = NSOpenPanel::openPanel(mtm);
        panel.setTitle(Some(&NSString::from_str(title)));
        panel.setMessage(Some(&NSString::from_str(message)));
        #[allow(deprecated)]
        panel.setAllowedFileTypes(
            file_type
                .map(|kind| NSArray::from_retained_slice(&[NSString::from_str(kind)]))
                .as_deref(),
        );
        panel.setCanChooseFiles(file_type.is_some());
        panel.setCanChooseDirectories(file_type.is_none());
        panel.setTreatsFilePackagesAsDirectories(false);
        panel.setAllowsMultipleSelection(false);
        panel.setCanCreateDirectories(false);
        let finished = Arc::new(AtomicBool::new(false));
        let expired = Arc::new(AtomicBool::new(false));
        let owned = Arc::new(dispatch2::MainThreadBound::new(panel.clone(), mtm));
        let watcher_owned = Arc::clone(&owned);
        let watcher_finished = finished.clone();
        let watcher_expired = expired.clone();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let watcher = std::thread::spawn(move || {
            if matches!(
                done_rx.recv_timeout(REVIEW_LIFETIME),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout)
            ) {
                watcher_expired.store(true, Ordering::Release);
                dispatch2::DispatchQueue::main().exec_async(move || {
                    if !watcher_finished.load(Ordering::Acquire) {
                        if let Some(marker) = MainThreadMarker::new() {
                            unsafe {
                                watcher_owned.get(marker).cancel(None);
                            }
                        }
                    }
                });
            }
        });
        let response = panel.runModal();
        finished.store(true, Ordering::Release);
        let _ = done_tx.send(());
        let _ = watcher.join();
        // MainThreadBound may synchronously return destruction to this thread.
        // Keep its last reference here until the watcher has finished joining.
        drop(owned);
        if response != NSModalResponseOK || expired.load(Ordering::Acquire) {
            bail!("Existing Host selection was cancelled");
        }
        let urls = panel.URLs();
        if urls.len() != 1 {
            bail!("Existing Host review requires exactly one selected folder");
        }
        Ok(urls.objectAtIndex(0))
    }

    fn review_confirmed(
        mtm: MainThreadMarker,
        alert: Retained<objc2_app_kit::NSAlert>,
    ) -> Result<bool> {
        let finished = Arc::new(AtomicBool::new(false));
        let expired = Arc::new(AtomicBool::new(false));
        let owned = Arc::new(dispatch2::MainThreadBound::new(alert.clone(), mtm));
        let watcher_owned = Arc::clone(&owned);
        let watcher_finished = finished.clone();
        let watcher_expired = expired.clone();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let watcher = std::thread::spawn(move || {
            if matches!(
                done_rx.recv_timeout(REVIEW_LIFETIME),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout)
            ) {
                watcher_expired.store(true, Ordering::Release);
                dispatch2::DispatchQueue::main().exec_async(move || {
                    if !watcher_finished.load(Ordering::Acquire) {
                        if let Some(marker) = MainThreadMarker::new() {
                            let app = objc2_app_kit::NSApplication::sharedApplication(marker);
                            let window = watcher_owned.get(marker).window();
                            if app.modalWindow().is_some_and(|active| {
                                Retained::as_ptr(&active) == Retained::as_ptr(&window)
                            }) {
                                app.stopModalWithCode(objc2_app_kit::NSModalResponseCancel);
                                window.orderOut(None);
                            }
                        }
                    }
                });
            }
        });
        let response = alert.runModal();
        finished.store(true, Ordering::Release);
        let _ = done_tx.send(());
        let _ = watcher.join();
        drop(owned);
        Ok(response == NSAlertFirstButtonReturn && !expired.load(Ordering::Acquire))
    }

    fn selected_path(url: &NSURL) -> Result<PathBuf> {
        if !url.isFileURL() {
            bail!("Existing Host review requires a local file URL");
        }
        let path = PathBuf::from(
            url.path()
                .context("Selected folder path is unavailable")?
                .to_string(),
        );
        if path.canonicalize()? != path {
            bail!("Existing Host selection cannot contain redirected paths");
        }
        Ok(path)
    }

    fn capture_facts(
        workspace: &Path,
        state_root: &Path,
        user_data: &Path,
        old_executable: &Path,
        new_executable: &Path,
        nonce: [u8; 32],
        handles: &mut Vec<File>,
    ) -> Result<ConsentFacts> {
        Ok(ConsentFacts {
            version: PROTOCOL_VERSION,
            nonce,
            workspace_root: workspace.to_path_buf(),
            effective_uid: unsafe { libc::geteuid() },
            old_native: capture_executable(old_executable, handles)?,
            new_native: capture_executable(new_executable, handles)?,
            state_root: capture_path(state_root, FileKind::Directory, handles)?,
            user_data: capture_path(user_data, FileKind::Directory, handles)?,
        })
    }

    fn capture_executable(path: &Path, handles: &mut Vec<File>) -> Result<ExecutableFacts> {
        let ancestry = capture_path(path, FileKind::RegularFile, handles)?;
        let file = handles
            .last_mut()
            .context("Native executable handle is unavailable")?;
        let before = file.metadata()?;
        if before.len() > MAX_EXECUTABLE_BYTES {
            bail!("Native executable exceeds its inspection limit");
        }
        let mut hasher = Sha256::new();
        let mut total = 0_u64;
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let length = file.read(&mut buffer)?;
            if length == 0 {
                break;
            }
            total = total
                .checked_add(length as u64)
                .context("Native executable grew during inspection")?;
            if total > before.len() || total > MAX_EXECUTABLE_BYTES {
                bail!("Native executable grew during inspection");
            }
            hasher.update(&buffer[..length]);
        }
        let after = file.metadata()?;
        let named = std::fs::symlink_metadata(path)?;
        if total != before.len()
            || executable_identity(&before) != executable_identity(&after)
            || executable_identity(&after) != executable_identity(&named)
            || named.file_type().is_symlink()
        {
            bail!("Native executable changed during inspection");
        }
        Ok(ExecutableFacts {
            sha256: hasher.finalize().into(),
            ancestry,
        })
    }

    fn executable_identity(
        metadata: &std::fs::Metadata,
    ) -> (u64, u64, u32, u32, u64, i64, i64, i64, i64) {
        (
            metadata.dev(),
            metadata.ino(),
            metadata.uid(),
            metadata.mode(),
            metadata.len(),
            metadata.mtime(),
            metadata.mtime_nsec(),
            metadata.ctime(),
            metadata.ctime_nsec(),
        )
    }

    fn capture_path(
        path: &Path,
        leaf_kind: FileKind,
        handles: &mut Vec<File>,
    ) -> Result<Vec<PathFact>> {
        if !path.is_absolute() || path.canonicalize()? != path {
            bail!("Host ownership path is not an unredirected absolute path");
        }
        let paths: Vec<_> = path.ancestors().collect();
        let mut facts = Vec::new();
        let mut parent_fd = None;
        for (index, item) in paths.iter().rev().enumerate() {
            let kind = if index + 1 == paths.len() {
                leaf_kind
            } else {
                FileKind::Directory
            };
            let flags = libc::O_RDONLY
                | libc::O_NOFOLLOW
                | libc::O_CLOEXEC
                | if kind == FileKind::Directory {
                    libc::O_DIRECTORY
                } else {
                    0
                };
            let name = CString::new(if parent_fd.is_some() {
                item.file_name()
                    .context("Host ownership component is invalid")?
                    .as_bytes()
            } else {
                item.as_os_str().as_bytes()
            })?;
            let fd = unsafe {
                match parent_fd {
                    Some(parent) => libc::openat(parent, name.as_ptr(), flags),
                    None => libc::open(name.as_ptr(), flags),
                }
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error())
                    .context("Host ownership path could not be inspected");
            }
            let file = unsafe { File::from_raw_fd(fd) };
            let metadata = file.metadata()?;
            let named = std::fs::symlink_metadata(item)?;
            if metadata.dev() != named.dev()
                || metadata.ino() != named.ino()
                || named.file_type().is_symlink()
            {
                bail!("Host ownership path changed during inspection");
            }
            facts.push(PathFact {
                path: item.to_path_buf(),
                resolved_path: item.to_path_buf(),
                identity: FileIdentity {
                    device: metadata.dev(),
                    inode: metadata.ino(),
                    uid: metadata.uid(),
                    mode: metadata.mode(),
                },
                kind,
                redirected: false,
            });
            handles.push(file);
            parent_fd = Some(fd);
        }
        if path.canonicalize()? != path {
            bail!("Host ownership path changed during inspection");
        }
        Ok(facts)
    }

    fn acquire_owner_guard(handles: &[File], expected: &FileIdentity) -> Result<File> {
        let held = handles
            .iter()
            .find(|handle| {
                handle.metadata().ok().is_some_and(|metadata| {
                    metadata.dev() == expected.device
                        && metadata.ino() == expected.inode
                        && metadata.uid() == expected.uid
                        && metadata.mode() == expected.mode
                })
            })
            .context("Captured Host owner handle is unavailable")?;
        let guard = held.try_clone()?;
        if unsafe { libc::flock(guard.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            bail!("Another Launcher is using the selected Host owner");
        }
        Ok(guard)
    }

    fn validate_named_paths(facts: &ConsentFacts, guard: &File) -> Result<()> {
        for ancestry in [
            &facts.state_root,
            &facts.user_data,
            &facts.old_native.ancestry,
            &facts.new_native.ancestry,
        ] {
            for item in ancestry {
                let metadata = std::fs::symlink_metadata(&item.path)?;
                if metadata.file_type().is_symlink()
                    || metadata.dev() != item.identity.device
                    || metadata.ino() != item.identity.inode
                    || metadata.uid() != item.identity.uid
                    || metadata.mode() != item.identity.mode
                {
                    bail!("Host owner path changed before continuation");
                }
            }
        }
        let expected = &facts
            .state_root
            .last()
            .context("Host owner identity is missing")?
            .identity;
        let metadata = guard.metadata()?;
        if metadata.dev() != expected.device
            || metadata.ino() != expected.inode
            || metadata.uid() != expected.uid
            || metadata.mode() != expected.mode
        {
            bail!("Host owner lock no longer matches the selected folder");
        }
        Ok(())
    }

    fn require_quiescent_owner(state_root: &Path, old_executable: &Path) -> Result<()> {
        let mut command = process_utils::command("/usr/sbin/lsof");
        command.args(["-t", "-nP", "+D"]).arg(state_root);
        let result = process_utils::bounded_output(&mut command, Duration::from_secs(15))?;
        if !matches!(result.status.code(), Some(0 | 1)) || !result.stderr.is_empty() {
            bail!("Previous Host ownership could not be verified");
        }
        // +D returns 1 when any expanded entry is unused, even with matching PIDs.
        if result.status.success() && result.stdout.is_empty() {
            bail!("Previous Host ownership evidence is inconsistent");
        }
        for line in std::str::from_utf8(&result.stdout)?.lines() {
            let pid: u32 = line
                .parse()
                .context("Previous Host process evidence is invalid")?;
            if pid != std::process::id() {
                bail!(
                    "Stop the previous Launcher and its managed runtime before continuing its Host"
                );
            }
        }
        let mut command = process_utils::command("/bin/ps");
        command.args(["-axo", "pid=,comm="]);
        let result = process_utils::bounded_output(&mut command, Duration::from_secs(10))?;
        if !result.status.success() || !result.stderr.is_empty() {
            bail!("Previous Native process evidence is unavailable");
        }
        let old = old_executable
            .to_str()
            .context("Previous Native path is invalid")?;
        for line in std::str::from_utf8(&result.stdout)?.lines() {
            let text = line.trim();
            let Some(split) = text.find(char::is_whitespace) else {
                continue;
            };
            let pid: u32 = text[..split]
                .parse()
                .context("Native process evidence is invalid")?;
            if pid != std::process::id() && text[split..].trim() == old {
                bail!("The previous Native Launcher is still running");
            }
        }
        Ok(())
    }

    fn verify_previous_bundle(bundle: &Path) -> Result<()> {
        let mut command = process_utils::command("/usr/bin/codesign");
        command
            .args(["--verify", "--deep", "--strict", "--all-architectures"])
            .arg(bundle);
        let result = process_utils::bounded_output(&mut command, Duration::from_secs(30))?;
        if !result.status.success() {
            bail!("Previous Launcher code signature is invalid");
        }
        let info = bundle.join("Contents/Info.plist");
        let mut command = process_utils::command("/usr/bin/plutil");
        command
            .args(["-extract", "CFBundleIdentifier", "raw", "-o", "-"])
            .arg(info);
        let result = process_utils::bounded_output(&mut command, Duration::from_secs(10))?;
        if !result.status.success()
            || !result.stderr.is_empty()
            || std::str::from_utf8(&result.stdout)?.trim() != "dev.tobkiri.local-launcher"
        {
            bail!("Previous Host must belong to Tobkiri Launcher Developer");
        }
        Ok(())
    }

    fn current_process(path: &Path, digest: [u8; 32]) -> Result<ProcessFacts> {
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of_val(&info) as i32;
        let returned = unsafe {
            libc::proc_pidinfo(
                std::process::id() as i32,
                libc::PROC_PIDTBSDINFO,
                0,
                (&mut info as *mut libc::proc_bsdinfo).cast(),
                size,
            )
        };
        if returned != size
            || info.pbi_pid != std::process::id()
            || info.pbi_uid != unsafe { libc::geteuid() }
        {
            bail!("Current Native process identity is unavailable");
        }
        let start_identity = info
            .pbi_start_tvsec
            .checked_mul(1_000_000)
            .and_then(|seconds| seconds.checked_add(info.pbi_start_tvusec))
            .filter(|identity| *identity != 0)
            .context("Native process start identity is unavailable")?;
        Ok(ProcessFacts {
            pid: info.pbi_pid,
            start_identity,
            uid: info.pbi_uid,
            executable_path: path.to_path_buf(),
            executable_sha256: digest,
            alive: true,
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::os::unix::fs::{symlink, PermissionsExt};

        struct Fixture(PathBuf);

        impl Fixture {
            fn new() -> Self {
                let path = std::env::temp_dir().canonicalize().unwrap().join(format!(
                    "tobkiri-native-owner-startup-{}-{:x}",
                    std::process::id(),
                    rand::random::<u64>()
                ));
                std::fs::create_dir(&path).unwrap();
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
                Self(path)
            }
        }

        impl Drop for Fixture {
            fn drop(&mut self) {
                std::fs::remove_dir_all(&self.0).unwrap();
            }
        }

        #[test]
        fn nofollow_capture_rejects_redirected_ancestor() {
            let fixture = Fixture::new();
            let target = fixture.0.join("target");
            std::fs::create_dir(&target).unwrap();
            symlink(&target, fixture.0.join("redirected")).unwrap();
            let mut handles = Vec::new();
            assert!(capture_path(
                &fixture.0.join("redirected"),
                FileKind::Directory,
                &mut handles
            )
            .is_err());
            assert!(handles.is_empty());
        }

        #[test]
        fn executable_hash_rejects_oversized_sparse_file_before_reading() {
            let fixture = Fixture::new();
            let path = fixture.0.join("too-large");
            File::create(&path)
                .unwrap()
                .set_len(MAX_EXECUTABLE_BYTES + 1)
                .unwrap();
            assert!(capture_executable(&path, &mut Vec::new()).is_err());
        }

        #[test]
        fn root_lock_is_bound_to_captured_inode_and_excludes_another_open() {
            let fixture = Fixture::new();
            let mut handles = Vec::new();
            let facts = capture_path(&fixture.0, FileKind::Directory, &mut handles).unwrap();
            let expected = &facts.last().unwrap().identity;
            let guard = acquire_owner_guard(&handles, expected).unwrap();
            let another = File::open(&fixture.0).unwrap();
            assert_ne!(
                unsafe { libc::flock(another.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
                0
            );
            let mut changed = expected.clone();
            changed.inode += 1;
            assert!(acquire_owner_guard(&handles, &changed).is_err());
            assert_eq!(guard.metadata().unwrap().ino(), expected.inode);
        }

        #[test]
        fn current_native_process_has_os_birth_identity() {
            fn managed_state<T: Send + Sync + 'static>() {}
            managed_state::<OwnerLease>();
            let path = std::env::current_exe().unwrap();
            let process = current_process(&path, [9_u8; 32]).unwrap();
            assert_eq!(process.pid, std::process::id());
            assert!(process.start_identity > 0);
            assert!(process.alive);
        }
    }
}
