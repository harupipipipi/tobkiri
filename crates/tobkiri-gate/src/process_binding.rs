//! Native process-launch commitments (issue1520, first slice).
//!
//! This module only generates and verifies *commitments* to a prepared
//! native launch. It never launches a process, opens a file descriptor,
//! allocates a PTY, grants an approval, or proves cleanup: `proc`/`pty`
//! remain `Unavailable` until later core work wires real execution.
//! Before that, core must retain the verified executable/cwd descriptors
//! it opened, route launches through the dedicated Gate approval entry,
//! and reject preapproval of bare-program executable requests.
//!
//! * [`ProcessLaunchKey`] is a random per-process HMAC key owned only by
//!   trusted core; there is no guest-key self-validation entry. After a
//!   restart the key is gone, so stale launch snapshots must never be
//!   reused or reapproved: durable recovery re-derives launches from
//!   recorded `args_digest`/terminal/outbox facts, never the old key.
//! * [`ProcessLaunchSnapshot`] borrows its inputs. `argv`/`env` may hold
//!   secrets and are streamed through HMAC exactly once - never copied,
//!   retained, or displayed. Trusted ids/labels stay inline and bounded
//!   inside [`ProcessLaunchBinding`].
//! * [`NativeIdentity`] is a supplied *fact*, not a proven descriptor
//!   capability; a pathname, raw fd integer, or resource-table index is
//!   never acceptable evidence. Later core must open, verify, and retain
//!   the real descriptor itself.

use crate::{ResourceLimits, Risk};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::fmt;
use tobkiri_canon::Digest;
use zeroize::Zeroize;

/// Fixed commitment domain; distinct from every other HMAC use.
const DOMAIN: &[u8] = b"tobkiri-gate/process-launch-binding/v1";
/// Commitment preimage layout version.
const ENCODING_VERSION: u16 = 1;
/// Process launch key format version.
const KEY_VERSION: u16 = 1;
/// Byte length of the secret HMAC key.
const KEY_BYTES: usize = 32;
/// Byte length of the non-secret key identity.
const KEY_IDENTITY_BYTES: usize = 16;

/// Maximum argv entries in one committed launch.
pub const MAX_ARGV_ENTRIES: usize = 256;
/// Maximum env pairs in one committed launch.
pub const MAX_ENV_ENTRIES: usize = 256;
/// Maximum byte length of any single argv/env string.
pub const MAX_STRING_BYTES: usize = 64 * 1024;
/// Maximum combined raw bytes of all argv and env strings.
pub const MAX_TOTAL_BYTES: usize = 1024 * 1024;
/// Maximum byte length of a native identity payload.
pub const MAX_IDENTITY_BYTES: usize = 256;
/// Maximum byte length of a native identity method name.
pub const MAX_METHOD_BYTES: usize = 64;
/// Maximum byte length of a permission/catalog program id.
pub const MAX_PROGRAM_ID_BYTES: usize = 256;
/// Maximum byte length of each trusted display label.
pub const MAX_LABEL_BYTES: usize = 128;

type LaunchMac = Hmac<Sha256>;

/// Closed failure set for launch commitments. Every variant is a fixed
/// code plus message; raw input bytes are never echoed back.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum LaunchBindingError {
    /// Program id is empty, over-long, or holds control characters.
    #[error("program id is empty, over-long, or contains control characters")]
    ProgramId,
    /// A trusted display label is empty, over-long, or holds controls.
    #[error("display label is empty, over-long, or contains control characters")]
    Label,
    /// More than [`MAX_ARGV_ENTRIES`] argv entries were supplied.
    #[error("too many argv entries")]
    ArgvCount,
    /// More than [`MAX_ENV_ENTRIES`] env pairs were supplied.
    #[error("too many env pairs")]
    EnvCount,
    /// A single argv/env string exceeds [`MAX_STRING_BYTES`].
    #[error("argv/env string exceeds the per-string limit")]
    StringBytes,
    /// Combined argv/env bytes exceed [`MAX_TOTAL_BYTES`].
    #[error("combined argv/env bytes exceed the total limit")]
    TotalBytes,
    /// Env name is empty or contains `=` or NUL.
    #[error("env name is empty or contains '=' or NUL")]
    EnvName,
    /// An argv/env value contains a NUL byte.
    #[error("argv/env value contains a NUL byte")]
    NulByte,
    /// A PTY dimension is zero.
    #[error("pty dimensions must be non-zero")]
    PtyDimensions,
    /// Identity method is empty, over-long, or not safe ASCII.
    #[error("identity method is empty, over-long, or not safe ASCII")]
    IdentityMethod,
    /// Identity version must be non-zero.
    #[error("identity version must be non-zero")]
    IdentityVersion,
    /// Identity payload is empty or over-long.
    #[error("identity bytes are empty or over-long")]
    IdentityBytes,
    /// Operating system randomness could not be drawn.
    #[error("operating system randomness failed")]
    Entropy,
    /// Commitment primitive failure.
    #[error("commitment primitive failure")]
    Internal,
    /// Binding does not match this key and snapshot.
    #[error("binding does not match this key and snapshot")]
    Mismatch,
}

impl LaunchBindingError {
    /// Stable machine-readable code for this failure.
    pub const fn code(self) -> &'static str {
        match self {
            Self::ProgramId => "LAUNCH_PROGRAM_ID",
            Self::Label => "LAUNCH_LABEL",
            Self::ArgvCount => "LAUNCH_ARGV_COUNT",
            Self::EnvCount => "LAUNCH_ENV_COUNT",
            Self::StringBytes => "LAUNCH_STRING_BYTES",
            Self::TotalBytes => "LAUNCH_TOTAL_BYTES",
            Self::EnvName => "LAUNCH_ENV_NAME",
            Self::NulByte => "LAUNCH_NUL",
            Self::PtyDimensions => "LAUNCH_PTY",
            Self::IdentityMethod => "LAUNCH_IDENTITY_METHOD",
            Self::IdentityVersion => "LAUNCH_IDENTITY_VERSION",
            Self::IdentityBytes => "LAUNCH_IDENTITY_BYTES",
            Self::Entropy => "LAUNCH_ENTROPY",
            Self::Internal => "LAUNCH_INTERNAL",
            Self::Mismatch => "LAUNCH_MISMATCH",
        }
    }
}

/// Field tags of the commitment preimage. Every item is emitted as
/// `tag || len_be64 || bytes`, a tagged fixed-width big-endian integer,
/// or an explicit tag opening a fixed-order record/sequence, so no two
/// distinct snapshots can share a preimage.
mod t {
    pub const DOMAIN: u8 = 0x01;
    pub const ENCODING_VERSION: u8 = 0x02;
    pub const KEY_VERSION: u8 = 0x03;
    pub const KEY_IDENTITY: u8 = 0x04;
    pub const UNSANDBOXED: u8 = 0x05;
    pub const PROGRAM_ID: u8 = 0x06;
    pub const EXECUTABLE: u8 = 0x07;
    pub const EXECUTABLE_DIGEST: u8 = 0x08;
    pub const IDENTITY_METHOD: u8 = 0x09;
    pub const IDENTITY_VERSION: u8 = 0x0a;
    pub const IDENTITY_BYTES: u8 = 0x0b;
    pub const ARGV: u8 = 0x0c;
    pub const ARGV_ITEM: u8 = 0x0d;
    pub const ENV: u8 = 0x0e;
    pub const ENV_PAIR: u8 = 0x0f;
    pub const ENV_NAME: u8 = 0x10;
    pub const ENV_VALUE: u8 = 0x11;
    pub const CWD: u8 = 0x12;
    pub const PTY: u8 = 0x13;
    pub const PTY_COLS: u8 = 0x14;
    pub const PTY_ROWS: u8 = 0x15;
    pub const LIMITS: u8 = 0x16;
    pub const LIMIT_FUEL: u8 = 0x17;
    pub const LIMIT_MEMORY: u8 = 0x18;
    pub const LIMIT_WALL: u8 = 0x19;
    pub const LIMIT_TABLE: u8 = 0x1a;
    pub const LIMIT_OUTPUT: u8 = 0x1b;
    pub const CATALOG_LABEL: u8 = 0x1c;
    pub const CWD_LABEL: u8 = 0x1d;
}

/// Append `tag || len_be64 || data` to the running MAC.
fn field(mac: &mut LaunchMac, tag: u8, data: &[u8]) {
    mac.update(&[tag]);
    mac.update(&(data.len() as u64).to_be_bytes());
    mac.update(data);
}
/// Append `tag || value` for fixed-width big-endian integers.
fn number8(mac: &mut LaunchMac, tag: u8, value: u8) {
    mac.update(&[tag, value]);
}
fn number16(mac: &mut LaunchMac, tag: u8, value: u16) {
    mac.update(&[tag]);
    mac.update(&value.to_be_bytes());
}
fn number32(mac: &mut LaunchMac, tag: u8, value: u32) {
    mac.update(&[tag]);
    mac.update(&value.to_be_bytes());
}
fn number64(mac: &mut LaunchMac, tag: u8, value: u64) {
    mac.update(&[tag]);
    mac.update(&value.to_be_bytes());
}
/// Method, version and payload under the enclosing role tag.
fn encode_identity(mac: &mut LaunchMac, identity: &NativeIdentity) {
    field(mac, t::IDENTITY_METHOD, identity.method().as_bytes());
    number64(mac, t::IDENTITY_VERSION, identity.version());
    field(mac, t::IDENTITY_BYTES, identity.bytes());
}

/// Stream the canonical preimage through HMAC. Secrets are borrowed for
/// the duration of the update calls only; nothing is copied into an
/// aggregate buffer.
fn encode(
    key: &ProcessLaunchKey,
    snapshot: &ProcessLaunchSnapshot<'_>,
) -> Result<LaunchMac, LaunchBindingError> {
    let mut mac =
        Hmac::<Sha256>::new_from_slice(&key.secret).map_err(|_| LaunchBindingError::Internal)?;
    field(&mut mac, t::DOMAIN, DOMAIN);
    number16(&mut mac, t::ENCODING_VERSION, ENCODING_VERSION);
    number16(&mut mac, t::KEY_VERSION, key.key_version());
    field(&mut mac, t::KEY_IDENTITY, &key.identity);
    number8(&mut mac, t::UNSANDBOXED, 1);
    field(&mut mac, t::PROGRAM_ID, snapshot.program_id.as_bytes());
    mac.update(&[t::EXECUTABLE]);
    field(
        &mut mac,
        t::EXECUTABLE_DIGEST,
        snapshot.executable_digest.as_str().as_bytes(),
    );
    encode_identity(&mut mac, snapshot.executable_identity);
    mac.update(&[t::ARGV]);
    mac.update(&(snapshot.argv.len() as u32).to_be_bytes());
    for arg in snapshot.argv {
        field(&mut mac, t::ARGV_ITEM, arg.as_bytes());
    }
    mac.update(&[t::ENV]);
    mac.update(&(snapshot.env.len() as u32).to_be_bytes());
    for (name, value) in snapshot.env {
        mac.update(&[t::ENV_PAIR]);
        field(&mut mac, t::ENV_NAME, name.as_bytes());
        field(&mut mac, t::ENV_VALUE, value.as_bytes());
    }
    mac.update(&[t::CWD]);
    encode_identity(&mut mac, snapshot.cwd_identity);
    mac.update(&[t::PTY]);
    match snapshot.pty {
        None => mac.update(&[0]),
        Some(pty) => {
            mac.update(&[1]);
            number16(&mut mac, t::PTY_COLS, pty.cols());
            number16(&mut mac, t::PTY_ROWS, pty.rows());
        }
    }
    mac.update(&[t::LIMITS]);
    number64(&mut mac, t::LIMIT_FUEL, snapshot.limits.fuel_per_call);
    number64(&mut mac, t::LIMIT_MEMORY, snapshot.limits.memory_mib);
    number64(&mut mac, t::LIMIT_WALL, snapshot.limits.wall_ms);
    number32(&mut mac, t::LIMIT_TABLE, snapshot.limits.table_elements);
    number64(&mut mac, t::LIMIT_OUTPUT, snapshot.limits.output_bytes);
    field(
        &mut mac,
        t::CATALOG_LABEL,
        snapshot.catalog_label.as_bytes(),
    );
    field(&mut mac, t::CWD_LABEL, snapshot.cwd_label.as_bytes());
    Ok(mac)
}

/// Render a raw HMAC output as a stack-fixed `sha256:hex` digest - the MAC
/// itself becomes the digest, it is never hashed a second time.
fn mac_digest(mac: [u8; 32]) -> Digest {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut text = [0u8; 71];
    text[..7].copy_from_slice(b"sha256:");
    for (index, byte) in mac.iter().enumerate() {
        text[7 + index * 2] = HEX[usize::from(byte >> 4)];
        text[8 + index * 2] = HEX[usize::from(byte & 15)];
    }
    // `text` is always ASCII "sha256:hex", so both conversions succeed.
    let encoded = std::str::from_utf8(&text).expect("sha256:hex text is always ASCII");
    Digest::parse(encoded).expect("sha256:hex encoding is always a valid digest")
}
/// Recover the raw HMAC bytes committed inside a digest for `verify_slice`.
fn digest_mac_bytes(digest: &Digest) -> [u8; 32] {
    fn nibble(byte: u8) -> u8 {
        match byte {
            b'0'..=b'9' => byte - b'0',
            _ => byte - b'a' + 10,
        }
    }
    let hex = digest.hex().as_bytes();
    let mut out = [0u8; 32];
    for (index, slot) in out.iter_mut().enumerate() {
        *slot = (nibble(hex[index * 2]) << 4) | nibble(hex[index * 2 + 1]);
    }
    out
}

/// Reject displayed metadata that is empty, over-long, or holds
/// control characters (NUL/ESC included). No truncation, no fallback.
fn check_metadata(
    text: &str,
    max: usize,
    error: LaunchBindingError,
) -> Result<(), LaunchBindingError> {
    if text.is_empty() || text.len() > max || text.chars().any(char::is_control) {
        return Err(error);
    }
    Ok(())
}

/// Check every cap and character rule before any byte is committed.
/// Arithmetic is checked; oversized or malformed input is rejected
/// outright rather than truncated or normalized.
fn validate_snapshot(snapshot: &ProcessLaunchSnapshot<'_>) -> Result<(), LaunchBindingError> {
    check_metadata(
        snapshot.program_id,
        MAX_PROGRAM_ID_BYTES,
        LaunchBindingError::ProgramId,
    )?;
    check_metadata(
        snapshot.catalog_label,
        MAX_LABEL_BYTES,
        LaunchBindingError::Label,
    )?;
    check_metadata(
        snapshot.cwd_label,
        MAX_LABEL_BYTES,
        LaunchBindingError::Label,
    )?;
    if snapshot.argv.len() > MAX_ARGV_ENTRIES {
        return Err(LaunchBindingError::ArgvCount);
    }
    if snapshot.env.len() > MAX_ENV_ENTRIES {
        return Err(LaunchBindingError::EnvCount);
    }
    let mut total: usize = 0;
    for arg in snapshot.argv {
        let bytes = arg.as_bytes();
        if bytes.len() > MAX_STRING_BYTES {
            return Err(LaunchBindingError::StringBytes);
        }
        if bytes.contains(&0) {
            return Err(LaunchBindingError::NulByte);
        }
        total = total
            .checked_add(bytes.len())
            .ok_or(LaunchBindingError::TotalBytes)?;
    }
    for (name, value) in snapshot.env {
        let name = name.as_bytes();
        let value = value.as_bytes();
        if name.len() > MAX_STRING_BYTES || value.len() > MAX_STRING_BYTES {
            return Err(LaunchBindingError::StringBytes);
        }
        if name.is_empty() || name.contains(&b'=') || name.contains(&0) {
            return Err(LaunchBindingError::EnvName);
        }
        if value.contains(&0) {
            return Err(LaunchBindingError::NulByte);
        }
        total = total
            .checked_add(name.len())
            .and_then(|sum| sum.checked_add(value.len()))
            .ok_or(LaunchBindingError::TotalBytes)?;
    }
    if total > MAX_TOTAL_BYTES {
        return Err(LaunchBindingError::TotalBytes);
    }
    Ok(())
}

/// Bounded inline storage for already-validated non-secret text.
/// Never heap-allocated: the binding keeps only these fixed slots.
#[derive(Clone)]
struct InlineText<const N: usize> {
    bytes: [u8; N],
    len: usize,
}

impl<const N: usize> InlineText<N> {
    /// Copy validated text into fixed storage. Callers maintain the
    /// `text.len() <= N` invariant via `validate_snapshot`.
    fn from_validated(text: &str) -> Self {
        debug_assert!(text.len() <= N);
        let mut bytes = [0u8; N];
        bytes[..text.len()].copy_from_slice(text.as_bytes());
        Self {
            bytes,
            len: text.len(),
        }
    }
    fn as_str(&self) -> &str {
        // Validated UTF-8 was copied in; empty is a safe fallback.
        std::str::from_utf8(&self.bytes[..self.len]).unwrap_or("")
    }
}

/// Non-secret random identity of a [`ProcessLaunchKey`] generation.
/// Safe to log, store and commit; distinguishes keys across restarts.
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct KeyIdentity([u8; KEY_IDENTITY_BYTES]);

impl KeyIdentity {
    /// Raw identity bytes.
    pub const fn as_bytes(&self) -> &[u8; KEY_IDENTITY_BYTES] {
        &self.0
    }
}

impl fmt::Display for KeyIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("keyid:")?;
        for byte in self.0 {
            write!(f, "{byte:02x}")?;
        }
        Ok(())
    }
}

impl fmt::Debug for KeyIdentity {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "KeyIdentity({self})")
    }
}

/// Bounded core-supplied identity fact for a native executable or cwd.
///
/// `method` names the platform scheme, `version` its nonzero revision
/// and `bytes` the opaque payload (never displayed). This is a supplied
/// claim committed into the binding, **not** a proven descriptor
/// capability: before real launch, core must open, verify and retain
/// the actual fd; a pathname, raw fd integer or resource-table index
/// is never acceptable evidence.
#[derive(Clone)]
pub struct NativeIdentity {
    method: InlineText<MAX_METHOD_BYTES>,
    version: u64,
    bytes: [u8; MAX_IDENTITY_BYTES],
    len: usize,
}

impl NativeIdentity {
    /// Build a validated identity fact: nonempty safe-ASCII `method` of
    /// at most [`MAX_METHOD_BYTES`], nonzero `version`, and 1..=
    /// [`MAX_IDENTITY_BYTES`] payload.
    pub fn new(method: &str, version: u64, bytes: &[u8]) -> Result<Self, LaunchBindingError> {
        if method.is_empty()
            || method.len() > MAX_METHOD_BYTES
            || !method.bytes().all(|b| (0x21..=0x7e).contains(&b))
        {
            return Err(LaunchBindingError::IdentityMethod);
        }
        if version == 0 {
            return Err(LaunchBindingError::IdentityVersion);
        }
        if bytes.is_empty() || bytes.len() > MAX_IDENTITY_BYTES {
            return Err(LaunchBindingError::IdentityBytes);
        }
        let mut stored = [0u8; MAX_IDENTITY_BYTES];
        stored[..bytes.len()].copy_from_slice(bytes);
        Ok(Self {
            method: InlineText::from_validated(method),
            version,
            bytes: stored,
            len: bytes.len(),
        })
    }
    /// Identity scheme name (bounded safe ASCII).
    pub fn method(&self) -> &str {
        self.method.as_str()
    }
    /// Identity scheme version (nonzero).
    pub const fn version(&self) -> u64 {
        self.version
    }
    /// Opaque payload; never rendered by `Debug` or `Display`.
    pub fn bytes(&self) -> &[u8] {
        &self.bytes[..self.len]
    }
}

impl fmt::Debug for NativeIdentity {
    /// Redacted: method, version and length only - never the payload.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("NativeIdentity")
            .field("method", &self.method())
            .field("version", &self.version)
            .field("len", &self.len)
            .finish_non_exhaustive()
    }
}

/// Validated PTY geometry; dimensions are always nonzero.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PtyDimensions {
    cols: u16,
    rows: u16,
}

impl PtyDimensions {
    /// Build dimensions; rejects zero columns or rows.
    pub fn new(cols: u16, rows: u16) -> Result<Self, LaunchBindingError> {
        if cols == 0 || rows == 0 {
            return Err(LaunchBindingError::PtyDimensions);
        }
        Ok(Self { cols, rows })
    }
    /// Terminal width in columns.
    pub const fn cols(&self) -> u16 {
        self.cols
    }
    /// Terminal height in rows.
    pub const fn rows(&self) -> u16 {
        self.rows
    }
}

/// Move-only per-process HMAC key for launch commitments.
///
/// Trusted core owns this key exclusively - there is no guest-key
/// self-validation entry. The 32-byte secret and the independent
/// 16-byte non-secret [`KeyIdentity`] are drawn separately via
/// `getrandom`; the secret is zeroized on drop, never cloned, and
/// never shown by `Debug` or errors. After a restart the key is gone:
/// stale snapshots must not be reused or reapproved, and durable
/// recovery re-derives launches from recorded `args_digest`/terminal/
/// outbox facts instead.
pub struct ProcessLaunchKey {
    secret: [u8; KEY_BYTES],
    identity: [u8; KEY_IDENTITY_BYTES],
}

impl ProcessLaunchKey {
    /// Draw a fresh secret and an independent random identity.
    pub fn generate() -> Result<Self, LaunchBindingError> {
        // Establish the move-only zeroizing owner before either fallible
        // entropy draw, including a draw that only partially fills its field.
        let mut key = Self {
            secret: [0u8; KEY_BYTES],
            identity: [0u8; KEY_IDENTITY_BYTES],
        };
        getrandom::fill(&mut key.secret).map_err(|_| LaunchBindingError::Entropy)?;
        getrandom::fill(&mut key.identity).map_err(|_| LaunchBindingError::Entropy)?;
        Ok(key)
    }
    /// Deterministic constructor for tests only.
    #[cfg(test)]
    fn testing(secret: [u8; KEY_BYTES], identity: [u8; KEY_IDENTITY_BYTES]) -> Self {
        Self { secret, identity }
    }
    /// Key format version committed into every binding.
    pub const fn key_version(&self) -> u16 {
        KEY_VERSION
    }
    /// Non-secret random identity of this key generation.
    pub const fn key_identity(&self) -> KeyIdentity {
        KeyIdentity(self.identity)
    }
}

impl Drop for ProcessLaunchKey {
    fn drop(&mut self) {
        self.secret.zeroize();
    }
}

impl fmt::Debug for ProcessLaunchKey {
    /// Shows only the non-secret identity and version - never key bytes.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProcessLaunchKey")
            .field("identity", &self.key_identity())
            .field("key_version", &self.key_version())
            .finish_non_exhaustive()
    }
}

/// Borrowed view of everything committed into a [`ProcessLaunchBinding`].
///
/// All fields are core-trusted facts: `program_id` is a permission/
/// catalog program id and `catalog_label`/`cwd_label` are core-generated
/// display labels - guest text is never trusted, and name/length checks
/// do not make a guest label trusted. `argv`/`env` may contain secrets:
/// they are borrowed only while the streaming HMAC runs and are never
/// copied, retained or displayed. `cwd_identity` is required; when a
/// guest supplies no cwd, core must provision and pin an empty cwd and
/// commit its identity here. Intentionally has no `Debug` impl.
pub struct ProcessLaunchSnapshot<'a> {
    /// Permission/catalog program id being launched.
    pub program_id: &'a str,
    /// Canonical digest of the actual executable.
    pub executable_digest: &'a Digest,
    /// Native identity of the actual executable (supplied fact).
    pub executable_identity: &'a NativeIdentity,
    /// Ordered argv, exactly as it will be exec'd.
    pub argv: &'a [String],
    /// Ordered env pairs, exactly as they will be installed.
    pub env: &'a [(String, String)],
    /// Native identity of the pinned working directory (supplied fact).
    pub cwd_identity: &'a NativeIdentity,
    /// Optional PTY geometry.
    pub pty: Option<PtyDimensions>,
    /// Exact limits committed for this launch.
    pub limits: &'a ResourceLimits,
    /// Trusted catalog display label (non-secret, bounded).
    pub catalog_label: &'a str,
    /// Trusted cwd display label (non-secret, bounded).
    pub cwd_label: &'a str,
}

/// Opaque commitment to a prepared native launch.
///
/// Fixed inline bounded metadata only - no `Vec`/`String`/`Box`/`Arc`
/// and no heap allocation on the binding path; the full prepared
/// snapshot is retained and precharged by core. No deserialization or
/// other construction path exists, so a pack can never forge one.
/// `args_digest` is the durable handle used by later record/terminal/
/// outbox recovery; it cannot be reverified after restart because the
/// process key is gone.
pub struct ProcessLaunchBinding {
    args_digest: Digest,
    encoding_version: u16,
    key_version: u16,
    key_identity: KeyIdentity,
    program_id: InlineText<MAX_PROGRAM_ID_BYTES>,
    catalog_label: InlineText<MAX_LABEL_BYTES>,
    cwd_label: InlineText<MAX_LABEL_BYTES>,
    argv_count: usize,
    env_count: usize,
    pty: Option<PtyDimensions>,
    limits: ResourceLimits,
    risk: Risk,
}

impl ProcessLaunchBinding {
    /// Commit `snapshot` under `key`. `argv`/`env` secrets are borrowed
    /// only for the streaming HMAC and are never retained.
    pub fn commit(
        key: &ProcessLaunchKey,
        snapshot: &ProcessLaunchSnapshot<'_>,
    ) -> Result<Self, LaunchBindingError> {
        validate_snapshot(snapshot)?;
        let mac = encode(key, snapshot)?;
        Ok(Self {
            args_digest: mac_digest(mac.finalize().into_bytes().into()),
            encoding_version: ENCODING_VERSION,
            key_version: key.key_version(),
            key_identity: key.key_identity(),
            program_id: InlineText::from_validated(snapshot.program_id),
            catalog_label: InlineText::from_validated(snapshot.catalog_label),
            cwd_label: InlineText::from_validated(snapshot.cwd_label),
            argv_count: snapshot.argv.len(),
            env_count: snapshot.env.len(),
            pty: snapshot.pty,
            limits: snapshot.limits.clone(),
            risk: Risk::Dangerous,
        })
    }
    /// Recompute the commitment under `key` and compare in constant
    /// time. Fails for a foreign key/identity/version or any changed
    /// input.
    pub fn verify(
        &self,
        key: &ProcessLaunchKey,
        snapshot: &ProcessLaunchSnapshot<'_>,
    ) -> Result<(), LaunchBindingError> {
        if self.encoding_version != ENCODING_VERSION
            || self.key_version != key.key_version()
            || self.key_identity != key.key_identity()
        {
            return Err(LaunchBindingError::Mismatch);
        }
        validate_snapshot(snapshot)?;
        encode(key, snapshot)?
            .verify_slice(&digest_mac_bytes(&self.args_digest))
            .map_err(|_| LaunchBindingError::Mismatch)
    }
    /// Durable `sha256:` digest holding the HMAC commitment itself.
    pub const fn args_digest(&self) -> &Digest {
        &self.args_digest
    }
    /// Commitment preimage layout version.
    pub const fn encoding_version(&self) -> u16 {
        self.encoding_version
    }
    /// Key format version this binding was committed under.
    pub const fn key_version(&self) -> u16 {
        self.key_version
    }
    /// Non-secret identity of the committing key generation.
    pub const fn key_identity(&self) -> KeyIdentity {
        self.key_identity
    }
    /// Trusted permission/catalog program id (bounded inline copy).
    pub fn program_id(&self) -> &str {
        self.program_id.as_str()
    }
    /// Trusted catalog display label (bounded inline copy).
    pub fn catalog_label(&self) -> &str {
        self.catalog_label.as_str()
    }
    /// Trusted cwd display label (bounded inline copy).
    pub fn cwd_label(&self) -> &str {
        self.cwd_label.as_str()
    }
    /// Number of committed argv entries.
    pub const fn argv_count(&self) -> usize {
        self.argv_count
    }
    /// Number of committed env pairs.
    pub const fn env_count(&self) -> usize {
        self.env_count
    }
    /// Committed PTY geometry, if any.
    pub const fn pty(&self) -> Option<PtyDimensions> {
        self.pty
    }
    /// Exact committed limits.
    pub const fn limits(&self) -> &ResourceLimits {
        &self.limits
    }
    /// Always unsandboxed [`Risk::Dangerous`]; this binding does not
    /// imply a universal jail or RSS enforcement.
    pub const fn risk(&self) -> Risk {
        self.risk
    }
    /// Whether the launch is unsandboxed - always true in this slice.
    pub const fn unsandboxed(&self) -> bool {
        true
    }
}

impl fmt::Debug for ProcessLaunchBinding {
    /// Bounded metadata only - never argv/env or identity bytes.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProcessLaunchBinding")
            .field("args_digest", &self.args_digest.as_str())
            .field("encoding_version", &self.encoding_version)
            .field("key_version", &self.key_version)
            .field("key_identity", &self.key_identity)
            .field("program_id", &self.program_id())
            .field("catalog_label", &self.catalog_label())
            .field("cwd_label", &self.cwd_label())
            .field("argv_count", &self.argv_count)
            .field("env_count", &self.env_count)
            .field("pty", &self.pty)
            .field("limits", &self.limits)
            .field("risk", &self.risk)
            .finish_non_exhaustive()
    }
}

impl fmt::Display for ProcessLaunchBinding {
    /// Redacted bounded summary for logs/audit: ids, counts, geometry,
    /// limits and the explicit unsandboxed flag - never argv/env values
    /// or raw identity bytes.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "process-launch {} prog={} argv={} env={}",
            self.args_digest.hex(),
            self.program_id(),
            self.argv_count,
            self.env_count
        )?;
        match &self.pty {
            Some(pty) => write!(f, " pty={}x{}", pty.cols(), pty.rows())?,
            None => f.write_str(" pty=none")?,
        }
        write!(
            f,
            " limits=fuel:{},mem_mib:{},wall_ms:{},table:{},out:{} unsandboxed risk={:?} key={}",
            self.limits.fuel_per_call,
            self.limits.memory_mib,
            self.limits.wall_ms,
            self.limits.table_elements,
            self.limits.output_bytes,
            self.risk,
            self.key_identity
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: [u8; KEY_BYTES] = [0xC3; KEY_BYTES];
    const IDENTITY_A: [u8; KEY_IDENTITY_BYTES] = [0x11; KEY_IDENTITY_BYTES];
    const IDENTITY_B: [u8; KEY_IDENTITY_BYTES] = [0x22; KEY_IDENTITY_BYTES];

    fn key_a() -> ProcessLaunchKey {
        ProcessLaunchKey::testing(SECRET, IDENTITY_A)
    }
    /// Same secret, new identity: emulates a restarted key generation.
    fn restarted() -> ProcessLaunchKey {
        ProcessLaunchKey::testing(SECRET, IDENTITY_B)
    }
    /// Foreign key: a different secret under the same identity.
    fn foreign() -> ProcessLaunchKey {
        ProcessLaunchKey::testing([0x5A; KEY_BYTES], IDENTITY_A)
    }

    #[derive(Clone)]
    struct Fixture {
        program_id: String,
        digest: Digest,
        executable: NativeIdentity,
        argv: Vec<String>,
        env: Vec<(String, String)>,
        cwd: NativeIdentity,
        pty: Option<PtyDimensions>,
        limits: ResourceLimits,
        catalog_label: String,
        cwd_label: String,
    }

    fn fixture() -> Fixture {
        Fixture {
            program_id: "pack.editor.run-tests".into(),
            digest: Digest::hash(b"executable-bytes"),
            executable: NativeIdentity::new("unix-fs", 3, b"exe-inode-fact").unwrap(),
            argv: vec!["cargo".into(), "test".into()],
            env: vec![("CI".into(), "1".into()), ("TERM".into(), "dumb".into())],
            cwd: NativeIdentity::new("unix-fs", 3, b"cwd-inode-fact").unwrap(),
            pty: Some(PtyDimensions::new(80, 24).unwrap()),
            limits: ResourceLimits {
                fuel_per_call: 1_000,
                memory_mib: 128,
                wall_ms: 5_000,
                table_elements: 1_024,
                output_bytes: 4_096,
            },
            catalog_label: "Run package tests".into(),
            cwd_label: "project workspace".into(),
        }
    }

    impl Fixture {
        fn snapshot(&self) -> ProcessLaunchSnapshot<'_> {
            ProcessLaunchSnapshot {
                program_id: &self.program_id,
                executable_digest: &self.digest,
                executable_identity: &self.executable,
                argv: &self.argv,
                env: &self.env,
                cwd_identity: &self.cwd,
                pty: self.pty,
                limits: &self.limits,
                catalog_label: &self.catalog_label,
                cwd_label: &self.cwd_label,
            }
        }
    }

    fn commit_digest(key: &ProcessLaunchKey, fixture: &Fixture) -> Digest {
        ProcessLaunchBinding::commit(key, &fixture.snapshot())
            .unwrap()
            .args_digest()
            .clone()
    }

    #[test]
    fn committed_binding_verifies_and_exposes_bounded_metadata() {
        let key = key_a();
        let case = fixture();
        let binding = ProcessLaunchBinding::commit(&key, &case.snapshot()).unwrap();
        binding.verify(&key, &case.snapshot()).unwrap();
        assert_eq!(binding.program_id(), "pack.editor.run-tests");
        assert_eq!(binding.catalog_label(), "Run package tests");
        assert_eq!(binding.cwd_label(), "project workspace");
        assert_eq!(binding.argv_count(), 2);
        assert_eq!(binding.env_count(), 2);
        assert_eq!(binding.pty(), Some(PtyDimensions::new(80, 24).unwrap()));
        assert_eq!(binding.limits(), &case.limits);
        assert_eq!(binding.risk(), Risk::Dangerous);
        assert!(binding.unsandboxed());
        assert_eq!(binding.encoding_version(), ENCODING_VERSION);
        assert_eq!(binding.key_version(), KEY_VERSION);
        assert_eq!(binding.key_identity(), key.key_identity());
        assert_eq!(binding.args_digest().as_str().len(), 71);
        let again = commit_digest(&key, &case);
        assert_eq!(binding.args_digest(), &again);
    }

    #[test]
    fn foreign_key_and_restarted_identity_fail() {
        let case = fixture();
        let key = key_a();
        let binding = ProcessLaunchBinding::commit(&key, &case.snapshot()).unwrap();
        // Same identity but a different secret fails the MAC check.
        assert_eq!(
            binding.verify(&foreign(), &case.snapshot()),
            Err(LaunchBindingError::Mismatch)
        );
        // Same secret but a post-restart identity is also rejected.
        assert_eq!(
            binding.verify(&restarted(), &case.snapshot()),
            Err(LaunchBindingError::Mismatch)
        );
        // A restarted generation commits a different binding entirely.
        assert_ne!(binding.args_digest(), &commit_digest(&restarted(), &case));
    }

    /// Mutating any committed field must change the commitment and
    /// invalidate the original binding.
    fn assert_committed(mutate: impl FnOnce(&mut Fixture)) {
        let key = key_a();
        let base = fixture();
        let binding = ProcessLaunchBinding::commit(&key, &base.snapshot()).unwrap();
        let mut changed = base.clone();
        mutate(&mut changed);
        let rebound = ProcessLaunchBinding::commit(&key, &changed.snapshot()).unwrap();
        assert_ne!(binding.args_digest(), rebound.args_digest());
        assert_eq!(
            binding.verify(&key, &changed.snapshot()),
            Err(LaunchBindingError::Mismatch)
        );
    }

    #[test]
    fn every_committed_field_changes_the_commitment() {
        assert_committed(|f| f.program_id.push_str(".other"));
        assert_committed(|f| f.digest = Digest::hash(b"other-executable"));
        assert_committed(|f| {
            f.executable = NativeIdentity::new("unix-fs-v2", 3, b"exe-inode-fact").unwrap()
        });
        assert_committed(|f| {
            f.executable = NativeIdentity::new("unix-fs", 4, b"exe-inode-fact").unwrap()
        });
        assert_committed(|f| {
            f.executable = NativeIdentity::new("unix-fs", 3, b"other-exe-fact").unwrap()
        });
        assert_committed(|f| f.argv[0] = "cargo-nextest".into());
        assert_committed(|f| f.argv.push("--nocapture".into()));
        assert_committed(|f| f.argv.swap(0, 1));
        assert_committed(|f| f.env[0].0 = "CI2".into());
        assert_committed(|f| f.env[0].1 = "0".into());
        assert_committed(|f| f.env.swap(0, 1));
        assert_committed(|f| f.env.push(("EXTRA".into(), "1".into())));
        assert_committed(|f| {
            f.cwd = NativeIdentity::new("unix-fs-v2", 3, b"cwd-inode-fact").unwrap()
        });
        assert_committed(|f| f.cwd = NativeIdentity::new("unix-fs", 9, b"cwd-inode-fact").unwrap());
        assert_committed(|f| f.cwd = NativeIdentity::new("unix-fs", 3, b"other-cwd-fact").unwrap());
        assert_committed(|f| f.pty = None);
        assert_committed(|f| f.pty = Some(PtyDimensions::new(81, 24).unwrap()));
        assert_committed(|f| f.pty = Some(PtyDimensions::new(80, 25).unwrap()));
        assert_committed(|f| f.limits.fuel_per_call += 1);
        assert_committed(|f| f.limits.memory_mib += 1);
        assert_committed(|f| f.limits.wall_ms += 1);
        assert_committed(|f| f.limits.table_elements += 1);
        assert_committed(|f| f.limits.output_bytes += 1);
        assert_committed(|f| f.catalog_label = "Other label".into());
        assert_committed(|f| f.cwd_label = "other cwd".into());
    }

    #[test]
    fn sequence_boundaries_are_not_ambiguous() {
        let key = key_a();
        let mut joined_left = fixture();
        joined_left.argv = vec!["ab".into(), "c".into()];
        let mut joined_right = fixture();
        joined_right.argv = vec!["a".into(), "bc".into()];
        assert_ne!(
            commit_digest(&key, &joined_left),
            commit_digest(&key, &joined_right)
        );
        // The same split applied to env pairs must also differ.
        let mut env_left = fixture();
        env_left.env = vec![("ab".into(), "c".into())];
        let mut env_right = fixture();
        env_right.env = vec![("a".into(), "bc".into())];
        assert_ne!(
            commit_digest(&key, &env_left),
            commit_digest(&key, &env_right)
        );
        // An empty sequence is distinct from a single empty item.
        let mut empty_item = fixture();
        empty_item.argv = vec!["".into()];
        let mut no_items = fixture();
        no_items.argv = vec![];
        assert_ne!(
            commit_digest(&key, &empty_item),
            commit_digest(&key, &no_items)
        );
    }

    #[test]
    fn pty_presence_and_dimensions_are_committed() {
        let key = key_a();
        let with_pty = fixture();
        let mut without_pty = fixture();
        without_pty.pty = None;
        assert_ne!(
            commit_digest(&key, &with_pty),
            commit_digest(&key, &without_pty)
        );
        let binding = ProcessLaunchBinding::commit(&key, &with_pty.snapshot()).unwrap();
        assert_eq!(
            binding.verify(&key, &without_pty.snapshot()),
            Err(LaunchBindingError::Mismatch)
        );
    }

    fn expect_commit_err(mutate: impl FnOnce(&mut Fixture), error: LaunchBindingError) {
        let key = key_a();
        let mut case = fixture();
        mutate(&mut case);
        assert_eq!(
            ProcessLaunchBinding::commit(&key, &case.snapshot()).unwrap_err(),
            error
        );
    }

    #[test]
    fn caps_and_invalid_metadata_fail_closed() {
        expect_commit_err(
            |f| f.argv = (0..=MAX_ARGV_ENTRIES).map(|i| i.to_string()).collect(),
            LaunchBindingError::ArgvCount,
        );
        expect_commit_err(
            |f| {
                f.env = (0..=MAX_ENV_ENTRIES)
                    .map(|i| (format!("K{i}"), "v".into()))
                    .collect()
            },
            LaunchBindingError::EnvCount,
        );
        expect_commit_err(
            |f| f.argv = vec!["x".repeat(MAX_STRING_BYTES + 1)],
            LaunchBindingError::StringBytes,
        );
        expect_commit_err(
            |f| f.env = vec![("K".into(), "x".repeat(MAX_STRING_BYTES + 1))],
            LaunchBindingError::StringBytes,
        );
        expect_commit_err(
            |f| f.env = vec![("k".repeat(MAX_STRING_BYTES + 1), "v".into())],
            LaunchBindingError::StringBytes,
        );
        expect_commit_err(
            |f| {
                f.env = (0..17)
                    .map(|i| (format!("K{i}"), "x".repeat(MAX_STRING_BYTES)))
                    .collect()
            },
            LaunchBindingError::TotalBytes,
        );
        expect_commit_err(
            |f| f.env = vec![("".into(), "v".into())],
            LaunchBindingError::EnvName,
        );
        expect_commit_err(
            |f| f.env = vec![("A=B".into(), "v".into())],
            LaunchBindingError::EnvName,
        );
        expect_commit_err(
            |f| f.env = vec![("A\0B".into(), "v".into())],
            LaunchBindingError::EnvName,
        );
        expect_commit_err(
            |f| f.env = vec![("A".into(), "v\0".into())],
            LaunchBindingError::NulByte,
        );
        expect_commit_err(
            |f| f.argv = vec!["a\0b".into()],
            LaunchBindingError::NulByte,
        );
        expect_commit_err(
            |f| f.program_id = String::new(),
            LaunchBindingError::ProgramId,
        );
        expect_commit_err(
            |f| f.program_id = "p".repeat(MAX_PROGRAM_ID_BYTES + 1),
            LaunchBindingError::ProgramId,
        );
        expect_commit_err(
            |f| f.program_id = "bad\u{7}bell".into(),
            LaunchBindingError::ProgramId,
        );
        expect_commit_err(
            |f| f.program_id = "bad\u{1b}esc".into(),
            LaunchBindingError::ProgramId,
        );
        expect_commit_err(
            |f| f.catalog_label = String::new(),
            LaunchBindingError::Label,
        );
        expect_commit_err(
            |f| f.catalog_label = "L".repeat(MAX_LABEL_BYTES + 1),
            LaunchBindingError::Label,
        );
        expect_commit_err(
            |f| f.cwd_label = "bad\u{1b}esc".into(),
            LaunchBindingError::Label,
        );
        expect_commit_err(|f| f.cwd_label = String::new(), LaunchBindingError::Label);
        // Zero PTY dimensions are unrepresentable.
        assert_eq!(
            PtyDimensions::new(0, 24),
            Err(LaunchBindingError::PtyDimensions)
        );
        assert_eq!(
            PtyDimensions::new(80, 0),
            Err(LaunchBindingError::PtyDimensions)
        );
        // Identity facts are validated at construction.
        assert_eq!(
            NativeIdentity::new("", 1, b"x").unwrap_err(),
            LaunchBindingError::IdentityMethod
        );
        assert_eq!(
            NativeIdentity::new(&"m".repeat(MAX_METHOD_BYTES + 1), 1, b"x").unwrap_err(),
            LaunchBindingError::IdentityMethod
        );
        assert_eq!(
            NativeIdentity::new("has space", 1, b"x").unwrap_err(),
            LaunchBindingError::IdentityMethod
        );
        assert_eq!(
            NativeIdentity::new("bad\u{7}", 1, b"x").unwrap_err(),
            LaunchBindingError::IdentityMethod
        );
        assert_eq!(
            NativeIdentity::new("unix-fs", 0, b"x").unwrap_err(),
            LaunchBindingError::IdentityVersion
        );
        assert_eq!(
            NativeIdentity::new("unix-fs", 1, b"").unwrap_err(),
            LaunchBindingError::IdentityBytes
        );
        assert_eq!(
            NativeIdentity::new("unix-fs", 1, &[0u8; MAX_IDENTITY_BYTES + 1]).unwrap_err(),
            LaunchBindingError::IdentityBytes
        );
    }

    #[test]
    fn oversized_env_fields_reject_before_content_validation() {
        for marker in ['=', '\0'] {
            expect_commit_err(
                |f| {
                    let mut name = "k".repeat(MAX_STRING_BYTES);
                    name.push(marker);
                    f.env = vec![(name, "v".into())];
                },
                LaunchBindingError::StringBytes,
            );
        }
        expect_commit_err(
            |f| {
                let mut value = "v".repeat(MAX_STRING_BYTES);
                value.push('\0');
                // Both field lengths are bounded before even the name is
                // scanned, so the oversized value wins over this bad name.
                f.env = vec![("A=B".into(), value)];
            },
            LaunchBindingError::StringBytes,
        );
    }

    #[test]
    fn boundary_sizes_commit_successfully() {
        let key = key_a();
        let mut case = fixture();
        case.argv = (0..MAX_ARGV_ENTRIES).map(|i| i.to_string()).collect();
        case.env = (0..MAX_ENV_ENTRIES)
            .map(|i| (format!("K{i}"), "v".to_string()))
            .collect();
        case.program_id = "p".repeat(MAX_PROGRAM_ID_BYTES);
        case.catalog_label = "L".repeat(MAX_LABEL_BYTES);
        case.cwd_label = "W".repeat(MAX_LABEL_BYTES);
        let binding = ProcessLaunchBinding::commit(&key, &case.snapshot()).unwrap();
        binding.verify(&key, &case.snapshot()).unwrap();
        assert_eq!(binding.argv_count(), MAX_ARGV_ENTRIES);
        assert_eq!(binding.env_count(), MAX_ENV_ENTRIES);
        // Exactly at the combined byte cap is still accepted.
        let mut full = fixture();
        full.argv = vec![];
        full.env = (0..16)
            .map(|_| ("K".to_string(), "x".repeat(MAX_STRING_BYTES - 1)))
            .collect();
        ProcessLaunchBinding::commit(&key, &full.snapshot()).unwrap();
        // Identity payload and PTY bounds accept their maximum values.
        assert!(NativeIdentity::new("m", 1, &[9u8; MAX_IDENTITY_BYTES]).is_ok());
        assert_eq!(
            PtyDimensions::new(u16::MAX, u16::MAX).unwrap().cols(),
            u16::MAX
        );
    }

    #[test]
    fn verify_revalidates_the_snapshot() {
        let key = key_a();
        let case = fixture();
        let binding = ProcessLaunchBinding::commit(&key, &case.snapshot()).unwrap();
        let mut bad = fixture();
        bad.argv = vec!["has\0nul".into()];
        assert_eq!(
            binding.verify(&key, &bad.snapshot()),
            Err(LaunchBindingError::NulByte)
        );
    }

    #[test]
    fn secrets_never_appear_in_debug_display_or_errors() {
        let key = ProcessLaunchKey::testing([0xC3; KEY_BYTES], IDENTITY_A);
        // The 32-byte secret must never show up in Debug output.
        assert!(!format!("{key:?}").contains("c3c3"));
        let mut case = fixture();
        case.argv = vec!["ARGV-SUPER-SECRET".into()];
        case.env = vec![("TOKEN".into(), "ENV-SUPER-SECRET".into())];
        case.executable = NativeIdentity::new("unix-fs", 3, b"RAW-EXE-EVIDENCE").unwrap();
        case.cwd = NativeIdentity::new("unix-fs", 3, b"RAW-CWD-EVIDENCE").unwrap();
        let binding = ProcessLaunchBinding::commit(&key, &case.snapshot()).unwrap();
        let shown = format!("{binding} {binding:?}");
        for marker in [
            "ARGV-SUPER-SECRET",
            "ENV-SUPER-SECRET",
            "RAW-EXE-EVIDENCE",
            "RAW-CWD-EVIDENCE",
        ] {
            assert!(!shown.contains(marker), "leaked {marker}");
        }
        // Identity payloads stay hidden even in their own Debug impl.
        assert!(!format!("{:?}", case.executable).contains("RAW-EXE-EVIDENCE"));
        // Errors are fixed codes: attacker-controlled text is not echoed.
        let mut bad = fixture();
        bad.program_id = "MARKER-\u{0}-HERE".into();
        let error = ProcessLaunchBinding::commit(&key, &bad.snapshot()).unwrap_err();
        let text = format!("{error} {error:?} {}", error.code());
        assert!(!text.contains("MARKER"));
        let error = binding.verify(&foreign(), &case.snapshot()).unwrap_err();
        let text = format!("{error} {error:?}");
        assert!(!text.contains("c3c3"));
        assert!(!text.contains("ARGV-SUPER-SECRET"));
    }

    #[test]
    fn generated_keys_have_independent_randomness() {
        let first = ProcessLaunchKey::generate().unwrap();
        let second = ProcessLaunchKey::generate().unwrap();
        assert_ne!(first.key_identity(), second.key_identity());
        // Distinct secrets commit distinct bindings for the same snapshot.
        let case = fixture();
        assert_ne!(commit_digest(&first, &case), commit_digest(&second, &case));
    }
}
