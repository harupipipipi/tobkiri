//! Real Linux contracts, not a fake owner or a filesystem-name override.
//! Run the positive suite on a local ext4 temp directory. The negative fixture
//! additionally requires the runner's actual /dev/shm to be tmpfs; a different
//! fixture layout is a test failure, never an implicit pass/skip.
#![cfg(all(target_os = "linux", target_arch = "x86_64", target_env = "gnu"))]

use std::{
    ffi::CStr,
    fs::File,
    mem::MaybeUninit,
    os::{fd::AsRawFd, unix::fs::MetadataExt},
};
use tobkiri_canon::ids::{PackId, ProfileId};
use tobkiri_gate::AllocationOwnerLease;
use tobkiri_store::{DataRoot, StoreError};

fn owner() -> (ProfileId, PackId) {
    (
        ProfileId::parse("prof_linux_owner").unwrap(),
        PackId::parse("local.linux-owner").unwrap(),
    )
}

fn fstatfs(file: &File) -> libc::statfs {
    let mut value = MaybeUninit::<libc::statfs>::uninit();
    assert_eq!(
        unsafe { libc::fstatfs(file.as_raw_fd(), value.as_mut_ptr()) },
        0,
        "fstatfs failed: {}",
        std::io::Error::last_os_error()
    );
    unsafe { value.assume_init() }
}

#[test]
fn native_ext4_owner_admission_emits_bounded_kernel_and_fd_facts() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let (profile, pack) = owner();
    // This public entry uses the production statx + procfs mountinfo proof and
    // then the common actual flock; the facts below do not stand in for it.
    let lease = root.owner_lease(&profile, &pack).unwrap();
    lease.validate().unwrap();

    let file = File::open(root.path()).unwrap();
    let metadata = file.metadata().unwrap();
    let filesystem = fstatfs(&file);
    assert_eq!(filesystem.f_type, 0xef53);
    let mut attributes = MaybeUninit::<libc::statx>::uninit();
    assert_eq!(
        unsafe {
            libc::statx(
                file.as_raw_fd(),
                c"".as_ptr(),
                libc::AT_EMPTY_PATH | libc::AT_STATX_SYNC_AS_STAT,
                libc::STATX_MNT_ID | libc::STATX_INO,
                attributes.as_mut_ptr(),
            )
        },
        0
    );
    let attributes = unsafe { attributes.assume_init() };
    assert_eq!(
        attributes.stx_mask & (libc::STATX_MNT_ID | libc::STATX_INO),
        libc::STATX_MNT_ID | libc::STATX_INO
    );
    assert_eq!(attributes.stx_ino, metadata.ino());
    assert_ne!(attributes.stx_mnt_id, 0);
    file.sync_all().unwrap();
    let mut system = MaybeUninit::<libc::utsname>::uninit();
    assert_eq!(unsafe { libc::uname(system.as_mut_ptr()) }, 0);
    let system = unsafe { system.assume_init() };
    let release = unsafe { CStr::from_ptr(system.release.as_ptr()) };
    let machine = unsafe { CStr::from_ptr(system.machine.as_ptr()) };
    // utsname fields have fixed native lengths. No filesystem paths, file
    // contents, secret values, external API calls or database rows are emitted.
    println!(
        "{}",
        serde_json::json!({
            "kind": "linux-native-owner-facts-v1",
            "target": "x86_64-unknown-linux-gnu",
            "kernel_release": release.to_str().unwrap(),
            "machine": machine.to_str().unwrap(),
            "filesystem_magic": filesystem.f_type,
            "filesystem_proof": "statx-mount-id+verified-procfs-mountinfo-ext4",
            "mount_id": attributes.stx_mnt_id,
            "device_major": attributes.stx_dev_major,
            "device_minor": attributes.stx_dev_minor,
            "inode": attributes.stx_ino,
            "uid": metadata.uid(),
            "effective_uid": unsafe { libc::geteuid() },
            "mode": metadata.mode(),
            "native_owner_admitted": true,
            "directory_fsync_succeeded": true
        })
    );
}

#[test]
fn actual_tmpfs_is_refused_even_with_a_private_owned_directory() {
    let temp = tempfile::tempdir_in("/dev/shm").unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let file = File::open(root.path()).unwrap();
    assert_eq!(
        fstatfs(&file).f_type,
        0x0102_1994,
        "negative fixture must be actual tmpfs"
    );
    let metadata = file.metadata().unwrap();
    assert_eq!(metadata.uid(), unsafe { libc::geteuid() });
    assert_eq!(metadata.mode() & 0o077, 0);
    let (profile, pack) = owner();
    assert!(matches!(
        root.owner_lease(&profile, &pack),
        Err(StoreError::Unavailable(_))
    ));
    assert!(matches!(
        root.user_owner_lease(&profile),
        Err(StoreError::Unavailable(_))
    ));
}

#[test]
fn inherited_owner_is_rejected_after_real_fork_without_releasing_parent_custody() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open(temp.path().canonicalize().unwrap().join("data")).unwrap();
    let (profile, pack) = owner();
    let lease = root.owner_lease(&profile, &pack).unwrap();
    let pid = unsafe { libc::fork() };
    assert!(pid >= 0, "fork failed: {}", std::io::Error::last_os_error());
    if pid == 0 {
        // No allocation, mutex, SQLite, formatting, unwinding or Rust destructor
        // in the post-fork child. This trait check is only a PID comparison.
        let refused = matches!(lease.validate(), Err(tobkiri_gate::GateError::Unavailable));
        unsafe { libc::_exit(if refused { 0 } else { 1 }) };
    }
    let mut status = 0;
    assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
    assert!(libc::WIFEXITED(status));
    assert_eq!(libc::WEXITSTATUS(status), 0);
    lease.validate().unwrap();
    // A fresh exec, unlike the fork child, can initialize its own broker. It
    // must still be excluded by the parent's retained real directory lock.
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "exec_probe_cannot_acquire_parent_held_root"])
        .env("TOBKIRI_LINUX_OWNER_HELD_ROOT", root.path())
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    lease.validate().unwrap();
}

#[test]
fn exec_probe_cannot_acquire_parent_held_root() {
    let Some(path) = std::env::var_os("TOBKIRI_LINUX_OWNER_HELD_ROOT") else {
        // Invocation helper only; the actual assertion is also executed by the
        // parent test above in an independently exec'd process.
        return;
    };
    let root = DataRoot::open(path).unwrap();
    let (profile, pack) = owner();
    assert!(matches!(
        root.owner_lease(&profile, &pack),
        Err(StoreError::Conflict(_))
    ));
}
