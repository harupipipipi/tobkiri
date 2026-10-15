//! Bounded native cancellation and waiter custody.
//!
//! Cancellation state is control-plane metadata. It is accounted separately
//! from guest allocations and carries no permission or dispatch authority.

use crate::{
    ControlKind, ControlReservation, CoreControl, CoreControlError,
    control::default_control_account,
};
use std::{
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicU8, Ordering},
    },
    task::{Context, Poll},
};
use tokio_util::sync::{
    CancellationToken as TokioCancellationToken, WaitForCancellationFutureOwned,
};

/// Notification origin only. Durable terminal disposition still checks the
/// original deadline and the Gate's immutable terminal winner.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CancellationCause {
    Explicit,
    Deadline,
}
impl CancellationCause {
    fn code(self) -> u8 {
        match self {
            Self::Explicit => 1,
            Self::Deadline => 2,
        }
    }
    fn decode(code: u8) -> Option<Self> {
        match code {
            1 => Some(Self::Explicit),
            2 => Some(Self::Deadline),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum CancellationError {
    #[error("Core cancellation control capacity exhausted")]
    Control(#[from] CoreControlError),
}

const CANCEL_NODE_CONTROL_BYTES: u64 = 256;
const WAITER_CONTROL_BYTES: u64 = 128;

struct CancellationNode {
    token: TokioCancellationToken,
    parent: Option<Arc<CancellationNode>>,
    control: CoreControl,
    depth: u8,
    cause: AtomicU8,
    _reservation: ControlReservation,
}

/// Cloneable cancellation handle. Clones share a node; children cancel only
/// themselves and their descendants, while parent cancellation reaches every
/// descendant. The retained parent chain preserves notification origin.
#[derive(Clone)]
pub struct CoreCancellation {
    node: Arc<CancellationNode>,
    // A fail-closed compatibility view used by inert CallCx snapshots. It
    // shares no new node or control record and cannot signal its real parent.
    inert: bool,
}
impl std::fmt::Debug for CoreCancellation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CoreCancellation")
            .field("depth", &self.node.depth)
            .field("cancelled", &self.is_cancelled())
            .finish()
    }
}
impl CoreCancellation {
    /// Compatibility root backed by the shared bounded default account.
    pub fn new() -> Self {
        Self::new_with_control(&default_control_account())
            .expect("bounded default cancellation capacity exhausted")
    }

    /// Create a root after reserving its native cancellation record.
    pub fn new_with_control(control: &CoreControl) -> Result<Self, CoreControlError> {
        let reservation = control.reserve(ControlKind::Context, CANCEL_NODE_CONTROL_BYTES)?;
        Ok(Self {
            node: Arc::new(CancellationNode {
                token: TokioCancellationToken::new(),
                parent: None,
                control: control.clone(),
                depth: 0,
                cause: AtomicU8::new(0),
                _reservation: reservation,
            }),
            inert: false,
        })
    }

    /// Create a separately funded child node. The parent is retained so its
    /// cancellation and cause remain observable for the child's lifetime.
    pub fn child_with_control(&self, control: &CoreControl) -> Result<Self, CoreControlError> {
        if self.inert || !self.node.control.same_account(control) {
            return Err(CoreControlError::Invalid);
        }
        let depth = self
            .node
            .depth
            .checked_add(1)
            .filter(|depth| *depth <= 32)
            .ok_or(CoreControlError::Limit)?;
        let reservation = control.reserve(ControlKind::Context, CANCEL_NODE_CONTROL_BYTES)?;
        Ok(Self {
            node: Arc::new(CancellationNode {
                token: self.node.token.child_token(),
                parent: Some(self.node.clone()),
                control: control.clone(),
                depth,
                cause: AtomicU8::new(0),
                _reservation: reservation,
            }),
            inert: false,
        })
    }

    pub(crate) fn inert_view(&self) -> Self {
        Self {
            node: self.node.clone(),
            inert: true,
        }
    }

    /// Bounded compatibility child using the same account as its parent.
    pub fn child(&self) -> Result<Self, CoreControlError> {
        self.child_with_control(&self.node.control)
    }

    /// Compatibility spelling for Tokio's scoped child token.
    pub fn child_token(&self) -> Self {
        self.child()
            .expect("bounded default cancellation capacity exhausted")
    }

    pub fn depth(&self) -> u8 {
        self.node.depth
    }

    pub(crate) fn uses_control(&self, control: &CoreControl) -> bool {
        self.node.control.same_account(control)
    }

    pub fn same_node(&self, other: &Self) -> bool {
        self.inert == other.inert && Arc::ptr_eq(&self.node, &other.node)
    }

    pub fn is_direct_child_of(&self, parent: &Self) -> bool {
        !self.inert
            && !parent.inert
            && self
                .node
                .parent
                .as_ref()
                .is_some_and(|actual| Arc::ptr_eq(actual, &parent.node))
    }

    pub fn inherits(&self, ancestor: &Self) -> bool {
        if self.inert || ancestor.inert {
            return false;
        }
        let mut current = Some(self.node.as_ref());
        while let Some(node) = current {
            if std::ptr::eq(node, ancestor.node.as_ref()) {
                return true;
            }
            current = node.parent.as_deref();
        }
        false
    }

    pub fn view(&self) -> CancellationView<'_> {
        CancellationView { token: self }
    }

    pub fn is_cancelled(&self) -> bool {
        self.inert || self.node.token.is_cancelled()
    }

    /// The nearest explicit/deadline notification in this node's ancestry.
    pub fn cause(&self) -> Option<CancellationCause> {
        if self.inert {
            return Some(CancellationCause::Explicit);
        }
        let mut current = Some(self.node.as_ref());
        while let Some(node) = current {
            if let Some(cause) = CancellationCause::decode(node.cause.load(Ordering::Acquire)) {
                return Some(cause);
            }
            current = node.parent.as_deref();
        }
        None
    }

    pub fn cancel(&self) {
        self.signal(CancellationCause::Explicit);
    }

    /// Signals cancellation only. Gate independently settles the deadline
    /// family from the original immutable deadline.
    pub fn notify_deadline(&self) {
        self.signal(CancellationCause::Deadline);
    }

    fn signal(&self, cause: CancellationCause) {
        if self.inert {
            return;
        }
        let _ =
            self.node
                .cause
                .compare_exchange(0, cause.code(), Ordering::AcqRel, Ordering::Acquire);
        self.node.token.cancel();
    }

    /// Reserve a waiter slot before returning its future. The slot stays live
    /// until that future is dropped, including when the token fires.
    pub fn cancelled(&self) -> CancellationFuture {
        let (inner, reservation, error) = if self.is_cancelled() {
            (None, None, None)
        } else {
            match self
                .node
                .control
                .reserve(ControlKind::Waiter, WAITER_CONTROL_BYTES)
            {
                Ok(reservation) => (
                    Some(Box::pin(self.node.token.clone().cancelled_owned())),
                    Some(reservation),
                    None,
                ),
                Err(_) if self.is_cancelled() => (None, None, None),
                Err(error) => (None, None, Some(error)),
            }
        };
        CancellationFuture {
            inner,
            node: self.node.clone(),
            _reservation: reservation,
            error,
        }
    }
}
impl Default for CoreCancellation {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Clone, Copy)]
pub struct CancellationView<'a> {
    token: &'a CoreCancellation,
}
impl CancellationView<'_> {
    pub fn is_cancelled(self) -> bool {
        self.token.is_cancelled()
    }
    pub fn cause(self) -> Option<CancellationCause> {
        self.token.cause()
    }
}

/// Owned Tokio waiter plus its Core control slot. It cannot lose a wake if a
/// cancel races future construction because Tokio's token stores cancellation.
pub struct CancellationFuture {
    inner: Option<Pin<Box<WaitForCancellationFutureOwned>>>,
    node: Arc<CancellationNode>,
    _reservation: Option<ControlReservation>,
    error: Option<CoreControlError>,
}
impl Future for CancellationFuture {
    type Output = Result<CancellationCause, CancellationError>;

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.as_mut().get_mut();
        if let Some(error) = this.error.take() {
            return Poll::Ready(Err(CancellationError::Control(error)));
        }
        let Some(inner) = this.inner.as_mut() else {
            return Poll::Ready(Ok(this
                .node
                .cause_from_ancestors()
                .unwrap_or(CancellationCause::Explicit)));
        };
        match inner.as_mut().poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(()) => Poll::Ready(Ok(this
                .node
                .cause_from_ancestors()
                .unwrap_or(CancellationCause::Explicit))),
        }
    }
}
impl CancellationNode {
    fn cause_from_ancestors(&self) -> Option<CancellationCause> {
        let mut current = Some(self);
        while let Some(node) = current {
            if let Some(cause) = CancellationCause::decode(node.cause.load(Ordering::Acquire)) {
                return Some(cause);
            }
            current = node.parent.as_deref();
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ControlKind, CoreControlLimits};

    #[tokio::test]
    async fn child_cancellation_is_scoped_and_parent_reaches_descendants() {
        let control = CoreControl::default();
        let root = CoreCancellation::new_with_control(&control).unwrap();
        let child = root.child_with_control(&control).unwrap();
        let sibling = root.child_with_control(&control).unwrap();
        let grandchild = child.child_with_control(&control).unwrap();
        child.cancel();
        assert!(grandchild.is_cancelled());
        assert!(!root.is_cancelled());
        assert!(!sibling.is_cancelled());
        root.notify_deadline();
        assert_eq!(sibling.cause(), Some(CancellationCause::Deadline));
    }

    #[tokio::test]
    async fn waiter_capacity_is_reserved_until_waiter_is_dropped() {
        let control = CoreControl::new(CoreControlLimits {
            max_waiters: 1,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let token = CoreCancellation::new_with_control(&control).unwrap();
        let mut waiter = Box::pin(token.cancelled());
        assert_eq!(control.usage().count(ControlKind::Waiter), 1);
        assert!(matches!(
            token.cancelled().await,
            Err(CancellationError::Control(CoreControlError::Limit))
        ));
        token.cancel();
        assert!(waiter.as_mut().await.is_ok());
        drop(waiter);
        assert_eq!(control.usage().count(ControlKind::Waiter), 0);
    }
}
