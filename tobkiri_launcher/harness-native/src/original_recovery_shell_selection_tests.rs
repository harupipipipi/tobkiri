//! Catalog selection and binding are separate from the descriptor walk.
use super::*;
use crate::original_authority::definitions::tests::{Source, operation, with_setup_source};
use serde_json::{Value, json};

const PROFILE: &str = "registered-profile";
const PACK: &str = "selected.pack";
const RELATIVE: &str = "nested//./TrustedShell.app";

fn guest(value: &Value, budget: &Arc<dyn AllocationBudget>) -> CoreValue {
    CoreValue::parse_guest(&serde_json::to_vec(value).unwrap(), budget).unwrap()
}

fn definition(availability: &str, matching_count: usize) -> Value {
    let mut variants = vec![json!({
        "platform": "macos", "architecture": "arm64",
        "entrypoint_digest": Digest::hash(b"different entrypoint"),
        "relative_path": "first.app",
    })];
    for _ in 0..matching_count {
        variants.push(json!({
            "platform": "macos", "architecture": "arm64",
            "entrypoint_digest": Digest::hash(b"entrypoint"),
            "relative_path": RELATIVE,
        }));
    }
    json!({
        "provider_id": "shell.fixture", "availability": availability,
        "launch": {"variants": variants},
        "definition_revision": Digest::hash(b"definition revision"),
    })
}

fn committed_profile() -> Value {
    json!({"profile_id": PROFILE, "shell": {
        "provider_id": "shell.fixture", "pack_id": PACK, "contract_id": "app.shell.v1",
        "platform": "macos", "architecture": "arm64",
        "executable_artifact_digest": Digest::hash(b"entrypoint"),
        "definition_revision": Digest::hash(b"definition revision"),
        "artifact_digest": Digest::hash(b"committed Pack"),
        "relative_path": "forged.app",
    }})
}

fn fixture_catalog(
    op: &Operation<'_, Source>,
    budget: &Arc<dyn AllocationBudget>,
    definition: Value,
    pack_digest: Option<Digest>,
) -> CatalogIo {
    let runtime = &op.owner.root;
    fs::create_dir_all(runtime).unwrap();
    let mut io = CatalogIo::new(
        op.owner,
        op.invocation,
        op.cancellation,
        op.cleanup.clone(),
        runtime,
        budget,
    )
    .unwrap();
    io.select_packaged(&runtime.join("fixture-catalog"))
        .unwrap();
    io.retain_recovery_pack_pins();
    io.bind_loader(CoreValue::string("fixture-catalog", budget).unwrap())
        .unwrap();
    let mut occurrences = vec![
        (
            "profile",
            json!({"profile_id": PROFILE, "shell": {"contract_id": "bundled.template"}}),
        ),
        ("shell", definition),
    ];
    if let Some(digest) = pack_digest {
        occurrences.push((
            "pack",
            json!({"pack": {"id": PACK, "artifact_digest": digest}}),
        ));
    }
    let entries: Vec<_> = occurrences
        .iter()
        .map(|(kind, _)| json!({"kind": kind}))
        .collect();
    let lock = serde_json::to_vec(&json!({"entries": entries})).unwrap();
    io.lock = Some(json::decode_catalog_lock(&lock, op, budget).unwrap());
    for (kind, value) in occurrences {
        let path = runtime.join(kind);
        fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
        let occurrence = read_file(op, &path, budget).unwrap();
        io.retain_pack_pin(op, &occurrence.value).unwrap();
        io.retain_shell_input(op, occurrence).unwrap();
        io.entry += 1;
        fs::remove_file(&path).unwrap();
    }
    io.artifact_root = Some(runtime.join("platform-artifacts"));
    io.loader = Loader::Done;
    assert!(io.completed());
    io
}

#[tokio::test(flavor = "current_thread")]
async fn recovery_catalog_verifies_the_selected_package_and_refuses_replaced_bytes() {
    with_setup_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let mut shell = definition("verified", 1);
        let root = source.root.join("platform-artifacts");
        let selected = root.join("nested/TrustedShell.app");
        fs::create_dir_all(selected.join("Contents/MacOS")).unwrap();
        let binary = selected.join("Contents/MacOS/Tobkiri");
        let mach_o = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01";
        let plist = br#"<?xml version="1.0" encoding="UTF-8"?>
            <plist version="1.0"><dict><key>CFBundleIdentifier</key>
            <string>io.tobkiri.fixture</string></dict></plist>"#;
        fs::write(&binary, mach_o).unwrap();
        fs::write(selected.join("Contents/Info.plist"), plist).unwrap();
        let mut tree = Sha256::new();
        for (name, data) in [
            (b"Contents/Info.plist".as_slice(), plist.as_slice()),
            (b"Contents/MacOS/Tobkiri".as_slice(), mach_o.as_slice()),
        ] {
            tree.update(name);
            tree.update(b"\0");
            tree.update(data);
        }
        let variant = &mut shell["launch"]["variants"][1];
        variant["artifact_digest"] = json!(Digest::from_sha256(tree.finalize().into()));
        variant["entrypoint_digest"] = json!(Digest::hash(mach_o));
        variant["entrypoint"] = json!("nested/TrustedShell.app/Contents/./MacOS/Tobkiri");
        variant["bundle_identity"] = json!("io.tobkiri.fixture");
        let mut profile = committed_profile();
        profile["shell"]["executable_artifact_digest"] = json!(Digest::hash(mach_o));
        let verify = |shell, profile: &Value| {
            let mut io = fixture_catalog(&op, budget, shell, Some(Digest::hash(b"committed Pack")));
            io.artifact_parent_identity = Some(identity(&fs::metadata(&source.root).unwrap()));
            let rows = guest(&json!([[PROFILE, profile]]), budget);
            RecoveryCatalogInputs::new(&io, &rows, None).verify_committed_artifact(
                &op,
                &guest(profile, budget),
                PROFILE,
            )
        };
        let assert_denied = |result: Result<()>, message: &str| {
            let denied = result.unwrap_err();
            assert_eq!(denied.class, RECOVERY_APP_DENIED);
            assert_eq!(
                denied.message,
                format!("active Profile Shell artifact rejected: {message}")
            );
        };
        verify(shell.clone(), &profile).unwrap();
        for (key, value, message) in [
            (
                "entrypoint",
                json!("elsewhere/Tobkiri"),
                "packaged artifact entrypoint is missing or outside artifact",
            ),
            (
                "bundle_identity",
                json!("io.tobkiri.different"),
                "macOS packaged artifact bundle identity does not match",
            ),
            (
                "architecture",
                json!("x86_64"),
                "packaged artifact architecture does not match selection",
            ),
            (
                "entrypoint_digest",
                json!(Digest::hash(b"bad pin")),
                "packaged artifact entrypoint digest does not match",
            ),
        ] {
            let mut changed_shell = shell.clone();
            let mut changed_profile = profile.clone();
            if key == "architecture" {
                changed_profile["shell"][key] = value.clone();
            } else if key == "entrypoint_digest" {
                changed_profile["shell"]["executable_artifact_digest"] = value.clone();
            }
            changed_shell["launch"]["variants"][1][key] = value;
            assert_denied(verify(changed_shell, &changed_profile), message);
        }
        fs::write(&binary, b"the replaced bytes").unwrap();
        assert_denied(
            verify(shell, &profile),
            "packaged artifact digest does not match selected bytes",
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn identity_selection_uses_the_catalog_digest_and_path_before_the_verify_binding() {
    with_setup_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let profile = committed_profile();
        let rows = guest(&json!([[PROFILE, &profile]]), budget);
        let profile = guest(&profile, budget);
        let io = fixture_catalog(&op, budget, definition("build_required", 1), None);
        let variant = io.recovery_identity_variant(&op, &profile).unwrap();
        assert_eq!(variant.relative(), RELATIVE);
        assert!(text_is(
            variant.variant(),
            "entrypoint_digest",
            Digest::hash(b"entrypoint").as_str()
        ));
        let error = io
            .verify_recovery_shell_binding(&op, &variant, PROFILE, &rows, None)
            .unwrap_err();
        assert_eq!(
            error.message,
            "active Profile Shell definition is unavailable"
        );

        for count in [0, 2] {
            let io = fixture_catalog(&op, budget, definition("verified", count), None);
            let error = io.recovery_identity_variant(&op, &profile).err().unwrap();
            assert_eq!(
                error.message,
                "active Profile Shell artifact is unavailable"
            );
        }
    });
}

#[tokio::test(flavor = "current_thread")]
async fn verification_binds_the_projected_registered_profile_live_pack_and_definition_revision() {
    with_setup_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let io = fixture_catalog(
            &op,
            budget,
            definition("verified", 1),
            Some(Digest::hash(b"committed Pack")),
        );
        let profile = committed_profile();
        let mut registered = profile.clone();
        registered["shell"]["artifact_digest"] = Value::Null;
        registered["shell"]["definition_revision"] = Value::Null;
        let rows = |registered: &Value| guest(&json!([[PROFILE, registered]]), budget);
        let selected = io
            .recovery_identity_variant(&op, &guest(&profile, budget))
            .unwrap();
        io.verify_recovery_shell_binding(&op, &selected, PROFILE, &rows(&registered), None)
            .unwrap();
        for field in [
            "provider_id",
            "pack_id",
            "contract_id",
            "platform",
            "architecture",
            "executable_artifact_digest",
        ] {
            let mut changed = registered.clone();
            changed["shell"][field] = json!("different");
            assert!(
                io.verify_recovery_shell_binding(&op, &selected, PROFILE, &rows(&changed), None)
                    .is_err()
            );
        }
        for field in ["artifact_digest", "definition_revision"] {
            let mut changed = profile.clone();
            changed["shell"][field] = json!(Digest::hash(b"retired"));
            let retired = io
                .recovery_identity_variant(&op, &guest(&changed, budget))
                .unwrap();
            assert!(
                io.verify_recovery_shell_binding(&op, &retired, PROFILE, &rows(&registered), None)
                    .is_err()
            );
        }
        assert!(
            io.verify_recovery_shell_binding(
                &op,
                &selected,
                "different-profile",
                &rows(&registered),
                None
            )
            .is_err()
        );
    });
}

#[tokio::test(flavor = "current_thread")]
async fn only_the_admitted_external_closure_supplies_a_missing_pack_pin() {
    with_setup_source(|source, read, cancellation, budget| {
        let op = operation(read, source, cancellation);
        let profile = committed_profile();
        let rows = guest(&json!([[PROFILE, &profile]]), budget);
        let profile = guest(&profile, budget);
        let external = |kind| {
            guest(
                &json!([[PACK, {"pack": {
            "id": PACK, "kind": kind, "artifact_digest": Digest::hash(b"committed Pack"),
        }}, {}]]),
                budget,
            )
        };
        let admitted = external("normal_sandbox");
        let wrong_kind = external("shell");

        let io = fixture_catalog(&op, budget, definition("verified", 1), None);
        let selected = io.recovery_identity_variant(&op, &profile).unwrap();
        for closure in [None, Some(&wrong_kind)] {
            assert!(
                io.verify_recovery_shell_binding(&op, &selected, PROFILE, &rows, closure)
                    .is_err()
            );
        }
        io.verify_recovery_shell_binding(&op, &selected, PROFILE, &rows, Some(&admitted))
            .unwrap();

        let io = fixture_catalog(
            &op,
            budget,
            definition("verified", 1),
            Some(Digest::hash(b"bundled Pack")),
        );
        let selected = io.recovery_identity_variant(&op, &profile).unwrap();
        assert!(
            io.verify_recovery_shell_binding(&op, &selected, PROFILE, &rows, Some(&admitted))
                .is_err()
        );
    });
}
