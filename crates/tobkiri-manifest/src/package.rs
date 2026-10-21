use crate::*;
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::SigningKey;
use std::{
    cell::{Cell, RefCell},
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::{self, Cursor, Read, Write},
    path::Path,
};
use tobkiri_canon::{
    CanonValue, Variant, digest, encode, hash_bytes, parse, sign, verify_signature,
};

fn invalid(message: impl Into<String>) -> ManifestError {
    ManifestError::Invalid(message.into())
}

const MAX_PACKAGE_BYTES: u64 = 512 * 1024 * 1024;
const MAX_PACKAGE_ENTRIES: usize = 16_384;
const MAX_PACKAGE_MEMBER_BYTES: u64 = 128 * 1024 * 1024;
const TAR_BLOCK_BYTES: u64 = 512;
const TAR_END_BYTES: u64 = 2 * TAR_BLOCK_BYTES;

fn check_entry_count(files: usize, signed: bool) -> Result<(), ManifestError> {
    let entries = files
        .checked_add(1 + usize::from(signed))
        .ok_or_else(|| invalid("package entry count overflow"))?;
    if entries > MAX_PACKAGE_ENTRIES {
        return Err(invalid("package requires bounded regular ustar entries"));
    }
    Ok(())
}

/// Account the exact ordinary ustar representation, including padding and EOF.
/// Check before reading a source member or cloning caller-owned artifact bytes.
struct PackageBudget {
    entries: usize,
    bytes: u64,
}

impl PackageBudget {
    fn new() -> Self {
        Self {
            entries: 0,
            bytes: TAR_END_BYTES,
        }
    }

    fn add(&mut self, size: u64) -> Result<(), ManifestError> {
        if self.entries >= MAX_PACKAGE_ENTRIES {
            return Err(invalid("package requires bounded regular ustar entries"));
        }
        if size > MAX_PACKAGE_MEMBER_BYTES {
            return Err(invalid("package member exceeds 128 MiB"));
        }
        let padded = size
            .checked_add(TAR_BLOCK_BYTES - 1)
            .map(|size| size / TAR_BLOCK_BYTES * TAR_BLOCK_BYTES)
            .ok_or_else(|| invalid("package byte count overflow"))?;
        let bytes = self
            .bytes
            .checked_add(TAR_BLOCK_BYTES)
            .and_then(|bytes| bytes.checked_add(padded))
            .ok_or_else(|| invalid("package byte count overflow"))?;
        if bytes > MAX_PACKAGE_BYTES {
            return Err(invalid("package exceeds 512 MiB including ustar framing"));
        }
        self.entries += 1;
        self.bytes = bytes;
        Ok(())
    }

    /// Pre-read metadata limit for one member through the no-follow reader.
    /// Rounding down reserves its complete header and final payload padding.
    fn remaining_member_limit(&self) -> Result<u64, ManifestError> {
        if self.entries >= MAX_PACKAGE_ENTRIES {
            return Err(invalid("package requires bounded regular ustar entries"));
        }
        let remaining = MAX_PACKAGE_BYTES
            .checked_sub(self.bytes)
            .and_then(|bytes| bytes.checked_sub(TAR_BLOCK_BYTES))
            .ok_or_else(|| invalid("package exceeds 512 MiB including ustar framing"))?;
        Ok((remaining / TAR_BLOCK_BYTES * TAR_BLOCK_BYTES).min(MAX_PACKAGE_MEMBER_BYTES))
    }
}

pub(crate) fn portable_path(value: &str) -> Result<(), ManifestError> {
    if value.is_empty()
        || value.len() > 1024
        || value.contains('\\')
        || value.contains(':')
        || value.chars().any(|c| c.is_control())
    {
        return Err(invalid("unsafe package path"));
    }
    for part in value.split('/') {
        if part.is_empty()
            || part == "."
            || part == ".."
            || part.ends_with(['.', ' '])
            || part.len() > 255
        {
            return Err(invalid("unsafe package path component"));
        }
        let base = part.split('.').next().unwrap_or("").to_ascii_uppercase();
        if matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (base.len() == 4
                && (base.starts_with("COM") || base.starts_with("LPT"))
                && matches!(base.as_bytes()[3], b'1'..=b'9'))
        {
            return Err(invalid("reserved filesystem path component"));
        }
    }
    Ok(())
}
fn typed(raw: &[u8]) -> Result<(CanonValue, PackManifest), ManifestError> {
    let value = parse(raw, Variant::StrictV1)?;
    let manifest: PackManifest = serde_json::from_slice(&encode(&value, Variant::StrictV1)?)?;
    validate_manifest(&manifest)?;
    Ok((value, manifest))
}
/// Parse untrusted manifest data through the strict codec before typed validation.
pub fn parse_manifest(raw: &[u8]) -> Result<PackManifest, ManifestError> {
    Ok(typed(raw)?.1)
}
/// Canonical bytes for a new typed manifest, including explicit default values.
pub fn canonical_manifest(manifest: &PackManifest) -> Result<Vec<u8>, ManifestError> {
    validate_manifest(manifest)?;
    let value = parse(&serde_json::to_vec(manifest)?, Variant::StrictV1)?;
    Ok(encode(&value, Variant::StrictV1)?)
}
fn validate_manifest(manifest: &PackManifest) -> Result<(), ManifestError> {
    if manifest.format != "tobkiri.pack/1" {
        return Err(invalid("unsupported manifest format"));
    }
    let version: Vec<_> = manifest.version.split('.').collect();
    if version.len() != 3
        || version.iter().any(|p| {
            p.is_empty()
                || p.len() > 1 && p.starts_with('0')
                || !p.bytes().all(|b| b.is_ascii_digit())
        })
    {
        return Err(invalid("Pack version must be a three-part numeric version"));
    }
    if manifest.publisher_key.is_none() && manifest.id.publisher() != "local" {
        return Err(invalid("unsigned Pack publisher must be local"));
    }
    if manifest.publisher_key.is_some() && manifest.id.publisher() == "local" {
        return Err(invalid("local publisher is only for unsigned Packs"));
    }
    let mut paths = BTreeSet::new();
    let mut folded = BTreeSet::new();
    for file in &manifest.files {
        portable_path(&file.path)?;
        if matches!(file.path.as_str(), "pack.json" | "signature.json")
            || !paths.insert(file.path.as_str())
            || !folded.insert(file.path.to_lowercase())
        {
            return Err(invalid("duplicate or reserved package file"));
        }
        if file.size > MAX_PACKAGE_MEMBER_BYTES {
            return Err(invalid("package file exceeds 128 MiB"));
        }
    }
    let has = |path: &str| -> Result<(), ManifestError> {
        portable_path(path)?;
        if !paths.contains(path) {
            return Err(invalid(format!("undeclared package file {path}")));
        }
        Ok(())
    };
    match &manifest.runtime {
        RuntimeSpec::Wasm {
            component,
            toolchain,
            ..
        } => {
            has(component)?;
            if toolchain.trim().is_empty() {
                return Err(invalid("missing guest toolchain"));
            }
        }
        RuntimeSpec::Native { executable, .. } => {
            has(executable)?;
            if !manifest.permissions.native {
                return Err(invalid(
                    "native runtime must declare dangerous native permission",
                ));
            }
        }
        RuntimeSpec::Data => {}
    }
    if let Some(config) = manifest.runtime.config() {
        has(&config.path)?;
    }
    if let Some(ui) = &manifest.ui {
        has(&ui.entry)?;
    }
    let mut shape_ids = BTreeSet::new();
    let mut mapped_names = BTreeMap::new();
    for shape in &manifest.shapes {
        if shape.name.owner() != manifest.id {
            return Err(invalid("shape must be owned by its defining Pack"));
        }
        if mapped_names
            .insert(shape.name.interface(), shape.name.clone())
            .is_some_and(|prior| prior != shape.name)
        {
            return Err(invalid(
                "distinct dotted shapes collide on the same WIT interface",
            ));
        }
        let reference = ShapeRef::new(shape.name.clone(), shape.version);
        wit_interface_name(&reference)?;
        if !shape_ids.insert(reference) {
            return Err(invalid("duplicate shape definition"));
        }
        has(&shape.wit)?;
        has(&format!("wit/{}.shape.json", shape.name.interface()))?;
    }
    for array in [&manifest.flows, &manifest.overlays] {
        let mut ids = BTreeSet::new();
        for path in array {
            has(path)?;
            if !ids.insert(path) {
                return Err(invalid("duplicate document path"));
            }
        }
    }
    let mut stores = BTreeSet::new();
    for store in &manifest.stores {
        portable_path(&store.name)?;
        if store.name.contains('/') || !stores.insert(&store.name) {
            return Err(invalid("invalid or duplicate store name"));
        }
        if let Some(seed) = &store.seed {
            has(seed)?;
        }
    }
    for ports in [&manifest.ports.provides, &manifest.ports.consumes] {
        let mut seen = BTreeSet::new();
        for port in ports {
            let reference = ShapeRef::new(port.shape.clone(), port.version);
            wit_interface_name(&reference)?;
            if !seen.insert(reference) {
                return Err(invalid("duplicate port declaration"));
            }
        }
    }
    let mut inputs = BTreeSet::new();
    for input in &manifest.inputs {
        if !inputs.insert(input) {
            return Err(invalid("duplicate Input declaration"));
        }
        if !manifest.ports.provides.iter().any(|port| {
            port.shape == input.exact_input_view.name
                && port.version == input.exact_input_view.version
        }) {
            return Err(invalid(
                "Input declaration must name an exact provided view",
            ));
        }
    }
    let mut untyped = BTreeSet::new();
    for port in &manifest.ports.untyped {
        if !matches!(port.direction.as_str(), "provides" | "consumes")
            || !untyped.insert((&port.name, &port.direction))
            || port.name.trim().is_empty()
        {
            return Err(invalid("invalid explicit untyped port"));
        }
    }
    Ok(())
}
fn key(value: &str) -> Result<[u8; 32], ManifestError> {
    let value = value
        .strip_prefix("ed25519:")
        .ok_or_else(|| invalid("publisher key must be ed25519 base64url"))?;
    let bytes = URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| invalid("invalid publisher key encoding"))?;
    if URL_SAFE_NO_PAD.encode(&bytes) != value {
        return Err(invalid("non-canonical publisher key"));
    }
    bytes
        .try_into()
        .map_err(|_| invalid("invalid publisher key length"))
}
fn verify(
    raw: Vec<u8>,
    signature: Option<Vec<u8>>,
    files: BTreeMap<String, Vec<u8>>,
    publishers: &BTreeMap<String, String>,
) -> Result<VerifiedPack, ManifestError> {
    let (value, manifest) = typed(&raw)?;
    if let Some(publisher_key) = &manifest.publisher_key {
        let binding = publishers
            .get(manifest.id.publisher())
            .ok_or_else(|| invalid("publisher has no Profile key binding"))?;
        if binding != publisher_key {
            return Err(invalid("publisher key differs from Profile binding"));
        }
        let signature = signature.ok_or_else(|| invalid("signed Pack has no signature.json"))?;
        let signature_value = parse(&signature, Variant::StrictV1)?;
        let signature: PackSignature =
            serde_json::from_slice(&encode(&signature_value, Variant::StrictV1)?)?;
        if signature.algorithm != "Ed25519" || signature.key != *publisher_key {
            return Err(invalid("signature algorithm/key mismatch"));
        }
        let bytes = URL_SAFE_NO_PAD
            .decode(&signature.signature)
            .map_err(|_| invalid("invalid signature encoding"))?;
        if URL_SAFE_NO_PAD.encode(&bytes) != signature.signature {
            return Err(invalid("non-canonical signature encoding"));
        }
        let signature: [u8; 64] = bytes.try_into().map_err(|_| invalid("signature length"))?;
        verify_signature(&value, Variant::StrictV1, &key(publisher_key)?, &signature)?;
    } else if signature.is_some() {
        return Err(invalid("unsigned local Pack has a signature envelope"));
    }
    if files.len() != manifest.files.len() {
        return Err(invalid("file inventory does not match manifest"));
    }
    for entry in &manifest.files {
        let content = files
            .get(&entry.path)
            .ok_or_else(|| invalid("missing declared package file"))?;
        if content.len() as u64 != entry.size || hash_bytes(content) != entry.sha256 {
            return Err(invalid(format!(
                "file digest/size mismatch: {}",
                entry.path
            )));
        }
    }
    let shapes = load_shapes(&manifest, &files)?;
    for input in &manifest.inputs {
        if let Some(view) = shapes
            .iter()
            .find(|view| view.reference == input.exact_input_view)
        {
            input_function(&manifest, input, view)?;
        }
    }

    if let Some(config) = manifest.runtime.config() {
        let raw = files
            .get(&config.path)
            .ok_or_else(|| invalid("config file missing"))?;
        if raw.len() > 256 * 1024 {
            return Err(invalid("config exceeds 256 KiB"));
        }
        let value = parse(raw, Variant::StrictV1)?;
        if !matches!(value, CanonValue::Obj(_)) {
            return Err(invalid("config must be a StrictV1 object"));
        }
        if let Some(reference) = &config.shape {
            let shape = shapes
                .iter()
                .find(|s| s.reference == *reference && s.kind == ShapeKind::Data)
                .ok_or_else(|| invalid("config shape is not a verified data shape"))?;
            let ty = shape
                .types
                .get(&reference.name.interface())
                .or_else(|| shape.types.get("data"))
                .or_else(|| {
                    (shape.types.len() == 1).then(|| shape.types.values().next().expect("one type"))
                })
                .ok_or_else(|| invalid("config data shape needs one unambiguous root type"))?;
            validate_json(ty, &value)?;
        }
    }
    for store in &manifest.stores {
        if let Some(seed) = &store.seed {
            parse(
                files
                    .get(seed)
                    .ok_or_else(|| invalid("seed file missing"))?,
                Variant::StrictV1,
            )?;
        }
    }
    Ok(VerifiedPack {
        hash: digest(&value, Variant::StrictV1)?.into(),
        manifest,
        files,
        shapes,
    })
}
/// Source-directory admission reads only declared artifacts; undeclared developer
/// sources are never copied, exposed to guests or included in the package.
pub fn verify_directory(
    root: &Path,
    publishers: &BTreeMap<String, String>,
) -> Result<VerifiedPack, ManifestError> {
    if fs::symlink_metadata(root)?.file_type().is_symlink() || !root.is_dir() {
        return Err(invalid("Pack source root must be a real directory"));
    }
    let read = |relative: &str, limit: u64| -> Result<Vec<u8>, ManifestError> {
        portable_path(relative)?;
        Ok(tobkiri_canon::files::read_relative(root, relative, limit)?)
    };
    let raw = read("pack.json", tobkiri_canon::MAX_BYTES as u64)?;
    let manifest = parse_manifest(&raw)?;
    let signed = match fs::symlink_metadata(root.join("signature.json")) {
        Ok(_) => true,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => false,
        Err(error) => return Err(error.into()),
    };
    check_entry_count(manifest.files.len(), signed)?;
    let mut budget = PackageBudget::new();
    budget.add(raw.len() as u64)?;
    for entry in &manifest.files {
        budget.add(entry.size)?;
    }
    // Every declared member is reserved before any artifact is read. The
    // signature's actual size is checked by the pinned reader before allocation.
    let signature = if signed {
        let signature = read("signature.json", budget.remaining_member_limit()?)?;
        budget.add(signature.len() as u64)?;
        Some(signature)
    } else {
        None
    };
    let mut files = BTreeMap::new();
    for entry in &manifest.files {
        // A false small declaration must not permit a larger read/allocation.
        files.insert(entry.path.clone(), read(&entry.path, entry.size)?);
    }
    verify(raw, signature, files, publishers)
}
/// Sign a normalized new-format manifest; label-only publisher identities fail.
pub fn sign_manifest(
    manifest: &PackManifest,
    signing: &SigningKey,
) -> Result<PackSignature, ManifestError> {
    let public = format!(
        "ed25519:{}",
        URL_SAFE_NO_PAD.encode(signing.verifying_key().to_bytes())
    );
    if manifest.publisher_key.as_deref() != Some(&public) {
        return Err(invalid("manifest publisher key does not match signer"));
    }
    let value = parse(&canonical_manifest(manifest)?, Variant::StrictV1)?;
    Ok(PackSignature {
        algorithm: "Ed25519".into(),
        key: public,
        signature: URL_SAFE_NO_PAD.encode(sign(&value, signing)?),
    })
}
fn archive_into<W: Write>(
    entries: &BTreeMap<String, Vec<u8>>,
    output: W,
) -> Result<W, ManifestError> {
    let mut budget = PackageBudget::new();
    for content in entries.values() {
        budget.add(content.len() as u64)?;
    }
    let mut builder = tar::Builder::new(output);
    for (path, content) in entries {
        portable_path(path)?;
        let mut header = tar::Header::new_ustar();
        header.set_path(path)?;
        header.set_size(content.len() as u64);
        header.set_mode(0o644);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_cksum();
        builder.append(&header, content.as_slice())?;
    }
    builder.finish()?;
    Ok(builder.into_inner()?)
}
fn archive(entries: &BTreeMap<String, Vec<u8>>) -> Result<Vec<u8>, ManifestError> {
    archive_into(entries, Vec::new())
}
/// Produce sorted, timestamp-free ustar bytes with a complete file inventory.
pub fn build_tpk(
    manifest: &PackManifest,
    files: &BTreeMap<String, Vec<u8>>,
    signature: Option<&PackSignature>,
) -> Result<Vec<u8>, ManifestError> {
    check_entry_count(files.len(), signature.is_some())?;
    check_entry_count(manifest.files.len(), signature.is_some())?;
    let raw = canonical_manifest(manifest)?;
    let signature = if let Some(signature) = signature {
        let value = parse(&serde_json::to_vec(signature)?, Variant::StrictV1)?;
        Some(encode(&value, Variant::StrictV1)?)
    } else {
        None
    };
    let mut budget = PackageBudget::new();
    budget.add(raw.len() as u64)?;
    if let Some(signature) = &signature {
        budget.add(signature.len() as u64)?;
    }
    for content in files.values() {
        budget.add(content.len() as u64)?;
    }
    let publishers = manifest
        .publisher_key
        .as_ref()
        .map(|key| BTreeMap::from([(manifest.id.publisher().into(), key.clone())]))
        .unwrap_or_default();
    let verified = verify(raw.clone(), signature.clone(), files.clone(), &publishers)?;
    let mut entries = verified.files;
    entries.insert("pack.json".into(), raw);
    if let Some(signature) = signature {
        entries.insert("signature.json".into(), signature);
    }
    archive(&entries)
}
/// Verify one bounded snapshot read from an opened regular package descriptor.
/// Payloads are retained once; original framing is captured separately so the
/// canonical comparison neither reopens nor rereads a possibly changed file.
pub fn verify_tpk_file(
    path: &Path,
    publishers: &BTreeMap<String, String>,
) -> Result<VerifiedPack, ManifestError> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // Opening a replaced path or FIFO must not block before the descriptor's
        // regular-file check. Preserve the existing symlink-to-file behavior.
        options.custom_flags(libc::O_NONBLOCK);
    }
    let mut file = options.open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() {
        return Err(invalid("package artifact is not a regular file"));
    }
    verify_tpk_reader(&mut file, metadata.len(), MAX_PACKAGE_BYTES, publishers)
}

// At most one header and 511 padding bytes per ordinary entry, plus EOF.
// Hidden extension records or arbitrary trailing data may exceed this small
// snapshot bound; keep draining within the input envelope, then reject them.
const MAX_PACKAGE_FRAMING_BYTES: usize =
    MAX_PACKAGE_ENTRIES * TAR_BLOCK_BYTES as usize * 2 + TAR_END_BYTES as usize;

#[derive(Default)]
struct InputSnapshot {
    capture: Cell<bool>,
    framing: RefCell<Vec<u8>>,
    bytes: Cell<u64>,
    framing_overflow: Cell<bool>,
    input_failed: Cell<bool>,
}

struct SnapshotInput<'a, R> {
    input: R,
    state: &'a InputSnapshot,
    maximum: u64,
}
impl<R: Read> Read for SnapshotInput<'_, R> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let remaining = self.maximum.saturating_add(1) - self.state.bytes.get();
        let limit = output.len().min(remaining as usize);
        if limit == 0 {
            return Ok(0);
        }
        // tar's header reader forwards Interrupted directly. The former
        // read_to_end capture retried it, so keep that behavior at this seam.
        let count = loop {
            match self.input.read(&mut output[..limit]) {
                Ok(count) => break count,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => {
                    self.state.input_failed.set(true);
                    return Err(error);
                }
            }
        };
        self.state.bytes.set(self.state.bytes.get() + count as u64);
        if self.state.capture.get() && !self.state.framing_overflow.get() {
            let mut framing = self.state.framing.borrow_mut();
            let length = framing.len() + count;
            if length > MAX_PACKAGE_FRAMING_BYTES {
                self.state.framing_overflow.set(true);
            } else {
                if length > framing.capacity() {
                    let capacity = framing
                        .capacity()
                        .saturating_mul(2)
                        .max(length)
                        .min(MAX_PACKAGE_FRAMING_BYTES);
                    let additional = capacity - framing.len();
                    framing
                        .try_reserve_exact(additional)
                        .map_err(io::Error::other)?;
                }
                framing.extend_from_slice(&output[..count]);
            }
        }
        Ok(count)
    }
}

/// Borrow the exact first-pass byte snapshot: original framing interleaved
/// with the same owned payloads that verification will return. No FD contents
/// or metadata are trusted as immutable, and no payload is copied for replay.
struct SnapshotReplay<'a> {
    framing: &'a [u8],
    frame_ends: std::slice::Iter<'a, usize>,
    contents: std::collections::btree_map::Values<'a, String, Vec<u8>>,
    pending: &'a [u8],
    payload_next: bool,
    ended: bool,
}
impl Read for SnapshotReplay<'_> {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        if output.is_empty() {
            return Ok(0);
        }
        loop {
            if !self.pending.is_empty() {
                let count = output.len().min(self.pending.len());
                output[..count].copy_from_slice(&self.pending[..count]);
                self.pending = &self.pending[count..];
                return Ok(count);
            }
            if self.ended {
                return Ok(0);
            }
            if self.payload_next {
                self.pending = self.contents.next().expect("one payload per framing span");
                self.payload_next = false;
            } else if let Some(&end) = self.frame_ends.next() {
                let (before, after) = self.framing.split_at(end);
                self.pending = before;
                self.framing = after;
                self.payload_next = true;
            } else {
                self.pending = self.framing;
                self.framing = &[];
                self.ended = true;
            }
        }
    }
}

struct ComparingWriter<R> {
    original: R,
    mismatch: Cell<bool>,
}
impl<R: Read> Write for ComparingWriter<R> {
    fn write(&mut self, expected: &[u8]) -> io::Result<usize> {
        // Returning a short write is deliberate. The authoritative tar writer
        // uses write_all, while this sink needs only fixed stack scratch.
        let mut actual = [0u8; 8192];
        let count = expected.len().min(actual.len());
        if let Err(error) = self.original.read_exact(&mut actual[..count]) {
            if error.kind() == io::ErrorKind::UnexpectedEof {
                self.mismatch.set(true);
            }
            return Err(error);
        }
        if actual[..count] != expected[..count] {
            self.mismatch.set(true);
            return Err(io::Error::other("noncanonical package bytes"));
        }
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn verify_tpk_reader(
    input: &mut impl Read,
    metadata_bytes: u64,
    maximum: u64,
    publishers: &BTreeMap<String, String>,
) -> Result<VerifiedPack, ManifestError> {
    if metadata_bytes > maximum {
        return Err(invalid("package exceeds 512 MiB"));
    }
    let state = InputSnapshot::default();
    state.capture.set(true);
    let mut archive = tar::Archive::new(SnapshotInput {
        input,
        state: &state,
        maximum,
    });
    let mut entries = BTreeMap::new();
    let mut frame_ends = Vec::new();
    let mut captured_framing = 0;
    let mut previous = String::new();
    let mut budget = PackageBudget::new();
    let parsed = (|| -> Result<(), ManifestError> {
        for entry in archive.entries()? {
            let mut entry = entry?;
            if !entry.header().entry_type().is_file() || entry.header().as_ustar().is_none() {
                return Err(invalid("package requires bounded regular ustar entries"));
            }
            let path = String::from_utf8(entry.path_bytes().into_owned())
                .map_err(|_| invalid("package path is not UTF-8"))?;
            portable_path(&path)?;
            if path <= previous {
                return Err(invalid("package entries are duplicated or not sorted"));
            }
            previous = path.clone();
            budget.add(entry.size())?;
            // Store span lengths rather than offsets so replay can borrow the
            // remaining framing slice without arithmetic over payload sizes.
            let framing = state.framing.borrow().len();
            frame_ends.push(framing - captured_framing);
            captured_framing = framing;
            // Let actual reads drive growth even for a large legal declaration.
            let mut content = Vec::new();
            state.capture.set(false);
            let read = entry.read_to_end(&mut content);
            state.capture.set(true);
            read?;
            entries.insert(path, content);
        }
        Ok(())
    })();
    if state.input_failed.get() {
        // An underlying IO failure ends the original read. Do not retry it
        // while draining and accidentally replace the first physical error.
        return match parsed {
            Err(error) => Err(error),
            Ok(()) => Err(invalid("package input read failed")),
        };
    }
    // tar stops at its first EOF block. Read every remaining input byte within
    // the same envelope so extra zeros, late nonzero data and growth fail the
    // exact comparison. Do this even after a parse error, preserving the old
    // bounded capture's input-size/IO precedence without retaining raw input.
    let trailing = io::copy(&mut archive.into_inner(), &mut io::sink());
    if state.bytes.get() > maximum {
        return Err(invalid("package exceeds 512 MiB"));
    }
    trailing?;
    parsed?;
    let noncanonical = || invalid("package headers/padding are not deterministic ustar");
    if state.framing_overflow.get() {
        return Err(noncanonical());
    }
    let framing = state.framing.into_inner();
    let snapshot = SnapshotReplay {
        framing: &framing,
        frame_ends: frame_ends.iter(),
        contents: entries.values(),
        pending: &[],
        payload_next: false,
        ended: false,
    };
    // Keep the sink borrowed across a writer error so its mismatch reason is
    // distinguishable from an actual writer/reader IO error.
    let mut comparison = ComparingWriter {
        original: snapshot,
        mismatch: Cell::new(false),
    };
    let canonical = archive_into(&entries, &mut comparison).map(|_| ());
    if comparison.mismatch.get() {
        return Err(noncanonical());
    }
    canonical?;
    if comparison.original.read(&mut [0u8; 1])? != 0 {
        return Err(noncanonical());
    }
    let raw = entries
        .remove("pack.json")
        .ok_or_else(|| invalid("package has no pack.json"))?;
    let signature = entries.remove("signature.json");
    verify(raw, signature, entries, publishers)
}

/// Verify an untrusted package in memory; extraction never runs before admission.
pub fn verify_tpk(
    bytes: &[u8],
    publishers: &BTreeMap<String, String>,
) -> Result<VerifiedPack, ManifestError> {
    verify_tpk_reader(
        &mut Cursor::new(bytes),
        bytes.len() as u64,
        MAX_PACKAGE_BYTES,
        publishers,
    )
}

#[cfg(test)]
mod budget_tests {
    use super::*;

    #[test]
    fn ordinary_ustar_accounting_matches_the_existing_writer() {
        let entries = BTreeMap::from([
            ("a".into(), vec![]),
            ("b".into(), vec![0; 1]),
            ("c".into(), vec![0; 512]),
            ("d".into(), vec![0; 513]),
        ]);
        let mut budget = PackageBudget::new();
        for content in entries.values() {
            budget.add(content.len() as u64).unwrap();
        }
        assert_eq!(budget.bytes, 5120);
        assert_eq!(archive(&entries).unwrap().len() as u64, budget.bytes);
    }

    #[test]
    fn canonical_writer_handles_partial_writes_and_interrupted_sinks() {
        #[derive(Default)]
        struct Choppy {
            bytes: Vec<u8>,
            calls: usize,
        }
        impl Write for Choppy {
            fn write(&mut self, input: &[u8]) -> io::Result<usize> {
                self.calls += 1;
                if self.calls.is_multiple_of(3) {
                    return Err(io::ErrorKind::Interrupted.into());
                }
                let count = input.len().min(7);
                self.bytes.extend_from_slice(&input[..count]);
                Ok(count)
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }
        let entries = BTreeMap::from([("a.txt".into(), vec![42; 8193])]);
        let canonical = archive(&entries).unwrap();
        assert_eq!(
            archive_into(&entries, Choppy::default()).unwrap().bytes,
            canonical
        );
    }

    #[test]
    fn aggregate_boundary_includes_all_headers_padding_and_eof_without_big_buffers() {
        let mut budget = PackageBudget::new();
        for _ in 0..3 {
            budget.add(MAX_PACKAGE_MEMBER_BYTES).unwrap();
        }
        let last = MAX_PACKAGE_MEMBER_BYTES - 4 * TAR_BLOCK_BYTES - TAR_END_BYTES;
        assert_eq!(budget.remaining_member_limit().unwrap(), last);
        assert!(budget.add(last + 1).is_err());
        budget.add(last).unwrap();
        assert_eq!(budget.bytes, MAX_PACKAGE_BYTES);
        assert!(budget.add(0).is_err());
        assert!(budget.remaining_member_limit().is_err());
    }

    #[test]
    fn member_count_and_checked_arithmetic_have_finite_limits() {
        let mut budget = PackageBudget::new();
        assert!(budget.add(MAX_PACKAGE_MEMBER_BYTES + 1).is_err());
        for _ in 0..MAX_PACKAGE_ENTRIES {
            budget.add(0).unwrap();
        }
        assert_eq!(budget.entries, MAX_PACKAGE_ENTRIES);
        assert!(budget.add(0).is_err());
        assert!(budget.remaining_member_limit().is_err());
        assert!(check_entry_count(MAX_PACKAGE_ENTRIES - 1, false).is_ok());
        assert!(check_entry_count(MAX_PACKAGE_ENTRIES - 1, true).is_err());
        assert!(check_entry_count(usize::MAX, true).is_err());
        let mut overflow = PackageBudget {
            entries: 0,
            bytes: u64::MAX,
        };
        assert!(overflow.add(0).is_err());
        assert!(overflow.remaining_member_limit().is_err());
    }
}

#[cfg(test)]
mod file_tests {
    use super::*;

    #[test]
    fn truncated_large_declared_member_is_rejected_from_a_tiny_input() {
        let mut header = tar::Header::new_ustar();
        header.set_path("a.txt").unwrap();
        header.set_size(MAX_PACKAGE_MEMBER_BYTES);
        header.set_mode(0o644);
        header.set_cksum();
        // A legal large declaration provides no permission to reserve its
        // payload capacity. This fixture contains only three actual bytes.
        let mut bytes = header.as_bytes().to_vec();
        bytes.extend_from_slice(b"abc");
        assert!(verify_tpk(&bytes, &BTreeMap::new()).is_err());
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("truncated-large-member.tpk");
        fs::write(&path, &bytes).unwrap();
        assert!(verify_tpk_file(&path, &BTreeMap::new()).is_err());
    }

    #[test]
    fn oversized_metadata_rejects_before_requesting_any_content_bytes() {
        struct Unreadable;
        impl Read for Unreadable {
            fn read(&mut self, _output: &mut [u8]) -> std::io::Result<usize> {
                panic!("oversized package must be rejected before any read")
            }
        }
        let error = verify_tpk_reader(
            &mut Unreadable,
            MAX_PACKAGE_BYTES + 1,
            MAX_PACKAGE_BYTES,
            &BTreeMap::new(),
        )
        .unwrap_err();
        assert!(error.to_string().contains("package exceeds 512 MiB"));
    }

    #[test]
    fn physical_read_error_is_preserved_without_retrying_during_tail_drain() {
        struct Failed(bool);
        impl Read for Failed {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                assert!(!self.0, "a failed physical read must not be retried");
                self.0 = true;
                Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "original read failure",
                ))
            }
        }
        let error = verify_tpk_reader(&mut Failed(false), 0, MAX_PACKAGE_BYTES, &BTreeMap::new())
            .unwrap_err();
        assert!(
            matches!(&error, ManifestError::Io(error) if error.kind() == io::ErrorKind::PermissionDenied)
        );
        assert!(error.to_string().contains("original read failure"));
    }

    #[test]
    fn input_envelope_accepts_the_exact_limit_and_stops_growth_at_one_extra_byte() {
        let expected = vec![42; 8];
        let state = InputSnapshot::default();
        let mut input = SnapshotInput {
            input: Cursor::new(&expected),
            state: &state,
            maximum: 8,
        };
        let mut captured = Vec::new();
        input.read_to_end(&mut captured).unwrap();
        assert_eq!(captured, expected);
        assert_eq!(state.bytes.get(), 8);

        // This reader never reaches EOF, as a growing file could. A tiny limit
        // exercises the production cap without allocating a large package.
        struct GrowingReader {
            consumed: usize,
        }
        impl Read for GrowingReader {
            fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
                output.fill(42);
                self.consumed += output.len();
                Ok(output.len())
            }
        }
        let mut reader = GrowingReader { consumed: 0 };
        let error = verify_tpk_reader(&mut reader, 0, 8, &BTreeMap::new()).unwrap_err();
        assert!(error.to_string().contains("package exceeds 512 MiB"));
        assert_eq!(reader.consumed, 9);
    }

    fn sample(
        payload: &[u8],
        signed: bool,
    ) -> (Vec<u8>, BTreeMap<String, Vec<u8>>, BTreeMap<String, String>) {
        let files: BTreeMap<String, Vec<u8>> = BTreeMap::from([
            ("a.txt".into(), payload.to_vec()),
            ("z-empty".into(), Vec::new()),
        ]);
        let signer = SigningKey::from_bytes(&[7; 32]);
        let public = format!(
            "ed25519:{}",
            URL_SAFE_NO_PAD.encode(signer.verifying_key().to_bytes())
        );
        let manifest = PackManifest {
            format: "tobkiri.pack/1".into(),
            id: PackId::parse(if signed {
                "acme.streaming"
            } else {
                "local.streaming"
            })
            .unwrap(),
            version: "1.0.0".into(),
            publisher_key: signed.then(|| public.clone()),
            runtime: RuntimeSpec::Data,
            shapes: vec![],
            ports: Ports::default(),
            inputs: vec![],
            flows: vec![],
            overlays: vec![],
            stores: vec![],
            permissions: tobkiri_gate::PermissionSet::default(),
            ui: None,
            files: files
                .iter()
                .map(|(path, content)| FileEntry {
                    path: path.clone(),
                    sha256: hash_bytes(content),
                    size: content.len() as u64,
                })
                .collect(),
        };
        let signature = signed.then(|| sign_manifest(&manifest, &signer).unwrap());
        let bytes = build_tpk(&manifest, &files, signature.as_ref()).unwrap();
        let publishers = if signed {
            BTreeMap::from([("acme".into(), public)])
        } else {
            BTreeMap::new()
        };
        (bytes, files, publishers)
    }

    #[test]
    fn file_and_forward_only_short_interrupted_reads_match_signed_and_unsigned_bytes() {
        struct Choppy<R> {
            input: R,
            calls: usize,
        }
        impl<R: Read> Read for Choppy<R> {
            fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
                self.calls += 1;
                if self.calls.is_multiple_of(3) {
                    return Err(io::ErrorKind::Interrupted.into());
                }
                let count = output.len().min(37);
                self.input.read(&mut output[..count])
            }
        }
        for signed in [false, true] {
            // Cross both the comparison sink's 8 KiB chunk and tar padding.
            let (bytes, files, publishers) = sample(&vec![42; 10_003], signed);
            let memory = verify_tpk(&bytes, &publishers).unwrap();
            let forward = verify_tpk_reader(
                &mut Choppy {
                    input: Cursor::new(&bytes),
                    calls: 0,
                },
                0,
                bytes.len() as u64,
                &publishers,
            )
            .unwrap();
            let root = tempfile::tempdir().unwrap();
            let path = root.path().join("sample.tpk");
            fs::write(&path, &bytes).unwrap();
            let disk = verify_tpk_file(&path, &publishers).unwrap();
            assert_eq!(memory.files, files);
            assert_eq!(forward.files, files);
            assert_eq!(disk.files, files);
            assert_eq!(memory.hash, forward.hash);
            assert_eq!(memory.hash, disk.hash);
            assert_eq!(
                canonical_manifest(&disk.manifest).unwrap(),
                canonical_manifest(&memory.manifest).unwrap()
            );
            let error = verify_tpk_reader(
                &mut Cursor::new(&bytes),
                0,
                bytes.len() as u64 - 1,
                &publishers,
            )
            .unwrap_err();
            assert!(error.to_string().contains("package exceeds 512 MiB"));
        }
    }

    #[test]
    fn original_header_padding_and_exact_eof_are_compared_without_normalization() {
        let (bytes, _, publishers) = sample(b"hello", false);
        let mut changed_header = bytes.clone();
        let mut header = tar::Header::new_ustar();
        header
            .as_mut_bytes()
            .copy_from_slice(&changed_header[..512]);
        header.set_mode(0o755);
        header.set_cksum();
        changed_header[..512].copy_from_slice(header.as_bytes());
        let mut changed_padding = bytes.clone();
        changed_padding[512 + 5] = 1;
        let mut changed_end = bytes.clone();
        *changed_end.last_mut().unwrap() = 1;
        let mut extra_zeros = bytes.clone();
        extra_zeros.extend_from_slice(&[0; 512]);
        let mut extra_nonzero = bytes.clone();
        extra_nonzero.push(1);
        let mut truncated = bytes;
        truncated.pop();
        for malformed in [
            changed_header,
            changed_padding,
            changed_end,
            extra_zeros,
            extra_nonzero,
            truncated,
        ] {
            let memory = verify_tpk(&malformed, &publishers).unwrap_err();
            assert!(
                memory
                    .to_string()
                    .contains("headers/padding are not deterministic ustar")
            );
            let root = tempfile::tempdir().unwrap();
            let path = root.path().join("malformed.tpk");
            fs::write(&path, &malformed).unwrap();
            let disk = verify_tpk_file(&path, &publishers).unwrap_err();
            assert_eq!(memory.to_string(), disk.to_string());
        }
    }

    #[test]
    fn same_inode_rewrite_cannot_rebind_extracted_payloads_to_a_later_manifest() {
        struct Rewrite {
            input: fs::File,
            path: std::path::PathBuf,
            replacement: Vec<u8>,
            read: usize,
            changed: bool,
        }
        impl Read for Rewrite {
            fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
                let limit = output.len().min(31);
                let count = self.input.read(&mut output[..limit])?;
                self.read += count;
                if self.read > 512 && !self.changed {
                    // Truncate/overwrite the same inode after part of a.txt
                    // was extracted. Its later manifest describes only B.
                    fs::write(&self.path, &self.replacement)?;
                    self.changed = true;
                }
                Ok(count)
            }
        }
        let (first, _, publishers) = sample(&vec![b'A'; 10_003], false);
        let (second, _, _) = sample(&vec![b'B'; 10_003], false);
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("changing.tpk");
        fs::write(&path, &first).unwrap();
        let input = fs::File::open(&path).unwrap();
        let mut changing = Rewrite {
            input,
            path,
            replacement: second,
            read: 0,
            changed: false,
        };
        let error = verify_tpk_reader(
            &mut changing,
            first.len() as u64,
            MAX_PACKAGE_BYTES,
            &publishers,
        )
        .unwrap_err();
        assert!(changing.changed);
        assert!(
            error
                .to_string()
                .contains("file digest/size mismatch: a.txt")
        );
    }

    #[test]
    fn sparse_oversized_package_is_rejected_before_tar_parsing_or_content_capture() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("oversized.tpk");
        let file = fs::File::create(&path).unwrap();
        file.set_len(MAX_PACKAGE_BYTES + 1).unwrap();
        // The sparse file has no tar header. Rejection must come from the
        // opened-file size check, before reading its large logical contents.
        let error = verify_tpk_file(&path, &BTreeMap::new()).unwrap_err();
        assert!(error.to_string().contains("package exceeds 512 MiB"));
    }

    #[cfg(unix)]
    #[test]
    fn fifo_package_is_rejected_without_waiting_for_a_writer() {
        use std::{ffi::CString, os::unix::ffi::OsStrExt};

        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("pipe.tpk");
        let name = CString::new(path.as_os_str().as_bytes()).unwrap();
        // SAFETY: name is a NUL-terminated temporary path with no existing file.
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let error = verify_tpk_file(&path, &BTreeMap::new()).unwrap_err();
        assert!(error.to_string().contains("not a regular file"));
    }
}
