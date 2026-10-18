//! Explicit secret ingress for the trusted native CLI, with no plaintext file
//! required for a caller that already holds a process environment credential.
use anyhow::{Result, bail};
use std::{ffi::OsString, io::Read, path::Path};
use zeroize::Zeroizing;

const MAX_SECRET_BYTES: usize = 64 * 1024;

pub(super) fn load(file: Option<&Path>, environment: Option<&str>) -> Result<Zeroizing<Vec<u8>>> {
    match (file, environment) {
        (Some(path), None) => {
            let file = std::fs::File::open(path)?;
            if file.metadata()?.len() > MAX_SECRET_BYTES as u64 {
                bail!("secret input exceeds the allowed size");
            }
            let mut bytes = Zeroizing::new(Vec::new());
            file.take(MAX_SECRET_BYTES as u64 + 1)
                .read_to_end(&mut bytes)?;
            check_length(&bytes)?;
            Ok(bytes)
        }
        (None, Some(name)) => environment_with(name, |name| std::env::var_os(name)),
        _ => bail!("select exactly one secret input source"),
    }
}

fn environment_with(
    name: &str,
    lookup: impl FnOnce(&str) -> Option<OsString>,
) -> Result<Zeroizing<Vec<u8>>> {
    if name.is_empty()
        || name.len() > 256
        || !name.bytes().enumerate().all(|(index, byte)| {
            byte == b'_' || byte.is_ascii_alphabetic() || (index > 0 && byte.is_ascii_digit())
        })
    {
        bail!("invalid secret environment variable name");
    }
    let Some(value) = lookup(name) else {
        bail!("secret environment variable is unavailable")
    };
    let bytes = Zeroizing::new(value.into_encoded_bytes());
    // Validate while the owned bytes already have a zeroizing guard, including
    // the invalid-Unicode error path. No error owns or formats secret bytes.
    std::str::from_utf8(&bytes)
        .map_err(|_| anyhow::anyhow!("secret environment value is not UTF-8"))?;
    check_length(&bytes)?;
    Ok(bytes)
}

fn check_length(bytes: &[u8]) -> Result<()> {
    if bytes.len() > MAX_SECRET_BYTES {
        bail!("secret input exceeds the allowed size")
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn environment_source_preserves_exact_bytes_without_trimming_or_echoing() {
        let value = environment_with("TOBKIRI_TEST_SECRET", |_| {
            Some(OsString::from("exact bytes\n"))
        })
        .unwrap();
        assert_eq!(&**value, b"exact bytes\n");
        let error = environment_with("not=a=value", |_| {
            panic!("invalid names must not be looked up")
        })
        .unwrap_err();
        assert!(!error.to_string().contains("not=a=value"));
        assert!(environment_with("TOBKIRI_TEST_SECRET", |_| None).is_err());
    }

    #[test]
    fn oversized_secret_errors_do_not_include_the_value() {
        let error = environment_with("TOBKIRI_TEST_SECRET", |_| {
            Some(OsString::from("private".repeat(10000)))
        })
        .unwrap_err();
        assert!(!format!("{error:?}").contains("private"));
    }

    #[test]
    fn invalid_environment_encoding_is_rejected_without_exposing_its_bytes() {
        #[cfg(windows)]
        let value = {
            use std::os::windows::ffi::OsStringExt;
            OsString::from_wide(&[b'p' as u16, 0xd800])
        };
        #[cfg(unix)]
        let value = {
            use std::os::unix::ffi::OsStringExt;
            OsString::from_vec(vec![b'p', 0xff])
        };
        #[cfg(any(windows, unix))]
        {
            let error = environment_with("TOBKIRI_TEST_SECRET", |_| Some(value)).unwrap_err();
            assert_eq!(error.to_string(), "secret environment value is not UTF-8");
        }
    }

    #[test]
    fn file_source_is_bounded_and_remains_backwards_compatible() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("fixture-key");
        std::fs::write(&path, b"unchanged\n").unwrap();
        assert_eq!(&**load(Some(&path), None).unwrap(), b"unchanged\n");
        std::fs::write(&path, vec![b'x'; MAX_SECRET_BYTES + 1]).unwrap();
        assert!(load(Some(&path), None).is_err());
        assert!(load(None, None).is_err());
        assert!(load(Some(&path), Some("TOBKIRI_TEST_SECRET")).is_err());
    }
}
