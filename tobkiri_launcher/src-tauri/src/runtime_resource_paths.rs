//! Canonical path domains shared by resource generation and verification.

use std::path::Path;

pub(crate) const SEALED_PYTHON_RESOURCE_DIRECTORY: &str = "python-runtime";
const SEALED_APPLICATION_DIRECTORY: &str = "app";

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) struct CanonicalResourcePath(String);

impl CanonicalResourcePath {
    pub(crate) fn parse(value: &str) -> Result<Self, &'static str> {
        if value.is_empty()
            || !value.is_ascii()
            || value.starts_with('/')
            || value.ends_with('/')
            || value.contains('\0')
            || value.contains('\\')
            || value.bytes().any(|byte| !(0x20..=0x7e).contains(&byte))
            || value
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == ".." || part.contains(':'))
        {
            return Err("resource path is not a canonical portable relative path");
        }
        Ok(Self(value.to_owned()))
    }

    pub(crate) fn as_str(&self) -> &str {
        &self.0
    }

    pub(crate) fn as_path(&self) -> &Path {
        Path::new(&self.0)
    }

    pub(crate) fn ambiguity_key(&self) -> String {
        self.0.to_ascii_lowercase()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct SealedApplicationResourceBinding {
    pub(crate) sealed: CanonicalResourcePath,
    pub(crate) outer: CanonicalResourcePath,
    pub(crate) application: CanonicalResourcePath,
}

impl SealedApplicationResourceBinding {
    pub(crate) fn from_sealed_path(value: &str) -> Result<Self, &'static str> {
        let sealed = CanonicalResourcePath::parse(value)?;
        let application_value = sealed
            .as_str()
            .strip_prefix("app/")
            .ok_or("sealed application path is outside the exact app domain")?;
        let application = CanonicalResourcePath::parse(application_value)?;
        let outer = CanonicalResourcePath::parse(&format!(
            "{SEALED_PYTHON_RESOURCE_DIRECTORY}/{}/{}",
            SEALED_APPLICATION_DIRECTORY,
            application.as_str()
        ))?;
        Ok(Self {
            sealed,
            outer,
            application,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_the_three_resource_domains_exactly() {
        let binding =
            SealedApplicationResourceBinding::from_sealed_path("app/defaultspack_entry.py")
                .unwrap();
        assert_eq!(binding.sealed.as_str(), "app/defaultspack_entry.py");
        assert_eq!(
            binding.outer.as_str(),
            "python-runtime/app/defaultspack_entry.py"
        );
        assert_eq!(binding.application.as_str(), "defaultspack_entry.py");
    }

    #[test]
    fn packaged_artifact_fixture_matches_the_typed_binding() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../schemas/runtime-resource-path-binding.v1.json"
        ))
        .unwrap();
        assert_eq!(
            fixture["schema"],
            "io.tobkiri.runtime-resource-path-binding.v1"
        );
        let binding = SealedApplicationResourceBinding::from_sealed_path(
            fixture["sealed_path"].as_str().unwrap(),
        )
        .unwrap();
        assert_eq!(binding.outer.as_str(), fixture["outer_path"]);
        assert_eq!(binding.application.as_str(), fixture["application_path"]);
    }

    #[test]
    fn rejects_prefix_traversal_and_ambiguous_paths() {
        for value in [
            "defaultspack_entry.py",
            "application/defaultspack_entry.py",
            "app/../defaultspack_entry.py",
            "app//defaultspack_entry.py",
            "app\\defaultspack_entry.py",
            "app/Default.py/../default.py",
            "app/\u{e9}.py",
        ] {
            assert!(SealedApplicationResourceBinding::from_sealed_path(value).is_err());
        }
    }
}
