//! Tobkiri's pinned dynamic canonical ABI allocation ownership.
//! Static/custom lifting is unsupported until its complete allocation paths are audited.
use alloc::alloc::{Layout, alloc, dealloc};
use alloc::string::String;
use alloc::sync::Arc;
use alloc::vec::Vec;
use core::mem::ManuallyDrop;
use core::ptr::NonNull;
use core::sync::atomic::{AtomicUsize, Ordering, fence};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
/// Classification of a pre-allocation reservation or bounded work request.
pub enum HostAllocationKind {
    /// String payload or encoding conversion.
    String,
    /// List, tuple or map storage.
    Sequence,
    /// Record entries and copied names.
    Record,
    /// Variant or result payload ownership.
    Variant,
    /// Flag names or temporary bit storage.
    Flags,
    /// Private conversion scratch storage.
    Temporary,
    /// Resource table or call metadata.
    Resource,
    /// Canonical allocation peak/work check.
    Realloc,
    /// Owned value control block.
    Metadata,
    /// Distinct physical deep-copy storage.
    Clone,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
/// Checked storage, element, depth and work requirements before a guest conversion.
pub struct HostAllocationRequest {
    /// Allocation or work category; this grants no host permission.
    pub kind: HostAllocationKind,
    /// New physical layout bytes reserved while old storage is still charged.
    pub bytes: u64,
    /// Number of elements checked against the owner's cumulative work budget.
    pub elements: u64,
    /// Actual conversion nesting depth, at most 64.
    pub depth: u32,
    /// Checked conversion/copy work charged before the operation.
    pub work: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
/// Fallible allocation or conversion denial without an unbudgeted fallback.
pub enum HostAllocationError {
    /// Owner or allocation path has no supported ownership proof.
    Unavailable,
    /// Owner storage, element or work cap refused the request.
    Limit,
    /// Checked layout, length or work arithmetic overflowed.
    Overflow,
    /// Actual conversion depth exceeds the supported bound.
    Depth,
    /// A fallible physical allocation failed.
    Allocator,
    /// Malformed input or an invalid fixed-capacity operation.
    Invalid,
}
impl core::fmt::Display for HostAllocationError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(match self {
            Self::Unavailable => "canonical host allocation unavailable",
            Self::Limit => "canonical host allocation limit exceeded",
            Self::Overflow => "canonical host allocation overflow",
            Self::Depth => "canonical host allocation depth exceeded",
            Self::Allocator => "canonical host allocation failed",
            Self::Invalid => "invalid canonical host allocation",
        })
    }
}
impl core::error::Error for HostAllocationError {}
/// Implementations retain the stable Profile+Pack owner and minimum live caps.
/// Their bookkeeping/control blocks must be bounded and prepaid independently.
pub trait HostAllocationBudget: Send + Sync {
    /// Atomically reserves storage and bounded work before any physical allocation.
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError>;
    /// Releases only storage whose payload and allocation have already been freed.
    fn release(&self, bytes: u64);
}
/// Trusted stable owner adapter; its Arc control block requires external funded custody.
pub type HostAllocationOwner = Arc<dyn HostAllocationBudget>;
pub(crate) struct Lease {
    owner: HostAllocationOwner,
    bytes: u64,
}
impl core::fmt::Debug for Lease {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("AllocationLease")
            .field("bytes", &self.bytes)
            .finish()
    }
}
impl Lease {
    pub(crate) fn new(owner: &HostAllocationOwner) -> Self {
        Self {
            owner: owner.clone(),
            bytes: 0,
        }
    }
    pub(crate) fn charge(
        &mut self,
        kind: HostAllocationKind,
        bytes: usize,
        elements: usize,
        depth: usize,
        work: usize,
    ) -> Result<(), HostAllocationError> {
        if depth > 64 {
            return Err(HostAllocationError::Depth);
        }
        let bytes = u64::try_from(bytes).map_err(|_| HostAllocationError::Overflow)?;
        let total = self
            .bytes
            .checked_add(bytes)
            .ok_or(HostAllocationError::Overflow)?;
        self.owner.reserve(HostAllocationRequest {
            kind,
            bytes,
            elements: u64::try_from(elements).map_err(|_| HostAllocationError::Overflow)?,
            depth: u32::try_from(depth).map_err(|_| HostAllocationError::Overflow)?,
            work: u64::try_from(work).map_err(|_| HostAllocationError::Overflow)?,
        })?;
        self.bytes = total;
        Ok(())
    }
}
impl Drop for Lease {
    fn drop(&mut self) {
        self.owner.release(self.bytes);
    }
}
struct Block<T> {
    count: AtomicUsize,
    value: ManuallyDrop<T>,
    lease: ManuallyDrop<Lease>,
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
pub(crate) struct Shared<T> {
    ptr: NonNull<Block<T>>,
}
impl<T> Shared<T> {
    pub(crate) fn new(value: T, mut lease: Lease) -> Result<Self, HostAllocationError> {
        let layout = Layout::new::<Block<T>>();
        if let Err(error) = lease.charge(HostAllocationKind::Metadata, layout.size(), 1, 0, 1) {
            drop(value);
            drop(lease);
            return Err(error);
        }
        let raw = unsafe { alloc(layout) }.cast::<Block<T>>();
        let Some(ptr) = NonNull::new(raw) else {
            drop(value);
            drop(lease);
            return Err(HostAllocationError::Allocator);
        };
        unsafe {
            ptr.as_ptr().write(Block {
                count: AtomicUsize::new(1),
                value: ManuallyDrop::new(value),
                lease: ManuallyDrop::new(lease),
            });
        }
        Ok(Self { ptr })
    }
    pub(crate) fn value(&self) -> &T {
        unsafe { &self.ptr.as_ref().value }
    }
    fn unique_mut(&mut self) -> Result<&mut T, HostAllocationError> {
        if unsafe { self.ptr.as_ref().count.load(Ordering::Acquire) } != 1 {
            return Err(HostAllocationError::Unavailable);
        }
        // Only runtime scratch storage calls this; no shared references survive a mutable borrow.
        Ok(unsafe { &mut self.ptr.as_mut().value })
    }
}
impl<T> Clone for Shared<T> {
    fn clone(&self) -> Self {
        let old = unsafe { self.ptr.as_ref().count.fetch_add(1, Ordering::Relaxed) };
        assert!(
            old <= isize::MAX as usize,
            "budgeted shared owner reference overflow"
        );
        Self { ptr: self.ptr }
    }
}
impl<T> Drop for Shared<T> {
    fn drop(&mut self) {
        if unsafe { self.ptr.as_ref().count.fetch_sub(1, Ordering::Release) } != 1 {
            return;
        }
        fence(Ordering::Acquire);
        unsafe {
            let block = &mut *self.ptr.as_ptr();
            let lease = ManuallyDrop::take(&mut block.lease);
            let free = FreeBlock {
                ptr: self.ptr.cast(),
                layout: Layout::new::<Block<T>>(),
            };
            ManuallyDrop::drop(&mut block.value);
            drop(free);
            drop(lease);
        }
    }
}
unsafe impl<T: Send + Sync> Send for Shared<T> {}
unsafe impl<T: Send + Sync> Sync for Shared<T> {}

/// Immutable owned UTF-8. Clone shares already charged storage, never deep-copies.
#[derive(Clone)]
pub struct BudgetedString {
    value: Shared<String>,
}
impl BudgetedString {
    /// Copies borrowed UTF-8 into new owner-funded immutable storage.
    pub fn copy_from(
        value: &str,
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError> {
        let mut lease = Lease::new(owner);
        let mut bytes =
            reserved_vec::<u8>(value.len(), &mut lease, HostAllocationKind::String, depth)?;
        bytes.extend_from_slice(value.as_bytes());
        let value = unsafe { String::from_utf8_unchecked(bytes) };
        Ok(Self {
            value: Shared::new(value, lease)?,
        })
    }
    /// Decodes little-endian UTF-16 after funding the complete worst-case output.
    pub fn copy_utf16(
        bytes: &[u8],
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError> {
        if bytes.len() % 2 != 0 {
            return Err(HostAllocationError::Invalid);
        }
        let capacity = (bytes.len() / 2)
            .checked_mul(3)
            .ok_or(HostAllocationError::Overflow)?;
        let mut lease = Lease::new(owner);
        let mut output =
            reserved_vec::<u8>(capacity, &mut lease, HostAllocationKind::String, depth)?;
        for ch in core::char::decode_utf16(
            bytes
                .chunks_exact(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]])),
        ) {
            let ch = ch.map_err(|_| HostAllocationError::Invalid)?;
            let mut tmp = [0; 4];
            output.extend_from_slice(ch.encode_utf8(&mut tmp).as_bytes());
        }
        let value = unsafe { String::from_utf8_unchecked(output) };
        Ok(Self {
            value: Shared::new(value, lease)?,
        })
    }
    /// Decodes Latin-1 after funding the complete worst-case UTF-8 output.
    pub fn copy_latin1(
        bytes: &[u8],
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError> {
        let capacity = bytes
            .len()
            .checked_mul(2)
            .ok_or(HostAllocationError::Overflow)?;
        let mut lease = Lease::new(owner);
        let mut output =
            reserved_vec::<u8>(capacity, &mut lease, HostAllocationKind::String, depth)?;
        for &byte in bytes {
            let mut tmp = [0; 4];
            output.extend_from_slice(char::from(byte).encode_utf8(&mut tmp).as_bytes());
        }
        let value = unsafe { String::from_utf8_unchecked(output) };
        Ok(Self {
            value: Shared::new(value, lease)?,
        })
    }
    /// Borrows UTF-8 while its physical allocation remains owned and charged.
    pub fn as_str(&self) -> &str {
        self.value.value()
    }
    /// Reserves a distinct physical destination copy without altering source references.
    pub fn try_copy_to(
        &self,
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError> {
        Self::copy_from(self.as_str(), owner, depth)
    }
}
impl core::ops::Deref for BudgetedString {
    type Target = str;
    fn deref(&self) -> &str {
        self.as_str()
    }
}
impl AsRef<str> for BudgetedString {
    fn as_ref(&self) -> &str {
        self.as_str()
    }
}
impl core::fmt::Debug for BudgetedString {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("BudgetedString")
            .field("bytes", &self.len())
            .finish()
    }
}
impl core::fmt::Display for BudgetedString {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str(self.as_str())
    }
}
impl PartialEq for BudgetedString {
    fn eq(&self, other: &Self) -> bool {
        self.as_str() == other.as_str()
    }
}
impl Eq for BudgetedString {}
mod sealed {
    pub trait Sealed {}
}
/// Sealed element types have allocation-free shared cloning and no plain owned payload.
pub trait BudgetedElement: sealed::Sealed + Clone {}
impl sealed::Sealed for BudgetedString {}
impl BudgetedElement for BudgetedString {}
impl sealed::Sealed for super::Val {}
impl BudgetedElement for super::Val {}
impl sealed::Sealed for u8 {}
impl BudgetedElement for u8 {}
impl sealed::Sealed for u32 {}
impl BudgetedElement for u32 {}
impl<A: BudgetedElement, B: BudgetedElement> sealed::Sealed for (A, B) {}
impl<A: BudgetedElement, B: BudgetedElement> BudgetedElement for (A, B) {}
/// Immutable owned container. Only private runtime scratch can mutate unique slots.
pub struct BudgetedVec<T> {
    value: Shared<Vec<T>>,
}
impl<T> Clone for BudgetedVec<T> {
    fn clone(&self) -> Self {
        Self {
            value: self.value.clone(),
        }
    }
}
impl<T: BudgetedElement> BudgetedVec<T> {
    /// Builds fixed-capacity storage before consuming an exact-size owned-value iterator.
    pub fn from_iter<I>(
        values: I,
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError>
    where
        I: ExactSizeIterator<Item = T>,
    {
        let mut builder = BudgetedVecBuilder::new(values.len(), owner, depth)?;
        for value in values {
            builder.push(value)?;
        }
        builder.finish()
    }
    /// Borrows immutable elements without detaching their allocation owner.
    pub fn as_slice(&self) -> &[T] {
        self.value.value()
    }
    pub(crate) fn scratch_mut(&mut self) -> Result<&mut [T], HostAllocationError> {
        Ok(self.value.unique_mut()?.as_mut_slice())
    }
    pub(crate) fn scratch_push(&mut self, value: T) -> Result<(), HostAllocationError> {
        let values = self.value.unique_mut()?;
        if values.len() == values.capacity() {
            return Err(HostAllocationError::Unavailable);
        }
        values.push(value);
        Ok(())
    }
}
impl<T> core::ops::Deref for BudgetedVec<T> {
    type Target = [T];
    fn deref(&self) -> &[T] {
        self.value.value()
    }
}
impl<T: PartialEq> PartialEq for BudgetedVec<T> {
    fn eq(&self, other: &Self) -> bool {
        self.value.value() == other.value.value()
    }
}
impl<T: Eq> Eq for BudgetedVec<T> {}
impl<T: core::fmt::Debug> core::fmt::Debug for BudgetedVec<T> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("BudgetedVec")
            .field("elements", &self.len())
            .finish()
    }
}
/// Fallible fixed-capacity builder which never reallocates or exposes owned storage.
pub struct BudgetedVecBuilder<T> {
    values: Vec<T>,
    lease: Lease,
    capacity: usize,
}
impl<T: BudgetedElement> BudgetedVecBuilder<T> {
    /// Borrows completed slots while the builder retains their storage lease.
    /// Searching a guest-sized prefix must separately charge its bounded work.
    pub fn as_slice(&self) -> &[T] {
        &self.values
    }
    /// Reserves this fixed allocation before creating any owned storage.
    pub fn new(
        capacity: usize,
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError> {
        let mut lease = Lease::new(owner);
        let values = reserved_vec::<T>(capacity, &mut lease, HostAllocationKind::Sequence, depth)?;
        Ok(Self {
            values,
            lease,
            capacity,
        })
    }
    /// Moves an already owned value into a prepaid slot; over-capacity insertion fails.
    pub fn push(&mut self, value: T) -> Result<(), HostAllocationError> {
        if self.values.len() == self.capacity {
            return Err(HostAllocationError::Invalid);
        }
        self.values.push(value);
        Ok(())
    }
    /// Allocates the prepaid shared control block and returns sealed immutable storage.
    pub fn finish(self) -> Result<BudgetedVec<T>, HostAllocationError> {
        Ok(BudgetedVec {
            value: Shared::new(self.values, self.lease)?,
        })
    }
}
/// Immutable boxed value with a prepaid owner/control block.
pub struct BudgetedBox<T> {
    value: Shared<T>,
}
impl<T> Clone for BudgetedBox<T> {
    fn clone(&self) -> Self {
        Self {
            value: self.value.clone(),
        }
    }
}
impl<T: BudgetedElement> BudgetedBox<T> {
    /// Reserves this fixed allocation before creating any owned storage.
    pub fn new(
        value: T,
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError> {
        let mut lease = Lease::new(owner);
        lease.charge(HostAllocationKind::Variant, 0, 1, depth, 1)?;
        Ok(Self {
            value: Shared::new(value, lease)?,
        })
    }
}
impl<T> core::ops::Deref for BudgetedBox<T> {
    type Target = T;
    fn deref(&self) -> &T {
        self.value.value()
    }
}
impl<T> AsRef<T> for BudgetedBox<T> {
    fn as_ref(&self) -> &T {
        self.value.value()
    }
}
impl<T: PartialEq> PartialEq for BudgetedBox<T> {
    fn eq(&self, other: &Self) -> bool {
        self.value.value() == other.value.value()
    }
}
impl<T: Eq> Eq for BudgetedBox<T> {}
impl<T: core::fmt::Debug> core::fmt::Debug for BudgetedBox<T> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.write_str("BudgetedBox(immutable owned value)")
    }
}
fn reserved_vec<T>(
    len: usize,
    lease: &mut Lease,
    kind: HostAllocationKind,
    depth: usize,
) -> Result<Vec<T>, HostAllocationError> {
    let bytes = len
        .checked_mul(size_of::<T>())
        .ok_or(HostAllocationError::Overflow)?;
    if bytes > isize::MAX as usize {
        return Err(HostAllocationError::Overflow);
    }
    lease.charge(kind, bytes, len, depth, len)?;
    let mut values = Vec::new();
    values
        .try_reserve_exact(len)
        .map_err(|_| HostAllocationError::Allocator)?;
    if values.capacity() != len && size_of::<T>() != 0 {
        return Err(HostAllocationError::Unavailable);
    }
    Ok(values)
}

/// Private mutable runtime metadata. Payload storage is declared before its
/// inline lease so physical deallocation precedes reservation release.
pub(crate) struct FundedVec<T> {
    values: Vec<T>,
    lease: Option<Lease>,
}
impl<T> Default for FundedVec<T> {
    fn default() -> Self {
        Self {
            values: Vec::new(),
            lease: None,
        }
    }
}
impl<T> FundedVec<T> {
    fn reserve_one(&mut self, owner: &HostAllocationOwner) -> Result<(), HostAllocationError> {
        let needed = self
            .values
            .len()
            .checked_add(1)
            .ok_or(HostAllocationError::Overflow)?;
        if needed > self.values.capacity() {
            let capacity = self
                .values
                .capacity()
                .checked_mul(2)
                .ok_or(HostAllocationError::Overflow)?
                .max(needed);
            let bytes = capacity
                .checked_mul(size_of::<T>())
                .ok_or(HostAllocationError::Overflow)?;
            if bytes > isize::MAX as usize {
                return Err(HostAllocationError::Overflow);
            }
            let mut replacement = Lease::new(owner);
            // Destination storage is funded while the old lease is still live.
            replacement.charge(HostAllocationKind::Resource, bytes, capacity, 0, capacity)?;
            let mut destination = Vec::new();
            destination
                .try_reserve_exact(capacity)
                .map_err(|_| HostAllocationError::Allocator)?;
            if destination.capacity() != capacity && size_of::<T>() != 0 {
                return Err(HostAllocationError::Unavailable);
            }
            // The fully funded destination has room for every existing element;
            // append moves them without copying payloads or allocating storage.
            destination.append(&mut self.values);
            let old_storage = core::mem::replace(&mut self.values, destination);
            drop(old_storage);
            // Physical old storage is freed before its reservation is released.
            let previous = self.lease.replace(replacement);
            drop(previous);
        } else {
            owner.reserve(HostAllocationRequest {
                kind: HostAllocationKind::Resource,
                bytes: 0,
                elements: 1,
                depth: 0,
                work: 1,
            })?;
        }
        Ok(())
    }
    pub(crate) fn push(
        &mut self,
        value: T,
        owner: &HostAllocationOwner,
    ) -> Result<(), HostAllocationError> {
        self.reserve_one(owner)?;
        self.values.push(value);
        Ok(())
    }
    pub(crate) fn insert(
        &mut self,
        index: usize,
        value: T,
        owner: &HostAllocationOwner,
    ) -> Result<(), HostAllocationError> {
        if index > self.values.len() {
            return Err(HostAllocationError::Invalid);
        }
        owner.reserve(HostAllocationRequest {
            kind: HostAllocationKind::Resource,
            bytes: 0,
            elements: self.values.len() as u64,
            depth: 0,
            work: self.values.len() as u64,
        })?;
        self.reserve_one(owner)?;
        self.values.insert(index, value);
        Ok(())
    }
    pub(crate) fn remove(&mut self, index: usize) -> T {
        self.values.remove(index)
    }
    pub(crate) fn pop(&mut self) -> Option<T> {
        self.values.pop()
    }
}
impl<T> core::fmt::Debug for FundedVec<T> {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("FundedVec")
            .field("elements", &self.values.len())
            .finish()
    }
}
impl<T> core::ops::Deref for FundedVec<T> {
    type Target = [T];
    fn deref(&self) -> &[T] {
        &self.values
    }
}
impl<T> core::ops::DerefMut for FundedVec<T> {
    fn deref_mut(&mut self) -> &mut [T] {
        &mut self.values
    }
}
pub(crate) struct FundedIntoIter<T> {
    values: alloc::vec::IntoIter<T>,
    _lease: Option<Lease>,
}
impl<T> Iterator for FundedIntoIter<T> {
    type Item = T;
    fn next(&mut self) -> Option<T> {
        self.values.next()
    }
}
impl<T> IntoIterator for FundedVec<T> {
    type Item = T;
    type IntoIter = FundedIntoIter<T>;
    fn into_iter(self) -> Self::IntoIter {
        FundedIntoIter {
            values: self.values.into_iter(),
            _lease: self.lease,
        }
    }
}

#[cfg(feature = "async")]
struct FutureBlock<F> {
    future: ManuallyDrop<F>,
    lease: ManuallyDrop<Lease>,
}
/// Fallibly allocated async state which retains its physical allocation owner.
/// This future has no Clone or owned state extraction. Cancellation destroys
/// captured payload and deallocates the block before releasing its reservation.
#[cfg(feature = "async")]
pub struct BudgetedFuture<'a, T> {
    ptr: NonNull<()>,
    poll: unsafe fn(NonNull<()>, &mut core::task::Context<'_>) -> core::task::Poll<T>,
    destroy: unsafe fn(NonNull<()>),
    _lifetime: core::marker::PhantomData<&'a mut ()>,
}
#[cfg(feature = "async")]
impl<'a, T> BudgetedFuture<'a, T> {
    /// Reserves exact async state/control storage before physical allocation.
    /// Production callback outputs must themselves retain any payload ownership.
    pub fn new<F>(
        future: F,
        owner: &HostAllocationOwner,
        depth: usize,
    ) -> Result<Self, HostAllocationError>
    where
        F: core::future::Future<Output = T> + Send + 'a,
    {
        let layout = Layout::new::<FutureBlock<F>>();
        let mut lease = Lease::new(owner);
        if let Err(error) = lease.charge(HostAllocationKind::Metadata, layout.size(), 1, depth, 1) {
            drop(future);
            drop(lease);
            return Err(error);
        }
        let Some(ptr) = NonNull::new(unsafe { alloc(layout) }.cast::<FutureBlock<F>>()) else {
            drop(future);
            drop(lease);
            return Err(HostAllocationError::Allocator);
        };
        unsafe {
            ptr.as_ptr().write(FutureBlock {
                future: ManuallyDrop::new(future),
                lease: ManuallyDrop::new(lease),
            });
        }
        unsafe fn poll<F: core::future::Future>(
            ptr: NonNull<()>,
            cx: &mut core::task::Context<'_>,
        ) -> core::task::Poll<F::Output> {
            let block = unsafe { &mut *ptr.cast::<FutureBlock<F>>().as_ptr() };
            // The heap block is stable from first poll through cancellation/drop.
            unsafe { core::pin::Pin::new_unchecked(&mut *block.future) }.poll(cx)
        }
        unsafe fn destroy<F>(ptr: NonNull<()>) {
            let ptr = ptr.cast::<FutureBlock<F>>();
            let block = unsafe { &mut *ptr.as_ptr() };
            let lease = unsafe { ManuallyDrop::take(&mut block.lease) };
            let free = FreeBlock {
                ptr: ptr.cast(),
                layout: Layout::new::<FutureBlock<F>>(),
            };
            unsafe {
                ManuallyDrop::drop(&mut block.future);
            }
            drop(free);
            drop(lease);
        }
        Ok(Self {
            ptr: ptr.cast(),
            poll: poll::<F>,
            destroy: destroy::<F>,
            _lifetime: core::marker::PhantomData,
        })
    }
}
#[cfg(feature = "async")]
impl<T> core::future::Future for BudgetedFuture<'_, T> {
    type Output = T;
    fn poll(
        self: core::pin::Pin<&mut Self>,
        cx: &mut core::task::Context<'_>,
    ) -> core::task::Poll<T> {
        let this = self.get_mut();
        unsafe { (this.poll)(this.ptr, cx) }
    }
}
#[cfg(feature = "async")]
impl<T> Drop for BudgetedFuture<'_, T> {
    fn drop(&mut self) {
        unsafe {
            (self.destroy)(self.ptr);
        }
    }
}
// The constructor requires the captured concrete future to be Send. Its
// storage remains pinned even when this owning handle is moved between threads.
#[cfg(feature = "async")]
unsafe impl<T: Send> Send for BudgetedFuture<'_, T> {}
