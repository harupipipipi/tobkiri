//! Core authority: permission intersection, trusted approvals and single-use grants.
//! Pack payloads never carry authority; a dispatch grant binds the core context.

mod admission;
mod budget;
mod cancellation;
mod capacity;
#[cfg(feature = "closed-request-metrics")]
pub mod closed_metrics;
mod context;
mod control;
mod engine;
mod frame;
mod ingress;
mod permissions;
mod protocol;

pub use admission::*;
pub use budget::*;
pub use cancellation::*;
pub use capacity::*;
pub use context::*;
pub use control::*;
pub use engine::*;
pub use frame::*;
pub use ingress::*;
pub use permissions::*;
pub use protocol::*;

// The disabled expansion evaluates exactly the original expression. The
// enabled hook times a synchronous operation without logging its inputs/error.
#[cfg(feature = "closed-request-metrics")]
#[macro_export]
macro_rules! measure_closed {
    ($metric:ident, $operation:expr) => {
        $crate::closed_metrics::current_span($crate::closed_metrics::Metric::$metric)
            .measure(|| $operation)
    };
}
#[cfg(not(feature = "closed-request-metrics"))]
#[macro_export]
macro_rules! measure_closed {
    ($metric:ident, $operation:expr) => {
        $operation
    };
}
