//! Original WASI46 nominal resources, entirely captured filesystem semantics.
//! No method delegates to a native filesystem Host implementation.
use crate::{
    Ctx,
    captured_inputs::{CapturedInputs, Kind, MAX_METADATA_BYTES, MAX_READ_BYTES},
};
use anyhow::{Result, ensure};
use bytes::Bytes;
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
};
use wasmtime::component::{HasData, Linker, Resource, ResourceTable};
use wasmtime_wasi::{
    DirPerms, FilePerms,
    filesystem::{Descriptor, Dir, OpenMode},
    p2::{
        DynInputStream, DynOutputStream, FsError, FsResult, InputStream, Pollable, ReaddirIterator,
        StreamError,
        bindings::{
            clocks::wall_clock,
            filesystem::{preopens, types},
        },
    },
};

const MAX_DESCRIPTORS: usize = 1024;
const MAX_STREAMS: usize = 256;
const MAX_ITERATORS: usize = 128;

struct OpenDescriptor {
    path: String,
    flags: types::DescriptorFlags,
}
struct Listing {
    queue: Arc<Mutex<VecDeque<types::DirectoryEntry>>>,
    charged: usize,
}

pub struct CapturedAdapter {
    pub inputs: CapturedInputs,
    descriptors: BTreeMap<u32, OpenDescriptor>,
    listings: BTreeMap<u32, Listing>,
    listing_bytes: usize,
    stream_count: Arc<AtomicUsize>,
    nominal_dir: Dir,
    // Empty private backing holds only nominal Descriptor identity. Guest
    // read/stat/list/open results never touch this OS directory.
    _nominal_backing: tempfile::TempDir,
}

impl CapturedAdapter {
    pub fn new(inputs: CapturedInputs, scratch: &std::path::Path) -> Result<Self> {
        let backing = tempfile::tempdir_in(scratch)?;
        let directory =
            cap_std::fs::Dir::open_ambient_dir(backing.path(), cap_std::ambient_authority())?;
        Ok(Self {
            inputs,
            descriptors: BTreeMap::new(),
            listings: BTreeMap::new(),
            listing_bytes: 0,
            stream_count: Arc::new(AtomicUsize::new(0)),
            nominal_dir: Dir::new(
                directory,
                DirPerms::READ,
                FilePerms::READ,
                OpenMode::READ,
                true,
            ),
            _nominal_backing: backing,
        })
    }
}

pub struct CapturedView<'a> {
    pub state: &'a mut CapturedAdapter,
    pub table: &'a mut ResourceTable,
}
struct CapturedData;
impl HasData for CapturedData {
    type Data<'a> = CapturedView<'a>;
}

pub fn install(linker: &mut Linker<Ctx>) -> Result<()> {
    // Replace the complete types/preopens interfaces, including resource drop,
    // with the same imported aliases. This is producer-only; Core's host/linker
    // and ABI admission have no exception or extra interface.
    linker.allow_shadowing(true);
    let result = (|| {
        types::add_to_linker::<Ctx, CapturedData>(linker, |ctx| CapturedView {
            state: &mut ctx.captured,
            table: &mut ctx.table,
        })?;
        preopens::add_to_linker::<Ctx, CapturedData>(linker, |ctx| CapturedView {
            state: &mut ctx.captured,
            table: &mut ctx.table,
        })?;
        Ok(())
    })();
    linker.allow_shadowing(false);
    result
}

impl CapturedView<'_> {
    fn descriptor(&self, resource: &Resource<Descriptor>) -> FsResult<&OpenDescriptor> {
        self.table.get(resource)?; // Validate actual nominal resource before rep.
        self.state
            .descriptors
            .get(&resource.rep())
            .ok_or_else(|| types::ErrorCode::BadDescriptor.into())
    }
    fn directory(&self, resource: &Resource<Descriptor>) -> FsResult<String> {
        let descriptor = self.descriptor(resource)?;
        if self.state.inputs.nodes[&descriptor.path].entry.kind != Kind::Directory {
            return Err(types::ErrorCode::NotDirectory.into());
        }
        if !descriptor.flags.contains(types::DescriptorFlags::READ) {
            return Err(types::ErrorCode::NotPermitted.into());
        }
        Ok(descriptor.path.clone())
    }
    fn file_bytes(&self, resource: &Resource<Descriptor>) -> FsResult<Bytes> {
        let descriptor = self.descriptor(resource)?;
        let node = &self.state.inputs.nodes[&descriptor.path];
        if node.entry.kind != Kind::File {
            return Err(types::ErrorCode::IsDirectory.into());
        }
        if !descriptor.flags.contains(types::DescriptorFlags::READ) {
            return Err(types::ErrorCode::NotPermitted.into());
        }
        Ok(node.bytes.clone())
    }
    fn at(&self, resource: &Resource<Descriptor>, path: &str) -> FsResult<String> {
        let base = self.directory(resource)?;
        self.state
            .inputs
            .child_path(&base, path)
            .map_err(|_| types::ErrorCode::NoEntry.into())
    }
    fn allocate(
        &mut self,
        path: String,
        flags: types::DescriptorFlags,
    ) -> FsResult<Resource<Descriptor>> {
        if self.state.descriptors.len() >= MAX_DESCRIPTORS {
            return Err(types::ErrorCode::InsufficientMemory.into());
        }
        if !self.state.inputs.nodes.contains_key(&path) {
            return Err(types::ErrorCode::NoEntry.into());
        }
        let resource = self
            .table
            .push(Descriptor::Dir(self.state.nominal_dir.clone()))?;
        if self
            .state
            .descriptors
            .insert(resource.rep(), OpenDescriptor { path, flags })
            .is_some()
        {
            return Err(anyhow::anyhow!("captured descriptor collision").into());
        }
        Ok(resource)
    }
    fn stat_path(&self, path: &str) -> FsResult<types::DescriptorStat> {
        let node = self
            .state
            .inputs
            .nodes
            .get(path)
            .ok_or(types::ErrorCode::NoEntry)?;
        let timestamp = wall_clock::Datetime {
            seconds: self.state.inputs.epoch,
            nanoseconds: 0,
        };
        Ok(types::DescriptorStat {
            type_: if node.entry.kind == Kind::Directory {
                types::DescriptorType::Directory
            } else {
                types::DescriptorType::RegularFile
            },
            link_count: 1,
            size: node.entry.size,
            data_access_timestamp: Some(timestamp),
            data_modification_timestamp: Some(timestamp),
            status_change_timestamp: Some(timestamp),
        })
    }
    fn hash_path(&self, path: &str) -> FsResult<types::MetadataHashValue> {
        let node = self
            .state
            .inputs
            .nodes
            .get(path)
            .ok_or(types::ErrorCode::NoEntry)?;
        Ok(types::MetadataHashValue {
            lower: node.inode,
            upper: 0,
        })
    }
    fn deny(&self, fd: &Resource<Descriptor>) -> FsResult<()> {
        self.descriptor(fd)?;
        Err(types::ErrorCode::NotPermitted.into())
    }
}

impl preopens::Host for CapturedView<'_> {
    fn get_directories(&mut self) -> wasmtime::Result<Vec<(Resource<Descriptor>, String)>> {
        let roots = self.state.inputs.roots.clone();
        ensure!(
            self.state
                .descriptors
                .len()
                .checked_add(roots.len())
                .is_some_and(|count| count <= MAX_DESCRIPTORS),
            "captured descriptor limit"
        );
        let mut output = Vec::with_capacity(roots.len());
        for root in roots {
            let resource = self
                .allocate(root.clone(), types::DescriptorFlags::READ)
                .map_err(|error| anyhow::anyhow!("{error:?}"))?;
            output.push((resource, root));
        }
        Ok(output)
    }
}
impl types::Host for CapturedView<'_> {
    fn convert_error_code(&mut self, error: FsError) -> wasmtime::Result<types::ErrorCode> {
        error.downcast()
    }
    fn filesystem_error_code(
        &mut self,
        error: Resource<wasmtime::Error>,
    ) -> wasmtime::Result<Option<types::ErrorCode>> {
        self.table.get(&error)?;
        Ok(None) // No captured stream ever embeds an OS/path error.
    }
}
impl types::HostDescriptor for CapturedView<'_> {
    async fn advise(
        &mut self,
        fd: Resource<Descriptor>,
        offset: u64,
        len: u64,
        _advice: types::Advice,
    ) -> FsResult<()> {
        self.file_bytes(&fd)?;
        offset.checked_add(len).ok_or(types::ErrorCode::Overflow)?;
        Ok(())
    }
    async fn sync_data(&mut self, fd: Resource<Descriptor>) -> FsResult<()> {
        self.descriptor(&fd)?;
        Ok(())
    }
    async fn get_flags(&mut self, fd: Resource<Descriptor>) -> FsResult<types::DescriptorFlags> {
        Ok(self.descriptor(&fd)?.flags)
    }
    async fn get_type(&mut self, fd: Resource<Descriptor>) -> FsResult<types::DescriptorType> {
        let path = &self.descriptor(&fd)?.path;
        Ok(
            if self.state.inputs.nodes[path].entry.kind == Kind::Directory {
                types::DescriptorType::Directory
            } else {
                types::DescriptorType::RegularFile
            },
        )
    }
    async fn set_size(&mut self, fd: Resource<Descriptor>, _size: u64) -> FsResult<()> {
        self.deny(&fd)
    }
    async fn set_times(
        &mut self,
        fd: Resource<Descriptor>,
        _atim: types::NewTimestamp,
        _mtim: types::NewTimestamp,
    ) -> FsResult<()> {
        self.deny(&fd)
    }
    async fn read(
        &mut self,
        fd: Resource<Descriptor>,
        len: u64,
        offset: u64,
    ) -> FsResult<(Vec<u8>, bool)> {
        let bytes = self.file_bytes(&fd)?;
        let Ok(start) = usize::try_from(offset) else {
            return Ok((Vec::new(), true));
        };
        if start >= bytes.len() {
            return Ok((Vec::new(), true));
        }
        let amount = usize::try_from(len)
            .unwrap_or(usize::MAX)
            .min(MAX_READ_BYTES)
            .min(bytes.len() - start);
        Ok((
            bytes[start..start + amount].to_vec(),
            start + amount == bytes.len(),
        ))
    }
    async fn write(
        &mut self,
        fd: Resource<Descriptor>,
        _buffer: Vec<u8>,
        _offset: u64,
    ) -> FsResult<u64> {
        self.deny(&fd)?;
        unreachable!()
    }
    async fn read_directory(
        &mut self,
        fd: Resource<Descriptor>,
    ) -> FsResult<Resource<ReaddirIterator>> {
        let base = self.directory(&fd)?;
        if self.state.listings.len() >= MAX_ITERATORS {
            return Err(types::ErrorCode::InsufficientMemory.into());
        }
        let prefix = format!("{base}/");
        let mut entries = VecDeque::new();
        let mut charged = 0usize;
        for (path, node) in self.state.inputs.nodes.range(prefix.clone()..) {
            if !path.starts_with(&prefix) {
                break;
            }
            let name = &path[prefix.len()..];
            if name.contains('/') {
                continue;
            }
            charged = charged
                .checked_add(name.len() + 64)
                .ok_or(types::ErrorCode::Overflow)?;
            if self
                .state
                .listing_bytes
                .checked_add(charged)
                .is_none_or(|size| size > MAX_METADATA_BYTES)
            {
                return Err(types::ErrorCode::InsufficientMemory.into());
            }
            entries.push_back(types::DirectoryEntry {
                type_: if node.entry.kind == Kind::Directory {
                    types::DescriptorType::Directory
                } else {
                    types::DescriptorType::RegularFile
                },
                name: name.to_owned(),
            });
        }
        let queue = Arc::new(Mutex::new(entries));
        let captured_queue = queue.clone();
        // Original constructor exposed by the narrow producer-only WASI46
        // patch. Same nominal type; iterator closes over captured records only.
        let iterator = ReaddirIterator::new(std::iter::from_fn(move || {
            captured_queue.lock().unwrap().pop_front().map(Ok)
        }));
        let resource = self.table.push(iterator)?;
        self.state.listing_bytes += charged;
        if self
            .state
            .listings
            .insert(resource.rep(), Listing { queue, charged })
            .is_some()
        {
            return Err(anyhow::anyhow!("captured iterator collision").into());
        }
        Ok(resource)
    }
    async fn sync(&mut self, fd: Resource<Descriptor>) -> FsResult<()> {
        self.descriptor(&fd)?;
        Ok(())
    }
    async fn create_directory_at(
        &mut self,
        fd: Resource<Descriptor>,
        _path: String,
    ) -> FsResult<()> {
        self.deny(&fd)
    }
    async fn stat(&mut self, fd: Resource<Descriptor>) -> FsResult<types::DescriptorStat> {
        self.stat_path(&self.descriptor(&fd)?.path)
    }
    async fn stat_at(
        &mut self,
        fd: Resource<Descriptor>,
        _flags: types::PathFlags,
        path: String,
    ) -> FsResult<types::DescriptorStat> {
        let path = self.at(&fd, &path)?;
        self.stat_path(&path)
    }
    async fn set_times_at(
        &mut self,
        fd: Resource<Descriptor>,
        _flags: types::PathFlags,
        _path: String,
        _atim: types::NewTimestamp,
        _mtim: types::NewTimestamp,
    ) -> FsResult<()> {
        self.deny(&fd)
    }
    async fn link_at(
        &mut self,
        fd: Resource<Descriptor>,
        _flags: types::PathFlags,
        _old: String,
        new: Resource<Descriptor>,
        _path: String,
    ) -> FsResult<()> {
        self.descriptor(&new)?;
        self.deny(&fd)
    }
    async fn open_at(
        &mut self,
        fd: Resource<Descriptor>,
        _flags: types::PathFlags,
        path: String,
        open: types::OpenFlags,
        flags: types::DescriptorFlags,
    ) -> FsResult<Resource<Descriptor>> {
        if !(open & !types::OpenFlags::DIRECTORY).is_empty()
            || !(flags & !types::DescriptorFlags::READ).is_empty()
        {
            self.descriptor(&fd)?;
            return Err(types::ErrorCode::NotPermitted.into());
        }
        let path = self.at(&fd, &path)?;
        if open.contains(types::OpenFlags::DIRECTORY)
            && self.state.inputs.nodes[&path].entry.kind != Kind::Directory
        {
            return Err(types::ErrorCode::NotDirectory.into());
        }
        self.allocate(path, flags)
    }
    fn drop(&mut self, fd: Resource<Descriptor>) -> wasmtime::Result<()> {
        self.table.get(&fd)?;
        ensure!(
            self.state.descriptors.contains_key(&fd.rep()),
            "unknown captured descriptor"
        );
        let id = fd.rep();
        self.table.delete(fd)?;
        self.state.descriptors.remove(&id);
        Ok(())
    }
    async fn readlink_at(&mut self, fd: Resource<Descriptor>, path: String) -> FsResult<String> {
        self.at(&fd, &path)?;
        Err(types::ErrorCode::Invalid.into())
    }
    async fn remove_directory_at(
        &mut self,
        fd: Resource<Descriptor>,
        _path: String,
    ) -> FsResult<()> {
        self.deny(&fd)
    }
    async fn rename_at(
        &mut self,
        fd: Resource<Descriptor>,
        _path: String,
        new: Resource<Descriptor>,
        _new_path: String,
    ) -> FsResult<()> {
        self.descriptor(&new)?;
        self.deny(&fd)
    }
    async fn symlink_at(
        &mut self,
        fd: Resource<Descriptor>,
        _source: String,
        _destination: String,
    ) -> FsResult<()> {
        self.deny(&fd)
    }
    async fn unlink_file_at(&mut self, fd: Resource<Descriptor>, _path: String) -> FsResult<()> {
        self.deny(&fd)
    }
    fn read_via_stream(
        &mut self,
        fd: Resource<Descriptor>,
        offset: u64,
    ) -> FsResult<Resource<DynInputStream>> {
        let bytes = self.file_bytes(&fd)?;
        let start = usize::try_from(offset)
            .unwrap_or(usize::MAX)
            .min(bytes.len());
        let count = self.state.stream_count.clone();
        if count
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |value| {
                (value < MAX_STREAMS).then_some(value + 1)
            })
            .is_err()
        {
            return Err(types::ErrorCode::InsufficientMemory.into());
        }
        let stream: DynInputStream = Box::new(CapturedStream {
            bytes: bytes.slice(start..),
            count,
        });
        Ok(self.table.push(stream)?) // failed push drops stream and releases count
    }
    fn write_via_stream(
        &mut self,
        fd: Resource<Descriptor>,
        _offset: u64,
    ) -> FsResult<Resource<DynOutputStream>> {
        self.deny(&fd)?;
        unreachable!()
    }
    fn append_via_stream(
        &mut self,
        fd: Resource<Descriptor>,
    ) -> FsResult<Resource<DynOutputStream>> {
        self.deny(&fd)?;
        unreachable!()
    }
    async fn is_same_object(
        &mut self,
        a: Resource<Descriptor>,
        b: Resource<Descriptor>,
    ) -> wasmtime::Result<bool> {
        Ok(self
            .descriptor(&a)
            .map_err(|error| anyhow::anyhow!("{error:?}"))?
            .path
            == self
                .descriptor(&b)
                .map_err(|error| anyhow::anyhow!("{error:?}"))?
                .path)
    }
    async fn metadata_hash(
        &mut self,
        fd: Resource<Descriptor>,
    ) -> FsResult<types::MetadataHashValue> {
        self.hash_path(&self.descriptor(&fd)?.path)
    }
    async fn metadata_hash_at(
        &mut self,
        fd: Resource<Descriptor>,
        _flags: types::PathFlags,
        path: String,
    ) -> FsResult<types::MetadataHashValue> {
        let path = self.at(&fd, &path)?;
        self.hash_path(&path)
    }
}
impl types::HostDirectoryEntryStream for CapturedView<'_> {
    async fn read_directory_entry(
        &mut self,
        stream: Resource<ReaddirIterator>,
    ) -> FsResult<Option<types::DirectoryEntry>> {
        self.table.get(&stream)?;
        let listing = self
            .state
            .listings
            .get_mut(&stream.rep())
            .ok_or(types::ErrorCode::BadDescriptor)?;
        let entry = listing.queue.lock().unwrap().pop_front();
        if let Some(entry) = &entry {
            let released = entry.name.len() + 64;
            listing.charged -= released;
            self.state.listing_bytes -= released;
        }
        Ok(entry)
    }
    fn drop(&mut self, stream: Resource<ReaddirIterator>) -> wasmtime::Result<()> {
        self.table.get(&stream)?;
        ensure!(
            self.state.listings.contains_key(&stream.rep()),
            "unknown captured iterator"
        );
        let id = stream.rep();
        self.table.delete(stream)?;
        let listing = self.state.listings.remove(&id).unwrap();
        self.state.listing_bytes -= listing.charged;
        Ok(())
    }
}

struct CapturedStream {
    bytes: Bytes,
    count: Arc<AtomicUsize>,
}
impl Drop for CapturedStream {
    fn drop(&mut self) {
        self.count.fetch_sub(1, Ordering::SeqCst);
    }
}
#[async_trait::async_trait]
impl InputStream for CapturedStream {
    fn read(&mut self, size: usize) -> std::result::Result<Bytes, StreamError> {
        if size > MAX_READ_BYTES {
            return Err(StreamError::Trap(anyhow::anyhow!(
                "captured read buffer limit"
            )));
        }
        if size == 0 {
            return Ok(Bytes::new());
        }
        if self.bytes.is_empty() {
            return Err(StreamError::Closed);
        }
        Ok(self.bytes.split_to(size.min(self.bytes.len())))
    }
}
#[async_trait::async_trait]
impl Pollable for CapturedStream {
    async fn ready(&mut self) {}
}
