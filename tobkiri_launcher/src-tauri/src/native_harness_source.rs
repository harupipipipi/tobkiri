//! Owned selection at the original resolved-application handoff, not an IO grant.
//!
//! The parent resolver alone constructs this value after its existing checks.
//! Keeping this relation does not pin an inode, authorize another phase, or
//! replace the original reached resolve/lstat/open/fstat/read/close operations.
//! Native invocation storage, real target admission and allocation ownership
//! still have to be connected before passing these inputs into nativeKit.

use super::{ApplicationAuthority, VerifiedBundleLock};
use std::path::{Path, PathBuf};

/// Distinct original root roles. Equal path values never merge these roles.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum NativeSourceRoot {
    Application,
    Bundle,
    ApplicationPack,
    MaterializedPack,
}

/// The actual resolver result and roots it used, retained without reinspection.
///
/// No JSON deserialization, public path constructor, Clone, permission boolean,
/// substituted Profile or capacity lease can construct this selection. Its
/// parent-only constructor is called at the original successful return point.
/// An effect must retain this actual selection in its native context and check
/// target/role/operation/phase/occurrence; a borrowed path is not that proof.
pub(crate) struct NativeApplicationSources {
    authority: ApplicationAuthority,
    application_root: PathBuf,
    bundle_root: PathBuf,
    application_pack_root: PathBuf,
    bundle_lock: VerifiedBundleLock,
}

impl NativeApplicationSources {
    /// Move existing resolver locals; do not resolve, stat, open or copy them.
    pub(super) fn from_resolver_result(
        authority: ApplicationAuthority,
        application_root: PathBuf,
        bundle_root: PathBuf,
        application_pack_root: PathBuf,
        bundle_lock: VerifiedBundleLock,
    ) -> Self {
        Self {
            authority,
            application_root,
            bundle_root,
            application_pack_root,
            bundle_lock,
        }
    }

    /// Borrow the exact accepted result, including its original pins.
    pub(crate) fn authority(&self) -> &ApplicationAuthority {
        &self.authority
    }

    /// Observe the original selected path value; perform no filesystem IO.
    pub(crate) fn root(&self, role: NativeSourceRoot) -> &Path {
        match role {
            NativeSourceRoot::Application => &self.application_root,
            NativeSourceRoot::Bundle => &self.bundle_root,
            NativeSourceRoot::ApplicationPack => &self.application_pack_root,
            NativeSourceRoot::MaterializedPack => &self.authority.pack_root,
        }
    }

    /// Borrow a pin from the actual verified lock, without rereading its file.
    /// A digest is DATA, not a new permission or proof of current contents.
    pub(crate) fn bundle_pin(&self, relative: &str) -> Option<&str> {
        self.bundle_lock
            .authority_digests
            .get(relative)
            .or_else(|| self.bundle_lock.sidecar_digests.get(relative))
            .map(String::as_str)
    }

    /// Preserve the existing resolver's return contract without another pass.
    pub(crate) fn into_authority(self) -> ApplicationAuthority {
        self.authority
    }
}
