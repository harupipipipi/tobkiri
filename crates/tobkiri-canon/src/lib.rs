//! Lossless JSON values and byte-compatible canonical codecs.

mod codec;
pub mod files;
pub mod ids;
mod legacy;
pub mod ownership;
mod serde_value;
pub use legacy::{
    artifact_tree_digest, derive_lease_mac_key, lease_token, legacy_key_id, verify_lease_token,
};
pub use ownership::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, ControlHandle, CoreArray,
    CoreArrayBuilder, CoreArraySlots, CoreBuffer, CoreBufferBuilder, CoreObject, CoreRef,
    CoreValue, LeasedShared,
};

use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use hmac::{Hmac, Mac};
use sha2::{Digest as _, Sha256};
use std::collections::BTreeMap;

/// JSON value retaining arbitrary precision integers separately from floats.
#[derive(Clone, Debug, Default, PartialEq)]
pub enum CanonValue {
    #[default]
    Null,
    Bool(bool),
    Int(i64),
    BigInt(String),
    Float(f64),
    Str(String),
    Arr(Vec<Self>),
    Obj(BTreeMap<String, Self>),
}

/// Byte encodings used by new documents and distinct persisted legacy formats.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Variant {
    StrictV1,
    LegacyFloat,
    LegacyPackSig,
}

/// Malformed, ambiguous, or unsupported canonical data.
#[derive(Debug, thiserror::Error)]
pub enum CanonError {
    #[error("invalid canonical JSON: {0}")]
    Invalid(String),
    #[error("canonical JSON exceeds {0}")]
    Limit(&'static str),
    #[error("signature or seal mismatch")]
    Authentication,
}
pub type Error = CanonError;

/// A validated lowercase SHA-256 identity, including its algorithm prefix.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Digest([u8; 71]);
impl Digest {
    pub fn parse(value: &str) -> Result<Self, CanonError> {
        if value.len() != 71
            || !value.starts_with("sha256:")
            || !value[7..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(CanonError::Invalid(
                "digest must be sha256: followed by 64 lowercase hexadecimal digits".into(),
            ));
        }
        let mut bytes = [0; 71];
        bytes.copy_from_slice(value.as_bytes());
        Ok(Self(bytes))
    }
    pub fn hash(bytes: &[u8]) -> Self {
        Self::from_sha256(Sha256::digest(bytes).into())
    }
    /// Format an already finalized SHA-256 digest without allocating a string.
    /// This is a byte identity, not evidence of admission or execution authority.
    pub fn from_sha256(hash: [u8; 32]) -> Self {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut bytes = [b'0'; 71];
        bytes[..7].copy_from_slice(b"sha256:");
        for (index, byte) in hash.into_iter().enumerate() {
            bytes[7 + index * 2] = HEX[usize::from(byte >> 4)];
            bytes[8 + index * 2] = HEX[usize::from(byte & 15)];
        }
        Self(bytes)
    }
    pub fn as_str(&self) -> &str {
        // SAFETY: parse validates all 71 ASCII bytes and from_sha256 creates
        // only the fixed ASCII prefix and lowercase hexadecimal characters.
        unsafe { std::str::from_utf8_unchecked(&self.0) }
    }
    pub fn hex(&self) -> &str {
        &self.as_str()[7..]
    }
}
impl std::fmt::Debug for Digest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("Digest").field(&self.as_str()).finish()
    }
}
impl std::hash::Hash for Digest {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        // Preserve the original String/str Hash input, including its terminator.
        std::hash::Hash::hash(self.as_str(), state);
    }
}
impl std::fmt::Display for Digest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}
impl std::str::FromStr for Digest {
    type Err = CanonError;
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::parse(value)
    }
}
impl serde::Serialize for Digest {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}
impl<'de> serde::Deserialize<'de> for Digest {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::parse(&<String as serde::Deserialize>::deserialize(d)?)
            .map_err(serde::de::Error::custom)
    }
}

pub const MAX_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_DEPTH: usize = 64;
pub const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

/// Parse with the variant's duplicate, number and Unicode rules.
pub fn parse(input: &[u8], variant: Variant) -> Result<CanonValue, Error> {
    codec::parse(input, variant)
}
/// Serialize with Python-compatible code-point key order and float spelling.
pub fn encode(value: &CanonValue, variant: Variant) -> Result<Vec<u8>, Error> {
    codec::encode(value, variant)
}
/// Return the lowercase, tagged SHA-256 of canonical bytes.
pub fn digest(value: &CanonValue, variant: Variant) -> Result<Digest, Error> {
    Ok(hash_bytes(&encode(value, variant)?))
}
/// Return the tagged SHA-256 of raw bytes.
pub fn hash_bytes(bytes: &[u8]) -> Digest {
    Digest::hash(bytes)
}
/// Seal canonical bytes using the legacy Workflow HMAC format.
pub fn seal(value: &CanonValue, variant: Variant, key: &[u8]) -> Result<String, Error> {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).map_err(|_| Error::Authentication)?;
    mac.update(&encode(value, variant)?);
    Ok(mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}
/// Verify the HMAC of exact stored bytes in constant time.
pub fn verify_seal_bytes(payload: &[u8], signature: &[u8], key: &[u8]) -> Result<(), Error> {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).map_err(|_| Error::Authentication)?;
    mac.update(payload);
    mac.verify_slice(signature)
        .map_err(|_| Error::Authentication)
}
/// Verify a hexadecimal Workflow seal without reserializing stored bytes.
pub fn verify_seal_hex(payload: &[u8], signature: &str, key: &[u8]) -> Result<(), Error> {
    if signature.len() != 64 {
        return Err(Error::Authentication);
    }
    let mut bytes = [0; 32];
    for (index, slot) in bytes.iter_mut().enumerate() {
        let text = signature
            .get(index * 2..index * 2 + 2)
            .ok_or(Error::Authentication)?;
        if !text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(Error::Authentication);
        }
        *slot = u8::from_str_radix(text, 16).map_err(|_| Error::Authentication)?;
    }
    verify_seal_bytes(payload, &bytes, key)
}
/// Sign a new StrictV1 document. Legacy variants are verification-only.
pub fn sign(value: &CanonValue, key: &SigningKey) -> Result<[u8; 64], Error> {
    Ok(key.sign(&encode(value, Variant::StrictV1)?).to_bytes())
}
/// Verify an Ed25519 signature over canonical bytes of the chosen format.
pub fn verify_signature(
    value: &CanonValue,
    variant: Variant,
    key: &[u8; 32],
    signature: &[u8; 64],
) -> Result<(), Error> {
    verify_signature_bytes(&encode(value, variant)?, key, signature)
}
/// Verify a historical envelope's exact newline/NUL/raw-byte preimage.
pub fn verify_signature_bytes(
    payload: &[u8],
    key: &[u8; 32],
    signature: &[u8; 64],
) -> Result<(), Error> {
    VerifyingKey::from_bytes(key)
        .map_err(|_| Error::Authentication)?
        .verify_strict(payload, &Signature::from_bytes(signature))
        .map_err(|_| Error::Authentication)
}
