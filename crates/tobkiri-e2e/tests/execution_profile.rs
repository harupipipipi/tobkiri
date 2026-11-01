//! Actual Core fixtures for the UNPROMOTED Core 1.2 Profile observation.
//! The small WAT components exercise the real canonical ABI and host import;
//! they are test probes, not migrated Registries or production activations.
mod support;

use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path, sync::Arc, time::Duration};
use support::{Host, parse_output, physical_temp};
use tobkiri_canon::{CanonValue, Digest, Variant, ids::ShapeRef};
use tobkiri_core::{Core, CoreConfig, CoreError, Session};
use tobkiri_gate::{EffectRequest, LoopLimits, Outcome, PermissionSet, ResourceLimits};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeConfig, RuntimeSpec, ShapeDefinition,
    ShapeKind, VerifiedPack,
};
use tobkiri_profile::{PackPin, Profile};

const CALLER: &str = "local.execution-profile-caller";
const OWNER: &str = "local.execution-profile-owner";
const CALLER_SHAPE: &str = "local.execution-profile-caller.probe@1.0";
const OWNER_SHAPE: &str = "local.execution-profile-owner.probe@1.0";

fn shape_wit(pack: &str, caller: bool) -> String {
    format!(
        "package local:{}@1.0.0;\ninterface probe {{\n\
         use tobkiri:core/types@1.2.0.{{error}};\n\
         read: func() -> result<string, error>;\n\
         verify: func(payload-profile: string, snapshot-profile: string) -> result<string, error>;\n\
         {}\n}}",
        pack.strip_prefix("local.").unwrap(),
        if caller {
            "relay: func() -> result<string, error>;"
        } else {
            ""
        },
    )
}

fn world_wit(pack: &str, caller: bool) -> String {
    format!(
        "package local:{}-component@1.0.0;\nworld pack {{\n\
         include tobkiri:core/pack-minimal@1.2.0;\n\
         {}\nexport local:{}/probe@1.0.0;\n}}",
        pack.strip_prefix("local.").unwrap(),
        if caller {
            "import local:execution-profile-owner/probe@1.0.0;"
        } else {
            ""
        },
        pack.strip_prefix("local.").unwrap(),
    )
}

fn component(caller: bool) -> Result<Vec<u8>> {
    // Each return buffer is in this component's own memory. Canonical lowering
    // writes the host result there; the probe never manufactures a Profile ID.
    let source = r#"(component
      (type $core-types (instance
        (type $code-definition (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
        (export "error-code" (type $named-code (eq $code-definition)))
        (type $error-definition (record (field "code" $named-code) (field "message" string)))
        (export "error" (type (eq $error-definition)))))
      (import "tobkiri:core/types@1.2.0" (instance $types-import (type $core-types)))
      (alias export $types-import "error-code" (type $code))
      (alias export $types-import "error" (type $error))
      (type $call-result (result string (error $error)))
      (type $init-result (result (error $error)))
      (type $flow (instance
        (export "current-profile" (func (result $call-result)))))
      (import "tobkiri:core/flow@1.2.0" (instance $flow-import (type $flow)))
      (alias export $flow-import "current-profile" (func $current-profile))
      $RELAY_IMPORT
      (core module $storage
        (memory (export "memory") 1 1)
        (global $next (mut i32) (i32.const 4096))
        (func (export "realloc") (param i32 i32) (param $align i32) (param $size i32) (result i32)
          (local $at i32)
          global.get $next local.get $align i32.const 1 i32.sub i32.add
          i32.const 0 local.get $align i32.sub i32.and local.tee $at
          local.get $size i32.add global.set $next local.get $at))
      (core instance $storage-instance (instantiate $storage))
      (alias core export $storage-instance "memory" (core memory $memory))
      (alias core export $storage-instance "realloc" (core func $realloc))
      (core func $profile-lowered
        (canon lower (func $current-profile) (memory $memory) (realloc $realloc)))
      $RELAY_LOWER
      (core module $probe
        (import "host" "memory" (memory 1 1))
        (import "host" "profile" (func $profile (param i32)))
        $RELAY_CORE_IMPORT
        (data (i32.const 256) "profile binding mismatch")
        (func (export "init") (param i32 i32) (result i32)
          i32.const 512 i32.const 0 i32.store i32.const 512)
        (func (export "shutdown"))
        (func $read (export "read") (result i32)
          i32.const 64 call $profile i32.const 64)
        (func $equal (param $a i32) (param $an i32) (param $b i32) (param $bn i32) (result i32)
          (local $i i32)
          local.get $an local.get $bn i32.ne if i32.const 0 return end
          block $done
            loop $again
              local.get $i local.get $an i32.ge_u br_if $done
              local.get $a local.get $i i32.add i32.load8_u
              local.get $b local.get $i i32.add i32.load8_u
              i32.ne if i32.const 0 return end
              local.get $i i32.const 1 i32.add local.set $i br $again
            end
          end i32.const 1)
        (func (export "verify") (param $p i32) (param $pn i32) (param $s i32) (param $sn i32) (result i32)
          call $read drop
          i32.const 64 i32.load if i32.const 64 return end
          local.get $p local.get $pn i32.const 68 i32.load i32.const 72 i32.load call $equal
          local.get $s local.get $sn i32.const 68 i32.load i32.const 72 i32.load call $equal
          i32.and if i32.const 64 return end
          i32.const 128 i32.const 1 i32.store
          i32.const 132 i32.const 4 i32.store
          i32.const 136 i32.const 256 i32.store
          i32.const 140 i32.const 24 i32.store
          i32.const 128)
        $RELAY_CORE_EXPORT)
      (core instance $probe-instance (instantiate $probe
        (with "host" (instance
          (export "memory" (memory $memory))
          (export "profile" (func $profile-lowered))
          $RELAY_CORE_BIND))))
      (alias core export $probe-instance "init" (core func $init))
      (alias core export $probe-instance "shutdown" (core func $shutdown))
      (alias core export $probe-instance "read" (core func $read))
      (alias core export $probe-instance "verify" (core func $verify))
      (func $init-value (param "config" string) (result $init-result)
        (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $shutdown-value (canon lift (core func $shutdown)))
      (func $read-value (result $call-result)
        (canon lift (core func $read) (memory $memory) (realloc $realloc)))
      (func $verify-value (param "payload-profile" string) (param "snapshot-profile" string) (result $call-result)
        (canon lift (core func $verify) (memory $memory) (realloc $realloc)))
      $RELAY_LIFT
      (instance $types (export "error-code" (type $code)) (export "error" (type $error)))
      (export "tobkiri:core/types@1.2.0" (instance $types))
      (instance $lifecycle (export "init" (func $init-value)) (export "shutdown" (func $shutdown-value)))
      (export "tobkiri:core/lifecycle@1.2.0" (instance $lifecycle))
      (instance $api (export "read" (func $read-value)) (export "verify" (func $verify-value)) $RELAY_EXPORT)
      (export "$EXPORT_INTERFACE" (instance $api)))"#;
    let replacements = [
        (
            "$RELAY_IMPORT",
            if caller {
                r#"(type $destination (instance (export "read" (func (result $call-result)))))
          (import "local:execution-profile-owner/probe@1.0.0" (instance $destination-import (type $destination)))
          (alias export $destination-import "read" (func $destination-read))"#
            } else {
                ""
            },
        ),
        (
            "$RELAY_LOWER",
            if caller {
                "(core func $destination-lowered (canon lower (func $destination-read) (memory $memory) (realloc $realloc)))"
            } else {
                ""
            },
        ),
        (
            "$RELAY_CORE_IMPORT",
            if caller {
                "(import \"host\" \"relay\" (func $relay (param i32)))"
            } else {
                ""
            },
        ),
        (
            "$RELAY_CORE_EXPORT",
            if caller {
                "(func (export \"relay\") (result i32) i32.const 96 call $relay i32.const 96)"
            } else {
                ""
            },
        ),
        (
            "$RELAY_CORE_BIND",
            if caller {
                "(export \"relay\" (func $destination-lowered))"
            } else {
                ""
            },
        ),
        (
            "$RELAY_LIFT",
            if caller {
                "(alias core export $probe-instance \"relay\" (core func $relay-read)) (func $relay-value (result $call-result) (canon lift (core func $relay-read) (memory $memory) (realloc $realloc)))"
            } else {
                ""
            },
        ),
        (
            "$RELAY_EXPORT",
            if caller {
                "(export \"relay\" (func $relay-value))"
            } else {
                ""
            },
        ),
        (
            "$EXPORT_INTERFACE",
            if caller {
                "local:execution-profile-caller/probe@1.0.0"
            } else {
                "local:execution-profile-owner/probe@1.0.0"
            },
        ),
    ];
    let mut source = source.to_owned();
    for (key, value) in replacements {
        source = source.replace(key, value);
    }
    Ok(wat::parse_str(source)?)
}

// Match the existing actual Core provider-proxy fixture's bounded limits.
fn permissions() -> PermissionSet {
    PermissionSet {
        wasm: ResourceLimits {
            fuel_per_call: 10_000_000,
            memory_mib: 16,
            wall_ms: 5000,
            table_elements: 10_000,
            output_bytes: 1024 * 1024,
        },
        loops: LoopLimits {
            max_iterations: 100,
            max_wall_ms: 5000,
            max_cost_units: 10_000,
        },
        ..Default::default()
    }
}

fn pack(root: &Path, caller: bool) -> Result<(VerifiedPack, PackPin)> {
    let id = if caller { CALLER } else { OWNER };
    let shape = ShapeRef::parse(if caller { CALLER_SHAPE } else { OWNER_SHAPE })?;
    let mut permissions = permissions();
    if caller {
        permissions.ports.insert(OWNER_SHAPE.into());
    }
    let mut files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
        ("component.wasm".into(), component(caller)?),
        ("wit/pack.wit".into(), world_wit(id, caller).into_bytes()),
        ("wit/probe.wit".into(), shape_wit(id, caller).into_bytes()),
        ("wit/deps/core.wit".into(), tobkiri_wit_source()),
        (
            "config.json".into(),
            br#"{"profile_id":"forged-runtime-config-profile"}"#.to_vec(),
        ),
        (
            "wit/probe.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
    ]);
    if caller {
        files.insert(
            "wit/deps/owner.wit".into(),
            shape_wit(OWNER, false).into_bytes(),
        );
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: id.parse()?,
        version: "0.1.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "wat/actual-core-execution-profile-test".into(),
            config: Some(RuntimeConfig {
                path: "config.json".into(),
                shape: None,
            }),
        },
        shapes: vec![ShapeDefinition {
            name: shape.name.clone(),
            version: shape.version,
            kind: ShapeKind::Port,
            // Shape loading resolves its own document plus the closed Core
            // packages. The complete world/dependency closure is verified
            // separately from all sealed WIT files by inspect_pack_wit.
            wit: "wit/probe.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: shape.name.clone(),
                version: shape.version,
                optional: false,
            }],
            consumes: if caller {
                vec![PortDeclaration {
                    shape: ShapeRef::parse(OWNER_SHAPE)?.name,
                    version: shape.version,
                    optional: false,
                }]
            } else {
                vec![]
            },
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions,
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let directory = root.join(id);
    fs::create_dir(&directory)?;
    for (path, bytes) in &files {
        let destination = directory.join(path);
        fs::create_dir_all(destination.parent().unwrap())?;
        fs::write(destination, bytes)?;
    }
    fs::write(
        directory.join("pack.json"),
        tobkiri_manifest::canonical_manifest(&manifest)?,
    )?;
    let verified = tobkiri_manifest::verify_directory(&directory, &BTreeMap::new())?;
    let pin = PackPin {
        id: verified.manifest.id.clone(),
        version: verified.manifest.version.clone(),
        sha256: verified.hash.clone(),
        source: format!("file:{}", directory.canonicalize()?.display()),
    };
    Ok((verified, pin))
}

fn tobkiri_wit_source() -> Vec<u8> {
    include_bytes!("../../tobkiri-wit/wit/core-v1-2/core.wit").to_vec()
}

fn profile(id: &str, packs: &[(VerifiedPack, PackPin)]) -> Result<Profile> {
    let ceiling = PermissionSet {
        ports: [CALLER_SHAPE.into(), OWNER_SHAPE.into()]
            .into_iter()
            .collect(),
        ..permissions()
    };
    let user = PermissionSet {
        ports: ceiling.ports.clone(),
        wasm: ceiling.wasm.clone(),
        loops: ceiling.loops.clone(),
        ..Default::default()
    };
    Ok(serde_json::from_value(json!({
        "format":"tobkiri.profile/1", "id":id, "name":"Unpromoted execution Profile actual-Core fixture",
        "packs":packs.iter().map(|(_,pin)| pin).collect::<Vec<_>>(), "publishers":{},
        "ceiling":ceiling, "user_permissions":user,
        "pack_limits":packs.iter().map(|(p,_)| (p.manifest.id.clone(),p.manifest.permissions.clone())).collect::<BTreeMap<_,_>>(),
        "bindings":{"local.execution-profile-caller.probe@1":CALLER,"local.execution-profile-owner.probe@1":OWNER},
        "overlay_order":[], "user_overlays":[], "startup":{"inputs":[],"ui":null}
    }))?)
}

fn audit(data: &Path, profile: &Profile) -> Result<Vec<Value>> {
    let connection = rusqlite::Connection::open_with_flags(
        data.join(tobkiri_store::DataRoot::profile_relative(
            &profile.id,
            "audit.sqlite",
        )?),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?;
    let mut statement = connection.prepare("SELECT body FROM audit ORDER BY seq LIMIT 1025")?;
    let rows = statement.query_map([], |row| row.get::<_, Vec<u8>>(0))?;
    let mut out = Vec::new();
    for row in rows {
        ensure!(out.len() < 1024, "audit fixture bound exceeded");
        out.push(serde_json::from_slice(&row?)?);
    }
    Ok(out)
}

fn succeeded_port(rows: &[Value], shape: &str, operation: &str, args: &CanonValue) -> Result<()> {
    let effect = EffectRequest::PortCall {
        shape: shape.parse()?,
        op: operation.parse()?,
    }
    .digest()?
    .to_string();
    let args = tobkiri_canon::digest(args, Variant::StrictV1)?.to_string();
    let dispatches = rows
        .iter()
        .filter(|r| {
            r["kind"] == "dispatch"
                && r["payload"]["effect_digest"] == effect
                && r["payload"]["args_digest"] == args
        })
        .collect::<Vec<_>>();
    ensure!(
        dispatches.len() == 1,
        "expected one actual dispatch for {shape}/{operation}"
    );
    let dispatch = dispatches[0];
    let outcomes = rows
        .iter()
        .filter(|r| {
            r["kind"] == "outcome"
                && r["request"] == dispatch["request"]
                && r["principal"] == dispatch["principal"]
                && r["payload"]["grant"] == dispatch["payload"]["grant"]
                && r["payload"]["effect_digest"] == effect
                && r["payload"]["args_digest"] == args
        })
        .collect::<Vec<_>>();
    ensure!(
        outcomes.len() == 1
            && outcomes[0]["payload"]["outcome"] == serde_json::to_value(Outcome::Succeeded)?,
        "same Grant did not reach one successful terminal"
    );
    Ok(())
}

async fn read(
    session: &Session,
    shape: &str,
    operation: &str,
    args: &CanonValue,
) -> Result<String> {
    let result = tokio::time::timeout(
        Duration::from_secs(30),
        session.call_user_port(shape.parse()?, operation.parse()?, args),
    )
    .await
    .context("Profile probe timed out")??;
    match parse_output(&result)? {
        CanonValue::Str(value) => Ok(value),
        _ => anyhow::bail!("Profile probe returned a non-string"),
    }
}

#[tokio::test]
async fn current_profile_uses_two_actual_sessions_and_inter_pack_destination_frames() -> Result<()>
{
    let directory = physical_temp()?;
    let packs = [
        pack(directory.path(), false)?,
        pack(directory.path(), true)?,
    ];
    let a = profile("prof_context_a", &packs)?;
    let b = profile("prof_context_b", &packs)?;
    let host = Arc::new(Host::default());
    for (shape, operation) in [
        (CALLER_SHAPE, "read"),
        (CALLER_SHAPE, "relay"),
        (OWNER_SHAPE, "read"),
        (OWNER_SHAPE, "verify"),
    ] {
        host.utility_ports
            .lock()
            .unwrap()
            .insert((shape.into(), operation.into()));
    }
    host.approve_literal_user(&a)?;
    host.approve_literal_user(&b)?;
    let data = directory.path().join("data");
    let core = Core::open(CoreConfig::for_test_data_root(data.clone()), host)?;
    core.save_profile(a.clone(), None).await?;
    core.save_profile(b.clone(), None).await?;
    let sa = core.start(&a.id).await?;
    let sb = match core.start(&b.id).await {
        Ok(session) => session,
        Err(error) => {
            let close = sa.close().await;
            return Err(anyhow::Error::new(error).context(format!(
                "second Profile start failed; first Session close: {close:?}"
            )));
        }
    };
    let empty = CanonValue::Obj(BTreeMap::new());
    let checked = async {
        for (session, profile) in [(&sa, &a), (&sb, &b), (&sa, &a)] {
            for (shape, operation) in [
                (CALLER_SHAPE, "read"),
                (OWNER_SHAPE, "read"),
                (CALLER_SHAPE, "relay"),
            ] {
                let before = audit(&data, profile)?.len();
                let observed = read(session, shape, operation, &empty).await?;
                ensure!(
                    observed == profile.id.as_str(),
                    "Profile observation escaped the actual Session: {observed}"
                );
                let rows = audit(&data, profile)?;
                let delta = rows.get(before..).context("audit shrank")?;
                succeeded_port(delta, shape, operation, &empty)?;
                if operation == "relay" {
                    succeeded_port(delta, OWNER_SHAPE, "read", &empty)?;
                }
            }
            for (payload, snapshot, valid) in [
                (profile.id.as_str(), profile.id.as_str(), true),
                ("forged-payload-profile", profile.id.as_str(), false),
                (profile.id.as_str(), "forged-snapshot-profile", false),
            ] {
                let args = CanonValue::Obj(BTreeMap::from([
                    ("payload-profile".into(), CanonValue::Str(payload.into())),
                    ("snapshot-profile".into(), CanonValue::Str(snapshot.into())),
                ]));
                if valid {
                    let observed = read(session, OWNER_SHAPE, "verify", &args).await?;
                    ensure!(
                        observed == profile.id.as_str(),
                        "owner Profile verification returned a foreign Profile: {observed}"
                    );
                } else {
                    let failure = session
                        .call_user_port(OWNER_SHAPE.parse()?, "verify".parse()?, &args)
                        .await
                        .err()
                        .context("foreign payload/snapshot accepted")?;
                    ensure!(
                        matches!(failure, CoreError::Step(ref error) if error.code == "invalid"),
                        "unexpected rejection: {failure}"
                    );
                }
            }
        }
        Ok::<(), anyhow::Error>(())
    }
    .await;
    let (ca, cb) = tokio::join!(sa.close(), sb.close());
    ensure!(
        ca.is_ok() && cb.is_ok(),
        "Profile checks: {checked:?}; Session closes: {ca:?}; {cb:?}"
    );
    checked
}
