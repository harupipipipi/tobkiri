//! One finite original review continuation. This prefix does real old-state IO;
//! neither reaching a boundary nor authenticating an intent is listing success.
use crate::original_authority::{PublicationPrefix, PublicationRead};
use crate::{
    OriginalAuthorityAdmission, OriginalAuthorityCause, OriginalAuthorityCleanup,
    OriginalAuthorityCloseReceipt, OriginalAuthorityError, OriginalAuthorityOwner,
};
use tobkiri_canon::CoreBuffer;
use tobkiri_gate::{CoreCancellation, NativeSetupInvocation};

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
    VerifiedPointerNotBound,
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
}
#[derive(Debug)]
pub struct BootstrapReviewCleanupFailure {
    pub receipt: OriginalAuthorityCloseReceipt,
}
impl std::fmt::Display for BootstrapReviewCleanupFailure {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.write_str("original publication physical close was not confirmed")
    }
}
impl std::error::Error for BootstrapReviewCleanupFailure {}

pub(crate) const DENIED: &str = "tobkiri.native.OriginalProfileResolutionDenied";
pub(crate) struct Review {
    read: PublicationRead,
    // Funded DATA only; the original outer input stays inside the owning job.
    _denial_diagnostic: Option<CoreBuffer>,
    _flags: (bool, bool, bool),
    attempted: bool,
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
        })
    }
    pub(crate) fn run<S: BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> Result<CoreBuffer, BootstrapReviewFailure> {
        if self.attempted {
            return Err(BootstrapReviewFailure::Admission(
                OriginalAuthorityAdmission::AlreadyAttempted,
            ));
        }
        self.attempted = true;
        for discard_superseded in [true, false] {
            match self
                .read
                .run(owner, cancellation, discard_superseded)
                .map_err(|mut error| {
                    if error.class == DENIED {
                        BootstrapReviewFailure::ResolutionDenied {
                            message: error.message,
                            cause: error.cause.take().expect("recovery's original cause"),
                        }
                    } else {
                        BootstrapReviewFailure::Original(error)
                    }
                })? {
                PublicationPrefix::NoJournal | PublicationPrefix::NoIntent => {}
                PublicationPrefix::VerifiedIntent => {
                    return Err(BootstrapReviewFailure::NotBound(
                        BootstrapReviewReachpoint::VerifiedPointerNotBound,
                    ));
                }
            }
        }
        // This is the next original _bootstrap_review_candidate load_catalog
        // reachpoint, not a fallback catalog or reconstructed description.
        Err(BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::CatalogNotBound,
        ))
    }
    pub(crate) fn receipt(&self) -> OriginalAuthorityCloseReceipt {
        self.read.receipt()
    }
    pub(crate) fn uncertain_cleanup(&self) -> Option<OriginalAuthorityCleanup> {
        self.read.uncertain_cleanup()
    }
}
