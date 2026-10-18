//! Allocation-owned guest values. Trusted metadata continues to use CanonValue.
//! No owned guest payload can be extracted from these immutable shared graphs.
use crate::{CanonValue, Digest, MAX_BYTES, MAX_DEPTH, MAX_SAFE_INTEGER};
use sha2::{Digest as _, Sha256};
use std::alloc::{Layout, alloc, dealloc};
use std::mem::ManuallyDrop;
use std::ptr::NonNull;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering, fence},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AllocationKind {
    Graph,
    String,
    Sequence,
    Object,
    Parse,
    Encode,
    Clone,
    Temporary,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AllocationRequest {
    pub kind: AllocationKind,
    pub bytes: u64,
    pub elements: u64,
    pub depth: u32,
    pub work: u64,
}
/// Checked bound for the fixed `op`/`key`/byte-array record conversion below.
/// `validation_work` also prepays borrowed validation and hashing outside the
/// graph codec. It is part of, not additional to, `allocation.work`.
#[derive(Clone, Copy, Debug)]
pub struct ByteRecordConversionBound {
    pub allocation: AllocationRequest,
    pub validation_work: u64,
}
/// Length-only inventory of the fixed Core network request digest shape.
/// The caller must fund collection and validate the WIT shape before using it.
#[derive(Clone, Copy, Debug, Default)]
pub struct NetworkDigestLengths {
    pub body_bytes: usize,
    pub headers: usize,
    pub secret_headers: usize,
    pub secret_names: usize,
    pub method_url_bytes: usize,
    pub header_bytes: usize,
    pub secret_header_bytes: usize,
    pub secret_name_bytes: usize,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum AllocationError {
    #[error("host allocation owner unavailable")]
    Unavailable,
    #[error("host allocation limit exceeded")]
    Limit,
    #[error("host allocation arithmetic overflow")]
    Overflow,
    #[error("host allocation failed")]
    Allocator,
    #[error("guest value nesting exceeds limit")]
    Depth,
    #[error("guest value is invalid strict JSON")]
    Invalid,
}
/// A trusted adapter retains the stable owner lock and all applicable live caps.
/// Reserve must deny atomically before allocation; release receives only storage
/// which has actually been freed. The adapter's own bookkeeping is prepaid.
pub trait AllocationBudget: Send + Sync {
    fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError>;
    fn release(&self, bytes: u64);
}
/// Inline ownership token: no boxed reservation or recursive metadata allocation.
struct Lease {
    budget: Arc<dyn AllocationBudget>,
    bytes: u64,
}
impl Lease {
    fn new(budget: &Arc<dyn AllocationBudget>) -> Self {
        Self {
            budget: budget.clone(),
            bytes: 0,
        }
    }
    fn charge(
        &mut self,
        kind: AllocationKind,
        bytes: usize,
        elements: usize,
        depth: usize,
        work: usize,
    ) -> Result<(), AllocationError> {
        if depth > MAX_DEPTH {
            return Err(AllocationError::Depth);
        }
        let bytes = u64::try_from(bytes).map_err(|_| AllocationError::Overflow)?;
        let total = self
            .bytes
            .checked_add(bytes)
            .ok_or(AllocationError::Overflow)?;
        self.budget.reserve(AllocationRequest {
            kind,
            bytes,
            elements: u64::try_from(elements).map_err(|_| AllocationError::Overflow)?,
            depth: u32::try_from(depth).map_err(|_| AllocationError::Overflow)?,
            work: u64::try_from(work).map_err(|_| AllocationError::Overflow)?,
        })?;
        self.bytes = total;
        Ok(())
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        self.budget.release(self.bytes);
    }
}

// A fallibly allocated Arc equivalent. The payload and control block are freed
// BEFORE releasing their inline byte lease. No mutation or owned extraction API.
struct Block<T, R> {
    count: AtomicUsize,
    value: ManuallyDrop<T>,
    release_plan: ManuallyDrop<R>,
}
struct FreeBlock {
    ptr: NonNull<u8>,
    layout: Layout,
}
impl Drop for FreeBlock {
    fn drop(&mut self) {
        unsafe {
            dealloc(self.ptr.as_ptr(), self.layout);
        }
    }
}
/// Immutable shared storage for a trusted, externally prepaid release plan.
/// This primitive grants no authority. Capacity factories must reserve the exact
/// `storage_bytes()` before construction, including their bounded metadata.
/// Last drop physically frees the payload and block before dropping the plan.
/// There is no Weak, mutation, or owned payload extraction surface.
pub struct LeasedShared<T, R> {
    ptr: NonNull<Block<T, R>>,
}
impl<T, R> std::fmt::Debug for LeasedShared<T, R> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LeasedShared(precharged immutable storage)")
    }
}
type Shared<T> = LeasedShared<T, Lease>;
impl<T> Shared<T> {
    fn new(value: T, mut lease: Lease) -> Result<Self, AllocationError> {
        let layout = Layout::new::<Block<T, Lease>>();
        if let Err(error) = lease.charge(AllocationKind::Graph, layout.size(), 1, 0, 1) {
            drop(value);
            drop(lease);
            return Err(error);
        }
        Self::try_new_precharged(value, lease)
    }
}
impl<T, R> LeasedShared<T, R> {
    /// Exact control-block layout bytes, including inline payload and plan.
    pub fn storage_bytes() -> usize {
        Layout::new::<Block<T, R>>().size()
    }
    /// Allocates only after a trusted factory has prepaid `storage_bytes()`.
    /// If allocation fails, payload destruction precedes release-plan drop.
    pub fn try_new_precharged(value: T, release_plan: R) -> Result<Self, AllocationError> {
        let layout = Layout::new::<Block<T, R>>();
        // SAFETY: layout is nonzero/aligned; null is handled without initialization.
        let raw = unsafe { alloc(layout) }.cast::<Block<T, R>>();
        let Some(ptr) = NonNull::new(raw) else {
            drop(value);
            drop(release_plan);
            return Err(AllocationError::Allocator);
        };
        // SAFETY: newly allocated storage is exclusively owned and correctly sized.
        unsafe {
            ptr.as_ptr().write(Block {
                count: AtomicUsize::new(1),
                value: ManuallyDrop::new(value),
                release_plan: ManuallyDrop::new(release_plan),
            });
        }
        Ok(Self { ptr })
    }
    /// Borrows immutable payload while retaining its physical storage owner.
    pub fn get(&self) -> &T {
        // SAFETY: every Shared retains the allocation until its own drop.
        unsafe { &self.ptr.as_ref().value }
    }
    fn value(&self) -> &T {
        self.get()
    }
}
impl<T, R> Clone for LeasedShared<T, R> {
    fn clone(&self) -> Self {
        // SAFETY: self owns a live reference. Overflow cannot permit early free.
        let old = unsafe { self.ptr.as_ref().count.fetch_add(1, Ordering::Relaxed) };
        if old > isize::MAX as usize {
            std::process::abort();
        }
        Self { ptr: self.ptr }
    }
}
impl<T, R> Drop for LeasedShared<T, R> {
    fn drop(&mut self) {
        // SAFETY: self owns a reference; only the unique last drop destroys it.
        if unsafe { self.ptr.as_ref().count.fetch_sub(1, Ordering::Release) } != 1 {
            return;
        }
        fence(Ordering::Acquire);
        unsafe {
            let block = &mut *self.ptr.as_ptr();
            let release_plan = ManuallyDrop::take(&mut block.release_plan);
            // This guard also deallocates on payload-drop unwinding, before R.
            let free = FreeBlock {
                ptr: self.ptr.cast(),
                layout: Layout::new::<Block<T, R>>(),
            };
            ManuallyDrop::drop(&mut block.value);
            drop(free);
            drop(release_plan);
        }
    }
}
// SAFETY: contents are immutable; atomic ownership protects their lifetime.
unsafe impl<T: Send + Sync, R: Send + Sync> Send for LeasedShared<T, R> {}
unsafe impl<T: Send + Sync, R: Send + Sync> Sync for LeasedShared<T, R> {}

/// A fallible prepaid immutable control allocation with borrowed type erasure.
/// Erasure retains the existing LeasedShared block, destructor and release plan;
/// it creates neither another allocation nor another reference-count mechanism.
/// This handle carries no authority. Private verifiers must validate actual
/// execution state after downcasting, not merely a type or allocation identity.
pub struct ControlHandle<T: ?Sized> {
    storage: ErasedStorage,
    value: NonNull<T>,
}
struct ErasedStorage {
    block: NonNull<()>,
    clone: unsafe fn(NonNull<()>),
    drop: unsafe fn(NonNull<()>),
}
impl Clone for ErasedStorage {
    fn clone(&self) -> Self {
        // SAFETY: only a live LeasedShared<T,R> creates this matching vtable.
        unsafe { (self.clone)(self.block) };
        Self {
            block: self.block,
            clone: self.clone,
            drop: self.drop,
        }
    }
}
impl Drop for ErasedStorage {
    fn drop(&mut self) {
        // SAFETY: this owns exactly one existing LeasedShared reference.
        unsafe { (self.drop)(self.block) };
    }
}
unsafe fn clone_control<T, R>(block: NonNull<()>) {
    let original = ManuallyDrop::new(LeasedShared::<T, R> { ptr: block.cast() });
    // Clone uses the original atomic counter; forgetting transfers that exact
    // additional reference into ErasedStorage, without creating a payload copy.
    std::mem::forget(LeasedShared::clone(&original));
}
unsafe fn drop_control<T, R>(block: NonNull<()>) {
    drop(LeasedShared::<T, R> { ptr: block.cast() });
}
impl<T: Send + Sync + 'static> ControlHandle<T> {
    /// Exact payload, release-plan and existing refcount/control layout bytes.
    /// A trusted preparation must reserve these BEFORE construction. Any owned
    /// allocations within T/R require their own separate prepaid inventory.
    pub fn storage_bytes<R>() -> usize {
        LeasedShared::<T, R>::storage_bytes()
    }
    /// Construct only after actual stable-owner preparation has prepaid the
    /// exact storage. On failure, destroy payload before returning its funds.
    /// Last drop frees payload and physical block before dropping release_plan.
    pub fn try_new_precharged<R>(value: T, release_plan: R) -> Result<Self, AllocationError>
    where
        R: Send + Sync + 'static,
    {
        let shared = LeasedShared::try_new_precharged(value, release_plan)?;
        let value = NonNull::from(shared.get());
        let storage = ErasedStorage {
            block: shared.ptr.cast(),
            clone: clone_control::<T, R>,
            drop: drop_control::<T, R>,
        };
        std::mem::forget(shared);
        Ok(Self { storage, value })
    }
}
impl<T: ?Sized> ControlHandle<T> {
    /// Borrow while this handle retains the original physical allocation.
    pub fn get(&self) -> &T {
        // SAFETY: construction retains the source; projections borrow from
        // that source or refer to independently longer-lived data.
        unsafe { self.value.as_ref() }
    }
    /// Project a borrowed view while retaining the same source storage.
    /// The returned handle may outlive this handle because it shares custody.
    /// The lifetime bound excludes shorter-lived captured borrows, but permits
    /// static data; projection alone does not authenticate object provenance.
    /// Sharing this handle does not physically copy the projected value.
    pub fn project<U: ?Sized>(
        &self,
        project: impl for<'a> FnOnce(&'a T) -> &'a U,
    ) -> ControlHandle<U> {
        let value = NonNull::from(project(self.get()));
        ControlHandle {
            storage: self.storage.clone(),
            value,
        }
    }
    /// Fallibly authenticate/project a borrowed view before sharing ownership.
    /// On denial this creates no reference and performs no allocation. Errors
    /// cannot borrow from the private payload through this lifetime boundary.
    pub fn try_project<U: ?Sized, E>(
        &self,
        project: impl for<'a> FnOnce(&'a T) -> Result<&'a U, E>,
    ) -> Result<ControlHandle<U>, E> {
        let value = NonNull::from(project(self.get())?);
        Ok(ControlHandle {
            storage: self.storage.clone(),
            value,
        })
    }
    /// Shared-storage and projected-pointer identity; this grants no permission
    /// and does not prove that the projected object is inside the source block.
    pub fn ptr_eq(left: &Self, right: &Self) -> bool {
        left.storage.block == right.storage.block && std::ptr::eq(left.get(), right.get())
    }
}
impl ControlHandle<dyn std::any::Any + Send + Sync> {
    /// Borrow a concrete payload; private execution-state validation is still
    /// mandatory after a successful downcast.
    pub fn downcast_ref<T: std::any::Any>(&self) -> Option<&T> {
        self.get().downcast_ref()
    }
}
impl<T: ?Sized> Clone for ControlHandle<T> {
    fn clone(&self) -> Self {
        Self {
            storage: self.storage.clone(),
            value: self.value,
        }
    }
}
impl<T: ?Sized> std::ops::Deref for ControlHandle<T> {
    type Target = T;
    fn deref(&self) -> &T {
        self.get()
    }
}
impl<T: ?Sized> AsRef<T> for ControlHandle<T> {
    fn as_ref(&self) -> &T {
        self.get()
    }
}
impl<T: ?Sized> std::fmt::Debug for ControlHandle<T> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ControlHandle(prepaid immutable shared control)")
    }
}
// SAFETY: construction enforces Send+Sync for the concrete payload AND release
// plan before erasure. Projection additionally requires the borrowed view to be
// Send+Sync when crossing threads; it cannot change the concrete destructor.
unsafe impl<T: ?Sized + Send + Sync> Send for ControlHandle<T> {}
unsafe impl<T: ?Sized + Send + Sync> Sync for ControlHandle<T> {}

enum Node {
    Null,
    Bool(bool),
    Int(i64),
    BigInt(String),
    Float(f64),
    Str(String),
    Arr(Vec<Node>),
    Obj(Vec<(String, Node)>),
    Link(CoreValue),
}
/// Opaque guest graph reference. Clone shares storage without allocating.
pub struct CoreValue {
    owner: Shared<Node>,
    node: NonNull<Node>,
}
impl Clone for CoreValue {
    fn clone(&self) -> Self {
        Self {
            owner: self.owner.clone(),
            node: self.node,
        }
    }
}
unsafe impl Send for CoreValue {}
unsafe impl Sync for CoreValue {}
impl std::fmt::Debug for CoreValue {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("CoreValue(immutable owned graph)")
    }
}
/// Borrowed inspection never creates an owned payload or releases its owner.
pub enum CoreRef<'a> {
    Null,
    Bool(bool),
    Int(i64),
    BigInt(&'a str),
    Float(f64),
    Str(&'a str),
    Arr(CoreArray<'a>),
    Obj(CoreObject<'a>),
}
pub struct CoreArray<'a> {
    source: &'a CoreValue,
    values: &'a [Node],
}
pub struct CoreObject<'a> {
    source: &'a CoreValue,
    values: &'a [(String, Node)],
}
impl CoreValue {
    /// Bound the unchanged network request graph and its StrictV1 digest.
    /// No byte traversal is needed to compute this checked, finite reservation.
    /// All scalar temporaries, deep copies and shared blocks are included; the
    /// digest itself streams into SHA and allocates no encoded byte buffer.
    pub fn network_digest_conversion_bound(
        lengths: NetworkDigestLengths,
    ) -> Result<AllocationRequest, AllocationError> {
        let add = |a: u64, b: u64| a.checked_add(b).ok_or(AllocationError::Overflow);
        let mul = |a: u64, b: u64| a.checked_mul(b).ok_or(AllocationError::Overflow);
        let count = |n: usize| u64::try_from(n).map_err(|_| AllocationError::Overflow);
        let n = count(lengths.body_bytes)?;
        let h = count(lengths.headers)?;
        let d = count(lengths.secret_headers)?;
        let s = count(lengths.secret_names)?;
        if add(h, d)? > 128 || s > 32 {
            return Err(AllocationError::Invalid);
        }
        let basic = count(lengths.method_url_bytes)?;
        let headers = count(lengths.header_bytes)?;
        let descriptors = count(lengths.secret_header_bytes)?;
        let names = count(lengths.secret_name_bytes)?;
        let text = add(add(basic, headers)?, add(descriptors, names)?)?;
        let node = size_of::<Node>() as u64;
        let field = size_of::<(String, Node)>() as u64;
        // The original codec constructs a scalar Shared for every node, then
        // copies the byte array once into the request record. Header tuples and
        // secret-header records are copied at each of their fixed three levels.
        let blocks = add(add(add(add(9, mul(h, 3)?)?, mul(d, 4)?)?, s)?, n)?;
        let slots = add(add(add(mul(h, 8)?, mul(d, 2)?)?, mul(n, 2)?)?, s)?;
        let fields = add(8, mul(d, 9)?)?;
        let copied_text = add(
            add(mul(basic, 2)?, mul(add(headers, descriptors)?, 4)?)?,
            add(mul(names, 2)?, add(63, mul(d, 45)?)?)?,
        )?;
        let bytes = add(
            add(
                mul(blocks, Shared::<Node>::storage_bytes() as u64)?,
                mul(slots, node)?,
            )?,
            add(mul(fields, field)?, copied_text)?,
        )?;
        if bytes > isize::MAX as u64 {
            return Err(AllocationError::Overflow);
        }
        // u8 encodes as <=3 decimal bytes plus a separator. UTF-8 strings need
        // <=6 encoded bytes per input byte. Include null body/timeout variants,
        // every quote/key/separator, and all fixed request/envelope containers.
        let encoded = add(
            add(mul(n, 4)?, mul(text, 6)?)?,
            add(add(mul(h, 8)?, mul(d, 43)?)?, add(mul(s, 3)?, 121)?)?,
        )?;
        // Seven original work units per body element cover scalar construction,
        // vector reservations, copies, depth and encoded-size walks. One more
        // prepays the narrow u8 validator. The metadata term conservatively
        // covers copies, field comparisons/shifts (max record arity six), depth
        // and sizing. Actual encoding/hash is prepaid as 2*encoded separately.
        let metadata_work = add(
            add(add(1024, mul(field, 32)?)?, mul(h, 32)?)?,
            add(
                add(mul(d, add(192, mul(field, 6)?)?)?, mul(s, 8)?)?,
                add(
                    mul(add(basic, names)?, 4)?,
                    mul(add(headers, descriptors)?, 6)?,
                )?,
            )?,
        )?;
        Ok(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes,
            elements: add(blocks, 2)?, // Include the two envelope links.
            depth: 4,
            work: add(add(mul(n, 8)?, metadata_work)?, mul(encoded, 2)?)?,
        })
    }

    /// Bound the existing KV put graph construction and its StrictV1 encoding.
    /// This inspects lengths only: callers must fund `validation_work` before
    /// inspecting the borrowed key or byte elements. Every scalar temporary,
    /// container, string, encoded buffer and shared control block is included.
    /// The bound sums temporary allocations rather than assuming early reuse.
    pub fn kv_put_conversion_bound(
        key_bytes: usize,
        value_bytes: usize,
    ) -> Result<ByteRecordConversionBound, AllocationError> {
        let key = u64::try_from(key_bytes).map_err(|_| AllocationError::Overflow)?;
        let count = u64::try_from(value_bytes).map_err(|_| AllocationError::Overflow)?;
        let add = |a: u64, b: u64| a.checked_add(b).ok_or(AllocationError::Overflow);
        let mul = |a: u64, b: u64| a.checked_mul(b).ok_or(AllocationError::Overflow);
        // A u8 needs at most three decimal bytes plus one separator. A key
        // byte needs at most six JSON escape bytes. The fixed record is <64.
        let encoded = add(add(mul(count, 4)?, mul(key, 6)?)?, 64)?;
        let graph_blocks = mul(add(count, 4)?, Shared::<Node>::storage_bytes() as u64)?;
        let sequence = mul(count, size_of::<Node>() as u64)?;
        let fields = mul(3, size_of::<(String, Node)>() as u64)?;
        // Owned text: key, "kv-put", and the three field names (2+3+5).
        let text = add(key, 16)?;
        let bytes = add(
            add(add(graph_blocks, sequence)?, add(fields, text)?)?,
            add(encoded, Shared::<Vec<u8>>::storage_bytes() as u64)?,
        )?;
        if bytes > isize::MAX as u64 {
            return Err(AllocationError::Overflow);
        }
        // Both borrowed validators and SHA traversal are otherwise outside
        // Lease::charge. Codec work includes scalar creation/copy, link-depth
        // checks, field comparison/shifts and strict sizing/emission passes.
        let validation_work = add(add(mul(count, 2)?, mul(key, 2)?)?, encoded)?;
        let work = add(
            add(add(mul(count, 16)?, mul(key, 16)?)?, mul(encoded, 3)?)?,
            add(1024, mul(16, size_of::<(String, Node)>() as u64)?)?,
        )?;
        Ok(ByteRecordConversionBound {
            allocation: AllocationRequest {
                kind: AllocationKind::Temporary,
                bytes,
                elements: count,
                depth: 2,
                work,
            },
            validation_work,
        })
    }

    /// Prepares a fixed prepaid append capacity before any stream/producer work.
    pub fn array_builder(
        budget: &Arc<dyn AllocationBudget>,
        maximum: usize,
    ) -> Result<CoreArrayBuilder, AllocationError> {
        let mut lease = Lease::new(budget);
        let values = reserve_vec::<Node>(maximum, &mut lease, 0, AllocationKind::Sequence)?;
        Ok(CoreArrayBuilder {
            values,
            lease,
            maximum,
            incremental: None,
        })
    }
    /// Starts an unknown-length collection with at most eight prepaid slots.
    /// Both the item count and complete StrictV1 array encoding remain bounded;
    /// each later physical growth needs admission before allocating storage.
    pub fn array_builder_incremental(
        budget: &Arc<dyn AllocationBudget>,
        maximum_bytes: usize,
    ) -> Result<CoreArrayBuilder, AllocationError> {
        let maximum_bytes = maximum_bytes.min(MAX_BYTES);
        if maximum_bytes < 2 {
            return Err(AllocationError::Limit);
        }
        // Even the smallest nonempty strict array needs 2*n + 1 bytes.
        let maximum = (maximum_bytes - 1) / 2;
        let mut lease = Lease::new(budget);
        let values = reserve_vec::<Node>(maximum.min(8), &mut lease, 0, AllocationKind::Sequence)?;
        Ok(CoreArrayBuilder {
            values,
            lease,
            maximum,
            incremental: Some(ArrayEncodingBound {
                maximum_bytes,
                encoded_bytes: 2,
            }),
        })
    }
    /// Prepares exact source-ordered slots for parallel results before spawning.
    pub fn array_builder_slots(
        budget: &Arc<dyn AllocationBudget>,
        count: usize,
    ) -> Result<CoreArraySlots, AllocationError> {
        let mut lease = Lease::new(budget);
        let mut values = reserve_vec::<Node>(count, &mut lease, 0, AllocationKind::Sequence)?;
        values.resize_with(count, || Node::Null);
        let mut filled = reserve_vec::<bool>(count, &mut lease, 0, AllocationKind::Temporary)?;
        filled.resize(count, false);
        Ok(CoreArraySlots {
            values,
            filled,
            lease,
        })
    }
    fn from_node(value: Node, lease: Lease) -> Result<Self, AllocationError> {
        let owner = Shared::new(value, lease)?;
        let node = NonNull::from(owner.value());
        Ok(Self { owner, node })
    }
    fn node(&self) -> &Node {
        let mut node = unsafe { self.node.as_ref() };
        while let Node::Link(value) = node {
            node = unsafe { value.node.as_ref() };
        }
        node
    }
    fn detached(&self, node: &Node) -> Self {
        if let Node::Link(value) = node {
            return value.clone();
        }
        Self {
            owner: self.owner.clone(),
            node: NonNull::from(node),
        }
    }
    pub fn inspect(&self) -> CoreRef<'_> {
        match self.node() {
            Node::Link(value) => value.inspect(),
            Node::Null => CoreRef::Null,
            Node::Bool(v) => CoreRef::Bool(*v),
            Node::Int(v) => CoreRef::Int(*v),
            Node::BigInt(v) => CoreRef::BigInt(v),
            Node::Float(v) => CoreRef::Float(*v),
            Node::Str(v) => CoreRef::Str(v),
            Node::Arr(v) => CoreRef::Arr(CoreArray {
                source: self,
                values: v,
            }),
            Node::Obj(v) => CoreRef::Obj(CoreObject {
                source: self,
                values: v,
            }),
        }
    }
    pub fn share(&self) -> Self {
        self.clone()
    }
    pub fn as_str(&self) -> Option<&str> {
        match self.node() {
            Node::Str(v) => Some(v),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self.node() {
            Node::Bool(v) => Some(*v),
            _ => None,
        }
    }
    pub fn as_i64(&self) -> Option<i64> {
        match self.node() {
            Node::Int(v) => Some(*v),
            _ => None,
        }
    }
    pub fn array(&self) -> Option<CoreArray<'_>> {
        match self.inspect() {
            CoreRef::Arr(v) => Some(v),
            _ => None,
        }
    }
    pub fn object(&self) -> Option<CoreObject<'_>> {
        match self.inspect() {
            CoreRef::Obj(v) => Some(v),
            _ => None,
        }
    }
    pub fn get_key(&self, key: &str) -> Option<Self> {
        self.object()?.get(key)
    }
    pub fn get_index(&self, index: usize) -> Option<Self> {
        self.array()?.get(index)
    }
    /// Trusted literal input is borrowed; every new owned guest copy is precharged.
    pub fn try_from_borrowed(
        value: &CanonValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError> {
        let mut lease = Lease::new(budget);
        let node = copy_canon(value, &mut lease, 0)?;
        Self::from_node(node, lease)
    }
    pub fn try_copy_to(&self, budget: &Arc<dyn AllocationBudget>) -> Result<Self, AllocationError> {
        let mut lease = Lease::new(budget);
        let node = copy_node(self.node(), &mut lease, 0)?;
        Self::from_node(node, lease)
    }
    pub fn string(
        value: &str,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError> {
        let mut lease = Lease::new(budget);
        let node = Node::Str(copy_string(value, &mut lease, 0)?);
        Self::from_node(node, lease)
    }
    pub fn string_from_utf8(
        value: &CoreBuffer,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError> {
        Self::string(value.as_str()?, budget)
    }
    pub fn null(budget: &Arc<dyn AllocationBudget>) -> Result<Self, AllocationError> {
        Self::from_node(Node::Null, Lease::new(budget))
    }
    pub fn bool(value: bool, budget: &Arc<dyn AllocationBudget>) -> Result<Self, AllocationError> {
        Self::from_node(Node::Bool(value), Lease::new(budget))
    }
    pub fn int(value: i64, budget: &Arc<dyn AllocationBudget>) -> Result<Self, AllocationError> {
        if value.unsigned_abs() > MAX_SAFE_INTEGER {
            return Err(AllocationError::Invalid);
        }
        Self::from_node(Node::Int(value), Lease::new(budget))
    }
    /// Copy bytes into the existing immutable integer-array representation.
    /// The entire Node vector and its traversal work are admitted before the
    /// first byte is read. No scalar shared blocks or unleased payload escape.
    pub fn u8_array(
        bytes: &[u8],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError> {
        let mut lease = Lease::new(budget);
        let mut values = reserve_vec::<Node>(bytes.len(), &mut lease, 1, AllocationKind::Sequence)?;
        for byte in bytes {
            values.push(Node::Int(i64::from(*byte)));
        }
        Self::from_node(Node::Arr(values), lease)
    }
    pub fn array_from<I>(
        values: I,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError>
    where
        I: ExactSizeIterator<Item = Result<Self, AllocationError>>,
    {
        let mut lease = Lease::new(budget);
        let mut out = reserve_vec::<Node>(values.len(), &mut lease, 1, AllocationKind::Sequence)?;
        for item in values {
            let item = item?;
            if out.len() == out.capacity() {
                return Err(AllocationError::Invalid);
            }
            out.push(copy_node(item.node(), &mut lease, 1)?);
        }
        Self::from_node(Node::Arr(out), lease)
    }
    pub fn object_from<'a, I>(
        values: I,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError>
    where
        I: ExactSizeIterator<Item = Result<(&'a str, Self), AllocationError>>,
    {
        let mut lease = Lease::new(budget);
        let mut out =
            reserve_vec::<(String, Node)>(values.len(), &mut lease, 1, AllocationKind::Object)?;
        for item in values {
            let (key, item) = item?;
            if out.len() == out.capacity() {
                return Err(AllocationError::Invalid);
            }
            let position = object_position(&out, key, &mut lease, 1)?;
            charge_shift(&out, position, &mut lease, 1)?;
            out.insert(
                position,
                (
                    copy_string(key, &mut lease, 1)?,
                    copy_node(item.node(), &mut lease, 1)?,
                ),
            );
        }
        Self::from_node(Node::Obj(out), lease)
    }
    /// Fund only the new container/ref metadata; child bodies retain original ownership.
    pub fn array_shared<I>(
        values: I,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError>
    where
        I: ExactSizeIterator<Item = Result<Self, AllocationError>>,
    {
        let mut lease = Lease::new(budget);
        let mut out = reserve_vec::<Node>(values.len(), &mut lease, 1, AllocationKind::Sequence)?;
        for value in values {
            if out.len() == out.capacity() {
                return Err(AllocationError::Invalid);
            }
            out.push(Node::Link(value?));
        }
        check_links(&out, &mut lease, 1)?;
        Self::from_node(Node::Arr(out), lease)
    }
    pub fn object_shared<'a, I>(
        values: I,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError>
    where
        I: ExactSizeIterator<Item = Result<(&'a str, Self), AllocationError>>,
    {
        let mut lease = Lease::new(budget);
        let mut out =
            reserve_vec::<(String, Node)>(values.len(), &mut lease, 1, AllocationKind::Object)?;
        for value in values {
            let (key, value) = value?;
            if out.len() == out.capacity() {
                return Err(AllocationError::Invalid);
            }
            let position = object_position(&out, key, &mut lease, 1)?;
            charge_shift(&out, position, &mut lease, 1)?;
            check_node_depth(value.node(), &mut lease, 1)?;
            out.insert(
                position,
                (copy_string(key, &mut lease, 1)?, Node::Link(value)),
            );
        }
        Self::from_node(Node::Obj(out), lease)
    }
    pub fn parse_guest(
        input: &[u8],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError> {
        if input.len() > MAX_BYTES {
            return Err(AllocationError::Invalid);
        }
        let mut lease = Lease::new(budget);
        lease.charge(AllocationKind::Parse, 0, input.len(), 0, input.len())?;
        if std::str::from_utf8(input).is_err() {
            return Err(AllocationError::Invalid);
        }
        let mut parser = GuestParser {
            input,
            at: 0,
            lease: &mut lease,
        };
        let value = parser.value(0)?;
        parser.ws();
        if parser.at != input.len() {
            return Err(AllocationError::Invalid);
        }
        Self::from_node(value, lease)
    }
    pub fn encode_guest(
        &self,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<CoreBuffer, AllocationError> {
        let mut lease = Lease::new(budget);
        let count = encoded_size(self.node(), &mut lease, 0)?;
        if count > MAX_BYTES {
            return Err(AllocationError::Limit);
        }
        let mut bytes = reserve_vec::<u8>(count, &mut lease, 0, AllocationKind::Encode)?;
        bytes.resize(count, 0);
        let mut sink = FixedSink {
            bytes: bytes.as_mut_slice(),
            at: 0,
        };
        emit(self.node(), &mut sink)?;
        if sink.at != count {
            return Err(AllocationError::Invalid);
        }
        CoreBuffer::from_vec(bytes, lease)
    }
    /// Hashes the exact StrictV1 byte stream without an owned serialization
    /// buffer. The verified creating frame funds traversal/encoding/hash work;
    /// this never obtains authority or a new budget from the source graph.
    /// Source storage remains borrowed and held throughout both passes.
    pub fn digest_guest(
        &self,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Digest, AllocationError> {
        let mut lease = Lease::new(budget);
        let count = encoded_size(self.node(), &mut lease, 0)?;
        if count > MAX_BYTES {
            return Err(AllocationError::Limit);
        }
        let work = count.checked_mul(2).ok_or(AllocationError::Overflow)?;
        lease.charge(AllocationKind::Encode, 0, count, 0, work)?;
        let mut hash = HashSink {
            hash: Sha256::new(),
            count: 0,
            maximum: count,
        };
        emit(self.node(), &mut hash)?;
        if hash.count != count {
            return Err(AllocationError::Invalid);
        }
        Ok(Digest::from_sha256(hash.hash.finalize().into()))
    }
    /// Compares strict guest values by borrowed structure. Both complete values
    /// are validated and all byte/node traversal work is funded before the
    /// comparison, including values whose different prefix permits early exit.
    /// No legacy scalar normalization or ownership transfer occurs.
    pub fn eq_guest(
        &self,
        other: &Self,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<bool, AllocationError> {
        let mut lease = Lease::new(budget);
        let left = encoded_size(self.node(), &mut lease, 0)?;
        let right = encoded_size(other.node(), &mut lease, 0)?;
        if left > MAX_BYTES || right > MAX_BYTES {
            return Err(AllocationError::Limit);
        }
        let work = left.checked_add(right).ok_or(AllocationError::Overflow)?;
        lease.charge(AllocationKind::Encode, 0, work, 0, work)?;
        Ok(equal_nodes(self.node(), other.node()))
    }
}

/// Bounded producer accumulation whose new reference storage is already paid.
/// Existing child nodes retain their original allocation owner without copying.
pub struct CoreArrayBuilder {
    values: Vec<Node>,
    lease: Lease,
    maximum: usize,
    incremental: Option<ArrayEncodingBound>,
}
struct ArrayEncodingBound {
    maximum_bytes: usize,
    encoded_bytes: usize,
}
impl CoreArrayBuilder {
    /// Fixed builders append within prepaid capacity. Incremental builders
    /// first fund traversal and any bounded physical growth, retaining child
    /// owners without copying or allocating a serialization buffer.
    pub fn push(&mut self, value: CoreValue) -> Result<(), AllocationError> {
        if self.values.len() == self.maximum {
            return Err(AllocationError::Limit);
        }
        if let Some(bound) = self.incremental.as_mut() {
            let size = encoded_size(value.node(), &mut self.lease, 1)?;
            let encoded_bytes = bound
                .encoded_bytes
                .checked_add(usize::from(!self.values.is_empty()))
                .and_then(|bytes| bytes.checked_add(size))
                .ok_or(AllocationError::Overflow)?;
            if encoded_bytes > bound.maximum_bytes {
                return Err(AllocationError::Limit);
            }
            grow_one_bounded(
                &mut self.values,
                &mut self.lease,
                0,
                AllocationKind::Sequence,
                self.maximum,
            )?;
            bound.encoded_bytes = encoded_bytes;
        } else {
            check_node_depth(value.node(), &mut self.lease, 1)?;
        }
        self.values.push(Node::Link(value));
        Ok(())
    }
    /// Number of completed results without creating a payload copy.
    pub fn len(&self) -> usize {
        self.values.len()
    }
    /// Whether no result has been appended.
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
    /// Seals storage as an immutable array; all child owners remain retained.
    pub fn finish(self) -> Result<CoreValue, AllocationError> {
        CoreValue::from_node(Node::Arr(self.values), self.lease)
    }
}
/// Fixed-index parallel result slots with exact source order and no reallocation.
pub struct CoreArraySlots {
    values: Vec<Node>,
    filled: Vec<bool>,
    lease: Lease,
}
impl CoreArraySlots {
    /// Fills a previously empty prepaid slot. Duplicate/out-of-range writes deny.
    pub fn set(&mut self, index: usize, value: CoreValue) -> Result<(), AllocationError> {
        if self.filled.get(index) != Some(&false) {
            return Err(AllocationError::Invalid);
        }
        check_node_depth(value.node(), &mut self.lease, 1)?;
        self.values[index] = Node::Link(value);
        self.filled[index] = true;
        Ok(())
    }
    /// Exact number of source-ordered slots.
    pub fn len(&self) -> usize {
        self.values.len()
    }
    /// Whether the fixed result array has zero slots.
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
    /// Requires every slot to be filled, then seals the source-ordered array.
    pub fn finish(mut self) -> Result<CoreValue, AllocationError> {
        self.lease.charge(
            AllocationKind::Temporary,
            0,
            self.filled.len(),
            0,
            self.filled.len(),
        )?;
        if self.filled.iter().any(|filled| !filled) {
            return Err(AllocationError::Invalid);
        }
        drop(self.filled);
        CoreValue::from_node(Node::Arr(self.values), self.lease)
    }
}
impl CoreArray<'_> {
    pub fn len(&self) -> usize {
        self.values.len()
    }
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
    pub fn get(&self, index: usize) -> Option<CoreValue> {
        self.values.get(index).map(|v| self.source.detached(v))
    }
    pub fn iter(&self) -> impl ExactSizeIterator<Item = CoreValue> + '_ {
        self.values.iter().map(|v| self.source.detached(v))
    }
}
impl CoreObject<'_> {
    pub fn len(&self) -> usize {
        self.values.len()
    }
    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
    pub fn get(&self, key: &str) -> Option<CoreValue> {
        self.values
            .binary_search_by(|(k, _)| k.as_str().cmp(key))
            .ok()
            .map(|i| self.source.detached(&self.values[i].1))
    }
    pub fn iter(&self) -> impl ExactSizeIterator<Item = (&str, CoreValue)> + '_ {
        self.values
            .iter()
            .map(|(k, v)| (k.as_str(), self.source.detached(v)))
    }
}
/// Controlled serialized bytes with the same allocation ownership guarantees.
#[derive(Clone)]
pub struct CoreBuffer {
    owner: Shared<Vec<u8>>,
}
impl CoreBuffer {
    pub fn builder(budget: &Arc<dyn AllocationBudget>, maximum: usize) -> CoreBufferBuilder {
        CoreBufferBuilder {
            bytes: Vec::new(),
            lease: Lease::new(budget),
            maximum,
        }
    }
    /// The callback receives only a fixed borrowed region. It cannot resize or
    /// take the allocation; all storage remains charged through opaque ownership.
    pub fn write_exact<F>(
        len: usize,
        budget: &Arc<dyn AllocationBudget>,
        write: F,
    ) -> Result<Self, AllocationError>
    where
        F: FnOnce(&mut [u8]) -> Result<(), AllocationError>,
    {
        let mut lease = Lease::new(budget);
        let mut bytes = reserve_vec::<u8>(len, &mut lease, 0, AllocationKind::Temporary)?;
        bytes.resize(len, 0);
        write(bytes.as_mut_slice())?;
        Self::from_vec(bytes, lease)
    }
    fn from_vec(value: Vec<u8>, lease: Lease) -> Result<Self, AllocationError> {
        Ok(Self {
            owner: Shared::new(value, lease)?,
        })
    }
    pub fn copy_from(
        input: &[u8],
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self, AllocationError> {
        let mut lease = Lease::new(budget);
        let mut bytes = reserve_vec::<u8>(input.len(), &mut lease, 0, AllocationKind::Temporary)?;
        bytes.extend_from_slice(input);
        Self::from_vec(bytes, lease)
    }
    pub fn as_bytes(&self) -> &[u8] {
        self.owner.value()
    }
    pub fn as_str(&self) -> Result<&str, AllocationError> {
        std::str::from_utf8(self.as_bytes()).map_err(|_| AllocationError::Invalid)
    }
    pub fn share(&self) -> Self {
        self.clone()
    }
    pub fn len(&self) -> usize {
        self.as_bytes().len()
    }
    pub fn is_empty(&self) -> bool {
        self.as_bytes().is_empty()
    }
    pub fn try_copy_to(&self, budget: &Arc<dyn AllocationBudget>) -> Result<Self, AllocationError> {
        Self::copy_from(self.as_bytes(), budget)
    }
}
impl std::fmt::Debug for CoreBuffer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("CoreBuffer(immutable owned bytes)")
    }
}
/// Append-only bounded producer buffer. No mutable or owned payload escape.
pub struct CoreBufferBuilder {
    bytes: Vec<u8>,
    lease: Lease,
    maximum: usize,
}
impl CoreBufferBuilder {
    fn reserve_tail(&mut self, count: usize) -> Result<usize, AllocationError> {
        let length = self
            .bytes
            .len()
            .checked_add(count)
            .ok_or(AllocationError::Overflow)?;
        if length > self.maximum {
            return Err(AllocationError::Limit);
        }
        if length > self.bytes.capacity() {
            let capacity = self
                .bytes
                .capacity()
                .checked_mul(2)
                .unwrap_or(self.maximum)
                .max(length)
                .min(self.maximum);
            self.lease
                .charge(AllocationKind::Temporary, capacity, capacity, 0, capacity)?;
            self.bytes
                .try_reserve_exact(capacity - self.bytes.len())
                .map_err(|_| AllocationError::Allocator)?;
            if self.bytes.capacity() != capacity {
                // Unsupported allocator capacity cannot escape into finish().
                // Destroy storage before its retained conservative lease.
                drop(std::mem::take(&mut self.bytes));
                return Err(AllocationError::Unavailable);
            }
        }
        self.lease
            .charge(AllocationKind::Temporary, 0, count, 0, count)?;
        Ok(length)
    }
    pub fn append(&mut self, input: &[u8]) -> Result<(), AllocationError> {
        self.reserve_tail(input.len())?;
        self.bytes.extend_from_slice(input);
        Ok(())
    }
    /// Reserves the complete input region before the producer performs a read.
    /// The producer can write only this borrowed tail; its length cannot change
    /// or escape. Failed/unused tail bytes are discarded while allocated storage
    /// remains charged until the builder or finished buffer is physically freed.
    pub fn write_chunk<F>(&mut self, max_chunk: usize, write: F) -> Result<usize, AllocationError>
    where
        F: FnOnce(&mut [u8]) -> Result<usize, AllocationError>,
    {
        let start = self.bytes.len();
        let end = self.reserve_tail(max_chunk)?;
        self.bytes.resize(end, 0);
        struct Rewind<'a> {
            bytes: &'a mut Vec<u8>,
            keep: usize,
        }
        impl Drop for Rewind<'_> {
            fn drop(&mut self) {
                self.bytes.truncate(self.keep);
            }
        }
        let mut rewind = Rewind {
            bytes: &mut self.bytes,
            keep: start,
        };
        let written = write(&mut rewind.bytes[start..end]);
        match written {
            Ok(count) if count <= max_chunk => {
                rewind.keep = start + count;
                Ok(count)
            }
            Ok(_) => Err(AllocationError::Invalid),
            Err(error) => Err(error),
        }
    }
    pub fn len(&self) -> usize {
        self.bytes.len()
    }
    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }
    pub fn finish(self) -> Result<CoreBuffer, AllocationError> {
        CoreBuffer::from_vec(self.bytes, self.lease)
    }
}
fn reserve_vec<T>(
    len: usize,
    lease: &mut Lease,
    depth: usize,
    kind: AllocationKind,
) -> Result<Vec<T>, AllocationError> {
    let bytes = len
        .checked_mul(size_of::<T>())
        .ok_or(AllocationError::Overflow)?;
    if bytes > isize::MAX as usize {
        return Err(AllocationError::Overflow);
    }
    lease.charge(kind, bytes, len, depth, len)?;
    let mut out = Vec::new();
    out.try_reserve_exact(len)
        .map_err(|_| AllocationError::Allocator)?;
    // Standard Vec exact reservation exposes its physical usable capacity.
    // An allocator returning a larger usable layout is unsupported, fail closed.
    if out.capacity() != len && size_of::<T>() != 0 {
        drop(out);
        return Err(AllocationError::Unavailable);
    }
    Ok(out)
}
fn copy_string(input: &str, lease: &mut Lease, depth: usize) -> Result<String, AllocationError> {
    let mut bytes = reserve_vec::<u8>(input.len(), lease, depth, AllocationKind::String)?;
    bytes.extend_from_slice(input.as_bytes());
    // SAFETY: this is an exact copy of validated Rust UTF-8, with no reallocation.
    Ok(unsafe { String::from_utf8_unchecked(bytes) })
}
fn check_links(values: &[Node], lease: &mut Lease, depth: usize) -> Result<(), AllocationError> {
    for value in values {
        check_node_depth(value, lease, depth)?;
    }
    Ok(())
}
fn check_node_depth(value: &Node, lease: &mut Lease, depth: usize) -> Result<(), AllocationError> {
    lease.charge(AllocationKind::Sequence, 0, 1, depth, 1)?;
    match value {
        Node::Link(value) => check_node_depth(value.node(), lease, depth),
        Node::Arr(values) => check_links(values, lease, depth + 1),
        Node::Obj(values) => {
            for (_, value) in values {
                check_node_depth(value, lease, depth + 1)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}
fn copy_canon(
    value: &CanonValue,
    lease: &mut Lease,
    depth: usize,
) -> Result<Node, AllocationError> {
    lease.charge(AllocationKind::Clone, 0, 1, depth, 1)?;
    Ok(match value {
        CanonValue::Null => Node::Null,
        CanonValue::Bool(v) => Node::Bool(*v),
        CanonValue::Int(v) if v.unsigned_abs() <= MAX_SAFE_INTEGER => Node::Int(*v),
        CanonValue::Int(_) => return Err(AllocationError::Invalid),
        CanonValue::Float(v) => Node::Float(*v),
        CanonValue::BigInt(v) => Node::BigInt(copy_string(v, lease, depth)?),
        CanonValue::Str(v) => Node::Str(copy_string(v, lease, depth)?),
        CanonValue::Arr(values) => {
            let mut out =
                reserve_vec::<Node>(values.len(), lease, depth, AllocationKind::Sequence)?;
            for v in values {
                out.push(copy_canon(v, lease, depth + 1)?);
            }
            Node::Arr(out)
        }
        CanonValue::Obj(values) => {
            let mut out =
                reserve_vec::<(String, Node)>(values.len(), lease, depth, AllocationKind::Object)?;
            for (k, v) in values {
                out.push((
                    copy_string(k, lease, depth)?,
                    copy_canon(v, lease, depth + 1)?,
                ));
            }
            Node::Obj(out)
        }
    })
}
fn copy_node(value: &Node, lease: &mut Lease, depth: usize) -> Result<Node, AllocationError> {
    lease.charge(AllocationKind::Clone, 0, 1, depth, 1)?;
    Ok(match value {
        Node::Link(value) => copy_node(value.node(), lease, depth)?,
        Node::Null => Node::Null,
        Node::Bool(v) => Node::Bool(*v),
        Node::Int(v) => Node::Int(*v),
        Node::Float(v) => Node::Float(*v),
        Node::BigInt(v) => Node::BigInt(copy_string(v, lease, depth)?),
        Node::Str(v) => Node::Str(copy_string(v, lease, depth)?),
        Node::Arr(values) => {
            let mut out =
                reserve_vec::<Node>(values.len(), lease, depth, AllocationKind::Sequence)?;
            for v in values {
                out.push(copy_node(v, lease, depth + 1)?);
            }
            Node::Arr(out)
        }
        Node::Obj(values) => {
            let mut out =
                reserve_vec::<(String, Node)>(values.len(), lease, depth, AllocationKind::Object)?;
            for (k, v) in values {
                out.push((
                    copy_string(k, lease, depth)?,
                    copy_node(v, lease, depth + 1)?,
                ));
            }
            Node::Obj(out)
        }
    })
}

// Guest codec helpers are deliberately separate from trusted legacy codecs.
// Every owned allocation and temporary follows the graph's prepaid inline lease.
struct GuestParser<'a, 'b> {
    input: &'a [u8],
    at: usize,
    lease: &'b mut Lease,
}
impl GuestParser<'_, '_> {
    fn ws(&mut self) {
        while self
            .input
            .get(self.at)
            .is_some_and(|b| matches!(b, b' ' | b'\n' | b'\r' | b'\t'))
        {
            self.at += 1;
        }
    }
    fn take(&mut self, b: u8) -> Result<(), AllocationError> {
        if self.input.get(self.at) != Some(&b) {
            return Err(AllocationError::Invalid);
        }
        self.at += 1;
        Ok(())
    }
    fn literal(&mut self, text: &[u8], node: Node) -> Result<Node, AllocationError> {
        if !self.input[self.at..].starts_with(text) {
            return Err(AllocationError::Invalid);
        }
        self.at += text.len();
        Ok(node)
    }
    fn value(&mut self, depth: usize) -> Result<Node, AllocationError> {
        self.lease.charge(AllocationKind::Parse, 0, 1, depth, 1)?;
        self.ws();
        match self.input.get(self.at) {
            Some(b'n') => self.literal(b"null", Node::Null),
            Some(b't') => self.literal(b"true", Node::Bool(true)),
            Some(b'f') => self.literal(b"false", Node::Bool(false)),
            Some(b'"') => Ok(Node::Str(self.string(depth)?)),
            Some(b'[') => {
                self.at += 1;
                let mut out = Vec::new();
                self.ws();
                if self.input.get(self.at) == Some(&b']') {
                    self.at += 1;
                    return Ok(Node::Arr(out));
                }
                loop {
                    let item = self.value(depth + 1)?;
                    grow_one(&mut out, self.lease, depth, AllocationKind::Sequence)?;
                    out.push(item);
                    self.ws();
                    if self.input.get(self.at) == Some(&b']') {
                        self.at += 1;
                        break;
                    }
                    self.take(b',')?;
                }
                Ok(Node::Arr(out))
            }
            Some(b'{') => {
                self.at += 1;
                let mut out: Vec<(String, Node)> = Vec::new();
                self.ws();
                if self.input.get(self.at) == Some(&b'}') {
                    self.at += 1;
                    return Ok(Node::Obj(out));
                }
                loop {
                    self.ws();
                    let key = self.string(depth)?;
                    let position = object_position(&out, &key, self.lease, depth)?;
                    self.ws();
                    self.take(b':')?;
                    let value = self.value(depth + 1)?;
                    grow_one(&mut out, self.lease, depth, AllocationKind::Object)?;
                    charge_shift(&out, position, self.lease, depth)?;
                    out.insert(position, (key, value));
                    self.ws();
                    if self.input.get(self.at) == Some(&b'}') {
                        self.at += 1;
                        break;
                    }
                    self.take(b',')?;
                }
                Ok(Node::Obj(out))
            }
            Some(b'-' | b'0'..=b'9') => {
                let start = self.at;
                if self.input.get(self.at) == Some(&b'-') {
                    self.at += 1;
                }
                match self.input.get(self.at) {
                    Some(b'0') => self.at += 1,
                    Some(b'1'..=b'9') => {
                        while self.input.get(self.at).is_some_and(u8::is_ascii_digit) {
                            self.at += 1;
                        }
                    }
                    _ => return Err(AllocationError::Invalid),
                }
                let text = std::str::from_utf8(&self.input[start..self.at])
                    .map_err(|_| AllocationError::Invalid)?;
                let value = text.parse::<i64>().map_err(|_| AllocationError::Invalid)?;
                if value.unsigned_abs() > MAX_SAFE_INTEGER {
                    return Err(AllocationError::Invalid);
                }
                Ok(Node::Int(value))
            }
            _ => Err(AllocationError::Invalid),
        }
    }
    fn string(&mut self, depth: usize) -> Result<String, AllocationError> {
        self.take(b'"')?;
        let start = self.at;
        let mut end = self.at;
        let mut escaped = false;
        loop {
            let b = *self.input.get(end).ok_or(AllocationError::Invalid)?;
            if !escaped && b == b'"' {
                break;
            }
            if !escaped && b < 32 {
                return Err(AllocationError::Invalid);
            }
            escaped = !escaped && b == b'\\';
            end += 1;
        }
        // Decoded UTF-8 cannot exceed the encoded byte span (including escapes).
        let capacity = end.checked_sub(start).ok_or(AllocationError::Overflow)?;
        let mut out = reserve_vec::<u8>(capacity, self.lease, depth, AllocationKind::String)?;
        while self.at < end {
            let b = self.input[self.at];
            self.at += 1;
            if b != b'\\' {
                out.push(b);
                continue;
            }
            let escape = *self.input.get(self.at).ok_or(AllocationError::Invalid)?;
            self.at += 1;
            match escape {
                b'"' | b'\\' | b'/' => out.push(escape),
                b'b' => out.push(8),
                b'f' => out.push(12),
                b'n' => out.push(10),
                b'r' => out.push(13),
                b't' => out.push(9),
                b'u' => {
                    let first = self.hex4()?;
                    let code = if (0xd800..=0xdbff).contains(&first) {
                        self.take(b'\\')?;
                        self.take(b'u')?;
                        let last = self.hex4()?;
                        if !(0xdc00..=0xdfff).contains(&last) {
                            return Err(AllocationError::Invalid);
                        }
                        0x10000 + ((u32::from(first) - 0xd800) << 10) + u32::from(last) - 0xdc00
                    } else if (0xdc00..=0xdfff).contains(&first) {
                        return Err(AllocationError::Invalid);
                    } else {
                        u32::from(first)
                    };
                    let ch = char::from_u32(code).ok_or(AllocationError::Invalid)?;
                    let mut bytes = [0; 4];
                    out.extend_from_slice(ch.encode_utf8(&mut bytes).as_bytes());
                }
                _ => return Err(AllocationError::Invalid),
            }
        }
        if self.at != end {
            return Err(AllocationError::Invalid);
        }
        self.take(b'"')?;
        String::from_utf8(out).map_err(|_| AllocationError::Invalid)
    }
    fn hex4(&mut self) -> Result<u16, AllocationError> {
        let mut value = 0;
        for _ in 0..4 {
            let b = *self.input.get(self.at).ok_or(AllocationError::Invalid)?;
            self.at += 1;
            let digit = match b {
                b'0'..=b'9' => b - b'0',
                b'a'..=b'f' => b - b'a' + 10,
                b'A'..=b'F' => b - b'A' + 10,
                _ => return Err(AllocationError::Invalid),
            };
            value = value * 16 + u16::from(digit);
        }
        Ok(value)
    }
}
fn object_position(
    values: &[(String, Node)],
    key: &str,
    lease: &mut Lease,
    depth: usize,
) -> Result<usize, AllocationError> {
    let comparisons = (usize::BITS - values.len().leading_zeros()) as usize + 1;
    let work = comparisons
        .checked_mul(key.len().checked_add(1).ok_or(AllocationError::Overflow)?)
        .ok_or(AllocationError::Overflow)?;
    lease.charge(AllocationKind::Object, 0, values.len(), depth, work)?;
    match values.binary_search_by(|(k, _)| k.as_str().cmp(key)) {
        Ok(_) => Err(AllocationError::Invalid),
        Err(position) => Ok(position),
    }
}
fn charge_shift(
    values: &[(String, Node)],
    position: usize,
    lease: &mut Lease,
    depth: usize,
) -> Result<(), AllocationError> {
    let work = values
        .len()
        .checked_sub(position)
        .and_then(|n| n.checked_mul(size_of::<(String, Node)>()))
        .ok_or(AllocationError::Overflow)?;
    lease.charge(AllocationKind::Object, 0, values.len(), depth, work)
}
fn grow_one<T>(
    out: &mut Vec<T>,
    lease: &mut Lease,
    depth: usize,
    kind: AllocationKind,
) -> Result<(), AllocationError> {
    grow_one_bounded(out, lease, depth, kind, usize::MAX)
}
fn grow_one_bounded<T>(
    out: &mut Vec<T>,
    lease: &mut Lease,
    depth: usize,
    kind: AllocationKind,
    maximum: usize,
) -> Result<(), AllocationError> {
    if out.len() >= maximum {
        return Err(AllocationError::Limit);
    }
    if out.len() < out.capacity() {
        return Ok(());
    }
    let capacity = out
        .capacity()
        .checked_mul(2)
        .and_then(|n| n.checked_add(1))
        .ok_or(AllocationError::Overflow)?
        .min(maximum);
    // Both old and new allocations remain charged through the graph lifetime.
    // This conservatively retains old-capacity credit after verified relocation.
    let bytes = capacity
        .checked_mul(size_of::<T>())
        .filter(|bytes| *bytes <= isize::MAX as usize)
        .ok_or(AllocationError::Overflow)?;
    lease.charge(kind, bytes, capacity, depth, capacity)?;
    let additional = capacity
        .checked_sub(out.len())
        .ok_or(AllocationError::Overflow)?;
    out.try_reserve_exact(additional)
        .map_err(|_| AllocationError::Allocator)?;
    if out.capacity() != capacity && size_of::<T>() != 0 {
        drop(std::mem::take(out));
        return Err(AllocationError::Unavailable);
    }
    Ok(())
}
struct Digits {
    bytes: [u8; 32],
    len: usize,
}
impl std::fmt::Write for Digits {
    fn write_str(&mut self, s: &str) -> std::fmt::Result {
        let end = self.len.checked_add(s.len()).ok_or(std::fmt::Error)?;
        let dst = self.bytes.get_mut(self.len..end).ok_or(std::fmt::Error)?;
        dst.copy_from_slice(s.as_bytes());
        self.len = end;
        Ok(())
    }
}
fn integer(v: i64) -> Digits {
    use std::fmt::Write;
    let mut out = Digits {
        bytes: [0; 32],
        len: 0,
    };
    write!(&mut out, "{v}").expect("i64 fits fixed stack buffer");
    out
}
fn str_size(v: &str) -> Result<usize, AllocationError> {
    v.chars().try_fold(2usize, |total, ch| {
        total
            .checked_add(match ch {
                '"' | '\\' | '\n' | '\r' | '\t' | '\u{8}' | '\u{c}' => 2,
                '\u{0}'..='\u{1f}' => 6,
                _ => ch.len_utf8(),
            })
            .ok_or(AllocationError::Overflow)
    })
}
fn encoded_size(node: &Node, lease: &mut Lease, depth: usize) -> Result<usize, AllocationError> {
    lease.charge(AllocationKind::Encode, 0, 1, depth, 1)?;
    match node {
        Node::Link(value) => encoded_size(value.node(), lease, depth),
        Node::Null => Ok(4),
        Node::Bool(v) => Ok(if *v { 4 } else { 5 }),
        Node::Int(v) if v.unsigned_abs() <= MAX_SAFE_INTEGER => Ok(integer(*v).len),
        Node::Int(_) | Node::Float(_) | Node::BigInt(_) => Err(AllocationError::Invalid),
        Node::Str(v) => {
            lease.charge(AllocationKind::Encode, 0, v.len(), depth, v.len())?;
            str_size(v)
        }
        Node::Arr(values) => {
            let mut count = 2usize;
            for (i, v) in values.iter().enumerate() {
                let size = encoded_size(v, lease, depth + 1)?;
                count = count
                    .checked_add(usize::from(i > 0))
                    .and_then(|n| n.checked_add(size))
                    .ok_or(AllocationError::Overflow)?;
            }
            Ok(count)
        }
        Node::Obj(values) => {
            let mut count = 2usize;
            for (i, (k, v)) in values.iter().enumerate() {
                let value = encoded_size(v, lease, depth + 1)?;
                lease.charge(AllocationKind::Encode, 0, k.len(), depth, k.len())?;
                let key_size = str_size(k)?;
                count = count
                    .checked_add(usize::from(i > 0))
                    .and_then(|n| n.checked_add(key_size))
                    .and_then(|n| n.checked_add(1))
                    .and_then(|n| n.checked_add(value))
                    .ok_or(AllocationError::Overflow)?;
            }
            Ok(count)
        }
    }
}
trait StrictSink {
    fn extend_from_slice(&mut self, bytes: &[u8]) -> Result<(), AllocationError>;
    fn push(&mut self, byte: u8) -> Result<(), AllocationError> {
        self.extend_from_slice(&[byte])
    }
}
struct FixedSink<'a> {
    bytes: &'a mut [u8],
    at: usize,
}
impl StrictSink for FixedSink<'_> {
    fn extend_from_slice(&mut self, bytes: &[u8]) -> Result<(), AllocationError> {
        let end = self
            .at
            .checked_add(bytes.len())
            .ok_or(AllocationError::Overflow)?;
        self.bytes
            .get_mut(self.at..end)
            .ok_or(AllocationError::Invalid)?
            .copy_from_slice(bytes);
        self.at = end;
        Ok(())
    }
}
struct HashSink {
    hash: Sha256,
    count: usize,
    maximum: usize,
}
impl StrictSink for HashSink {
    fn extend_from_slice(&mut self, bytes: &[u8]) -> Result<(), AllocationError> {
        let end = self
            .count
            .checked_add(bytes.len())
            .filter(|n| *n <= self.maximum)
            .ok_or(AllocationError::Invalid)?;
        self.hash.update(bytes);
        self.count = end;
        Ok(())
    }
}
fn emit_str(v: &str, out: &mut impl StrictSink) -> Result<(), AllocationError> {
    out.push(b'"')?;
    for ch in v.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{8}' => out.extend_from_slice(b"\\b"),
            '\u{c}' => out.extend_from_slice(b"\\f"),
            '\u{0}'..='\u{1f}' => {
                const HEX: &[u8; 16] = b"0123456789abcdef";
                out.extend_from_slice(&[
                    b'\\',
                    b'u',
                    b'0',
                    b'0',
                    HEX[(ch as usize) >> 4],
                    HEX[(ch as usize) & 15],
                ])
            }
            _ => {
                let mut bytes = [0; 4];
                out.extend_from_slice(ch.encode_utf8(&mut bytes).as_bytes())
            }
        }?;
    }
    out.push(b'"')
}
fn emit(node: &Node, out: &mut impl StrictSink) -> Result<(), AllocationError> {
    match node {
        Node::Link(value) => emit(value.node(), out),
        Node::Null => out.extend_from_slice(b"null"),
        Node::Bool(v) => out.extend_from_slice(if *v { b"true" } else { b"false" }),
        Node::Int(v) => {
            let d = integer(*v);
            out.extend_from_slice(&d.bytes[..d.len])
        }
        Node::Str(v) => emit_str(v, out),
        Node::Arr(values) => {
            out.push(b'[')?;
            for (i, v) in values.iter().enumerate() {
                if i > 0 {
                    out.push(b',')?;
                }
                emit(v, out)?;
            }
            out.push(b']')
        }
        Node::Obj(values) => {
            out.push(b'{')?;
            for (i, (k, v)) in values.iter().enumerate() {
                if i > 0 {
                    out.push(b',')?;
                }
                emit_str(k, out)?;
                out.push(b':')?;
                emit(v, out)?;
            }
            out.push(b'}')
        }
        Node::BigInt(_) | Node::Float(_) => unreachable!("strict size pass rejects legacy scalars"),
    }
}

fn equal_nodes(left: &Node, right: &Node) -> bool {
    match (left, right) {
        (Node::Link(left), right) => equal_nodes(left.node(), right),
        (left, Node::Link(right)) => equal_nodes(left, right.node()),
        (Node::Null, Node::Null) => true,
        (Node::Bool(left), Node::Bool(right)) => left == right,
        (Node::Int(left), Node::Int(right)) => left == right,
        (Node::Str(left), Node::Str(right)) => left == right,
        (Node::Arr(left), Node::Arr(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(a, b)| equal_nodes(a, b))
        }
        (Node::Obj(left), Node::Obj(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|((ka, va), (kb, vb))| ka == kb && equal_nodes(va, vb))
        }
        // The complete precheck already rejects legacy floats and BigInt.
        _ => false,
    }
}

#[cfg(test)]
mod byte_conversion_tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Default)]
    struct Usage {
        live: u64,
        peak: u64,
        work: u64,
        calls: usize,
    }
    struct Budget {
        bytes: u64,
        work: u64,
        deny_call: Option<usize>,
        usage: Mutex<Usage>,
    }
    impl Budget {
        fn new(bytes: u64, work: u64, deny_call: Option<usize>) -> Arc<Self> {
            Arc::new(Self {
                bytes,
                work,
                deny_call,
                usage: Mutex::default(),
            })
        }
    }
    impl AllocationBudget for Budget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            let mut usage = self.usage.lock().unwrap();
            usage.calls += 1;
            if self.deny_call == Some(usage.calls) {
                return Err(AllocationError::Limit);
            }
            let live = usage
                .live
                .checked_add(request.bytes)
                .ok_or(AllocationError::Overflow)?;
            let work = usage
                .work
                .checked_add(request.work.max(request.elements))
                .ok_or(AllocationError::Overflow)?;
            if live > self.bytes || work > self.work {
                return Err(AllocationError::Limit);
            }
            usage.live = live;
            usage.peak = usage.peak.max(live);
            usage.work = work;
            Ok(())
        }
        fn release(&self, bytes: u64) {
            let mut usage = self.usage.lock().unwrap();
            usage.live = usage
                .live
                .checked_sub(bytes)
                .expect("known physical release");
        }
    }

    #[test]
    fn u8_array_preserves_all_bytes_and_uses_two_admissions_including_empty() {
        for length in [0usize, 1, 256, 6144] {
            let counted = Budget::new(u64::MAX, u64::MAX, None);
            let budget: Arc<dyn AllocationBudget> = counted.clone();
            let bytes: Vec<u8> = (0..length).map(|index| index as u8).collect();
            let array = CoreValue::u8_array(&bytes, &budget).unwrap();
            assert_eq!(counted.usage.lock().unwrap().calls, 2);
            assert_eq!(counted.usage.lock().unwrap().work, length as u64 + 1);
            let old = CoreValue::array_from(
                bytes
                    .iter()
                    .map(|byte| CoreValue::int(i64::from(*byte), &budget)),
                &budget,
            )
            .unwrap();
            assert_eq!(
                array.encode_guest(&budget).unwrap().as_bytes(),
                old.encode_guest(&budget).unwrap().as_bytes()
            );
            let CoreRef::Arr(items) = array.inspect() else {
                panic!("integer array required")
            };
            assert_eq!(items.len(), length);
            for (item, expected) in items.iter().zip(&bytes) {
                assert!(
                    matches!(item.inspect(), CoreRef::Int(value) if value == i64::from(*expected))
                );
            }
            drop(old);
            let alias = array.clone();
            let retained = counted.usage.lock().unwrap().live;
            assert!(retained > 0);
            drop(array);
            assert_eq!(counted.usage.lock().unwrap().live, retained);
            drop(alias);
            assert_eq!(counted.usage.lock().unwrap().live, 0);
        }
    }

    #[test]
    fn u8_array_precharge_denial_and_shared_block_failure_release_everything() {
        for deny_call in [1usize, 2] {
            let counted = Budget::new(u64::MAX, u64::MAX, Some(deny_call));
            let budget: Arc<dyn AllocationBudget> = counted.clone();
            assert!(matches!(
                CoreValue::u8_array(&[0, 1, 255], &budget),
                Err(AllocationError::Limit)
            ));
            let usage = counted.usage.lock().unwrap();
            assert_eq!(usage.calls, deny_call);
            assert_eq!(usage.live, 0);
            if deny_call == 1 {
                assert_eq!(usage.peak, 0);
            }
        }
    }

    #[test]
    fn checked_kv_conversion_bound_covers_scalar_temporaries_and_strict_encoding() {
        for length in [0usize, 1, 256, 6144] {
            let key = "\u{1}\\\"\n日本語".repeat(40);
            let bound = CoreValue::kv_put_conversion_bound(key.len(), length).unwrap();
            let counted = Budget::new(bound.allocation.bytes, bound.allocation.work, None);
            let budget: Arc<dyn AllocationBudget> = counted.clone();
            budget
                .reserve(AllocationRequest {
                    bytes: 0,
                    elements: 0,
                    work: bound.validation_work,
                    ..bound.allocation
                })
                .unwrap();
            let bytes = CoreValue::array_from(
                (0..length).map(|byte| CoreValue::int(i64::from(byte as u8), &budget)),
                &budget,
            )
            .unwrap();
            let record = CoreValue::object_shared(
                [
                    Ok(("op", CoreValue::string("kv-put", &budget).unwrap())),
                    Ok(("key", CoreValue::string(&key, &budget).unwrap())),
                    Ok(("value", bytes)),
                ]
                .into_iter(),
                &budget,
            )
            .unwrap();
            let encoded = record.encode_guest(&budget).unwrap();
            let _digest = Digest::hash(encoded.as_bytes());
            let usage = counted.usage.lock().unwrap();
            assert!(usage.peak <= bound.allocation.bytes);
            assert!(usage.work <= bound.allocation.work);
            drop(usage);
            drop(encoded);
            drop(record);
            assert_eq!(counted.usage.lock().unwrap().live, 0);
        }
        assert!(matches!(
            CoreValue::kv_put_conversion_bound(usize::MAX, usize::MAX),
            Err(AllocationError::Overflow)
        ));
    }

    #[test]
    fn network_digest_bound_checks_shape_overflow_and_all_body_temporaries() {
        let base = NetworkDigestLengths {
            headers: 2,
            secret_headers: 1,
            secret_names: 2,
            method_url_bytes: 40,
            header_bytes: 80,
            secret_header_bytes: 22,
            secret_name_bytes: 30,
            ..NetworkDigestLengths::default()
        };
        let empty = CoreValue::network_digest_conversion_bound(base).unwrap();
        for count in [1, 1024, 6144] {
            let bound = CoreValue::network_digest_conversion_bound(NetworkDigestLengths {
                body_bytes: count,
                ..base
            })
            .unwrap();
            assert_eq!(
                bound.bytes - empty.bytes,
                count as u64 * (Shared::<Node>::storage_bytes() + 2 * size_of::<Node>()) as u64
            );
            // Eight units include the paid u8 validator; hash bound adds 8*N.
            assert_eq!(bound.work - empty.work, count as u64 * 16);
            assert_eq!(bound.depth, 4);
        }
        for invalid in [
            NetworkDigestLengths {
                headers: 128,
                secret_headers: 1,
                ..base
            },
            NetworkDigestLengths {
                secret_names: 33,
                ..base
            },
        ] {
            assert_eq!(
                CoreValue::network_digest_conversion_bound(invalid),
                Err(AllocationError::Invalid)
            );
        }
        for overflow in [
            NetworkDigestLengths {
                body_bytes: usize::MAX,
                ..base
            },
            NetworkDigestLengths {
                method_url_bytes: usize::MAX,
                ..base
            },
            NetworkDigestLengths {
                header_bytes: usize::MAX,
                ..base
            },
            NetworkDigestLengths {
                secret_header_bytes: usize::MAX,
                ..base
            },
            NetworkDigestLengths {
                secret_name_bytes: usize::MAX,
                ..base
            },
        ] {
            assert_eq!(
                CoreValue::network_digest_conversion_bound(overflow),
                Err(AllocationError::Overflow)
            );
        }
    }
}
