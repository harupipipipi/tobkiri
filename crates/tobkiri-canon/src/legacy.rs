use crate::{CanonValue, Digest, Error, Variant, encode, parse, verify_seal_bytes};
use base64::{Engine as _, engine::general_purpose::URL_SAFE};
use hmac::{Hmac, Mac};
use sha2::{Digest as _, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::Read,
    path::Path,
};

/// Legacy authority derivation uses the encoded Fernet key bytes, not decoded key bytes.
pub fn derive_lease_mac_key(encoded_fernet_key: &[u8]) -> [u8; 32] {
    let mut hash = Sha256::new();
    hash.update(encoded_fernet_key);
    hash.update(b":lease-mac:v1");
    hash.finalize().into()
}
/// Legacy one-use lease wire codec. The returned bytes confer no authority themselves.
pub fn lease_token(lease_id: &str, lease_digest: &Digest, mac_key: &[u8]) -> Result<String, Error> {
    let payload = encode(
        &CanonValue::Obj(BTreeMap::from([
            ("lease_id".into(), CanonValue::Str(lease_id.into())),
            (
                "digest".into(),
                CanonValue::Str(lease_digest.as_str().into()),
            ),
        ])),
        Variant::LegacyFloat,
    )?;
    let mut mac = Hmac::<Sha256>::new_from_slice(mac_key).map_err(|_| Error::Authentication)?;
    mac.update(&payload);
    Ok(format!(
        "{}.{}",
        URL_SAFE.encode(&payload),
        URL_SAFE.encode(mac.finalize().into_bytes())
    ))
}
/// Authenticate legacy stored token bytes; live lease state must still be checked by the gate.
pub fn verify_lease_token(token: &str, mac_key: &[u8]) -> Result<(String, Digest), Error> {
    let (payload, signature) = token.split_once('.').ok_or(Error::Authentication)?;
    let payload = URL_SAFE
        .decode(payload)
        .map_err(|_| Error::Authentication)?;
    let signature = URL_SAFE
        .decode(signature)
        .map_err(|_| Error::Authentication)?;
    verify_seal_bytes(&payload, &signature, mac_key)?;
    let value = parse(&payload, Variant::LegacyFloat)?;
    let id = value
        .get("lease_id")
        .and_then(CanonValue::as_str)
        .ok_or(Error::Authentication)?
        .to_string();
    let digest = Digest::parse(
        value
            .get("digest")
            .and_then(CanonValue::as_str)
            .ok_or(Error::Authentication)?,
    )?;
    Ok((id, digest))
}
/// Historical Pack signing-key identifier: the first 32 lowercase SHA-256 hex digits.
pub fn legacy_key_id(public_key: &[u8; 32]) -> String {
    Digest::hash(public_key).hex()[..32].into()
}

fn io(error: std::io::Error) -> Error {
    Error::Invalid(format!("artifact IO: {error}"))
}
fn stable(a: &fs::Metadata, b: &fs::Metadata) -> bool {
    if a.len() != b.len() || a.modified().ok() != b.modified().ok() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if (a.dev(), a.ino(), a.mode(), a.mtime_nsec())
            != (b.dev(), b.ino(), b.mode(), b.mtime_nsec())
        {
            return false;
        }
    }
    true
}
/// Original path-NUL-content artifact-tree identity, with stable regular-file reads.
pub fn artifact_tree_digest(root: &Path) -> Result<Digest, Error> {
    fn visit(path: &Path, relative: &str, hash: &mut Sha256) -> Result<(), Error> {
        let meta = fs::symlink_metadata(path).map_err(io)?;
        if meta.file_type().is_symlink() {
            return Err(Error::Invalid("artifact tree contains a symlink".into()));
        }
        if meta.is_file() {
            hash.update(relative.as_bytes());
            hash.update([0]);
            let mut options = OpenOptions::new();
            options.read(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
            }
            let mut file: File = options.open(path).map_err(io)?;
            let before = file.metadata().map_err(io)?;
            if !before.is_file() || !stable(&meta, &before) {
                return Err(Error::Invalid("artifact replaced before hashing".into()));
            }
            let mut buffer = [0; 64 * 1024];
            let mut count = 0_u64;
            loop {
                let read = file.read(&mut buffer).map_err(io)?;
                if read == 0 {
                    break;
                }
                hash.update(&buffer[..read]);
                count += read as u64;
            }
            if count != before.len() || !stable(&before, &file.metadata().map_err(io)?) {
                return Err(Error::Invalid("artifact changed while hashing".into()));
            }
            return Ok(());
        }
        if !meta.is_dir() {
            return Err(Error::Invalid("artifact member is not regular".into()));
        }
        let mut entries = fs::read_dir(path)
            .map_err(io)?
            .map(|e| {
                let e = e.map_err(io)?;
                let name = e
                    .file_name()
                    .into_string()
                    .map_err(|_| Error::Invalid("artifact filename is not UTF-8".into()))?;
                Ok((name, e.path()))
            })
            .collect::<Result<Vec<_>, Error>>()?;
        entries.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, child) in entries {
            visit(
                &child,
                &if relative.is_empty() {
                    name
                } else {
                    format!("{relative}/{name}")
                },
                hash,
            )?;
        }
        Ok(())
    }
    let mut hash = Sha256::new();
    visit(root, "", &mut hash)?;
    Digest::parse(&format!("sha256:{:x}", hash.finalize()))
}
