//! Stage the ordinary Setup Wasm from a separately pinned build receipt.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::Path;

#[path = "src/setup_component_path.rs"]
mod component;

pub const SOURCE_ENV: &str = "TOBKIRI_SETUP_COMPONENT_SOURCE";
pub const RECEIPT_ENV: &str = "TOBKIRI_SETUP_COMPONENT_RECEIPT";
pub const PIN_ENV: &str = "TOBKIRI_SETUP_COMPONENT_RECEIPT_SHA256";
pub const GENERATED: &str = "setup_component.rs";
const RECEIPT_LIMIT: u64 = 64 * 1024;
const RECEIPT_V1: &str = "io.tobkiri.launcher-setup-component.v1";
const RECEIPT_V2: &str = "io.tobkiri.launcher-setup-component.v2";
const RECEIPT_V3: &str = "io.tobkiri.launcher-setup-component.v3";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    format: String,
    component: Component,
    source_index_sha256: String,
    modules: Vec<Module>,
    profile_schemas: Option<ProfileSchemas>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Component {
    bytes: u64,
    sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Module {
    module: String,
    relative_file: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileSchemas {
    v4: Schema,
    v5: Schema,
    references: Option<ProfileSchemaReferences>,
    recovery: Option<ProfileRecoverySchemas>,
}

impl ProfileSchemas {
    fn verified(&self) -> bool {
        self.v4.verified(
            "https://schemas.tobkiri.local/io.tobkiri/profile.v4.schema.json",
            Some("io.tobkiri.profile.v4"),
        ) && self.v5.verified(
            "https://schemas.tobkiri.local/io.tobkiri/profile.v5.schema.json",
            Some("io.tobkiri.profile.v5"),
        ) && self.references.as_ref().is_none_or(|references| {
            references.common.verified(
                "https://schemas.tobkiri.local/io.tobkiri/common.v1.schema.json",
                None,
            ) && references.content_projection.verified(
                "https://schemas.tobkiri.local/io.tobkiri/profile-content-projection.v1.schema.json",
                None,
            )
        }) && self.recovery.as_ref().is_none_or(|recovery| {
            recovery
                .entries()
                .into_iter()
                .all(|(schema, id)| schema.verified(id, None))
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileSchemaReferences {
    common: Schema,
    content_projection: Schema,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ProfileRecoverySchemas {
    profile_lock_v4: Schema,
    profile_lock_v5: Schema,
    resolved_plan_v1: Schema,
    resolved_plan_v2: Schema,
    activation_record_v1: Schema,
    activation_record_v2: Schema,
    function_principal_v1: Schema,
}

impl ProfileRecoverySchemas {
    // Preserve the order of BootstrapProfileSchemas::recovery.
    fn entries(&self) -> [(&Schema, &str); 7] {
        [
            (
                &self.profile_lock_v4,
                "https://schemas.tobkiri.local/io.tobkiri/profile-lock.v4.schema.json",
            ),
            (
                &self.profile_lock_v5,
                "https://schemas.tobkiri.local/io.tobkiri/profile-lock.v5.schema.json",
            ),
            (
                &self.resolved_plan_v1,
                "https://schemas.tobkiri.local/io.tobkiri/resolved-plan.v1.schema.json",
            ),
            (
                &self.resolved_plan_v2,
                "https://schemas.tobkiri.local/io.tobkiri/resolved-plan.v2.schema.json",
            ),
            (
                &self.activation_record_v1,
                "https://schemas.tobkiri.local/io.tobkiri/activation-record.v1.schema.json",
            ),
            (
                &self.activation_record_v2,
                "https://schemas.tobkiri.local/io.tobkiri/activation-record.v2.schema.json",
            ),
            (
                &self.function_principal_v1,
                "https://schemas.tobkiri.local/io.tobkiri/function-principal.v1.schema.json",
            ),
        ]
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Schema {
    sha256: String,
    content: String,
}

impl Schema {
    fn verified(&self, id: &str, api_version: Option<&str>) -> bool {
        let bytes = self.content.as_bytes();
        if !(1..=RECEIPT_LIMIT as usize).contains(&bytes.len())
            || !valid_digest(&self.sha256)
            || format!("{:x}", Sha256::digest(bytes)) != self.sha256
        {
            return false;
        }
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(bytes) else {
            return false;
        };
        value.get("$id").and_then(serde_json::Value::as_str) == Some(id)
            && api_version.is_none_or(|version| {
                value
                    .pointer("/properties/profile_api_version/const")
                    .and_then(serde_json::Value::as_str)
                    == Some(version)
            })
    }
}

fn rust_schema(out: &mut String, bytes: &[u8]) {
    out.push_str("b\"");
    for (index, byte) in bytes.iter().enumerate() {
        if index > 0 && index % 20 == 0 {
            out.push_str("\\\n        ");
        }
        write!(out, "\\x{byte:02x}").expect("writing to String");
    }
    out.push_str("\",\n");
}

fn rust_schema_group<const N: usize>(out: &mut String, name: &str, schemas: Option<[&Schema; N]>) {
    if let Some(schemas) = schemas {
        writeln!(out, "    {name}: Some([").expect("writing to String");
        for schema in schemas {
            out.push_str("        ");
            rust_schema(out, schema.content.as_bytes());
        }
        out.push_str("    ]),\n");
    } else {
        writeln!(out, "    {name}: None,").expect("writing to String");
    }
}

fn schema_receipt_objects(raw: &[u8]) -> bool {
    // serde's derived structs also accept arrays of positional fields. This
    // receipt format requires named JSON objects in every schema position.
    fn fields_are_objects(value: &serde_json::Value, fields: &[&str]) -> bool {
        fields
            .iter()
            .all(|field| value.get(*field).is_some_and(serde_json::Value::is_object))
    }
    serde_json::from_slice::<serde_json::Value>(raw)
        .ok()
        .is_some_and(|receipt| {
            receipt.get("profile_schemas").is_some_and(|schemas| {
                fields_are_objects(schemas, &["v4", "v5"])
                    && schemas.get("references").is_none_or(|references| {
                        fields_are_objects(references, &["common", "content_projection"])
                    })
                    && schemas.get("recovery").is_none_or(|recovery| {
                        recovery
                            .as_object()
                            .is_some_and(|rows| rows.values().all(serde_json::Value::is_object))
                    })
            })
        })
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn valid_digest(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn physical_input(path: &Path, limit: u64) -> io::Result<impl Read> {
    if !path.is_absolute() || path.canonicalize()? != path {
        return Err(invalid(
            "Setup component input must be a physical absolute path",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC);
    }
    let file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || !(1..=limit).contains(&metadata.len()) {
        return Err(invalid(
            "Setup component input is not a bounded regular file",
        ));
    }
    Ok(file.take(limit + 1))
}

fn valid_module(row: &Module) -> bool {
    let name = row.module.split('.').all(|part| {
        let mut bytes = part.bytes();
        bytes
            .next()
            .is_some_and(|b| b == b'_' || b.is_ascii_alphabetic())
            && bytes.all(|b| b == b'_' || b.is_ascii_alphanumeric())
    });
    let path = row.relative_file.ends_with(".py")
        && row.relative_file.split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
        });
    name && path
}

pub fn stage(
    source: &Path,
    receipt: &Path,
    expected_receipt: &str,
    staged_root: &Path,
    generated: &Path,
) -> io::Result<()> {
    let mut raw = Vec::new();
    physical_input(receipt, RECEIPT_LIMIT)?.read_to_end(&mut raw)?;
    if raw.len() as u64 > RECEIPT_LIMIT
        || !valid_digest(expected_receipt)
        || format!("{:x}", Sha256::digest(&raw)) != expected_receipt
    {
        return Err(invalid(
            "Setup component receipt differs from the expected digest",
        ));
    }
    let receipt: Receipt = serde_json::from_slice(&raw).map_err(io::Error::other)?;
    let verified_schemas =
        |schemas: &ProfileSchemas| schema_receipt_objects(&raw) && schemas.verified();
    let catalog_delivery = match (receipt.format.as_str(), &receipt.profile_schemas) {
        (RECEIPT_V1, None) => "OriginalReads",
        (RECEIPT_V2, Some(schemas)) if verified_schemas(schemas) => "OriginalReads",
        // The receipt format itself binds the catalog protocol to this Wasm.
        (RECEIPT_V3, Some(schemas))
            if schemas.references.is_some()
                && schemas.recovery.is_some()
                && verified_schemas(schemas) =>
        {
            "OwnedSnapshotV1"
        }
        _ => {
            return Err(invalid(
                "Setup component receipt has invalid Profile schemas or format",
            ))
        }
    };
    let mut modules = BTreeSet::new();
    if !valid_digest(&receipt.component.sha256)
        || !(1..=component::MAX_COMPONENT_BYTES).contains(&receipt.component.bytes)
        || !valid_digest(&receipt.source_index_sha256)
        || !(1..=128).contains(&receipt.modules.len())
        || receipt
            .modules
            .iter()
            .any(|row| !valid_module(row) || !modules.insert(&row.module))
    {
        return Err(invalid(
            "Setup component receipt has an invalid component or module index",
        ));
    }

    let mut input = physical_input(source, component::MAX_COMPONENT_BYTES)?;
    let mut digest = Sha256::new();
    let mut bytes = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    let target = staged_root.join(component::RESOURCE);
    fs::create_dir(target.parent().expect("fixed component subdirectory"))?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)?;
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        bytes += read as u64;
        if bytes > receipt.component.bytes {
            return Err(invalid("Setup Wasm exceeds the verified size"));
        }
        digest.update(&buffer[..read]);
        output.write_all(&buffer[..read])?;
    }
    if bytes != receipt.component.bytes
        || format!("{:x}", digest.finalize()) != receipt.component.sha256
    {
        return Err(invalid("Setup Wasm differs from the verified component"));
    }
    output.flush()?;
    let component_digest = format!("sha256:{}", receipt.component.sha256);
    let (mut rust, schemas_ref) = if let Some(schemas) = receipt.profile_schemas {
        let mut rust = format!(
            "static PROFILE_SCHEMAS: BootstrapProfileSchemas = BootstrapProfileSchemas {{\n    component_sha256: {component_digest:?},\n"
        );
        for (version, schema) in [("v4", schemas.v4), ("v5", schemas.v5)] {
            write!(rust, "    {version}: ").expect("writing to String");
            rust_schema(&mut rust, schema.content.as_bytes());
        }
        rust_schema_group(
            &mut rust,
            "references",
            schemas
                .references
                .as_ref()
                .map(|rows| [&rows.common, &rows.content_projection]),
        );
        rust_schema_group(
            &mut rust,
            "recovery",
            schemas
                .recovery
                .as_ref()
                .map(|rows| rows.entries().map(|(schema, _)| schema)),
        );
        rust.push_str("};\n");
        (rust, "Some(&PROFILE_SCHEMAS)")
    } else {
        (String::new(), "None")
    };
    writeln!(
        rust,
        "static COMPONENT: Option<SetupComponent> = Some(SetupComponent {{ sha256: {:?}, catalog_delivery: BootstrapCatalogDelivery::{catalog_delivery}, profile_schemas: {schemas_ref}, index: &BootstrapModuleIndex {{ sha256: {:?}, modules: &[",
        component_digest, receipt.source_index_sha256,
    )
    .expect("writing to String");
    for row in receipt.modules {
        writeln!(
            rust,
            "    BootstrapModule {{ module: {:?}, relative_file: {:?} }},",
            row.module, row.relative_file
        )
        .expect("writing to String");
    }
    rust.push_str("] } });\n");
    fs::write(generated, rust)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn verified_bytes_and_index_are_staged_together() -> io::Result<()> {
        let root = std::env::temp_dir().canonicalize()?.join(format!(
            "tobkiri-setup-component-{:016x}",
            rand::random::<u64>(),
        ));
        fs::create_dir(&root)?;
        struct Cleanup<'a>(&'a Path);
        impl Drop for Cleanup<'_> {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(self.0);
            }
        }
        let _cleanup = Cleanup(&root);
        let source = root.join("source.wasm");
        let receipt = root.join("receipt.json");
        let generated = root.join(GENERATED);
        let wasm = b"opaque bounded fixture component";
        fs::write(&source, wasm)?;
        let mut document = serde_json::json!({
            "format": RECEIPT_V1,
            "component": {"sha256": format!("{:x}", Sha256::digest(wasm)), "bytes": wasm.len()},
            "source_index_sha256": "a".repeat(64),
            "modules": [{"module": "a.b", "relative_file": "a/b.py"}],
        });
        let pin = |document: &serde_json::Value| {
            let raw = document.to_string();
            fs::write(&receipt, &raw)?;
            Ok::<_, io::Error>(format!("{:x}", Sha256::digest(raw.as_bytes())))
        };
        let expected = pin(&document)?;
        assert!(stage(&source, &receipt, &"0".repeat(64), &root, &generated).is_err());
        document["modules"][0]["relative_file"] = "../b.py".into();
        assert!(stage(&source, &receipt, &pin(&document)?, &root, &generated).is_err());
        document["modules"][0]["relative_file"] = "a/b.py".into();
        assert_eq!(pin(&document)?, expected);
        let linked = root.join("link.wasm");
        std::os::unix::fs::symlink(&source, &linked)?;
        assert!(stage(&linked, &receipt, &expected, &root, &generated).is_err());
        let changed = root.join("changed.wasm");
        fs::write(&changed, [b'x'; 31])?;
        let rejected = root.join("rejected");
        fs::create_dir(&rejected)?;
        assert!(stage(&changed, &receipt, &expected, &rejected, &generated).is_err());
        assert!(!generated.exists());
        stage(&source, &receipt, &expected, &root, &generated)?;
        assert_eq!(fs::read(root.join(component::RESOURCE))?, wasm);
        let rust = fs::read_to_string(&generated)?;
        assert!(rust.contains(document["component"]["sha256"].as_str().unwrap()));
        assert!(rust.contains(&"a".repeat(64)));
        assert!(rust.contains("module: \"a.b\", relative_file: \"a/b.py\""));
        assert!(rust.contains("profile_schemas: None"));
        assert!(!rust.contains("static PROFILE_SCHEMAS"));
        assert!(rust.contains("catalog_delivery: BootstrapCatalogDelivery::OriginalReads"));

        let v2 = root.join("v2");
        fs::create_dir(&v2)?;
        let generated = v2.join(GENERATED);
        document["format"] = RECEIPT_V2.into();
        assert!(stage(&source, &receipt, &pin(&document)?, &v2, &generated).is_err());
        let row = |content: &str| serde_json::json!({"content": content, "sha256": format!("{:x}", Sha256::digest(content.as_bytes()))});
        let content = |version: &str| {
            serde_json::json!({
            "$id": format!("https://schemas.tobkiri.local/io.tobkiri/profile.{version}.schema.json"),
            "properties": {"profile_api_version": {"const": format!("io.tobkiri.profile.{version}")}},
            "title": "é",
        }).to_string()
        };
        let raw_v4 = content("v4") + "\r\n";
        let raw_v5 = content("v5") + "\n";
        let schemas = serde_json::json!({"v4": row(&raw_v4), "v5": row(&raw_v5)});
        document["profile_schemas"] = schemas.clone();
        let expected_v2 = pin(&document)?;
        document["profile_schemas"]["v4"]["content"] = raw_v4.trim_end().into();
        let altered_pin = pin(&document)?;
        // Reject both an untrusted receipt and a pinned receipt with bad DATA.
        assert!(stage(&source, &receipt, &expected_v2, &v2, &generated).is_err());
        assert!(stage(&source, &receipt, &altered_pin, &v2, &generated).is_err());
        document["profile_schemas"] = schemas.clone();
        document["profile_schemas"]["v6"] = row("{}");
        assert!(stage(&source, &receipt, &pin(&document)?, &v2, &generated).is_err());
        document["profile_schemas"] = serde_json::json!([row(&raw_v4), row(&raw_v5)]);
        assert!(stage(&source, &receipt, &pin(&document)?, &v2, &generated).is_err());
        document["profile_schemas"] = serde_json::json!({"v4": row(&raw_v5), "v5": row(&raw_v4)});
        assert!(stage(&source, &receipt, &pin(&document)?, &v2, &generated).is_err());
        document["profile_schemas"] = schemas.clone();
        document["profile_schemas"]["v4"] =
            row(&raw_v4.replace("io.tobkiri.profile.v4", "io.tobkiri.profile.v5"));
        assert!(stage(&source, &receipt, &pin(&document)?, &v2, &generated).is_err());
        assert!(!generated.exists());
        assert!(!v2.join(component::RESOURCE).exists());
        document["profile_schemas"] = schemas;
        assert_eq!(pin(&document)?, expected_v2);
        stage(&source, &receipt, &expected_v2, &v2, &generated)?;
        assert_eq!(fs::read(v2.join(component::RESOURCE))?, wasm);
        let rust = fs::read_to_string(generated)?;
        let component_digest = document["component"]["sha256"].as_str().unwrap();
        assert!(rust.contains("profile_schemas: Some(&PROFILE_SCHEMAS)"));
        assert!(rust.contains("references: None"));
        assert!(rust.contains("recovery: None"));
        assert!(rust.contains("catalog_delivery: BootstrapCatalogDelivery::OriginalReads"));
        assert!(rust.contains(&format!("component_sha256: \"sha256:{component_digest}\"")));
        let decode_literal = |literal: &str| {
            literal
                .split("\\x")
                .skip(1)
                .map(|value| u8::from_str_radix(value, 16).unwrap())
                .collect::<Vec<_>>()
        };
        let emitted = rust.replace("\\\n        ", "");
        for (version, content) in [("v4", raw_v4), ("v5", raw_v5)] {
            let prefix = format!("    {version}: b\"");
            let literals = emitted
                .split_once(&prefix)
                .unwrap()
                .1
                .split_once('"')
                .unwrap()
                .0;
            assert_eq!(decode_literal(literals), content.as_bytes());
        }

        let v2_references = root.join("v2-references");
        fs::create_dir(&v2_references)?;
        let generated = v2_references.join(GENERATED);
        let common =
            "{\"$id\":\"https://schemas.tobkiri.local/io.tobkiri/common.v1.schema.json\"}\r\n";
        let projection = "{\"$id\":\"https://schemas.tobkiri.local/io.tobkiri/profile-content-projection.v1.schema.json\"}\n";
        let references =
            serde_json::json!({"common": row(common), "content_projection": row(projection)});
        for invalid_references in [
            serde_json::Value::Null,
            serde_json::json!([row(common), row(projection)]),
            serde_json::json!({"common": row(common)}),
            serde_json::json!({"content_projection": row(projection)}),
            serde_json::json!({"common": row(common), "content_projection": row(projection), "extra": row("{}")}),
            serde_json::json!({"common": row(projection), "content_projection": row(common)}),
            serde_json::json!({"common": [references["common"]["sha256"], common], "content_projection": row(projection)}),
            serde_json::json!({"common": {"content": common.trim_end(), "sha256": references["common"]["sha256"]}, "content_projection": row(projection)}),
        ] {
            document["profile_schemas"]["references"] = invalid_references;
            assert!(stage(
                &source,
                &receipt,
                &pin(&document)?,
                &v2_references,
                &generated
            )
            .is_err());
        }
        assert!(!generated.exists());
        assert!(!v2_references.join(component::RESOURCE).exists());
        document["profile_schemas"]["references"] = references;
        stage(
            &source,
            &receipt,
            &pin(&document)?,
            &v2_references,
            &generated,
        )?;
        let group_contents = |rust: &str, name: &str| {
            let emitted = rust.replace("\\\n        ", "");
            let literals = emitted
                .split_once(&format!("    {name}: Some([\n"))
                .unwrap()
                .1
                .split_once("    ]),")
                .unwrap()
                .0;
            literals
                .lines()
                .map(|line| {
                    decode_literal(
                        line.strip_prefix("        b\"")
                            .unwrap()
                            .strip_suffix("\",")
                            .unwrap(),
                    )
                })
                .collect::<Vec<_>>()
        };
        let rust = fs::read_to_string(&generated)?;
        assert_eq!(
            group_contents(&rust, "references"),
            [common.as_bytes(), projection.as_bytes()]
        );
        assert!(rust.contains("recovery: None"));

        let v2_recovery = root.join("v2-recovery");
        fs::create_dir(&v2_recovery)?;
        let generated = v2_recovery.join(GENERATED);
        let recovery = [
            (
                "profile_lock_v4",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/profile_lock_v4.schema.json"),
            ),
            (
                "profile_lock_v5",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/profile_lock_v5.schema.json"),
            ),
            (
                "resolved_plan_v1",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/resolved_plan_v1.schema.json"),
            ),
            (
                "resolved_plan_v2",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/resolved_plan_v2.schema.json"),
            ),
            (
                "activation_record_v1",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/activation_record_v1.schema.json"),
            ),
            (
                "activation_record_v2",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/activation_record_v2.schema.json"),
            ),
            (
                "function_principal_v1",
                include_str!("../../tobkiri_runtime/tobkiri_protocol/schemas/function_principal_v1.schema.json"),
            ),
        ];
        let recovery_rows: serde_json::Map<_, _> = recovery
            .iter()
            .map(|(key, content)| (key.to_string(), row(content)))
            .collect();
        let (key, raw) = recovery[0];
        let mut missing = recovery_rows.clone();
        missing.remove(key);
        let mut extra = recovery_rows.clone();
        extra.insert("other".to_owned(), row("{}"));
        let mut wrong_id = recovery_rows.clone();
        wrong_id.insert(key.to_owned(), recovery_rows[recovery[1].0].clone());
        let mut wrong_digest = recovery_rows.clone();
        wrong_digest.get_mut(key).unwrap()["content"] = raw.trim_end().into();
        let mut positional_row = recovery_rows.clone();
        positional_row.insert(
            key.to_owned(),
            serde_json::json!([recovery_rows[key]["sha256"], raw]),
        );
        for invalid in [
            serde_json::Value::Null,
            serde_json::Value::Array(recovery.iter().map(|(_, content)| row(content)).collect()),
            missing.into(),
            extra.into(),
            wrong_id.into(),
            wrong_digest.into(),
            positional_row.into(),
        ] {
            document["profile_schemas"]["recovery"] = invalid;
            assert!(stage(
                &source,
                &receipt,
                &pin(&document)?,
                &v2_recovery,
                &generated
            )
            .is_err());
        }
        assert!(!generated.exists());
        assert!(!v2_recovery.join(component::RESOURCE).exists());
        document["profile_schemas"]["recovery"] = recovery_rows.into();
        stage(
            &source,
            &receipt,
            &pin(&document)?,
            &v2_recovery,
            &generated,
        )?;
        let rust = fs::read_to_string(&generated)?;
        assert_eq!(
            group_contents(&rust, "recovery"),
            recovery.map(|(_, content)| content.as_bytes())
        );
        assert!(rust.contains("catalog_delivery: BootstrapCatalogDelivery::OriginalReads"));

        let v3 = root.join("v3");
        fs::create_dir(&v3)?;
        let generated = v3.join(GENERATED);
        let legacy_pin = pin(&document)?;
        document["format"] = RECEIPT_V3.into();
        let expected = pin(&document)?;
        assert!(stage(&source, &receipt, &legacy_pin, &v3, &generated).is_err());

        for schema_path in [
            "/profile_schemas",
            "/profile_schemas/v4",
            "/profile_schemas/v5",
            "/profile_schemas/references",
            "/profile_schemas/references/content_projection",
            "/profile_schemas/recovery",
            "/profile_schemas/recovery/function_principal_v1",
        ] {
            let mut incomplete = document.clone();
            let (parent, field) = schema_path.rsplit_once('/').unwrap();
            incomplete
                .pointer_mut(parent)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(
                stage(&source, &receipt, &pin(&incomplete)?, &v3, &generated).is_err(),
                "v3 receipt accepted missing {schema_path}"
            );
        }

        let mut altered = document.clone();
        altered["profile_schemas"]["recovery"]["function_principal_v1"]["sha256"] =
            "0".repeat(64).into();
        assert!(stage(&source, &receipt, &pin(&altered)?, &v3, &generated).is_err());
        let mut open_format = document.clone();
        open_format["catalog_delivery"] = "owned-snapshot-v1".into();
        assert!(stage(&source, &receipt, &pin(&open_format)?, &v3, &generated).is_err());
        assert_eq!(pin(&document)?, expected);
        let linked_receipt = root.join("linked-receipt.json");
        std::os::unix::fs::symlink(&receipt, &linked_receipt)?;
        assert!(stage(&source, &linked_receipt, &expected, &v3, &generated).is_err());
        assert!(!generated.exists());
        assert!(!v3.join(component::RESOURCE).exists());
        stage(&source, &receipt, &expected, &v3, &generated)?;
        assert_eq!(fs::read(v3.join(component::RESOURCE))?, wasm);
        let rust = fs::read_to_string(&generated)?;
        assert!(rust.contains("catalog_delivery: BootstrapCatalogDelivery::OwnedSnapshotV1"));
        assert_eq!(
            group_contents(&rust, "references"),
            [common.as_bytes(), projection.as_bytes()]
        );
        assert_eq!(
            group_contents(&rust, "recovery"),
            recovery.map(|(_, content)| content.as_bytes())
        );
        Ok(())
    }
}
