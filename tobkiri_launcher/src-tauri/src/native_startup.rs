//! Trusted native process startup options. No renderer or environment fallback.

use std::{
    ffi::OsString,
    fmt,
    path::{Component, PathBuf},
};

const TEST_DATA_ROOT: &str = "--test-data-root";
const MAX_PATH_BYTES: usize = 4096;

#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct StartupOptions {
    pub(crate) test_data_root: Option<PathBuf>,
}

impl StartupOptions {
    /// Keep test WebView state beside, rather than inside, the Core DATA root.
    /// This only derives a path; WebView/Core retain their own file ownership.
    pub(crate) fn test_webview_data_directory(&self) -> Result<Option<PathBuf>, StartupError> {
        self.test_data_root
            .as_ref()
            .map(|root| {
                let parent = root.parent().ok_or(StartupError::InvalidTestDataRoot)?;
                let mut name = root
                    .file_name()
                    .ok_or(StartupError::InvalidTestDataRoot)?
                    .to_os_string();
                name.push(".tobkiri-webview");
                validate_absolute_path(parent.join(name).into_os_string())
            })
            .transpose()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum StartupError {
    UnsupportedArgument,
    DuplicateTestDataRoot,
    MissingTestDataRoot,
    InvalidTestDataRoot,
    TestDataRootUnavailable,
}

impl fmt::Display for StartupError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::UnsupportedArgument => "unsupported native startup argument",
            Self::DuplicateTestDataRoot => "duplicate native test data root",
            Self::MissingTestDataRoot => "native test data root requires an absolute path",
            Self::InvalidTestDataRoot => "invalid native test data root",
            Self::TestDataRootUnavailable => {
                "native test data root is available only in debug/test builds"
            }
        })
    }
}

impl std::error::Error for StartupError {}

/// Parse arguments excluding the executable name before opening any Core state.
pub(crate) fn parse(
    arguments: impl IntoIterator<Item = OsString>,
) -> Result<StartupOptions, StartupError> {
    parse_for_build(arguments, cfg!(debug_assertions))
}

fn parse_for_build(
    arguments: impl IntoIterator<Item = OsString>,
    debug_build: bool,
) -> Result<StartupOptions, StartupError> {
    let mut options = StartupOptions::default();
    let mut arguments = arguments.into_iter();
    while let Some(argument) = arguments.next() {
        if argument != TEST_DATA_ROOT {
            return Err(StartupError::UnsupportedArgument);
        }
        if !debug_build {
            return Err(StartupError::TestDataRootUnavailable);
        }
        if options.test_data_root.is_some() {
            return Err(StartupError::DuplicateTestDataRoot);
        }
        let path = arguments.next().ok_or(StartupError::MissingTestDataRoot)?;
        if path == TEST_DATA_ROOT {
            return Err(StartupError::DuplicateTestDataRoot);
        }
        options.test_data_root = Some(validate_absolute_path(path)?);
    }
    Ok(options)
}

fn validate_absolute_path(value: OsString) -> Result<PathBuf, StartupError> {
    if value.is_empty() || value.as_encoded_bytes().len() > MAX_PATH_BYTES {
        return Err(StartupError::InvalidTestDataRoot);
    }
    let text = value.to_str().ok_or(StartupError::InvalidTestDataRoot)?;
    if text.chars().any(char::is_control) {
        return Err(StartupError::InvalidTestDataRoot);
    }
    let path = PathBuf::from(value);
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
    {
        return Err(StartupError::InvalidTestDataRoot);
    }
    // This is lexical validation only. Core still opens the selected DataRoot
    // through its existing ownership, filesystem, recovery and approval checks.
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn absolute_path() -> OsString {
        if cfg!(windows) {
            OsString::from(r"C:\Tobkiri Native\isolated-tést-root")
        } else {
            OsString::from("/native-test/Tobkiri isolated-tést-root")
        }
    }

    #[test]
    fn empty_arguments_preserve_the_default_root() {
        assert_eq!(
            parse_for_build(Vec::<OsString>::new(), true),
            Ok(StartupOptions::default())
        );
        assert_eq!(
            parse_for_build(Vec::<OsString>::new(), false),
            Ok(StartupOptions::default())
        );
    }

    #[test]
    fn explicit_absolute_path_is_preserved_without_filesystem_access() {
        let path = absolute_path();
        assert_eq!(
            parse_for_build([TEST_DATA_ROOT.into(), path.clone()], true),
            Ok(StartupOptions {
                test_data_root: Some(PathBuf::from(path)),
            })
        );
    }

    #[test]
    fn missing_path_is_rejected() {
        assert_eq!(
            parse_for_build([TEST_DATA_ROOT.into()], true),
            Err(StartupError::MissingTestDataRoot)
        );
    }

    #[test]
    fn repeated_flag_never_selects_a_second_root() {
        assert_eq!(
            parse_for_build(
                [
                    TEST_DATA_ROOT.into(),
                    absolute_path(),
                    TEST_DATA_ROOT.into(),
                    absolute_path(),
                ],
                true,
            ),
            Err(StartupError::DuplicateTestDataRoot)
        );
        assert_eq!(
            parse_for_build([TEST_DATA_ROOT.into(), TEST_DATA_ROOT.into()], true),
            Err(StartupError::DuplicateTestDataRoot)
        );
    }

    #[test]
    fn empty_relative_and_parent_paths_are_rejected() {
        for path in [
            OsString::new(),
            OsString::from("relative-root"),
            PathBuf::from(absolute_path())
                .join("..")
                .join("other")
                .into_os_string(),
        ] {
            assert_eq!(
                parse_for_build([TEST_DATA_ROOT.into(), path], true),
                Err(StartupError::InvalidTestDataRoot)
            );
        }
    }

    #[test]
    fn unknown_arguments_and_equals_form_are_rejected() {
        for argument in [
            "--data-root",
            "--test-data-root=relative",
            "relative-root",
            "--",
        ] {
            assert_eq!(
                parse_for_build([argument.into()], true),
                Err(StartupError::UnsupportedArgument)
            );
        }
        assert_eq!(
            parse_for_build(
                [TEST_DATA_ROOT.into(), absolute_path(), "--other".into()],
                true
            ),
            Err(StartupError::UnsupportedArgument)
        );
    }

    #[test]
    fn oversized_and_control_paths_are_rejected_without_echoing_them() {
        let mut oversized = absolute_path();
        oversized.push("x".repeat(MAX_PATH_BYTES));
        for path in [
            oversized,
            OsString::from("/private\npath"),
            OsString::from("/private\0path"),
        ] {
            let error = parse_for_build([TEST_DATA_ROOT.into(), path], true).unwrap_err();
            assert_eq!(error, StartupError::InvalidTestDataRoot);
            assert_eq!(error.to_string(), "invalid native test data root");
        }
    }

    #[test]
    fn production_explicitly_rejects_the_test_flag() {
        assert_eq!(
            parse_for_build([TEST_DATA_ROOT.into(), absolute_path()], false),
            Err(StartupError::TestDataRootUnavailable)
        );
        assert_eq!(
            parse_for_build([TEST_DATA_ROOT.into()], false),
            Err(StartupError::TestDataRootUnavailable)
        );
    }

    #[test]
    fn test_cache_is_a_distinct_sibling_without_filesystem_access() {
        let root = PathBuf::from(absolute_path());
        let options = StartupOptions {
            test_data_root: Some(root.clone()),
        };
        let cache = options.test_webview_data_directory().unwrap().unwrap();
        assert_eq!(cache.parent(), root.parent());
        assert!(!cache.starts_with(&root));
        let mut name = root.file_name().unwrap().to_os_string();
        name.push(".tobkiri-webview");
        assert_eq!(cache.file_name(), Some(name.as_os_str()));
        assert_eq!(
            StartupOptions::default().test_webview_data_directory(),
            Ok(None)
        );
    }

    #[test]
    fn test_cache_cannot_fall_back_for_a_filesystem_root() {
        let root = if cfg!(windows) {
            PathBuf::from(r"C:\")
        } else {
            PathBuf::from("/")
        };
        let options = StartupOptions {
            test_data_root: Some(root),
        };
        assert_eq!(
            options.test_webview_data_directory(),
            Err(StartupError::InvalidTestDataRoot)
        );
    }

    #[test]
    fn data_roots_with_different_extensions_do_not_share_a_test_cache() {
        let first = PathBuf::from(absolute_path()).with_extension("one");
        let second = first.with_extension("two");
        let first = StartupOptions {
            test_data_root: Some(first),
        };
        let second = StartupOptions {
            test_data_root: Some(second),
        };
        assert_ne!(
            first.test_webview_data_directory(),
            second.test_webview_data_directory()
        );
    }

    #[test]
    fn public_parser_uses_the_compiled_build_mode() {
        let result = parse([TEST_DATA_ROOT.into(), absolute_path()]);
        if cfg!(debug_assertions) {
            assert!(result.is_ok());
        } else {
            assert_eq!(result, Err(StartupError::TestDataRootUnavailable));
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_drive_relative_and_root_relative_paths_are_rejected() {
        for path in [r"C:relative-root", r"\root-without-drive"] {
            assert_eq!(
                parse_for_build([TEST_DATA_ROOT.into(), path.into()], true),
                Err(StartupError::InvalidTestDataRoot)
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn non_unicode_path_is_rejected() {
        use std::os::unix::ffi::OsStringExt;
        assert_eq!(
            parse_for_build(
                [
                    TEST_DATA_ROOT.into(),
                    OsString::from_vec(b"/test/\xff".to_vec())
                ],
                true
            ),
            Err(StartupError::InvalidTestDataRoot)
        );
    }

    #[cfg(windows)]
    #[test]
    fn non_unicode_path_is_rejected() {
        use std::os::windows::ffi::OsStringExt;
        assert_eq!(
            parse_for_build(
                [
                    TEST_DATA_ROOT.into(),
                    OsString::from_wide(&[b'C' as u16, b':' as u16, b'\\' as u16, 0xd800])
                ],
                true
            ),
            Err(StartupError::InvalidTestDataRoot)
        );
    }
}
