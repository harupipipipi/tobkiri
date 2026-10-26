//! Actual native metadata IO on disposable physical SDK fixtures. These do not
//! prove the original Tauri reachpoint, opaque parents evaluation or file reads.
use super::*;
use crate::{BootstrapModule, BootstrapModuleIndex, ORIGINAL_PROFILE_SOURCE_METADATA};
use anyhow::{Result as AnyResult, ensure};
use std::{fs, os::unix::fs::symlink, path::PathBuf};
use tobkiri_canon::CoreValue;
use tobkiri_core::RuntimeOwner;
use tobkiri_wasm::WasmEngine;

async fn actual(root: PathBuf, relative: &'static str) -> AnyResult<Result<CoreBuffer>> {
    let data = tempfile::tempdir()?;
    let owner = RuntimeOwner::open(data.path().canonicalize()?.join("metadata-runtime"))?;
    let invocation = owner.begin_native_setup()?;
    let deadline = invocation.deadline()?;
    let engine = WasmEngine::new_with_control(owner.control())?;
    let mut store = engine.native_setup_store(invocation.clone(), ())?;
    let io = store.bind_native_data_io()?;
    let value = io
        .inspect(move |output| {
            let resolved = resolve(output, &root, relative)?;
            output.append(resolved.as_bytes())
        })
        .await;
    ensure!(
        invocation.deadline()? == deadline,
        "metadata renewed the deadline"
    );
    io.retire_and_wait().await?;
    Ok(value)
}

#[tokio::test(flavor = "current_thread")]
async fn original_normal_missing_unicode_and_non_directory_suffixes() -> AnyResult<()> {
    let sdk = tempfile::tempdir()?;
    let root = sdk.path().canonicalize()?;
    fs::create_dir(root.join("既存"))?;
    fs::write(root.join("file"), b"fixture")?;
    for relative in ["既存", "missing/child", "file/child", "missing/../既存"] {
        let resolved = actual(root.clone(), relative).await??;
        let expected = if relative == "missing/../既存" {
            root.join("既存")
        } else {
            root.join(relative)
        };
        ensure!(
            resolved.as_str()? == expected.to_str().unwrap(),
            "original lexical suffix changed"
        );
    }
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn actual_internal_relative_absolute_dangling_and_trailing_root_links() -> AnyResult<()> {
    let sdk = tempfile::tempdir()?;
    let root = sdk.path().canonicalize()?;
    fs::create_dir(root.join("real"))?;
    symlink("real", root.join("relative"))?;
    symlink(root.join("real"), root.join("absolute"))?;
    symlink("missing", root.join("dangling"))?;
    symlink(&root, root.join("to-root"))?;
    for (relative, expected) in [
        ("relative", root.join("real")),
        ("absolute", root.join("real")),
        ("dangling/leaf", root.join("missing/leaf")),
    ] {
        let resolved = actual(root.clone(), relative).await??;
        ensure!(
            resolved.as_str()? == expected.to_str().unwrap(),
            "internal link resolution changed"
        );
    }
    let trailing = PathBuf::from(format!("{}/", root.display()));
    ensure!(
        actual(trailing, "to-root").await??.as_str()? == root.to_str().unwrap(),
        "trailing root prefix changed"
    );
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn actual_non_strict_link_loop_and_unadmitted_external_relation() -> AnyResult<()> {
    let sdk = tempfile::tempdir()?;
    let root = sdk.path().canonicalize()?;
    symlink("loop", root.join("loop"))?;
    ensure!(
        actual(root.clone(), "loop/leaf").await??.as_str()?
            == root.join("loop/leaf").to_str().unwrap(),
        "non-strict loop suffix changed"
    );
    let outside = tempfile::tempdir()?;
    symlink(outside.path(), root.join("external"))?;
    ensure!(
        matches!(
            actual(root, "external").await?,
            Err(NativeDataIoError::Unavailable)
        ),
        "unadmitted relation became an original path result"
    );
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn full_path_syscall_boundary_is_unproven_instead_of_fd_relative_success() -> AnyResult<()> {
    let sdk = tempfile::tempdir()?;
    let mut root = sdk.path().canonicalize()?;
    let limit = libc::PATH_MAX as usize - 16;
    while root.as_os_str().as_bytes().len() < limit {
        let remaining = limit - root.as_os_str().as_bytes().len();
        if remaining < 2 {
            break;
        }
        root.push("x".repeat((remaining - 1).min(200)));
        fs::create_dir(&root)?;
    }
    let result = actual(
        root,
        "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
    )
    .await?;
    ensure!(
        matches!(result, Err(NativeDataIoError::Unavailable)),
        "descriptor traversal bypassed the original full-path syscall boundary"
    );
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn original_module_policy_requires_actual_scope_exact_name_path_and_operation()
-> AnyResult<()> {
    static RIGHT: BootstrapModuleIndex = BootstrapModuleIndex {
        sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        modules: &[BootstrapModule {
            module: "ecosystem.defaultspack.defaultspack.profile_runtime_composition",
            relative_file: "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
        }],
    };
    static WRONG_PATH: BootstrapModuleIndex = BootstrapModuleIndex {
        sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        modules: &[BootstrapModule {
            module: "ecosystem.defaultspack.defaultspack.profile_runtime_composition",
            relative_file: "different.py",
        }],
    };
    let data = tempfile::tempdir()?;
    let owner = RuntimeOwner::open(data.path().canonicalize()?.join("policy-runtime"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(owner.control())?;
    let store = engine.native_setup_store(invocation.clone(), ())?;
    let p = invocation.provenance();
    let frame = serde_json::json!({"format":"io.tobkiri.native-inspection-request.v1","scope":[format!("{:x}",p.account_generation),format!("{:x}",p.invocation),"1","1"],"target":{"binding":"module_0","steps":[]},"operation":"resolve","args":[false]});
    let budget = store.allocation_owners().graph();
    let parse = |frame: &serde_json::Value| {
        CoreValue::parse_guest(&serde_json::to_vec(frame).unwrap(), budget)
    };
    let selected = |frame: &CoreValue, index| {
        crate::source_metadata::selected(
            frame,
            &invocation,
            index,
            &ORIGINAL_PROFILE_SOURCE_METADATA,
        )
        .is_some()
    };
    ensure!(selected(&parse(&frame)?, &RIGHT));
    ensure!(!selected(&parse(&frame)?, &WRONG_PATH));
    for changed in [
        {
            let mut f = frame.clone();
            f["scope"][2] = "2".into();
            f
        },
        {
            let mut f = frame.clone();
            f["target"]["binding"] = "module_00".into();
            f
        },
        {
            let mut f = frame.clone();
            f["target"]["steps"] = serde_json::json!([{"kind":"parent","index":0}]);
            f
        },
        {
            let mut f = frame.clone();
            f["args"] = serde_json::json!([true]);
            f
        },
        {
            let mut f = frame.clone();
            f["operation"] = "read-bytes".into();
            f
        },
        {
            let mut f = frame.clone();
            f["grant"] = true.into();
            f
        },
    ] {
        ensure!(
            !selected(&parse(&changed)?, &RIGHT),
            "forged metadata request admitted"
        );
    }
    Ok(())
}
