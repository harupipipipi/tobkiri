//! Trusted descriptor registration and current-policy picker issuance.
//! Registration fixes a non-reused identity and ceiling; only the exact
//! approved Profile/Pack permission intersection authorizes its later use.

use std::{
    fs::File,
    sync::{Arc, Mutex},
    time::Duration,
};
use tobkiri_gate::{CallCx, ControlKind, ControlReservation, CoreControl, Gate, GateEngine};
use tobkiri_store::DataRoot;
use tobkiri_wasm::{
    FilesystemAccess,
    filesystem::{FsError, FsRegistry},
};

pub use tobkiri_wasm::filesystem::{
    FsRights as FilesystemRights, FsRootIdentity as FilesystemRoot,
};

const MAX_ROOTS: usize = 64;
struct Root {
    identity: FilesystemRoot,
    rights: FilesystemRights,
}
pub(crate) struct CoreFilesystem {
    registry: FsRegistry,
    roots: Mutex<Vec<Root>>,
    control: CoreControl,
    _reservation: ControlReservation,
}
impl CoreFilesystem {
    pub(crate) fn new(root: &DataRoot, control: CoreControl) -> Result<Arc<Self>, FsError> {
        let reservation = control
            .reserve(
                ControlKind::Registration,
                (std::mem::size_of::<Self>()
                    + MAX_ROOTS * std::mem::size_of::<Root>()
                    + 2 * std::mem::size_of::<usize>()) as u64,
            )
            .map_err(|_| FsError::Unavailable)?;
        let registry = FsRegistry::new(control.clone())?;
        // Every live authority/approval/audit/catalog/vault/staging object is
        // beneath this exact pinned Core data root. Clone failure disables FS;
        // never substitute DataRoot::path() or an unprotected registry.
        registry.protect_root(
            root.filesystem_protection_handle()
                .map_err(|_| FsError::Unavailable)?,
        )?;
        let mut roots = Vec::new();
        roots
            .try_reserve_exact(MAX_ROOTS)
            .map_err(|_| FsError::Unavailable)?;
        Ok(Arc::new(Self {
            registry,
            roots: Mutex::new(roots),
            control,
            _reservation: reservation,
        }))
    }

    pub(crate) fn register(
        &self,
        directory: File,
        rights: FilesystemRights,
    ) -> Result<FilesystemRoot, FsError> {
        let mut roots = self.roots.lock().map_err(|_| FsError::Unavailable)?;
        if roots.len() == MAX_ROOTS {
            return Err(FsError::Unavailable);
        }
        let identity = self.registry.register_root(directory, rights)?;
        roots.push(Root {
            identity: identity.clone(),
            rights,
        });
        Ok(identity)
    }

    pub(crate) fn for_gate(
        self: &Arc<Self>,
        gate: Arc<GateEngine>,
    ) -> Result<Arc<dyn FilesystemAccess>, FsError> {
        let reservation = self
            .control
            .reserve(
                ControlKind::Registration,
                (std::mem::size_of::<SessionFilesystem>() + 2 * std::mem::size_of::<usize>())
                    as u64,
            )
            .map_err(|_| FsError::Unavailable)?;
        Ok(Arc::new(SessionFilesystem {
            core: self.clone(),
            gate,
            _reservation: reservation,
        }))
    }
}

struct SessionFilesystem {
    core: Arc<CoreFilesystem>,
    gate: Arc<GateEngine>,
    _reservation: ControlReservation,
}
impl FilesystemAccess for SessionFilesystem {
    fn registry(&self) -> &FsRegistry {
        &self.core.registry
    }
    fn issue_open_token(&self, cx: &CallCx, candidate: &str) -> Result<String, FsError> {
        if candidate.len() != 72
            || !candidate.starts_with("fs-root:")
            || !candidate.as_bytes()[8..]
                .iter()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
        {
            return Err(FsError::Denied("invalid filesystem candidate identity"));
        }
        self.gate.check_context(cx)?;
        let roots = self.core.roots.lock().map_err(|_| FsError::Unavailable)?;
        let root = roots
            .iter()
            .find(|root| root.identity.id() == candidate)
            .ok_or(FsError::Denied("unknown filesystem candidate identity"))?;
        let permissions = self.gate.effective_permissions(cx)?;
        let permission = permissions
            .fs
            .iter()
            .find(|permission| permission.root == candidate)
            .ok_or(FsError::Denied(
                "filesystem root is not in current approved policy",
            ))?;
        let rights = FilesystemRights {
            read: root.rights.read && permission.read,
            write: root.rights.write && permission.write,
        };
        if !rights.read && !rights.write {
            return Err(FsError::Denied(
                "filesystem candidate has no approved rights",
            ));
        }
        self.gate.check_context(cx)?;
        self.core
            .registry
            .issue_open_token(cx, &root.identity, rights, Duration::from_secs(30))
    }
}
