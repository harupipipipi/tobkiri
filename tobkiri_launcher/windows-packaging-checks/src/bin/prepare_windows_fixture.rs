//! Test-only setup launcher. It creates a new protected fixture parent and
//! retains the actual creation handle while the explicit preparer runs.
#![allow(dead_code)]
#[cfg(windows)]
include!(concat!(env!("OUT_DIR"), "/filesystem.rs"));

#[cfg(windows)]
fn prepare() -> std::io::Result<()> {
    use std::{collections::BTreeMap, ffi::OsString, io, path::PathBuf, process::Command};
    let mut args = std::env::args_os().skip(1);
    let mut options = BTreeMap::<OsString, OsString>::new();
    while let Some(key) = args.next() {
        let value = args.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "every option requires a value")
        })?;
        if !["--python", "--uv", "--version", "--script", "--root"]
            .iter()
            .any(|name| key == *name)
            || options.insert(key, value).is_some()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "unknown or duplicate fixture option",
            ));
        }
    }
    let get = |key: &str| {
        options
            .get(std::ffi::OsStr::new(key))
            .cloned()
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, format!("{key} is required"))
            })
    };
    let python = PathBuf::from(get("--python")?);
    let uv = PathBuf::from(get("--uv")?);
    let script = PathBuf::from(get("--script")?);
    let root = PathBuf::from(get("--root")?);
    let version = get("--version")?;
    for path in [&python, &uv, &script, &root] {
        if !path.is_absolute() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "all fixture paths must be absolute",
            ));
        }
    }
    // The caller explicitly selects existing official tooling. These guards
    // prevent path replacement during this test-only preparation operation.
    let python_pin = windows_packaging_fs::open_pinned(&python, false)?;
    let uv_pin = windows_packaging_fs::open_pinned(&uv, false)?;
    let script_pin = windows_packaging_fs::open_pinned(&script, false)?;
    let sid = windows_packaging_fs::current_user_sid()?;
    let parent = windows_packaging_fs::create_private_directory_pinned(&root, &sid)?;
    windows_packaging_fs::verify_acl(&parent.file, &sid, false, true)?;
    let output = root.join("closure");
    let status = Command::new(&python)
        .args(["-I", "-B"])
        .arg(&script)
        .arg("--uv")
        .arg(&uv)
        .arg("--runtime-python")
        .arg(&python)
        .arg("--expected-version")
        .arg(version)
        .arg("--output")
        .arg(&output)
        .env("UV_OFFLINE", "1")
        .status()?;
    if !status.success() {
        return Err(io::Error::other(format!(
            "fixture preparation failed with {status}; new private fixture retained"
        )));
    }
    windows_packaging_fs::verify_acl(&parent.file, &sid, false, true)?;
    let closure = windows_packaging_fs::open_pinned(&output, true)?;
    windows_packaging_fs::verify_acl(&closure.file, &sid, false, false)?;
    drop(closure);
    drop(parent);
    drop(script_pin);
    drop(uv_pin);
    drop(python_pin);
    Ok(())
}

#[cfg(windows)]
fn main() {
    if let Err(error) = prepare() {
        eprintln!("Windows fixture setup: {error}");
        std::process::exit(1);
    }
}
#[cfg(not(windows))]
fn main() {
    eprintln!("This fixture launcher requires native Windows");
    std::process::exit(1);
}
