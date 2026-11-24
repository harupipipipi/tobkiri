//! Reached original Fernet transport. AES/CBC comes from the audited primitive
//! crates; authentication precedes decryption and padding, as in Fernet.
use super::*;
use aes::Aes128;
use base64::{Engine as _, engine::general_purpose::URL_SAFE};
use cbc::cipher::{BlockDecryptMut, BlockEncryptMut, KeyIvInit, block_padding::Pkcs7};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use zeroize::{Zeroize, Zeroizing};

pub(super) struct Keys {
    pub encoded: Zeroizing<Vec<u8>>,
    signing: Zeroizing<[u8; 16]>,
    encryption: Zeroizing<[u8; 16]>,
    // Original leases derive this from ENCODED key bytes, not the AES key.
    pub mac: Zeroizing<[u8; 32]>,
    _capacity: NativeSetupReservation,
}
impl Keys {
    pub fn parse<S: OriginalAuthorityOwner>(op: &Operation<'_, S>, encoded: &[u8]) -> Result<Self> {
        let capacity = op.reserve(
            OriginalAuthorityStage::Key,
            encoded
                .len()
                .checked_add(64)
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?,
        )?;
        let decoded = decode(op, encoded, OriginalAuthorityStage::Key).map_err(|error| {
            if error.class == "binascii.Error" {
                invalid_key(Some(python_error(error.class, error.message)))
            } else {
                error
            }
        })?;
        if decoded.len() != 32 {
            return Err(invalid_key(None));
        }
        let mut signing = Zeroizing::new([0; 16]);
        let mut encryption = Zeroizing::new([0; 16]);
        signing.copy_from_slice(&decoded[..16]);
        encryption.copy_from_slice(&decoded[16..]);
        Ok(Self {
            encoded: Zeroizing::new(encoded.to_vec()),
            signing,
            encryption,
            mac: Zeroizing::new(tobkiri_canon::derive_lease_mac_key(encoded)),
            _capacity: capacity,
        })
    }
    pub fn generate<S: OriginalAuthorityOwner>(op: &Operation<'_, S>) -> Result<Self> {
        let mut raw = Zeroizing::new([0; 32]);
        random(&mut *raw, OriginalAuthorityStage::Key)?;
        let mut encoded = Zeroizing::new([0; 44]);
        URL_SAFE
            .encode_slice(raw.as_slice(), &mut *encoded)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
        Self::parse(op, &*encoded)
    }
    pub fn decrypt<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        token: &[u8],
    ) -> Result<Funded<Zeroizing<Vec<u8>>>> {
        let stage = OriginalAuthorityStage::Audit;
        let mut decoded = decode(op, token, stage).map_err(|error| {
            if error.class == "binascii.Error" {
                authentication_with(Some(python_error(error.class, error.message)))
            } else {
                error
            }
        })?;
        // Fernet first reads version and timestamp, then authenticates. ttl=None
        // intentionally does not invent an expiration check for stored DATA.
        if decoded.first() != Some(&0x80) || decoded.len() < 9 {
            return Err(authentication());
        }
        let signed = decoded.len().saturating_sub(32);
        let mut mac =
            Hmac::<Sha256>::new_from_slice(&*self.signing).map_err(|_| authentication())?;
        mac.update(&decoded[..signed]);
        mac.verify_slice(&decoded[signed..]).map_err(|_| {
            authentication_with(Some(python_error(
                "cryptography.exceptions.InvalidSignature",
                "Signature did not match digest.",
            )))
        })?;
        if signed < 25 {
            return Err(authentication_with(Some(python_error(
                "builtins.ValueError",
                "Invalid padding bytes.",
            ))));
        }
        if (signed - 25) % 16 != 0 {
            return Err(authentication_with(Some(python_error(
                "builtins.ValueError",
                "The length of the provided data is not a multiple of the block length.",
            ))));
        }
        let iv: [u8; 16] = decoded[9..25].try_into().map_err(|_| authentication())?;
        let plaintext = cbc::Decryptor::<Aes128>::new((&*self.encryption).into(), (&iv).into())
            .decrypt_padded_mut::<Pkcs7>(&mut decoded[25..signed])
            .map_err(|_| {
                authentication_with(Some(python_error(
                    "builtins.ValueError",
                    "Invalid padding bytes.",
                )))
            })?;
        let size = plaintext.len();
        // Move the authenticated plaintext within its SAME admitted allocation.
        decoded.value.copy_within(25..25 + size, 0);
        decoded.value[size..].zeroize();
        decoded.value.truncate(size);
        Ok(decoded)
    }
    pub fn encrypt<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        plaintext: &[u8],
    ) -> Result<Funded<Vec<u8>>> {
        let stage = OriginalAuthorityStage::Migration;
        let encrypted = plaintext
            .len()
            .checked_div(16)
            .and_then(|n| n.checked_add(1))
            .and_then(|n| n.checked_mul(16))
            .ok_or_else(|| OriginalAuthorityError::capacity(stage))?;
        let raw_size = encrypted
            .checked_add(57)
            .ok_or_else(|| OriginalAuthorityError::capacity(stage))?;
        let raw_capacity = op.reserve(stage, raw_size)?;
        let mut raw = Zeroizing::new(vec![0; raw_size]);
        raw[0] = 0x80;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| {
                OriginalAuthorityError::unsupported(stage, "Fernet timestamp conversion")
            })?;
        raw[1..9].copy_from_slice(&now.as_secs().to_be_bytes());
        random(&mut raw[9..25], stage)?;
        let iv: [u8; 16] = raw[9..25].try_into().map_err(|_| authentication())?;
        raw[25..25 + plaintext.len()].copy_from_slice(plaintext);
        cbc::Encryptor::<Aes128>::new((&*self.encryption).into(), (&iv).into())
            .encrypt_padded_mut::<Pkcs7>(&mut raw[25..25 + encrypted], plaintext.len())
            .map_err(|_| authentication())?;
        let signed = raw_size - 32;
        let mut mac =
            Hmac::<Sha256>::new_from_slice(&*self.signing).map_err(|_| authentication())?;
        mac.update(&raw[..signed]);
        raw[signed..].copy_from_slice(&mac.finalize().into_bytes());
        let output_size = raw_size
            .checked_add(2)
            .and_then(|n| n.checked_div(3))
            .and_then(|n| n.checked_mul(4))
            .ok_or_else(|| OriginalAuthorityError::capacity(stage))?;
        let capacity = op.reserve(stage, output_size)?;
        let mut output = vec![0; output_size];
        let written = URL_SAFE
            .encode_slice(&*raw, &mut output)
            .map_err(|_| OriginalAuthorityError::capacity(stage))?;
        output.truncate(written);
        drop(raw);
        drop(raw_capacity);
        Ok(Funded {
            value: output,
            _capacity: capacity,
        })
    }
}

pub(super) fn random(output: &mut [u8], stage: OriginalAuthorityStage) -> Result<()> {
    getrandom::fill(output)
        .map_err(|_| OriginalAuthorityError::unsupported(stage, "OS random failure"))
}
fn authentication() -> OriginalAuthorityError {
    authentication_with(None)
}
fn authentication_with(context: Option<OriginalAuthorityCause>) -> OriginalAuthorityError {
    OriginalAuthorityError::new(
        OriginalAuthorityStage::Audit,
        "authority record authentication failed",
        Some(OriginalAuthorityCause::Python {
            class: "cryptography.fernet.InvalidToken",
            message: String::new(),
            cause: None,
            context: context.map(Box::new),
            suppressed: false,
        }),
    )
}
fn invalid_key(cause: Option<OriginalAuthorityCause>) -> OriginalAuthorityError {
    let suppressed = cause.is_some();
    OriginalAuthorityError::new(
        OriginalAuthorityStage::Key,
        "authority encryption key is invalid",
        Some(OriginalAuthorityCause::Python {
            class: "builtins.ValueError",
            message: "Fernet key must be 32 url-safe base64-encoded bytes.".into(),
            cause: cause.map(Box::new),
            context: None,
            suppressed,
        }),
    )
}

// CPython's non-strict binascii decoder, after urlsafe's -_/+ translation.
// Keep the original encoded bytes for the lease MAC; never canonicalize keys.
fn decode<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    input: &[u8],
    stage: OriginalAuthorityStage,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    let size = input
        .len()
        .checked_add(3)
        .and_then(|n| n.checked_div(4))
        .and_then(|n| n.checked_mul(3))
        .ok_or_else(|| OriginalAuthorityError::capacity(stage))?;
    let capacity = op.reserve(stage, size)?;
    let mut output = Zeroizing::new(Vec::new());
    output
        .try_reserve_exact(size)
        .map_err(|_| OriginalAuthorityError::capacity(stage))?;
    let (mut position, mut left, mut pads) = (0_u8, 0_u8, 0_usize);
    for byte in input {
        if *byte == b'=' {
            pads += 1;
            continue;
        }
        let value = match byte {
            b'A'..=b'Z' => *byte - b'A',
            b'a'..=b'z' => *byte - b'a' + 26,
            b'0'..=b'9' => *byte - b'0' + 52,
            b'+' | b'-' => 62,
            b'/' | b'_' => 63,
            _ => continue,
        };
        pads = 0;
        match position {
            0 => {
                position = 1;
                left = value;
            }
            1 => {
                position = 2;
                output.push((left << 2) | (value >> 4));
                left = value & 15;
            }
            2 => {
                position = 3;
                output.push((left << 4) | (value >> 2));
                left = value & 3;
            }
            _ => {
                position = 0;
                output.push((left << 6) | value);
                left = 0;
            }
        }
    }
    if position == 1 || (position != 0 && usize::from(position) + pads < 4) {
        return Err(OriginalAuthorityError {
            stage,
            class: "binascii.Error",
            message: if position == 1 {
                format!(
                    "Invalid base64-encoded string: number of data characters ({}) cannot be 1 more than a multiple of 4",
                    output.len() / 3 * 4 + 1
                )
            } else {
                "Incorrect padding".into()
            },
            cause: None,
            context: None,
            suppressed: false,
        });
    }
    Ok(Funded {
        value: output,
        _capacity: capacity,
    })
}
