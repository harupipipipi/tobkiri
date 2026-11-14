//! One finite original review continuation. This prefix does real old-state IO;
//! neither reaching a boundary nor authenticating an intent is listing success.
use crate::original_authority::PublicationRead;
use crate::{
    OriginalAuthorityAdmission, OriginalAuthorityCause, OriginalAuthorityCleanup,
    OriginalAuthorityCloseReceipt, OriginalAuthorityError, OriginalAuthorityOwner,
};
use tobkiri_canon::{CoreBuffer, CoreValue};
use tobkiri_gate::NativeSetupInvocation;

/// Borrowed original Kernel launch inputs at the fixed catalog reachpoint.
/// The environment fields select original branches; they confer no IO rights.
pub struct BootstrapCatalogSource<'host> {
    pub runtime: &'host std::path::Path,
    pub environment: &'host str,
    pub configured_app: Option<&'host std::path::Path>,
}

/// Explicit original review capability, separate from module metadata and the
/// fixed old-Authority read. Implemented by the same actual application owner.
pub trait BootstrapReviewOwner: OriginalAuthorityOwner {
    fn with_original_review_user_data<R>(
        &self,
        use_root: impl FnOnce(&std::path::Path) -> R,
    ) -> Result<R, OriginalAuthorityError>;

    /// Actual selected runtime resource origin at the fixed catalog caller.
    /// Neither metadata policy nor a guest Path can supply this Host borrow.
    /// An owner that also implements BootstrapInputOwner must borrow the SAME
    /// immutable selected runtime in with_roots and here for the operation's
    /// lifetime. Work retains that actual owner through native IO cleanup;
    /// resolved DATA, path equality and allocation addresses cannot prove it.
    fn with_original_catalog_source<R>(
        &self,
        _operation: impl for<'host> FnOnce(BootstrapCatalogSource<'host>) -> R,
    ) -> Result<R, OriginalAuthorityError> {
        Err(OriginalAuthorityError::unavailable())
    }
}

/// Original Host presentation inputs. They select no owner, path or authority.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct BootstrapReviewOptions {
    pub include_source_additions: bool,
    pub active: bool,
    pub activation_denied: bool,
    pub denial_diagnostic: Option<String>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootstrapReviewReachpoint {
    BootstrapProfileIdNotBound,
    HostProfileCatalogNotBound,
    CatalogSourceNotBound,
    BootstrapPredecessorActivationNotBound,
    ActiveCandidateAuthorityNotBound,
    ProfileDefinitionStoreNotBound,
    ImportLegacyCollectionNotBound,
    LegacySuccessorBuilderNotBound,
    RepairLegacyDisplayNamesNotBound,
    AdmittedPackInventoryNotBound,
    AdmittedHostExtensionProjectionNotBound,
    CatalogWithProfilesNotBound,
}

/// ResolutionDenied is a typed request to the original Pack's denied factory,
/// not a new fixed Pack class in Core/Host and never an approval decision.
#[derive(Debug)]
pub enum BootstrapReviewFailure {
    NotBound(BootstrapReviewReachpoint),
    Admission(OriginalAuthorityAdmission),
    ResolutionDenied {
        message: String,
        cause: OriginalAuthorityCause,
    },
    Original(OriginalAuthorityError),
    ActualDenied {
        denied: crate::BootstrapDenied,
        getter: Option<crate::BootstrapIdentityOutcome>,
    },
    ActualFactoryFailure {
        failure: crate::BootstrapDeniedFailure,
        getter: Option<crate::BootstrapIdentityOutcome>,
    },
    GetterRaised(crate::BootstrapGetterError),
    ObservationRefused(crate::BootstrapIdentityFailure),
}
impl BootstrapReviewFailure {
    /// Only the original native diagnostics borrow Read's prepaid string
    /// capacity. Actual guest/cause buffers keep their own allocation owner and
    /// must survive Store retirement, IO Join and journal close instead.
    pub fn owns_original_diagnostics(&self) -> bool {
        matches!(
            self,
            Self::NotBound(_)
                | Self::Admission(_)
                | Self::ResolutionDenied { .. }
                | Self::Original(_)
        )
    }
}
#[derive(Debug)]
pub struct BootstrapReviewCleanupFailure {
    pub receipt: OriginalAuthorityCloseReceipt,
}
impl std::fmt::Display for BootstrapReviewCleanupFailure {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.write_str("original publication continuation cleanup failed")
    }
}
impl std::error::Error for BootstrapReviewCleanupFailure {}

pub(crate) const DENIED: &str = "tobkiri.native.OriginalProfileResolutionDenied";
pub(crate) struct Review {
    pub(crate) read: PublicationRead,
    // Funded DATA only; the original outer input stays inside the owning job.
    pub(crate) denial_diagnostic: Option<CoreBuffer>,
    pub(crate) active: bool,
    pub(crate) activation_denied: bool,
    pub(crate) include_source_additions: bool,
    pub(crate) attempted: bool,
    pub(crate) actual: ActualCustody,
    pub(crate) identity_prefix: bool,
    pub(crate) catalog_prefix: bool,
}
#[derive(Default)]
pub(crate) struct ActualCustody {
    pub(crate) getter: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) recovery_template_getter: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) _recovery_profile_definitions: Option<CoreValue>,
    #[cfg(unix)]
    pub(crate) _recovery_external_catalog:
        Option<crate::original_authority::external_catalog::ExternalPackRead>,
    pub(crate) denied: Option<crate::BootstrapDenied>,
    pub(crate) factory_failure: Option<crate::BootstrapDeniedFailure>,
    pub(crate) catalog_roots: BundleCalls,
    pub(crate) resolver_roots: BundleCalls,
    pub(crate) resolver_getter: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) catalog: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) catalog_input: Option<CoreBuffer>,
    pub(crate) resolver_source_profile: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) resolver_static_rows: Option<CoreBuffer>,
    pub(crate) resolver_dynamic_rows: Option<CoreBuffer>,
    pub(crate) resolver_verified_artifacts: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) resolver_bindings: Option<CoreValue>,
    pub(crate) resolver_profile_result: Option<CoreBuffer>,
    pub(crate) resolver_identity: Option<crate::bootstrap_runtime::ResolvedProfileIdentity>,
    pub(crate) predecessor_inputs:
        Option<crate::original_authority::definitions::BootstrapPredecessorInputs>,
    pub(crate) bootstrap_confirmation: Option<CoreBuffer>,
    pub(crate) bootstrap_preview: Option<CoreBuffer>,
    pub(crate) bootstrap_listing: Option<CoreBuffer>,
    pub(crate) _successor_candidate:
        Option<crate::original_authority::definitions::SuccessorCandidate>,
    pub(crate) resolver_authority: Option<crate::original_authority::ResolverAuthority>,
}

#[derive(Default)]
pub(crate) struct BundleCalls {
    pub(crate) host: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) packaged: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) manifest: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) _manifest_input: Option<CoreBuffer>,
}
impl Review {
    pub(crate) fn new(
        invocation: &NativeSetupInvocation,
        options: &BootstrapReviewOptions,
        diagnostic: Option<CoreBuffer>,
    ) -> Result<Self, OriginalAuthorityAdmission> {
        // Construction can only refuse admission. Keep actual guest/cause
        // carriers inline and funded; no uncharged Box is needed to shrink
        // an unrelated execution-error variant on this constructor path.
        Ok(Self {
            read: PublicationRead::new(invocation)?,
            denial_diagnostic: diagnostic,
            active: options.active,
            activation_denied: options.activation_denied,
            include_source_additions: options.include_source_additions,
            attempted: false,
            actual: ActualCustody::default(),
            identity_prefix: false,
            catalog_prefix: false,
        })
    }
    pub(crate) fn receipt(&self) -> OriginalAuthorityCloseReceipt {
        self.read.receipt()
    }
    pub(crate) fn uncertain_cleanup(&self) -> Option<OriginalAuthorityCleanup> {
        self.read.uncertain_cleanup()
    }
    pub(crate) fn close_continuation(&mut self) -> bool {
        // Consume/drop any native owned cleanup diagnostic while Read's
        // diagnostic reservation is still live. The receipt is independent.
        self.read.close_continuation().is_ok()
    }
    pub(crate) fn release_actual_data(&mut self) {
        drop(std::mem::take(&mut self.actual));
    }
}

pub(crate) fn original_failure(mut error: OriginalAuthorityError) -> BootstrapReviewFailure {
    if error.class == DENIED
        && let Some(cause) = error.cause.take()
    {
        BootstrapReviewFailure::ResolutionDenied {
            message: error.message,
            cause,
        }
    } else {
        BootstrapReviewFailure::Original(error)
    }
}
