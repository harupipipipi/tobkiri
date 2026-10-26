//! Borrow the actual existing Launcher selection at a nativeKit reachpoint.
//!
//! No path is resolved, cloned or allocated here. The original startup already
//! selected AppConfig, including its original resource-dir fallback. The caller
//! must retain this actual AppHandle through its managed worker/physical Join;
//! new captures, proxies, derived paths and output still need original funding.
//! These selected roots are not filesystem authority or verified artifacts.

use crate::{config::AppConfig, ShutdownState};
use std::{path::Path, sync::atomic::Ordering};
use tauri::{AppHandle, Manager};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum NativeHostSourceRole {
    Runtime,
    WorkingDirectory,
    DevelopmentWorkspace,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum NativeHostInputsUnavailable {
    ConfigurationMissing,
    ShutdownStateMissing,
    ShuttingDown,
}

/// A non-owning view of the same selected configuration, not reconstructed DATA.
/// Private construction and the callback lifetime prevent serialized paths or
/// capacity leases from substituting for the actual Launcher state.
pub(crate) struct NativeHostSourceInputs<'host> {
    config: &'host AppConfig,
}

impl NativeHostSourceInputs<'_> {
    /// Borrow the existing backing allocation. Equal-valued roles stay distinct;
    /// no existence/resolve/stat/validation is advanced ahead of its reachpoint.
    pub(crate) fn root(&self, role: NativeHostSourceRole) -> Option<&Path> {
        match role {
            NativeHostSourceRole::Runtime => Some(self.config.app_dir.as_path()),
            NativeHostSourceRole::WorkingDirectory => Some(self.config.rumi_home.as_path()),
            NativeHostSourceRole::DevelopmentWorkspace => self.config.dev_workspace_root.as_deref(),
        }
    }
}

/// Enter only when the original native caller actually needs selected inputs.
///
/// This adds no startup prerequisite and does not invoke the later signed-app
/// resolver. NativeKit must still validate the original invocation, actual
/// target, phase, operation and disclosure; an existing path is not an IO grant.
/// The higher-ranked callback cannot return a borrow of the selected roots.
pub(crate) fn with_original_host_inputs<T>(
    app: &AppHandle,
    use_inputs: impl for<'host> FnOnce(NativeHostSourceInputs<'host>) -> T,
) -> Result<T, NativeHostInputsUnavailable> {
    let shutdown = app
        .try_state::<ShutdownState>()
        .ok_or(NativeHostInputsUnavailable::ShutdownStateMissing)?;
    let config = app
        .try_state::<AppConfig>()
        .ok_or(NativeHostInputsUnavailable::ConfigurationMissing)?;
    with_selected_config(config.inner(), &shutdown.inner().0, use_inputs)
}

fn with_selected_config<T>(
    config: &AppConfig,
    shutdown: &std::sync::atomic::AtomicBool,
    use_inputs: impl for<'host> FnOnce(NativeHostSourceInputs<'host>) -> T,
) -> Result<T, NativeHostInputsUnavailable> {
    if shutdown.load(Ordering::Acquire) {
        return Err(NativeHostInputsUnavailable::ShuttingDown);
    }
    let value = use_inputs(NativeHostSourceInputs { config });
    if shutdown.load(Ordering::Acquire) {
        // Drop any result before refusing publication. Do not turn retirement
        // into a replacement selection or silently fall back to ambient paths.
        drop(value);
        return Err(NativeHostInputsUnavailable::ShuttingDown);
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, sync::atomic::AtomicBool};

    fn selected_config() -> AppConfig {
        let root = std::env::temp_dir().join("tobkiri-native-host-selection-view");
        AppConfig::detect_for_tauri(root.join("resources"), root.join("data")).unwrap()
    }

    #[test]
    fn borrows_existing_config_allocations_and_keeps_root_roles_distinct() {
        let config = selected_config();
        let shutdown = AtomicBool::new(false);
        with_selected_config(&config, &shutdown, |inputs| {
            let runtime = inputs.root(NativeHostSourceRole::Runtime).unwrap();
            let working = inputs.root(NativeHostSourceRole::WorkingDirectory).unwrap();
            assert!(std::ptr::eq(runtime, config.app_dir.as_path()));
            assert!(std::ptr::eq(working, config.rumi_home.as_path()));
            assert_eq!(runtime, working);
            assert_ne!(
                runtime.as_os_str().as_encoded_bytes().as_ptr(),
                working.as_os_str().as_encoded_bytes().as_ptr()
            );
            assert_eq!(
                inputs.root(NativeHostSourceRole::DevelopmentWorkspace),
                config.dev_workspace_root.as_deref()
            );
        })
        .unwrap();
    }

    #[test]
    fn shutdown_prevents_and_invalidates_source_publication() {
        let config = selected_config();
        let shutdown = AtomicBool::new(true);
        assert_eq!(
            with_selected_config(&config, &shutdown, |_| panic!("retired selection used")),
            Err::<(), _>(NativeHostInputsUnavailable::ShuttingDown),
        );
        shutdown.store(false, Ordering::Release);
        let dropped = Cell::new(false);
        struct ResultProbe<'a>(&'a Cell<bool>);
        impl Drop for ResultProbe<'_> {
            fn drop(&mut self) {
                self.0.set(true);
            }
        }
        let result = with_selected_config(&config, &shutdown, |_| {
            shutdown.store(true, Ordering::Release);
            ResultProbe(&dropped)
        });
        assert!(matches!(
            result,
            Err(NativeHostInputsUnavailable::ShuttingDown)
        ));
        assert!(dropped.get());
    }
}
