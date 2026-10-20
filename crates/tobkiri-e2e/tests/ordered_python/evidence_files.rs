//! Filesystem admission regressions, not guest or native Core execution.
use super::bounded_read;
use anyhow::{Result, ensure};
use std::{
    fs,
    os::unix::fs::symlink,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

#[test]
fn bounded_read_preserves_regular_file_bytes_and_enforces_limit() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let root = directory.path().canonicalize()?;
    fs::create_dir(root.join("nested"))?;
    fs::write(root.join("nested/source"), b"\0reviewed\r\n\xff")?;
    fs::write(root.join("empty"), b"")?;
    assert_eq!(
        bounded_read(&root, "nested/source", 12)?,
        b"\0reviewed\r\n\xff"
    );
    assert!(bounded_read(&root, "nested/source", 11).is_err());
    assert!(bounded_read(&root, "empty", 0)?.is_empty());
    assert!(bounded_read(&root, "nested", 16).is_err());
    Ok(())
}

#[test]
fn bounded_read_rejects_symlink_leaf_directory_and_root() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let root = directory.path().canonicalize()?;
    fs::create_dir(root.join("real"))?;
    fs::write(root.join("real/source"), b"reviewed")?;
    symlink("real/source", root.join("leaf"))?;
    symlink("real", root.join("child"))?;
    symlink("real", root.join("root"))?;
    assert!(bounded_read(&root, "leaf", 8).is_err());
    assert!(bounded_read(&root, "child/source", 8).is_err());
    assert!(bounded_read(&root.join("root"), "source", 8).is_err());
    Ok(())
}

#[test]
fn bounded_read_rejects_unsafe_relative_paths() -> Result<()> {
    let directory = tempfile::tempdir()?;
    let root = directory.path().canonicalize()?;
    fs::create_dir(root.join("nested"))?;
    fs::write(root.join("source"), b"reviewed")?;
    assert!(bounded_read(&root.join("nested"), "../source", 8).is_err());
    assert!(bounded_read(&root, root.join("source").to_str().unwrap(), 8).is_err());
    for relative in [
        "",
        ".",
        "..",
        "../source",
        "nested/../source",
        "/source",
        "./source",
        "nested//source",
        "source/",
        "nested\\source",
        "C:source",
        "source\0suffix",
    ] {
        assert!(
            bounded_read(&root, relative, 8).is_err(),
            "accepted unsafe source path {relative:?}"
        );
    }
    Ok(())
}

#[test]
fn bounded_read_rejects_fifo_without_blocking() -> Result<()> {
    const ROOT_ENV: &str = "TOBKIRI_TEST_EVIDENCE_FIFO_ROOT";
    if let Some(root) = std::env::var_os(ROOT_ENV) {
        let root = std::path::Path::new(&root);
        assert!(bounded_read(root, "source", 8).is_err());
        fs::write(root.join("rejected"), b"FIFO rejected")?;
        return Ok(());
    }
    let directory = tempfile::tempdir()?;
    let root = directory.path().canonicalize()?;
    ensure!(
        Command::new("mkfifo")
            .arg(root.join("source"))
            .status()?
            .success(),
        "failed to create FIFO regression fixture"
    );
    // Isolate the read so a future blocking-open regression cannot hang the
    // test runner. There is deliberately no writer to unblock the FIFO.
    let mut child = Command::new(std::env::current_exe()?)
        .args([
            "--exact",
            "evidence::filesystem_tests::bounded_read_rejects_fifo_without_blocking",
            "--nocapture",
        ])
        .env(ROOT_ENV, &root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.try_wait()? {
            ensure!(status.success(), "FIFO evidence was not rejected");
            ensure!(
                fs::read(root.join("rejected"))? == b"FIFO rejected",
                "FIFO child did not run the expected regression"
            );
            return Ok(());
        }
        if Instant::now() >= deadline {
            child.kill()?;
            child.wait()?;
            anyhow::bail!("FIFO evidence read blocked");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
