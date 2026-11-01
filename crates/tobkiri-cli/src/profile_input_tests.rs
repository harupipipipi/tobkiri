use super::*;
use serde_json::{Value, json};

fn document(format: &str, sources: &[&str]) -> Value {
    let mut value = json!({
        "format": format,
        "packs": sources.iter().enumerate().map(|(index, source)| json!({
            "id": format!("local.fixture{index}"), "version": "1.0.0",
            "sha256": format!("sha256:{}", "0".repeat(64)), "source": source,
        })).collect::<Vec<_>>(),
        "publishers": {}, "ceiling": {"loops": {
            "max_iterations": 1, "max_wall_ms": 1000, "max_cost_units": 10,
        }},
        "pack_limits": {}, "bindings": {}, "overlay_order": [],
        "user_overlays": [], "startup": {"inputs": [], "ui": null},
    });
    if format == "tobkiri.profile/1" {
        value["id"] = json!("prof_fixture");
        value["name"] = json!("Source fixture");
    }
    value
}

fn write_document(path: &Path, value: &Value) {
    std::fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

#[test]
fn create_and_import_resolve_local_sources_from_the_input_file() {
    let fixture = tempfile::tempdir().unwrap();
    let profiles = fixture.path().join("profiles");
    let pack = fixture.path().join("pack");
    let artifact = fixture.path().join("fixture.tpk");
    std::fs::create_dir(&profiles).unwrap();
    std::fs::create_dir(&pack).unwrap();
    std::fs::write(&artifact, b"pinned artifact").unwrap();
    for format in ["tobkiri.profile/1", "tobkiri.published-profile/1"] {
        let path = profiles.join("input.json");
        write_document(
            &path,
            &document(format, &["file:../pack", "path:../fixture.tpk"]),
        );
        let bytes = definition(&path).unwrap();
        let normalized: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(normalized["format"], format);
        assert_eq!(
            normalized["packs"][0]["source"],
            format!("file:{}", std::fs::canonicalize(&pack).unwrap().display())
        );
        assert_eq!(
            normalized["packs"][1]["source"],
            format!(
                "path:{}",
                std::fs::canonicalize(&artifact).unwrap().display()
            )
        );
        assert_eq!(
            normalized["packs"][0]["sha256"],
            format!("sha256:{}", "0".repeat(64))
        );
        if format == "tobkiri.published-profile/1" {
            assert_eq!(published(&path).unwrap(), bytes);
        } else {
            assert!(published(&path).is_err());
        }
    }
}

#[test]
fn absolute_and_bare_local_paths_preserve_compatibility() {
    let fixture = tempfile::tempdir().unwrap();
    let artifact = fixture.path().join("fixture.tpk");
    std::fs::write(&artifact, b"artifact").unwrap();
    let absolute = std::fs::canonicalize(&artifact).unwrap();
    for prefix in ["file:", "path:", ""] {
        let source = format!("{prefix}{}", absolute.display());
        assert_eq!(
            resolve_source(&source, Path::new("/unrelated")).unwrap(),
            source
        );
    }
    assert_eq!(
        resolve_source("fixture.tpk", fixture.path()).unwrap(),
        absolute.to_str().unwrap()
    );
    #[cfg(windows)]
    {
        // Both native canonical verbatim-drive paths and ordinary drive paths
        // are local filesystem paths, unlike UNC authorities.
        let drive = absolute.to_str().unwrap().strip_prefix(r"\\?\").unwrap();
        for local in [drive.to_string(), drive.replace('\\', "/")] {
            assert_eq!(
                resolve_source(&format!("file:{local}"), Path::new("unrelated")).unwrap(),
                format!("file:{}", absolute.display())
            );
        }
    }
}

#[test]
fn malformed_create_preserves_the_primary_profile_parse_diagnostic() {
    let fixture = tempfile::tempdir().unwrap();
    let path = fixture.path().join("input.json");
    let mut malformed = document("tobkiri.profile/1", &[]);
    malformed.as_object_mut().unwrap().remove("id");
    let bytes = serde_json::to_vec(&malformed).unwrap();
    let primary = parse_profile(&bytes).unwrap_err().to_string();
    let fallback = parse_published(&bytes).unwrap_err().to_string();
    assert_ne!(primary, fallback);
    std::fs::write(&path, bytes).unwrap();
    let error = definition(&path).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("published Profile fallback also failed")
    );
    assert!(error.chain().any(|cause| cause.to_string() == primary));
}

#[test]
fn url_and_unc_authorities_are_rejected_before_filesystem_resolution() {
    for source in [
        "file://example.test/share/pack.tpk",
        "file:///tmp/pack.tpk",
        "path://example.test/share/pack.tpk",
        "//example.test/share/pack.tpk",
        r"\\example.test\share\pack.tpk",
        r"file:\\example.test\share\pack.tpk",
        r"path:\\example.test\share\pack.tpk",
        r"\\?\UNC\example.test\share\pack.tpk",
        r"\\.\pipe\fixture",
        r"/\example.test\share\pack.tpk",
        r"\/example.test/share/pack.tpk",
    ] {
        let error = resolve_source(source, Path::new("unresolved-origin")).unwrap_err();
        // This exact error is raised before canonicalize; a filesystem error
        // would instead carry the `resolve local Pack source` context.
        assert_eq!(
            error.to_string(),
            "Pack source must resolve to a verified local directory or .tpk artifact",
            "{source:?}"
        );
        assert_eq!(error.chain().count(), 1, "{source:?}");
    }
}

#[test]
fn local_percent_characters_are_not_url_decoded() {
    let fixture = tempfile::tempdir().unwrap();
    let literal = fixture.path().join("literal%20name.tpk");
    std::fs::write(&literal, b"literal percent path").unwrap();
    std::fs::write(fixture.path().join("literal name.tpk"), b"different file").unwrap();
    let source = resolve_source("file:literal%20name.tpk", fixture.path()).unwrap();
    let frozen = source.strip_prefix("file:").unwrap();
    let canonical = std::fs::canonicalize(literal).unwrap();
    assert_eq!(Path::new(frozen), canonical.as_path());
    assert_eq!(std::fs::read(frozen).unwrap(), b"literal percent path");
}

#[cfg(unix)]
#[test]
fn non_utf8_absolute_source_paths_fail_without_relative_fallback() {
    use std::{ffi::OsString, os::unix::ffi::OsStringExt};

    // APFS cannot create this filename. Exercise the exact final conversion
    // used after canonicalize with an in-memory absolute Path instead.
    let resolved = PathBuf::from(OsString::from_vec(b"/non-utf8-\xff/fixture.tpk".to_vec()));
    assert!(resolved.is_absolute());
    for prefix in ["file:", "path:", ""] {
        let error = stored_source(prefix, &resolved).unwrap_err();
        assert_eq!(
            error.to_string(),
            "resolved Pack source path must be UTF-8 for an absolute stored locator"
        );
    }
    assert_eq!(
        stored_source("file:", Path::new("fixture.tpk"))
            .unwrap_err()
            .to_string(),
        "resolved Pack source path must be absolute for a stored locator"
    );
}

#[test]
fn malformed_or_unsupported_sources_and_documents_still_fail() {
    let fixture = tempfile::tempdir().unwrap();
    for source in [
        "",
        "file:",
        "path:",
        "https://example.test/pack.tpk",
        "file:https://example.test/pack.tpk",
        "bundled:fixture",
        "path:bundled:fixture",
        "file:missing.tpk",
        "file:bad\0path",
    ] {
        assert!(
            resolve_source(source, fixture.path()).is_err(),
            "{source:?}"
        );
    }
    let path = fixture.path().join("input.json");
    let mut malformed = document("tobkiri.published-profile/1", &[]);
    malformed["approved"] = json!(true);
    write_document(&path, &malformed);
    assert!(published(&path).is_err());
    std::fs::write(
        &path,
        br#"{"format":"tobkiri.published-profile/1","format":"duplicate"}"#,
    )
    .unwrap();
    assert!(definition(&path).is_err());
}

#[test]
fn frozen_import_is_independent_of_working_directory() {
    const CHILD: &str = "TOBKIRI_PROFILE_SOURCE_TEST_INPUT";
    if let Some(input) = std::env::var_os(CHILD) {
        let bytes = std::fs::read(input).unwrap();
        let document = parse_published(&bytes).unwrap();
        let source = document.packs[0].source.strip_prefix("file:").unwrap();
        assert!(Path::new(source).is_absolute());
        assert_eq!(std::fs::read(source).unwrap(), b"actual pinned artifact");
        return;
    }
    let fixture = tempfile::tempdir().unwrap();
    let profiles = fixture.path().join("profiles");
    let unrelated = fixture.path().join("other-cwd");
    std::fs::create_dir(&profiles).unwrap();
    std::fs::create_dir(&unrelated).unwrap();
    std::fs::write(
        fixture.path().join("fixture.tpk"),
        b"actual pinned artifact",
    )
    .unwrap();
    let input = profiles.join("input.json");
    write_document(
        &input,
        &document("tobkiri.published-profile/1", &["file:../fixture.tpk"]),
    );
    let frozen = fixture.path().join("frozen.json");
    std::fs::write(&frozen, published(&input).unwrap()).unwrap();
    // A child isolates the process-wide CWD change from parallel unit tests.
    let status = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "commands::profile_input::tests::frozen_import_is_independent_of_working_directory",
        ])
        .env(CHILD, &frozen)
        .current_dir(&unrelated)
        .status()
        .unwrap();
    assert!(status.success());
}
