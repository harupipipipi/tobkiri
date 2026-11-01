use super::Resource;
use crate::component::{
    FundedVec, HostAllocationError, HostAllocationKind, HostAllocationLease, HostAllocationOwner,
};
use crate::prelude::*;
use alloc::collections::BTreeMap;
use core::any::Any;
use core::fmt;
use core::ops::{Deref, DerefMut};

#[derive(Debug)]
/// Errors returned by operations on `ResourceTable`
pub enum ResourceTableError {
    /// ResourceTable has no free keys
    Full,
    /// This table has no supported allocation owner or reservation failed.
    Allocation(HostAllocationError),
    /// Resource not present in table
    NotPresent,
    /// Resource present in table, but with a different type
    WrongType,
    /// Resource cannot be deleted because child resources exist in the table. Consult wit docs for
    /// the particular resource to see which methods may return child resources.
    HasChildren,
}

impl fmt::Display for ResourceTableError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Full => write!(f, "resource table has no free keys"),
            Self::Allocation(error) => fmt::Display::fmt(error, f),
            Self::NotPresent => write!(f, "resource not present"),
            Self::WrongType => write!(f, "resource is of another type"),
            Self::HasChildren => write!(f, "resource has children"),
        }
    }
}

impl core::error::Error for ResourceTableError {}

/// The `ResourceTable` type maps a `Resource<T>` to its `T`.
pub struct ResourceTable {
    entries: Entries,
    allocation_owner: Option<HostAllocationOwner>,
    free_head: Option<usize>,
    max_capacity: usize,
}

enum Entries {
    Native(Vec<Entry>),
    Owned(FundedVec<Entry>),
}

impl Deref for Entries {
    type Target = [Entry];

    fn deref(&self) -> &Self::Target {
        match self {
            Self::Native(entries) => entries,
            Self::Owned(entries) => entries,
        }
    }
}

impl DerefMut for Entries {
    fn deref_mut(&mut self) -> &mut Self::Target {
        match self {
            Self::Native(entries) => entries,
            Self::Owned(entries) => entries,
        }
    }
}

impl Entries {
    fn push(
        &mut self,
        entry: Entry,
        owner: Option<&HostAllocationOwner>,
    ) -> Result<(), ResourceTableError> {
        match self {
            Self::Native(entries) => {
                entries
                    .try_reserve(1)
                    .map_err(|_| ResourceTableError::Allocation(HostAllocationError::Allocator))?;
                entries.push(entry);
            }
            Self::Owned(entries) => {
                let owner = owner.ok_or(ResourceTableError::Allocation(
                    HostAllocationError::Unavailable,
                ))?;
                entries
                    .push(entry, owner)
                    .map_err(ResourceTableError::Allocation)?;
            }
        }
        Ok(())
    }
}

#[derive(Debug)]
enum Entry {
    Free { next: Option<usize> },
    Occupied { entry: TableEntry },
}

impl Entry {
    pub fn occupied(&self) -> Option<&TableEntry> {
        match self {
            Self::Occupied { entry } => Some(entry),
            Self::Free { .. } => None,
        }
    }

    pub fn occupied_mut(&mut self) -> Option<&mut TableEntry> {
        match self {
            Self::Occupied { entry } => Some(entry),
            Self::Free { .. } => None,
        }
    }
}

// Change this to `true` to assist with handle debugging in development if
// necessary.
const DELETE_WITH_TOMBSTONE: bool = false;

/// Default setting for `ResourceTable::max_capacity`, chosen to be high
/// enough that it doesn't need changing all that often but low enough that
/// exhausting it isn't a massive problem for the host.
const DEFAULT_MAX_CAPACITY: usize = 1_000_000;

/// This structure tracks parent and child relationships for a given table entry.
///
/// Parents and children are referred to by table index. We maintain the
/// following invariants:
/// * the parent must exist when adding a child.
/// * whenever a child is created, its index is added to children.
/// * whenever a child is deleted, its index is removed from children.
/// * an entry with children may not be deleted.
#[derive(Debug)]
struct TableEntry {
    /// The entry in the table, as a boxed dynamically-typed object
    entry: Box<dyn Any + Send>,
    /// The index of the parent of this entry, if it has one.
    parent: Option<u32>,
    /// The indices of any children of this entry.
    children: Children,
    _lease: Option<HostAllocationLease>,
}

#[derive(Debug)]
enum Children {
    Native(Vec<u32>),
    Owned(FundedVec<u32>),
}

impl Deref for Children {
    type Target = [u32];

    fn deref(&self) -> &Self::Target {
        match self {
            Self::Native(children) => children,
            Self::Owned(children) => children,
        }
    }
}

impl TableEntry {
    fn add_child(
        &mut self,
        child: u32,
        owner: Option<&HostAllocationOwner>,
    ) -> Result<(), ResourceTableError> {
        let at = self
            .children
            .binary_search(&child)
            .err()
            .ok_or(ResourceTableError::Allocation(HostAllocationError::Invalid))?;
        match &mut self.children {
            Children::Native(children) => {
                children
                    .try_reserve(1)
                    .map_err(|_| ResourceTableError::Allocation(HostAllocationError::Allocator))?;
                children.insert(at, child);
            }
            Children::Owned(children) => {
                let owner = owner.ok_or(ResourceTableError::Allocation(
                    HostAllocationError::Unavailable,
                ))?;
                children
                    .insert(at, child, owner)
                    .map_err(ResourceTableError::Allocation)?;
            }
        }
        Ok(())
    }
    fn remove_child(&mut self, child: u32) {
        let at = self
            .children
            .binary_search(&child)
            .expect("existing resource child");
        match &mut self.children {
            Children::Native(children) => {
                children.remove(at);
            }
            Children::Owned(children) => {
                children.remove(at);
            }
        }
    }
}

impl TableEntry {
    fn new<T: Send + 'static>(
        entry: T,
        parent: Option<u32>,
        owner: Option<&HostAllocationOwner>,
    ) -> Result<Self, ResourceTableError> {
        let lease = if let Some(owner) = owner {
            let mut lease = HostAllocationLease::new(owner);
            lease
                .charge(HostAllocationKind::Resource, size_of::<T>(), 1, 0, 1)
                .map_err(ResourceTableError::Allocation)?;
            Some(lease)
        } else {
            None
        };
        let entry = try_new::<Box<_>>(entry)
            .map_err(|_| ResourceTableError::Allocation(HostAllocationError::Allocator))?;
        Ok(Self {
            entry,
            parent,
            children: if owner.is_some() {
                Children::Owned(FundedVec::default())
            } else {
                Children::Native(Vec::new())
            },
            _lease: lease,
        })
    }
}

impl ResourceTable {
    /// Creates a table whose boxed data, entries and child metadata are funded.
    /// Host payload inside T must independently retain its production ownership.
    pub fn new_owned(owner: &HostAllocationOwner) -> Self {
        Self {
            entries: Entries::Owned(FundedVec::default()),
            allocation_owner: Some(owner.clone()),
            free_head: None,
            max_capacity: DEFAULT_MAX_CAPACITY,
        }
    }

    /// Create an empty table
    pub fn new() -> Self {
        ResourceTable::with_capacity(0)
    }

    /// Returns whether or not this table is empty.
    ///
    /// Note that this is an `O(n)` operation, where `n` is the number of
    /// entries in the backing `Vec`.
    pub fn is_empty(&self) -> bool {
        self.entries
            .iter()
            .all(|entry| matches!(entry, Entry::Free { .. }))
    }

    /// Returns the maximum capacity of this table, in elements, before adding
    /// any more will be refused.
    pub fn max_capacity(&self) -> usize {
        self.max_capacity
    }

    /// Configures the maximum number of entries that may be present within this
    /// table.
    ///
    /// Note that this does not retroactively shrink the table nor evict
    /// existing entries should the maximum be smaller than the current size of
    /// the entry table.
    pub fn set_max_capacity(&mut self, max: usize) {
        self.max_capacity = max;
    }

    /// Create an empty table with at least the specified capacity.
    pub fn with_capacity(capacity: usize) -> Self {
        ResourceTable {
            entries: Entries::Native(Vec::with_capacity(capacity)),
            allocation_owner: None,
            free_head: None,
            max_capacity: DEFAULT_MAX_CAPACITY,
        }
    }

    /// Inserts a new value `T` into this table, returning a corresponding
    /// `Resource<T>` which can be used to refer to it after it was inserted.
    pub fn push<T>(&mut self, entry: T) -> Result<Resource<T>, ResourceTableError>
    where
        T: Send + 'static,
    {
        let data = TableEntry::new(entry, None, self.allocation_owner.as_ref())?;
        let idx = self.push_(data)?;
        Ok(Resource::new_own(idx))
    }

    /// Pop an index off of the free list, if it's not empty.
    fn pop_free_list(&mut self) -> Option<usize> {
        if let Some(ix) = self.free_head {
            // Advance free_head to the next entry if one is available.
            match &self.entries[ix] {
                Entry::Free { next } => self.free_head = *next,
                Entry::Occupied { .. } => unreachable!(),
            }
            Some(ix)
        } else {
            None
        }
    }

    /// Free an entry in the table, returning its [`TableEntry`]. Add the index to the free list.
    fn free_entry(&mut self, ix: usize) -> TableEntry {
        let entry = match core::mem::replace(
            &mut self.entries[ix],
            Entry::Free {
                next: self.free_head,
            },
        ) {
            Entry::Occupied { entry } => entry,
            Entry::Free { .. } => unreachable!(),
        };

        self.free_head = Some(ix);

        entry
    }

    /// Push a new entry into the table, returning its handle. This will prefer to use free entries
    /// if they exist, falling back on pushing new entries onto the end of the table.
    fn push_(&mut self, e: TableEntry) -> Result<u32, ResourceTableError> {
        if let Some(free) = self.pop_free_list() {
            self.entries[free] = Entry::Occupied { entry: e };
            Ok(free.try_into().unwrap())
        } else {
            if self.entries.len() >= self.max_capacity {
                return Err(ResourceTableError::Full);
            }
            let ix = self
                .entries
                .len()
                .try_into()
                .map_err(|_| ResourceTableError::Full)?;
            self.entries
                .push(Entry::Occupied { entry: e }, self.allocation_owner.as_ref())?;
            Ok(ix)
        }
    }

    fn occupied(&self, key: u32) -> Result<&TableEntry, ResourceTableError> {
        self.entries
            .get(key as usize)
            .and_then(Entry::occupied)
            .ok_or(ResourceTableError::NotPresent)
    }

    fn occupied_mut(&mut self, key: u32) -> Result<&mut TableEntry, ResourceTableError> {
        self.entries
            .get_mut(key as usize)
            .and_then(Entry::occupied_mut)
            .ok_or(ResourceTableError::NotPresent)
    }

    /// Insert a resource at the next available index, and track that it has a
    /// parent resource.
    ///
    /// The parent must exist to create a child. All children resources must
    /// be destroyed before a parent can be destroyed - otherwise
    /// [`ResourceTable::delete`] will fail with
    /// [`ResourceTableError::HasChildren`].
    ///
    /// Parent-child relationships are tracked inside the table to ensure that
    /// a parent resource is not deleted while it has live children. This
    /// allows child resources to hold "references" to a parent by table
    /// index, to avoid needing e.g. an `Arc<Mutex<parent>>` and the associated
    /// locking overhead and design issues, such as child existence extending
    /// lifetime of parent referent even after parent resource is destroyed,
    /// possibility for deadlocks.
    pub fn push_child<T, U>(
        &mut self,
        entry: T,
        parent: &Resource<U>,
    ) -> Result<Resource<T>, ResourceTableError>
    where
        T: Send + 'static,
        U: 'static,
    {
        let parent = parent.rep();
        self.occupied(parent)?;
        let owner = self.allocation_owner.clone();
        let data = TableEntry::new(entry, Some(parent), self.allocation_owner.as_ref())?;
        let child = self.push_(data)?;
        let linked = match self.occupied_mut(parent) {
            Ok(parent) => parent.add_child(child, owner.as_ref()),
            Err(error) => Err(error),
        };
        if let Err(error) = linked {
            drop(self.free_entry(child as usize));
            return Err(error);
        }
        Ok(Resource::new_own(child))
    }

    /// Add an already-resident child to a resource.
    pub fn add_child<T: 'static, U: 'static>(
        &mut self,
        child: Resource<T>,
        parent: Resource<U>,
    ) -> Result<(), ResourceTableError> {
        if self.occupied(child.rep())?.parent.is_some() {
            return Err(ResourceTableError::Allocation(HostAllocationError::Invalid));
        }
        let owner = self.allocation_owner.clone();
        self.occupied_mut(parent.rep())?
            .add_child(child.rep(), owner.as_ref())?;
        self.occupied_mut(child.rep())?.parent = Some(parent.rep());
        Ok(())
    }

    /// Remove a child to from a resource (but leave it in the table).
    pub fn remove_child<T: 'static, U: 'static>(
        &mut self,
        child: Resource<T>,
        parent: Resource<U>,
    ) -> Result<(), ResourceTableError> {
        let entry = self.occupied_mut(child.rep())?;
        assert_eq!(entry.parent, Some(parent.rep()));
        entry.parent = None;
        self.occupied_mut(parent.rep())?.remove_child(child.rep());
        Ok(())
    }

    /// Get an immutable reference to a resource of a given type at a given
    /// index.
    ///
    /// Multiple shared references can be borrowed at any given time.
    pub fn get<T: Any + Sized>(&self, key: &Resource<T>) -> Result<&T, ResourceTableError> {
        self.get_(key.rep())?
            .downcast_ref()
            .ok_or(ResourceTableError::WrongType)
    }

    fn get_(&self, key: u32) -> Result<&dyn Any, ResourceTableError> {
        let r = self.occupied(key)?;
        Ok(&*r.entry)
    }

    /// Get an mutable reference to a resource of a given type at a given
    /// index.
    pub fn get_mut<T: Any + Sized>(
        &mut self,
        key: &Resource<T>,
    ) -> Result<&mut T, ResourceTableError> {
        self.get_any_mut(key.rep())?
            .downcast_mut()
            .ok_or(ResourceTableError::WrongType)
    }

    /// Returns the raw `Any` at the `key` index provided.
    pub fn get_any_mut(&mut self, key: u32) -> Result<&mut dyn Any, ResourceTableError> {
        let r = self.occupied_mut(key)?;
        Ok(&mut *r.entry)
    }

    /// Remove the specified entry from the table.
    pub fn delete<T>(&mut self, resource: Resource<T>) -> Result<T, ResourceTableError>
    where
        T: Any,
    {
        self.delete_maybe_debug(resource, DELETE_WITH_TOMBSTONE)
    }

    fn delete_maybe_debug<T>(
        &mut self,
        resource: Resource<T>,
        debug: bool,
    ) -> Result<T, ResourceTableError>
    where
        T: Any,
    {
        debug_assert!(resource.owned());
        let entry = self.delete_entry(resource.rep(), debug)?;
        match entry.entry.downcast() {
            Ok(t) => Ok(*t),
            Err(_e) => Err(ResourceTableError::WrongType),
        }
    }

    fn delete_entry(&mut self, key: u32, debug: bool) -> Result<TableEntry, ResourceTableError> {
        if debug {
            // Tombstone storage has no funded implementation yet. Deny before
            // mutating the resource or its parent instead of panicking.
            return Err(ResourceTableError::Allocation(
                HostAllocationError::Unavailable,
            ));
        }
        if !self.occupied(key)?.children.is_empty() {
            return Err(ResourceTableError::HasChildren);
        }
        let e = self.free_entry(key as usize);
        if let Some(parent) = e.parent {
            // Remove deleted resource from parent's child list.
            // Parent must still be present because it can't be deleted while still having
            // children:
            self.occupied_mut(parent)
                .expect("missing parent")
                .remove_child(key);
        }
        Ok(e)
    }

    /// Zip the values of the map with mutable references to table entries corresponding to each
    /// key. As the keys in the `BTreeMap` are unique, this iterator can give mutable references
    /// with the same lifetime as the mutable reference to the [ResourceTable].
    pub fn iter_entries<'a, T>(
        &'a mut self,
        map: BTreeMap<u32, T>,
    ) -> impl Iterator<Item = (Result<&'a mut dyn Any, ResourceTableError>, T)> {
        map.into_iter().map(move |(k, v)| {
            let item = self
                .occupied_mut(k)
                .map(|e| Box::as_mut(&mut e.entry))
                // Safety: extending the lifetime of the mutable reference.
                .map(|item| unsafe { &mut *(item as *mut dyn Any) });
            (item, v)
        })
    }

    /// Iterate over all children belonging to the provided parent
    pub fn iter_children<T>(
        &self,
        parent: &Resource<T>,
    ) -> Result<impl Iterator<Item = &(dyn Any + Send)> + use<'_, T>, ResourceTableError>
    where
        T: 'static,
    {
        let parent_entry = self.occupied(parent.rep())?;
        Ok(parent_entry.children.iter().map(|child_index| {
            let child = self.occupied(*child_index).expect("missing child");
            child.entry.as_ref()
        }))
    }

    /// Iterate over all the entries in this table.
    pub fn iter_mut(&mut self) -> impl Iterator<Item = &mut (dyn Any + Send)> {
        self.entries.iter_mut().filter_map(|entry| match entry {
            Entry::Occupied { entry } => Some(&mut *entry.entry),
            Entry::Free { .. } => None,
        })
    }
}

impl Default for ResourceTable {
    fn default() -> Self {
        ResourceTable::new()
    }
}

impl fmt::Debug for ResourceTable {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "[")?;
        let mut wrote = false;
        for (index, entry) in self.entries.iter().enumerate() {
            if let Entry::Occupied { .. } = entry {
                if wrote {
                    write!(f, ", ")?;
                } else {
                    wrote = true;
                }
                write!(f, "{index}")?;
            }
        }
        write!(f, "]")
    }
}

#[test]
pub fn test_free_list() {
    let mut table = ResourceTable::new();

    let x = table.push(()).unwrap();
    assert_eq!(x.rep(), 0);

    let y = table.push(()).unwrap();
    assert_eq!(y.rep(), 1);

    // Deleting x should put it on the free list, so the next entry should have the same rep.
    table.delete_maybe_debug(x, false).unwrap();
    let x = table.push(()).unwrap();
    assert_eq!(x.rep(), 0);

    // Deleting x and then y should yield indices 1 and then 0 for new entries.
    table.delete_maybe_debug(x, false).unwrap();
    table.delete_maybe_debug(y, false).unwrap();

    let y = table.push(()).unwrap();
    assert_eq!(y.rep(), 1);

    let x = table.push(()).unwrap();
    assert_eq!(x.rep(), 0);

    // As the free list is empty, this entry will have a new id.
    let x = table.push(()).unwrap();
    assert_eq!(x.rep(), 2);
}

#[test]
fn test_max_capacity() {
    let mut table = ResourceTable::new();
    assert_eq!(table.max_capacity(), DEFAULT_MAX_CAPACITY);

    table.set_max_capacity(0);
    assert_eq!(table.max_capacity(), 0);
    assert!(table.push(()).is_err());

    table.set_max_capacity(1);
    assert_eq!(table.max_capacity(), 1);
    let x = table.push(()).unwrap();
    assert!(table.push(()).is_err());

    table.set_max_capacity(0);
    assert!(table.push(()).is_err());
    table.delete(x).unwrap();
    let x = table.push(()).unwrap();
    table.delete(x).unwrap();

    table.set_max_capacity(10);

    let handles = (0..10).map(|_| table.push(()).unwrap()).collect::<Vec<_>>();
    assert!(table.push(()).is_err());
    for handle in handles {
        table.delete(handle).unwrap();
    }

    table.push(()).unwrap();
}
