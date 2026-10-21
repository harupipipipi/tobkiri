use super::*;
use std::io::Cursor;

struct Unreadable;
impl Read for Unreadable {
    fn read(&mut self, _output: &mut [u8]) -> std::io::Result<usize> {
        panic!("invalid input size must be rejected before reading")
    }
}

#[test]
fn oversized_metadata_is_rejected_without_reading_any_content() {
    let error = capture_bounded_input(&mut Unreadable, 9, 8, Path::new("fixture")).unwrap_err();
    assert!(error.to_string().contains("exceeds the allowed input size"));
}

#[test]
fn sparse_oversized_file_is_rejected_at_the_existing_cli_package_limit() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("oversized.tpk");
    let max = 128 * 1024 * 1024;
    let file = std::fs::File::create(&path).unwrap();
    file.set_len(max as u64 + 1).unwrap();
    let error = bounded_read(&path, max).unwrap_err();
    assert!(error.to_string().contains("exceeds the allowed input size"));
}

#[test]
fn growing_stream_stops_at_one_byte_over_the_callers_limit() {
    struct GrowingReader {
        consumed: usize,
    }
    impl Read for GrowingReader {
        fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
            output.fill(42);
            self.consumed += output.len();
            Ok(output.len())
        }
    }
    let mut reader = GrowingReader { consumed: 0 };
    let error = capture_bounded_input(&mut reader, 0, 8, Path::new("fixture")).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("grew beyond the allowed input size")
    );
    assert_eq!(reader.consumed, 9);
    assert_eq!(
        capture_bounded_input(&mut Cursor::new(b"12345678"), 8, 8, Path::new("fixture")).unwrap(),
        b"12345678"
    );
}

#[cfg(target_pointer_width = "64")]
#[test]
fn overflowing_read_limit_is_rejected_before_reading() {
    let error =
        capture_bounded_input(&mut Unreadable, 0, usize::MAX, Path::new("fixture")).unwrap_err();
    assert!(error.to_string().contains("input size limit is too large"));
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("empty");
    std::fs::write(&path, b"").unwrap();
    let error = bounded_read(&path, usize::MAX).unwrap_err();
    assert!(error.to_string().contains("input size limit is too large"));
}

#[test]
fn valid_python_and_binary_bytes_keep_exact_contents_and_file_path_behavior() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("source.py");
    let python = b"#!/usr/bin/env python3\r\n# coding: utf-8\r\nprint('Tobkiri')\r\n";
    std::fs::write(&path, python).unwrap();
    assert_eq!(bounded_read(&path, python.len()).unwrap(), python);
    #[cfg(unix)]
    {
        let alias = root.path().join("alias.py");
        std::os::unix::fs::symlink(&path, &alias).unwrap();
        assert_eq!(bounded_read(&alias, python.len()).unwrap(), python);
    }
    let binary = b"\0\xff\x80\r\n";
    std::fs::write(&path, binary).unwrap();
    assert_eq!(bounded_read(&path, binary.len()).unwrap(), binary);
    std::fs::write(&path, b"").unwrap();
    assert!(bounded_read(&path, 0).unwrap().is_empty());
}

#[cfg(unix)]
#[test]
fn fifo_is_rejected_without_waiting_for_a_writer() {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};

    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("pipe.tpk");
    let name = CString::new(path.as_os_str().as_bytes()).unwrap();
    // SAFETY: name is a NUL-terminated temporary path with no existing file.
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    let error = bounded_read(&path, 8).unwrap_err();
    assert!(error.to_string().contains("not a regular file"));
}
