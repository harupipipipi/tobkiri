use crate::{args::*, terminal::TerminalHost};
use anyhow::{Context, Result, bail};
use base64::Engine as _;
use std::{
    collections::BTreeMap,
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
};
use tobkiri_core::{Core, CoreConfig};
use tobkiri_core::{
    NativeInputRouteStatus, NativeInputStatus, RunEventKind, Variant,
    ids::{InputSourceId, OpName, PackId, ProfileId, ShapeRef},
};

#[path = "profile_apply.rs"]
mod profile_apply;
#[path = "profile_input.rs"]
mod profile_input;
#[path = "profile_session.rs"]
mod profile_session;
#[path = "secret_input.rs"]
mod secret_input;
#[path = "user_invoke.rs"]
mod user_invoke;

pub async fn run(args: Args) -> Result<()> {
    if let Command::Pack { command } = args.command {
        return pack(command).await;
    }
    // Reject malformed/foreign structural identities before opening DATA or
    // constructing a trusted prompt. This reads no authority from the file.
    let prepared_apply = match &args.command {
        Command::Profile {
            command:
                ProfileCommand::Apply {
                    profile,
                    definition,
                    ..
                },
        } => Some(profile_apply::load(profile, definition)?),
        _ => None,
    };
    let mut config = CoreConfig::default();
    if let Some(root) = args.data_root {
        #[cfg(debug_assertions)]
        {
            config = CoreConfig::for_test_data_root(root);
        }
        #[cfg(not(debug_assertions))]
        {
            let _ = root;
            bail!("data-root override is available only in debug/test builds");
        }
    }
    let host = Arc::new(TerminalHost::new(args.non_interactive)?);
    let core = Core::open(config, host.clone())?;
    match args.command {
        Command::Profile { command } => profile(&core, &host, command, prepared_apply).await,
        Command::Approve { profile } => {
            core.approve_profile(&ProfileId::parse(&profile)?).await?;
            Ok(())
        }
        Command::Secrets {
            command:
                SecretCommand::Set {
                    profile,
                    name,
                    from_file,
                    from_env,
                },
        } => {
            let profile_id = ProfileId::parse(&profile)?;
            let bytes = secret_input::load(from_file.as_deref(), from_env.as_deref())?;
            core.set_secret(&profile_id, &name, &bytes).await?;
            println!("Secret stored for Profile {profile}.");
            Ok(())
        }
        Command::Pack { .. } => unreachable!(),
    }
}

async fn profile(
    core: &Core,
    host: &TerminalHost,
    command: ProfileCommand,
    prepared_apply: Option<profile_apply::Definition>,
) -> Result<()> {
    match command {
        ProfileCommand::Session { profile } => {
            profile_session::run(core, host, &profile).await?;
        }
        ProfileCommand::Invoke {
            profile,
            shape,
            op,
            args_file,
        } => {
            user_invoke::run(core, &profile, &shape, &op, &args_file).await?;
        }
        ProfileCommand::Create { name, definition } => {
            let id = core
                .create_profile(&profile_input::definition(&definition)?, name)
                .await?;
            println!("{id}");
        }
        ProfileCommand::Apply {
            profile,
            expected_head,
            ..
        } => {
            let receipt = profile_apply::apply(
                core,
                &profile,
                &expected_head,
                prepared_apply.context("Profile apply definition was not prepared")?,
            )
            .await?;
            let bytes = canonical_json(&receipt)?;
            let stdout = std::io::stdout();
            let mut output = stdout.lock();
            output.write_all(&bytes)?;
            output.write_all(b"\n")?;
        }
        ProfileCommand::Run {
            profile,
            source,
            input_shape,
            input_op,
            once,
            ..
        } => {
            let session = core.start(&ProfileId::parse(&profile)?).await?;
            let outcome:Result<()> = async {
            let operation = OpName::parse(&input_op)?;
            let requested_source = source
                .map(|value| InputSourceId::parse(&value))
                .transpose()?;
            let requested_shape = input_shape
                .map(|value| ShapeRef::parse(&value))
                .transpose()?;
            let mut matching = session.input_entries().iter().filter(|entry| {
                requested_source
                    .as_ref()
                    .is_none_or(|value| value.as_str() == entry.source_pack().as_str())
                    && requested_shape
                        .as_ref()
                        .is_none_or(|value| value == entry.exact_input_view())
                    && &operation == entry.operation()
            });
            let selected = matching
                .next()
                .context("select one exact frozen source/view/operation input entry")?;
            if matching.next().is_some() {
                bail!("input selection is ambiguous; specify its exact source and view");
            }
            let source = InputSourceId::parse(selected.source_pack().as_str())?;
            let shape = selected.exact_input_view().clone();
            let operation = selected.operation().clone();
            loop {
                let read_cancel = tokio_util::sync::CancellationToken::new();
                let mut read = Box::pin(session.read_input_with_cancel(
                    source.clone(),
                    shape.clone(),
                    operation.clone(),
                    read_cancel.clone(),
                ));
                let completed = tokio::select! {
                    result = &mut read => Some(result),
                    _ = tokio::signal::ctrl_c() => {
                        read_cancel.cancel();
                        None
                    },
                };
                let input = match completed {
                    Some(result) => result?,
                    None => {
                        // Keep polling the same source call after signaling its
                        // Core cancellation so the consumed physical grant can
                        // finish cleanup and terminal delivery.
                        let _ = read.await;
                        bail!("NativeInput read cancelled");
                    }
                };
                let mut run = session.submit_native_input(input).await?;
                let mut cancelled = false;
                loop {
                    tokio::select! {
                        _ = tokio::signal::ctrl_c(), if !cancelled=>{run.cancel.cancel();cancelled=true;},
                        event=run.events.recv()=>match event {
                            Some(event)=>if let RunEventKind::StepFailed{step,error,..}=event.event.kind{
                                eprintln!("Route {} step {step}: {}", event.route_index, error.audit_code());
                            },
                            None=>break,
                        },
                    }
                }
                let completion = run
                    .result
                    .await
                    .context("NativeInput completion channel closed")?;
                {
                    let stdout = std::io::stdout();
                    let mut output = stdout.lock();
                    output.write_all(completion.buffer.as_bytes())?;
                    output.write_all(b"\n")?;
                }
                for route in &completion.routes {
                    match route.status {
                        NativeInputRouteStatus::Succeeded => {}
                        NativeInputRouteStatus::Denied => {
                            eprintln!("Route {}: denied", route.route_index);
                        }
                        NativeInputRouteStatus::Failed => {
                            eprintln!("Route {}: failed ({})", route.route_index, route.error_code.unwrap_or("internal"));
                        }
                        NativeInputRouteStatus::Cancelled => {
                            eprintln!("Route {}: cancelled", route.route_index);
                        }
                        NativeInputRouteStatus::Unstarted => {
                            eprintln!("Route {}: not started", route.route_index);
                        }
                        NativeInputRouteStatus::RetentionLimit => {
                            eprintln!("Route {}: output retention limit exceeded", route.route_index);
                        }
                    }
                }
                match completion.status {
                    NativeInputStatus::Succeeded => {}
                    NativeInputStatus::Cancelled => {
                        eprintln!("Cancelled.");
                        bail!("NativeInput run cancelled");
                    }
                    NativeInputStatus::Failed => bail!("One or more NativeInput routes failed"),
                }
                if once {
                    break;
                }
            }
                Ok(())
            }.await;
            complete_with_cleanup(outcome, session.close().await)?;
        }
        ProfileCommand::Export { profile, output } => {
            write(&output, &core.export_profile(&ProfileId::parse(&profile)?)?)?
        }
        ProfileCommand::Import {
            file,
            name,
            ceiling,
        } => {
            let narrowing = ceiling.as_ref().map(|path| json(path)).transpose()?;
            let id = core
                .import_profile(&profile_input::published(&file)?, name, narrowing)
                .await?;
            println!("{id}");
        }
        ProfileCommand::Add { profile, pack } => {
            let digest = core
                .add_pack(
                    &ProfileId::parse(&profile)?,
                    format!("file:{}", std::fs::canonicalize(pack)?.display()),
                    false,
                )
                .await?;
            println!("{digest}");
        }
        ProfileCommand::Update { profile, pack } => {
            let digest = core
                .add_pack(
                    &ProfileId::parse(&profile)?,
                    format!("file:{}", std::fs::canonicalize(pack)?.display()),
                    true,
                )
                .await?;
            println!("{digest}");
        }
        ProfileCommand::Remove { profile, pack } => {
            let digest = core
                .remove_pack(&ProfileId::parse(&profile)?, &PackId::parse(&pack)?)
                .await?;
            println!("{digest}");
        }
        ProfileCommand::Repair {
            profile,
            candidate,
            generator,
            output,
        } => {
            let (original, head) = core.profile(&ProfileId::parse(&profile)?)?;
            let mut proposed = tobkiri_core::profile::parse_profile(&bounded_read(
                &candidate,
                tobkiri_core::MAX_BYTES,
            )?)?;
            proposed.id = original.id.clone();
            let report = core.check_profile(&proposed)?;
            let document = serde_json::json!({"original_revision":head,"original_packs":original.packs,"report":report});
            let bytes = canonical_json(&document)?;
            if let Some(generator) = generator {
                let session = core.start(&original.id).await?;
                let outcome: Result<()> = async {
                    let request = tobkiri_core::parse(&bytes, Variant::StrictV1)?;
                    let output_buffer = session
                        .call_user_port(
                            ShapeRef::parse(&generator)?,
                            OpName::parse("generate")?,
                            &request,
                        )
                        .await?;
                    write(&output, output_buffer.as_bytes())?;
                    Ok(())
                }
                .await;
                complete_with_cleanup(outcome, session.close().await)?;
            } else {
                write(&output, &bytes)?;
            }
        }
    }
    Ok(())
}

async fn pack(command: PackCommand) -> Result<()> {
    match command {
        PackCommand::Build { directory, output } => {
            let manifest = tobkiri_core::pack::parse_manifest(&bounded_read(
                &directory.join("pack.json"),
                tobkiri_core::MAX_BYTES,
            )?)?;
            let mut files = BTreeMap::new();
            for entry in &manifest.files {
                safe_relative(&entry.path)?;
                files.insert(
                    entry.path.clone(),
                    bounded_read(&directory.join(&entry.path), 128 * 1024 * 1024)?,
                );
            }
            let signature = if directory.join("signature.json").exists() {
                Some(json::<tobkiri_core::pack::PackSignature>(
                    &directory.join("signature.json"),
                )?)
            } else {
                None
            };
            let bytes = tobkiri_core::pack::build_tpk(&manifest, &files, signature.as_ref())?;
            write(&output, &bytes)?;
        }
        PackCommand::Sign {
            manifest,
            key,
            output,
        } => {
            let document = tobkiri_core::pack::parse_manifest(&bounded_read(
                &manifest,
                tobkiri_core::MAX_BYTES,
            )?)?;
            let mut bytes = bounded_read(&key, 4096)?;
            let decoded = decode_key(&bytes);
            bytes.fill(0);
            let mut key_bytes = decoded?;
            let key = ed25519_dalek::SigningKey::from_bytes(&key_bytes);
            key_bytes.fill(0);
            let signature = tobkiri_core::pack::sign_manifest(&document, &key)?;
            write(&output, &canonical_json(&signature)?)?;
        }
        PackCommand::Verify {
            artifact,
            publishers,
        } => {
            let publishers = publishers
                .as_ref()
                .map(|path| json(path))
                .transpose()?
                .unwrap_or_default();
            let pack = if artifact.is_dir() {
                tobkiri_core::pack::verify_directory(&artifact, &publishers)?
            } else {
                tobkiri_core::pack::verify_tpk(
                    &bounded_read(&artifact, 128 * 1024 * 1024)?,
                    &publishers,
                )?
            };
            println!(
                "{} {} {}",
                pack.manifest.id, pack.manifest.version, pack.hash
            );
        }
        PackCommand::WrapPi {
            source,
            upstream,
            id,
            output,
            builder,
        } => {
            PackId::parse(&id)?;
            let builder = builder.unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../sdk/js/wrap-pi.mjs")
            });
            if !builder.is_file() {
                bail!(
                    "pi SDK builder was not found; provide --builder with the installed SDK entry point"
                );
            }
            let status = std::process::Command::new("node")
                .arg(builder)
                .arg("--source")
                .arg(source)
                .arg("--upstream")
                .arg(upstream)
                .arg("--out")
                .arg(output)
                .arg("--id")
                .arg(id)
                .status()
                .context("Node and the pi SDK compilation dependencies are required")?;
            if !status.success() {
                bail!("pi SDK compilation failed; the original source was not admitted as a Pack");
            }
        }
    }
    Ok(())
}

fn select<T>(explicit: Option<T>, mut values: Vec<T>, name: &str) -> Result<T> {
    if let Some(value) = explicit {
        return Ok(value);
    }
    if values.len() != 1 {
        bail!("select {name} explicitly because the Profile does not have exactly one");
    }
    Ok(values.remove(0))
}
fn bounded_read(path: &Path, max: usize) -> Result<Vec<u8>> {
    if std::fs::metadata(path)?.len() > max as u64 {
        bail!("file exceeds the allowed input size: {}", path.display());
    }
    let bytes = std::fs::read(path)?;
    if bytes.len() > max {
        bail!("file grew beyond the allowed input size");
    }
    Ok(bytes)
}
fn json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let value = tobkiri_core::parse(
        &bounded_read(path, tobkiri_core::MAX_BYTES)?,
        Variant::StrictV1,
    )?;
    Ok(serde_json::from_slice(&tobkiri_core::encode(
        &value,
        Variant::StrictV1,
    )?)?)
}
fn canonical_json<T: serde::Serialize>(document: &T) -> Result<Vec<u8>> {
    let value = tobkiri_core::parse(&serde_json::to_vec(document)?, Variant::StrictV1)?;
    Ok(tobkiri_core::encode(&value, Variant::StrictV1)?)
}
fn write(path: &Path, bytes: &[u8]) -> Result<()> {
    std::fs::write(path, bytes).with_context(|| format!("write {}", path.display()))
}
fn safe_relative(path: &str) -> Result<()> {
    if Path::new(path)
        .components()
        .any(|component| !matches!(component, std::path::Component::Normal(_)))
        || path.contains('\\')
    {
        bail!("Pack file path must be a safe relative path");
    }
    Ok(())
}
fn decode_key(bytes: &[u8]) -> Result<[u8; 32]> {
    if bytes.len() == 32 {
        return Ok(bytes.try_into().expect("checked"));
    }
    let text = std::str::from_utf8(bytes)
        .context("signing key must be raw bytes, hex or base64url")?
        .trim();
    let result = if text.len() == 64 && text.bytes().all(|b| b.is_ascii_hexdigit()) {
        (0..64)
            .step_by(2)
            .map(|index| u8::from_str_radix(&text[index..index + 2], 16))
            .collect::<Result<Vec<_>, _>>()?
    } else {
        base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(text)
            .map_err(|_| anyhow::anyhow!("invalid signing key encoding"))?
    };
    result
        .try_into()
        .map_err(|_| anyhow::anyhow!("Ed25519 signing key must contain 32 bytes"))
}

fn complete_with_cleanup<T>(
    outcome: Result<T>,
    cleanup: Result<(), tobkiri_core::CoreError>,
) -> Result<T> {
    match (outcome, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(cleanup)) => Err(anyhow::Error::new(cleanup).context("Session cleanup failed")),
        (Err(error), Err(cleanup)) => {
            if filesystem_unknown(&cleanup)
                && !error.chain().any(|error| {
                    error
                        .downcast_ref::<tobkiri_core::CoreError>()
                        .is_some_and(filesystem_unknown)
                })
            {
                Err(anyhow::Error::new(cleanup)
                    .context(format!("User operation also failed: {error}")))
            } else {
                Err(error.context(format!("Session cleanup also failed: {cleanup}")))
            }
        }
    }
}
fn filesystem_unknown(error: &tobkiri_core::CoreError) -> bool {
    match error {
        tobkiri_core::CoreError::Step(error) => error.code == "filesystem-commit-unknown",
        tobkiri_core::CoreError::Cleanup { original, cleanup } => {
            filesystem_unknown(original) || filesystem_unknown(cleanup)
        }
        _ => false,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cleanup_failure_cannot_turn_a_success_into_success() {
        let result = complete_with_cleanup(
            Ok(()),
            Err(tobkiri_core::CoreError::Runtime(
                "fixture cleanup failed".into(),
            )),
        );
        assert!(result.is_err());
        assert!(format!("{:#}", result.unwrap_err()).contains("Session cleanup failed"));
    }
    #[test]
    fn original_operation_error_survives_a_cleanup_failure() {
        let result = complete_with_cleanup::<()>(
            Err(anyhow::anyhow!("original operation failed")),
            Err(tobkiri_core::CoreError::Runtime(
                "fixture cleanup failed".into(),
            )),
        );
        let error = result.unwrap_err();
        assert_eq!(error.root_cause().to_string(), "original operation failed");
        let text = format!("{error:#}");
        assert!(text.contains("original operation failed"));
        assert!(text.contains("fixture cleanup failed"));
    }
    #[test]
    fn typed_unknown_cleanup_takes_priority_over_a_plain_transport_failure() {
        let result = complete_with_cleanup::<()>(
            Err(anyhow::anyhow!("fixture stdout failed")),
            Err(tobkiri_core::CoreError::Step(tobkiri_core::StepError::new(
                "filesystem-commit-unknown",
                "fixture uncertain effect",
            ))),
        );
        let error = result.unwrap_err();
        assert!(
            error
                .downcast_ref::<tobkiri_core::CoreError>()
                .is_some_and(filesystem_unknown)
        );
        let text = format!("{error:#}");
        assert!(text.contains("fixture stdout failed"));
        assert!(text.contains("filesystem-commit-unknown"));
    }
}
