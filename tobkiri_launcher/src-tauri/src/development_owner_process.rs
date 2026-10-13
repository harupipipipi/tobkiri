//! Bounded, read-only inspection for processes that may own a development Host.
//!
//! This uses the public libproc and sysctl interfaces. It reads kernel process
//! metadata and command/environment bytes only for same-effective-uid processes
//! whose executable path looks like Tobkiri, Python, or a helper. It never
//! formats or logs those bytes.

#[cfg(not(target_os = "macos"))]
use std::path::Path;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum OwnerProcessState {
    Clear,
    Busy,
    Uncertain,
}

#[cfg(target_os = "macos")]
mod macos {
    use super::OwnerProcessState;
    use std::ffi::{c_int, c_void, CStr};
    use std::os::unix::ffi::OsStrExt;
    use std::path::Path;
    use std::ptr;

    const MAX_PIDS: usize = 16_384;
    const MAX_ARGS_BYTES: usize = 256 * 1024;
    const MAX_ARGC: usize = 16_384;
    const PROC_ALL_PIDS: u32 = 1;
    const KERN_PROCARGS2: c_int = 49;
    const CTL_KERN: c_int = 1;

    extern "C" {
        fn sysctl(
            name: *mut c_int,
            name_len: u32,
            old: *mut c_void,
            old_len: *mut usize,
            new: *mut c_void,
            new_len: usize,
        ) -> c_int;
    }

    struct SensitiveBytes(Vec<u8>);

    impl Drop for SensitiveBytes {
        fn drop(&mut self) {
            for byte in &mut self.0 {
                // Volatile writes prevent the optimizer from eliding the wipe.
                unsafe { ptr::write_volatile(byte, 0) };
            }
        }
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct ProcessIdentity {
        pid: u32,
        uid: u32,
        start: u64,
        status: u32,
    }

    impl ProcessIdentity {
        fn same_process(&self, other: &Self) -> bool {
            self.pid == other.pid && self.uid == other.uid && self.start == other.start
        }
    }

    fn confirmed_nonrunning_zombie(before: &ProcessIdentity, after: &ProcessIdentity) -> bool {
        before.status == libc::SZOMB && before == after
    }

    fn process_identity(pid: u32) -> Result<Option<ProcessIdentity>, ()> {
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of::<libc::proc_bsdinfo>() as c_int;
        // A zero return is an exited process only when a second pid-list check
        // also confirms it has disappeared; every other failure is uncertain.
        let returned = unsafe {
            libc::proc_pidinfo(
                pid as c_int,
                libc::PROC_PIDTBSDINFO,
                // Include unreaped zombies so their non-running state can be
                // verified instead of confusing a listed zombie with a live PID.
                1,
                (&mut info as *mut libc::proc_bsdinfo).cast(),
                size,
            )
        };
        if returned == 0 {
            return Ok(None);
        }
        if returned != size || info.pbi_pid != pid {
            return Err(());
        }
        let start = info
            .pbi_start_tvsec
            .checked_mul(1_000_000)
            .and_then(|seconds| seconds.checked_add(info.pbi_start_tvusec))
            .filter(|value| *value != 0)
            .ok_or(())?;
        Ok(Some(ProcessIdentity {
            pid,
            // pbi_uid is the process's effective UID in proc_bsdinfo.
            uid: info.pbi_uid,
            start,
            status: info.pbi_status,
        }))
    }

    fn verified_exit(pid: u32) -> Result<bool, ()> {
        Ok(!list_pids()?.contains(&pid))
    }

    fn process_path(pid: u32) -> Result<Vec<u8>, ()> {
        let mut path = [0_u8; libc::PROC_PIDPATHINFO_MAXSIZE as usize];
        let size = unsafe {
            libc::proc_pidpath(pid as c_int, path.as_mut_ptr().cast(), path.len() as u32)
        };
        if size <= 0 || size as usize > path.len() {
            return Err(());
        }
        // libproc versions differ on whether the returned byte count includes
        // the terminator. The whole fixed buffer was initialized, so accept a
        // terminator at either boundary while requiring it within the report.
        let terminator = path.iter().position(|byte| *byte == 0).ok_or(())?;
        if terminator > size as usize || size as usize > terminator + 1 {
            return Err(());
        }
        let path = CStr::from_bytes_until_nul(&path).map_err(|_| ())?;
        if path.to_bytes().is_empty() {
            return Err(());
        }
        Ok(path.to_bytes().to_vec())
    }

    fn list_pids() -> Result<Vec<u32>, ()> {
        let mut pids = vec![0_u32; MAX_PIDS];
        let capacity = pids.len() * std::mem::size_of::<u32>();
        let bytes = unsafe {
            libc::proc_listpids(
                PROC_ALL_PIDS,
                0,
                pids.as_mut_ptr().cast(),
                capacity as c_int,
            )
        };
        if bytes <= 0
            || bytes as usize >= capacity
            || !(bytes as usize).is_multiple_of(std::mem::size_of::<u32>())
        {
            return Err(());
        }
        pids.truncate(bytes as usize / std::mem::size_of::<u32>());
        pids.retain(|pid| *pid != 0);
        Ok(pids)
    }

    fn native_launcher(path: &[u8], old_executable: &[u8]) -> bool {
        if path == old_executable {
            return true;
        }
        path.rsplit(|byte| *byte == b'/')
            .next()
            .unwrap_or(path)
            .eq_ignore_ascii_case(b"tobkiri-launcher")
    }

    fn candidate(path: &[u8], old_executable: &[u8]) -> bool {
        if native_launcher(path, old_executable) {
            return true;
        }
        let name = path.rsplit(|byte| *byte == b'/').next().unwrap_or(path);
        let name = name.iter().map(u8::to_ascii_lowercase).collect::<Vec<_>>();
        name.starts_with(b"python")
            || matches!(
                name.as_slice(),
                b"tobkiri-packvm-vz-helper" | b"packvm-vz-helper"
            )
    }

    fn args_and_environment(pid: u32) -> Result<SensitiveBytes, ()> {
        let mut mib = [CTL_KERN, KERN_PROCARGS2, pid as c_int];
        let mut length = 0_usize;
        if unsafe {
            sysctl(
                mib.as_mut_ptr(),
                mib.len() as u32,
                ptr::null_mut(),
                &mut length,
                ptr::null_mut(),
                0,
            )
        } != 0
            || length < std::mem::size_of::<c_int>()
            || length > MAX_ARGS_BYTES
        {
            return Err(());
        }
        let mut bytes = SensitiveBytes(vec![0_u8; length]);
        let mut actual = length;
        if unsafe {
            sysctl(
                mib.as_mut_ptr(),
                mib.len() as u32,
                bytes.0.as_mut_ptr().cast(),
                &mut actual,
                ptr::null_mut(),
                0,
            )
        } != 0
            || actual > bytes.0.len()
            || actual < std::mem::size_of::<c_int>()
        {
            return Err(());
        }
        bytes.0.truncate(actual);
        Ok(bytes)
    }

    fn contains_owner_marker(
        bytes: &[u8],
        state_root: &[u8],
        user_data: &[u8],
    ) -> Result<bool, ()> {
        let argc_bytes: [u8; 4] = bytes.get(..4).ok_or(())?.try_into().map_err(|_| ())?;
        let argc = i32::from_ne_bytes(argc_bytes);
        if argc < 0 || argc as usize > MAX_ARGC {
            return Err(());
        }
        let mut offset = 4_usize;
        // The executable path starts after the argc integer and is NUL-padded.
        while bytes.get(offset) == Some(&0) {
            offset += 1;
            if offset >= bytes.len() {
                return Err(());
            }
        }
        let executable_end = bytes[offset..]
            .iter()
            .position(|byte| *byte == 0)
            .map(|end| offset + end)
            .ok_or(())?;
        offset = executable_end + 1;

        // KERN_PROCARGS2 pads the executable string with NUL bytes before
        // argv[0]. Skip this kernel alignment padding before parsing argv.
        while offset < bytes.len() && bytes[offset] == 0 {
            offset += 1;
        }

        let mut argv = Vec::with_capacity(argc as usize);
        for _ in 0..argc {
            let end = bytes
                .get(offset..)
                .ok_or(())?
                .iter()
                .position(|byte| *byte == 0)
                .map(|end| offset + end)
                .ok_or(())?;
            argv.push(&bytes[offset..end]);
            offset = end + 1;
        }
        let state_arg = argv.iter().any(|arg| is_state_path(arg, state_root));
        if state_arg {
            return Ok(true);
        }

        // Skip padding NULs between argv and envp, then inspect NUL-delimited
        // environment entries. A malformed final entry makes the result unknown.
        while offset < bytes.len() && bytes[offset] == 0 {
            offset += 1;
        }
        // XNU can intentionally omit a protected process's environment from
        // KERN_PROCARGS2. An absent environment is therefore unavailable
        // evidence, rather than evidence that the owner marker is absent.
        if offset == bytes.len() {
            return Err(());
        }
        let mut user_data_match = false;
        while offset < bytes.len() {
            let end = bytes[offset..]
                .iter()
                .position(|byte| *byte == 0)
                .map(|end| offset + end)
                .ok_or(())?;
            let entry = &bytes[offset..end];
            if let Some(value) = entry.strip_prefix(b"RUMI_USER_DATA=") {
                user_data_match = value == user_data;
            }
            offset = end + 1;
        }
        Ok(user_data_match)
    }

    fn is_state_path(argument: &[u8], state_root: &[u8]) -> bool {
        argument == state_root
            || argument
                .strip_prefix(state_root)
                .is_some_and(|remainder| remainder.starts_with(b"/") && remainder.len() > 1)
    }

    fn uncertain_unless_exited(pid: u32) -> OwnerProcessState {
        match verified_exit(pid) {
            Ok(true) => OwnerProcessState::Clear,
            Ok(false) | Err(()) => OwnerProcessState::Uncertain,
        }
    }

    fn inspect_one(
        pid: u32,
        euid: u32,
        state_root: &[u8],
        user_data: &[u8],
        old_executable: &[u8],
    ) -> OwnerProcessState {
        let before = match process_identity(pid) {
            Ok(Some(identity)) => identity,
            Ok(None) => {
                return match verified_exit(pid) {
                    Ok(true) => OwnerProcessState::Clear,
                    Ok(false) | Err(()) => OwnerProcessState::Uncertain,
                }
            }
            Err(()) => return uncertain_unless_exited(pid),
        };
        if before.uid != euid {
            return OwnerProcessState::Clear;
        }
        if before.status == libc::SZOMB {
            return match process_identity(pid) {
                Ok(Some(after)) if confirmed_nonrunning_zombie(&before, &after) => {
                    OwnerProcessState::Clear
                }
                Ok(Some(_)) => OwnerProcessState::Uncertain,
                Ok(None) | Err(()) => uncertain_unless_exited(pid),
            };
        }
        let path = match process_path(pid) {
            Ok(path) => path,
            Err(()) => return uncertain_unless_exited(pid),
        };
        if !candidate(&path, old_executable) {
            return OwnerProcessState::Clear;
        }
        // An idle Native can keep its owner only in memory, and a preserved
        // predecessor bundle can have a different path from the running app.
        // Single-Native continuation therefore rejects any same-user Launcher,
        // without requiring open owner files or command/environment markers.
        let matches = if native_launcher(&path, old_executable) {
            true
        } else {
            let bytes = match args_and_environment(pid) {
                Ok(bytes) => bytes,
                Err(()) => return uncertain_unless_exited(pid),
            };
            match contains_owner_marker(&bytes.0, state_root, user_data) {
                Ok(matches) => matches,
                Err(()) => return uncertain_unless_exited(pid),
            }
        };
        let after = match process_identity(pid) {
            Ok(Some(identity)) => identity,
            // A verified disappearance between snapshots is safe to ignore.
            Ok(None) => {
                return match verified_exit(pid) {
                    Ok(true) => OwnerProcessState::Clear,
                    Ok(false) | Err(()) => OwnerProcessState::Uncertain,
                }
            }
            Err(()) => return uncertain_unless_exited(pid),
        };
        let after_path = match process_path(pid) {
            Ok(path) => path,
            Err(()) => return uncertain_unless_exited(pid),
        };
        // Sleep/run transitions do not change the live process's identity.
        if !before.same_process(&after) || path != after_path {
            return OwnerProcessState::Uncertain;
        }
        if matches {
            OwnerProcessState::Busy
        } else {
            OwnerProcessState::Clear
        }
    }

    pub(crate) fn inspect_owner_processes(
        state_root: &Path,
        user_data: &Path,
        old_executable: &Path,
    ) -> OwnerProcessState {
        let state_root = state_root.as_os_str().as_bytes();
        let user_data = user_data.as_os_str().as_bytes();
        let old_executable = old_executable.as_os_str().as_bytes();
        let euid = unsafe { libc::geteuid() };
        let pids = match list_pids() {
            Ok(pids) => pids,
            Err(()) => return OwnerProcessState::Uncertain,
        };
        let mut uncertain = false;
        for pid in pids {
            if pid == std::process::id() {
                continue;
            }
            match inspect_one(pid, euid, state_root, user_data, old_executable) {
                OwnerProcessState::Busy => return OwnerProcessState::Busy,
                OwnerProcessState::Uncertain => uncertain = true,
                OwnerProcessState::Clear => {}
            }
        }
        if uncertain {
            OwnerProcessState::Uncertain
        } else {
            OwnerProcessState::Clear
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::process::{Command, Stdio};
        use std::thread;
        use std::time::Duration;

        struct ReapedChild(std::process::Child);

        impl Drop for ReapedChild {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }

        #[test]
        fn zombie_confirmation_requires_stable_pid_uid_birth_and_status() {
            let before = ProcessIdentity {
                pid: 11,
                uid: 501,
                start: 77,
                status: libc::SZOMB,
            };
            assert!(confirmed_nonrunning_zombie(&before, &before));
            for after in [
                ProcessIdentity { pid: 12, ..before },
                ProcessIdentity { uid: 502, ..before },
                ProcessIdentity {
                    start: 78,
                    ..before
                },
                ProcessIdentity {
                    status: 2,
                    ..before
                },
            ] {
                assert!(!confirmed_nonrunning_zombie(&before, &after));
            }
            let running = ProcessIdentity {
                status: 2,
                ..before
            };
            assert!(!confirmed_nonrunning_zombie(&running, &running));
        }

        #[test]
        fn live_process_identity_ignores_sleep_run_transitions() {
            let running = ProcessIdentity {
                pid: 11,
                uid: 501,
                start: 77,
                status: 2,
            };
            let sleeping = ProcessIdentity {
                status: 3,
                ..running
            };
            assert!(running.same_process(&sleeping));
            assert!(!running.same_process(&ProcessIdentity {
                start: 78,
                ..sleeping
            }));
        }

        #[test]
        fn unreaped_child_zombie_is_clear_after_stable_kernel_observations() {
            let child = Command::new("/bin/sh")
                .args(["-c", "exit 0"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let child = ReapedChild(child);
            let deadline = std::time::Instant::now() + Duration::from_secs(2);
            let before = loop {
                if let Some(identity) = process_identity(child.0.id()).unwrap() {
                    if identity.status == libc::SZOMB {
                        break identity;
                    }
                }
                assert!(
                    std::time::Instant::now() < deadline,
                    "child did not become a zombie"
                );
                thread::sleep(Duration::from_millis(5));
            };
            assert!(list_pids().unwrap().contains(&child.0.id()));
            let after = process_identity(child.0.id()).unwrap().unwrap();
            assert!(confirmed_nonrunning_zombie(&before, &after));
            assert_eq!(
                inspect_one(
                    child.0.id(),
                    unsafe { libc::geteuid() },
                    b"/unused/host",
                    b"/unused/host/user_data",
                    b"/unused/tobkiri-launcher",
                ),
                OwnerProcessState::Clear
            );
        }

        #[test]
        fn live_python_candidate_owner_marker_remains_busy() {
            let fixture = std::env::temp_dir().join(format!(
                "tobkiri-python-owner-test-{}-{}",
                std::process::id(),
                rand::random::<u64>()
            ));
            std::fs::create_dir(&fixture).unwrap();
            let executable = fixture.join("python-owner-probe");
            std::fs::copy("/bin/sh", &executable).unwrap();
            let root = fixture.join("host");
            let child = Command::new(&executable)
                .args(["-c", "read -r line", root.to_str().unwrap()])
                .env_clear()
                .env("PATH", "/usr/bin:/bin")
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let child = ReapedChild(child);
            assert_eq!(
                inspect_one(
                    child.0.id(),
                    unsafe { libc::geteuid() },
                    root.as_os_str().as_bytes(),
                    root.join("user_data").as_os_str().as_bytes(),
                    b"/previous/tobkiri-launcher",
                ),
                OwnerProcessState::Busy
            );
            drop(child);
            std::fs::remove_dir_all(fixture).unwrap();
        }

        #[test]
        fn native_launcher_matches_selected_executable_and_other_bundle_paths() {
            let selected = b"/backup/Developer.app/Contents/MacOS/tobkiri-launcher";
            assert!(native_launcher(selected, selected));
            assert!(native_launcher(
                b"/Applications/Developer.app/Contents/MacOS/tobkiri-launcher",
                selected
            ));
            assert!(native_launcher(
                b"/selected/renamed-native",
                b"/selected/renamed-native"
            ));
            assert!(!native_launcher(
                b"/other/renamed-native",
                b"/selected/renamed-native"
            ));
            assert!(!native_launcher(b"/usr/bin/python3", selected));
            assert!(!native_launcher(
                b"/helper/tobkiri-packvm-vz-helper",
                selected
            ));
        }

        #[test]
        fn parser_rejects_truncated_and_invalid_kernel_arguments() {
            assert_eq!(
                contains_owner_marker(&[], b"/tmp/host", b"/tmp/host/user_data"),
                Err(())
            );
            let mut bad_argc = 99_999_i32.to_ne_bytes().to_vec();
            bad_argc.extend_from_slice(b"/python\0");
            assert_eq!(
                contains_owner_marker(&bad_argc, b"/tmp/host", b"/tmp/host/user_data"),
                Err(())
            );

            let mut missing_exe_terminator = 1_i32.to_ne_bytes().to_vec();
            missing_exe_terminator.extend_from_slice(b"/python");
            assert_eq!(
                contains_owner_marker(
                    &missing_exe_terminator,
                    b"/tmp/host",
                    b"/tmp/host/user_data"
                ),
                Err(())
            );

            let mut missing_argv_terminator = 1_i32.to_ne_bytes().to_vec();
            missing_argv_terminator.extend_from_slice(b"/python\0/tmp/host/child");
            assert_eq!(
                contains_owner_marker(
                    &missing_argv_terminator,
                    b"/tmp/host",
                    b"/tmp/host/user_data"
                ),
                Err(())
            );

            assert_eq!(
                contains_owner_marker(
                    &synthetic_args(&[b"/python", b"unrelated"], &[]),
                    b"/tmp/host",
                    b"/tmp/host/user_data"
                ),
                Err(())
            );
        }

        #[test]
        fn parser_matches_only_exact_environment_key_and_path_boundary() {
            let root = b"/tmp/host";
            let data = b"/tmp/host/user_data";
            assert!(contains_owner_marker(
                &synthetic_args(
                    &[b"/python", b"/tmp/host/child"],
                    &[b"RUMI_USER_DATA=/elsewhere"]
                ),
                root,
                data
            )
            .unwrap());
            assert!(!contains_owner_marker(
                &synthetic_args(
                    &[b"/python", b"/tmp/hostile/child"],
                    &[b"RUMI_USER_DATA=/elsewhere"]
                ),
                root,
                data
            )
            .unwrap());
            assert!(!contains_owner_marker(
                &synthetic_args(
                    &[b"/python"],
                    &[b"OTHER_RUMI_USER_DATA=/tmp/host/user_data"]
                ),
                root,
                data
            )
            .unwrap());
            assert!(contains_owner_marker(
                &synthetic_args(&[b"/python"], &[b"RUMI_USER_DATA=/tmp/host/user_data"]),
                root,
                data
            )
            .unwrap());
        }

        fn synthetic_args(argv: &[&[u8]], env: &[&[u8]]) -> Vec<u8> {
            let mut result = (argv.len() as i32).to_ne_bytes().to_vec();
            result.extend_from_slice(b"/python\0");
            result.extend_from_slice(&[0, 0, 0]);
            for arg in argv {
                result.extend_from_slice(arg);
                result.push(0);
            }
            for value in env {
                result.extend_from_slice(value);
                result.push(0);
            }
            result
        }

        #[test]
        fn live_child_owner_environment_is_detected_with_closed_standard_files() {
            if std::env::var_os("NATIVE_OWNER_INSPECTION_CHILD").is_some() {
                thread::sleep(Duration::from_millis(700));
                return;
            }
            let root = std::env::temp_dir().join("native-owner-test-root");
            let data = root.join("user_data");
            let executable = std::env::current_exe().unwrap();
            let mut child = Command::new(&executable)
                .arg("--exact")
                .arg("development_owner_process::macos::tests::live_child_owner_environment_is_detected_with_closed_standard_files")
                .arg("--nocapture")
                .env("NATIVE_OWNER_INSPECTION_CHILD", "1")
                .env("RUMI_USER_DATA", &data)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .stdin(Stdio::null())
                .spawn()
                .unwrap();
            thread::sleep(Duration::from_millis(100));
            inspect_disposable_child(&mut child, &root, &data, true);
        }

        #[test]
        fn live_child_argv_state_root_is_detected_without_owner_environment() {
            let root = std::env::temp_dir().join("native-owner-argv-test-root");
            let data = root.join("user_data");
            let root_arg = root.to_str().unwrap();
            let mut child = Command::new("/bin/sh")
                .args(["-c", "sleep 1; true", root_arg])
                .env_remove("RUMI_USER_DATA")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .stdin(Stdio::null())
                .spawn()
                .unwrap();
            thread::sleep(Duration::from_millis(100));
            inspect_disposable_child(&mut child, &root, &data, true);
        }

        #[test]
        fn live_idle_native_at_other_bundle_path_is_busy_without_owner_markers() {
            let fixture = std::env::temp_dir().join(format!(
                "tobkiri-idle-native-test-{}-{}",
                std::process::id(),
                rand::random::<u64>()
            ));
            let executable = fixture.join("Original.app/Contents/MacOS/tobkiri-launcher");
            std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
            std::fs::copy("/bin/sleep", &executable).unwrap();
            let root = fixture.join("selected-host");
            let data = root.join("user_data");
            let selected = fixture.join("Preserved.app/Contents/MacOS/tobkiri-launcher");
            let mut child = Command::new(&executable)
                .arg("1")
                .current_dir(&fixture)
                .env_remove("RUMI_USER_DATA")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .stdin(Stdio::null())
                .spawn()
                .unwrap();
            thread::sleep(Duration::from_millis(100));
            assert_ne!(
                process_path(child.id()).unwrap(),
                selected.as_os_str().as_bytes()
            );
            assert_eq!(
                inspect_one(
                    child.id(),
                    unsafe { libc::geteuid() },
                    root.as_os_str().as_bytes(),
                    data.as_os_str().as_bytes(),
                    selected.as_os_str().as_bytes(),
                ),
                OwnerProcessState::Busy
            );
            child.wait().unwrap();
            assert_eq!(
                inspect_one(
                    child.id(),
                    unsafe { libc::geteuid() },
                    root.as_os_str().as_bytes(),
                    data.as_os_str().as_bytes(),
                    selected.as_os_str().as_bytes(),
                ),
                OwnerProcessState::Clear
            );
            std::fs::remove_dir_all(fixture).unwrap();
        }

        fn inspect_disposable_child(
            child: &mut std::process::Child,
            root: &Path,
            data: &Path,
            expect_busy: bool,
        ) {
            let executable = process_path(child.id()).unwrap();
            let busy = inspect_one(
                child.id(),
                unsafe { libc::geteuid() },
                root.as_os_str().as_encoded_bytes(),
                data.as_os_str().as_encoded_bytes(),
                &executable,
            );
            assert_eq!(
                busy,
                if expect_busy {
                    OwnerProcessState::Busy
                } else {
                    OwnerProcessState::Clear
                }
            );
            child.wait().unwrap();
            assert_eq!(
                inspect_one(
                    child.id(),
                    unsafe { libc::geteuid() },
                    root.as_os_str().as_encoded_bytes(),
                    data.as_os_str().as_encoded_bytes(),
                    &executable,
                ),
                OwnerProcessState::Clear
            );
        }
    }
}

#[cfg(target_os = "macos")]
pub(crate) use macos::inspect_owner_processes;

#[cfg(not(target_os = "macos"))]
pub(crate) fn inspect_owner_processes(
    _state_root: &Path,
    _user_data: &Path,
    _old_executable: &Path,
) -> OwnerProcessState {
    OwnerProcessState::Uncertain
}
