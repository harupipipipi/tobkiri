//! One finite original review continuation. This prefix does real old-state IO;
//! neither reaching a boundary nor authenticating an intent is listing success.
use crate::original_authority::PublicationRead;
use crate::{
    OriginalAuthorityAdmission, OriginalAuthorityCause, OriginalAuthorityCleanup,
    OriginalAuthorityCloseReceipt, OriginalAuthorityError, OriginalAuthorityOwner,
};
use tobkiri_canon::CoreBuffer;
use tobkiri_gate::NativeSetupInvocation;

/// Explicit original review capability, separate from module metadata and the
/// fixed old-Authority read. Implemented by the same actual application owner.
pub trait BootstrapReviewOwner: OriginalAuthorityOwner {
    fn with_original_review_user_data<R>(
        &self,
        use_root: impl FnOnce(&std::path::Path) -> R,
    ) -> Result<R, OriginalAuthorityError>;
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
    CatalogNotBound,
    BootstrapProfileIdNotBound,
    HostProfileCatalogNotBound,
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
    _denial_diagnostic: Option<CoreBuffer>,
    _flags: (bool, bool, bool),
    pub(crate) attempted: bool,
    pub(crate) actual: ActualCustody,
}
#[derive(Default)]
pub(crate) struct ActualCustody {
    pub(crate) getter: Option<crate::BootstrapIdentityOutcome>,
    pub(crate) denied: Option<crate::BootstrapDenied>,
    pub(crate) factory_failure: Option<crate::BootstrapDeniedFailure>,
}
impl Review {
    pub(crate) fn new(
        invocation: &NativeSetupInvocation,
        options: &BootstrapReviewOptions,
        diagnostic: Option<CoreBuffer>,
    ) -> Result<Self, BootstrapReviewFailure> {
        Ok(Self {
            read: PublicationRead::new(invocation).map_err(BootstrapReviewFailure::Admission)?,
            _denial_diagnostic: diagnostic,
            _flags: (
                options.include_source_additions,
                options.active,
                options.activation_denied,
            ),
            attempted: false,
            actual: ActualCustody::default(),
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
    if error.class == DENIED {
        BootstrapReviewFailure::ResolutionDenied {
            message: error.message,
            cause: error.cause.take().expect("recovery's original cause"),
        }
    } else {
        BootstrapReviewFailure::Original(error)
    }
}
