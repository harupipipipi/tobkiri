use crate::{DataRoot, StoreError, canonical};
use chacha20poly1305::{
    KeyInit, XChaCha20Poly1305, XNonce,
    aead::{Aead, Payload},
};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, sync::Arc};
use tobkiri_canon::{Digest, ids::ProfileId};
use tobkiri_gate::{CallCx, DispatchPermit, EffectRequest, Grant, Principal};
use zeroize::{Zeroize, Zeroizing};

const MAGIC: &[u8; 8] = b"TBKVLT01";
/// Key custody belongs to core; Packs never receive a key-provider object.
pub trait VaultKeyProvider: Send + Sync {
    fn key(&self, profile: &ProfileId) -> Result<[u8; 32], StoreError>;
}
/// OS keychain provider. Linux CLI falls back to a private Profile key file.
pub struct DefaultKeyProvider {
    root: DataRoot,
}
impl DefaultKeyProvider {
    pub fn new(root: DataRoot) -> Self {
        Self { root }
    }
}
impl VaultKeyProvider for DefaultKeyProvider {
    fn key(&self, profile: &ProfileId) -> Result<[u8; 32], StoreError> {
        #[cfg(any(unix, windows))]
        let _lock = self.root.profile_lock(profile, ".vault-key.lock")?;
        let result = (|| {
            let entry = keyring::Entry::new("Tobkiri", profile.as_str())
                .map_err(|_| StoreError::Unavailable("OS keychain unavailable".into()))?;
            match entry.get_secret() {
                Ok(bytes) => key_array(Zeroizing::new(bytes)),
                Err(keyring::Error::NoEntry) => {
                    let mut key = Zeroizing::new([0; 32]);
                    getrandom::fill(key.as_mut())
                        .map_err(|e| StoreError::Unavailable(e.to_string()))?;
                    entry.set_secret(key.as_ref()).map_err(|_| {
                        StoreError::Unavailable("OS keychain key storage failed".into())
                    })?;
                    Ok(*key)
                }
                Err(_) => Err(StoreError::Unavailable(
                    "OS keychain locked or unavailable".into(),
                )),
            }
        })();
        #[cfg(target_os = "linux")]
        if result.is_err() {
            eprintln!("Tobkiri: OS keychain unavailable; using a private 0600 Profile key file.");
            return match _lock.directory.read("vault.key", 32) {
                Ok(bytes) => key_array(Zeroizing::new(bytes)),
                Err(StoreError::Io(e)) if e.kind() == std::io::ErrorKind::NotFound => {
                    let mut key = Zeroizing::new([0; 32]);
                    getrandom::fill(key.as_mut())
                        .map_err(|e| StoreError::Unavailable(e.to_string()))?;
                    _lock.directory.write_atomic("vault.key", key.as_ref())?;
                    Ok(*key)
                }
                Err(e) => Err(e),
            };
        }
        result
    }
}
fn key_array(bytes: Zeroizing<Vec<u8>>) -> Result<[u8; 32], StoreError> {
    bytes
        .as_slice()
        .try_into()
        .map_err(|_| StoreError::Invalid("invalid vault key length".into()))
}
/// Secret vectors wipe on every exit path, including map replacement and decoding failure.
#[derive(Serialize, Deserialize)]
#[serde(transparent)]
struct SecretBytes(Vec<u8>);
impl Drop for SecretBytes {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}
type SecretMap = BTreeMap<String, SecretBytes>;
fn secret_wire(values: &SecretMap) -> Result<Zeroizing<Vec<u8>>, StoreError> {
    // Names are restricted ASCII and values are u8 arrays. BTreeMap order and
    // serde's compact integer encoding are exactly this StrictV1 subset.
    serde_json::to_vec(values)
        .map(Zeroizing::new)
        .map_err(|e| StoreError::Canon(e.to_string()))
}
/// An opaque, Profile/principal-bound secret. Debug never prints its contents.
pub struct SecretRef {
    profile: ProfileId,
    principal: Principal,
    name: String,
    bytes: SecretBytes,
}
impl std::fmt::Debug for SecretRef {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SecretRef")
            .field("name", &self.name)
            .finish_non_exhaustive()
    }
}
impl SecretRef {
    /// Network host only: consume secret bytes through an already-bound network effect.
    pub fn with_network_value<R>(
        &self,
        cx: &CallCx,
        permit: &DispatchPermit,
        prefix: &str,
        use_value: impl FnOnce(&[u8]) -> R,
    ) -> Result<R, StoreError> {
        if self.profile != *cx.profile()
            || self.principal != *cx.principal()
            || !permit.matches_context(cx)
            || !matches!(permit.effect(), EffectRequest::Network { .. })
        {
            return Err(StoreError::Denied(
                "secret reference context/network mismatch".into(),
            ));
        }
        if prefix.contains(['\r', '\n'])
            || self.bytes.0.contains(&b'\r')
            || self.bytes.0.contains(&b'\n')
        {
            return Err(StoreError::Invalid("secret header contains newline".into()));
        }
        let mut header = Zeroizing::new(Vec::with_capacity(prefix.len() + self.bytes.0.len()));
        header.extend_from_slice(prefix.as_bytes());
        header.extend_from_slice(&self.bytes.0);
        let result = use_value(&header);
        Ok(result)
    }
}
/// XChaCha20-Poly1305 encrypted per-Profile vault. No data-export API exists.
pub struct Vault {
    root: DataRoot,
    profile: ProfileId,
    key: Zeroizing<[u8; 32]>,
}
impl Vault {
    pub fn open(
        root: &DataRoot,
        profile: &ProfileId,
        keys: Arc<dyn VaultKeyProvider>,
    ) -> Result<Self, StoreError> {
        let key = Zeroizing::new(keys.key(profile)?);
        let vault = Self {
            root: root.clone(),
            profile: profile.clone(),
            key,
        };
        let _ = vault.load()?;
        Ok(vault)
    }
    pub fn open_default(root: &DataRoot, profile: &ProfileId) -> Result<Self, StoreError> {
        Self::open(
            root,
            profile,
            Arc::new(DefaultKeyProvider::new(root.clone())),
        )
    }
    /// Core user operation. This is deliberately absent from guest imports.
    pub fn set(&self, name: &str, value: &[u8]) -> Result<(), StoreError> {
        crate::secure::name(name)?;
        if value.len() > 64 * 1024 {
            return Err(StoreError::Quota);
        }
        #[cfg(any(unix, windows))]
        let _lock = self.root.profile_lock(&self.profile, ".vault.lock")?;
        #[cfg(any(unix, windows))]
        let mut values =
            self.decode_file(_lock.directory.read("secrets.vault", 4 * 1024 * 1024 + 48))?;
        #[cfg(not(any(unix, windows)))]
        let mut values = self.load()?;
        values.insert(name.into(), SecretBytes(value.to_vec()));
        let plaintext = secret_wire(&values)?;
        if plaintext.len() > 4 * 1024 * 1024 {
            return Err(StoreError::Quota);
        }
        let mut nonce = [0; 24];
        getrandom::fill(&mut nonce).map_err(|e| StoreError::Unavailable(e.to_string()))?;
        let cipher = XChaCha20Poly1305::new(self.key.as_ref().into());
        let aad = format!("tobkiri.vault/1:{}", self.profile);
        let encrypted = cipher
            .encrypt(
                XNonce::from_slice(&nonce),
                Payload {
                    msg: &plaintext,
                    aad: aad.as_bytes(),
                },
            )
            .map_err(|_| StoreError::Invalid("vault encryption failed".into()))?;
        let mut file = Vec::with_capacity(32 + encrypted.len());
        file.extend_from_slice(MAGIC);
        file.extend_from_slice(&nonce);
        file.extend_from_slice(&encrypted);
        #[cfg(any(unix, windows))]
        {
            _lock.directory.write_atomic("secrets.vault", &file)
        }
        #[cfg(not(any(unix, windows)))]
        {
            self.root
                .write_profile_file_atomic(&self.profile, "secrets.vault", &file)
        }
    }
    fn load(&self) -> Result<SecretMap, StoreError> {
        self.decode_file(self.root.read_profile_file(
            &self.profile,
            "secrets.vault",
            4 * 1024 * 1024 + 48,
        ))
    }
    fn decode_file(&self, file: Result<Vec<u8>, StoreError>) -> Result<SecretMap, StoreError> {
        let bytes = match file {
            Ok(v) => v,
            Err(StoreError::Io(e)) if e.kind() == std::io::ErrorKind::NotFound => {
                return Ok(BTreeMap::new());
            }
            Err(e) => return Err(e),
        };
        if bytes.len() < 48 || &bytes[..8] != MAGIC {
            return Err(StoreError::Invalid("invalid vault file".into()));
        }
        let aad = format!("tobkiri.vault/1:{}", self.profile);
        let cipher = XChaCha20Poly1305::new(self.key.as_ref().into());
        let plaintext = Zeroizing::new(
            cipher
                .decrypt(
                    XNonce::from_slice(&bytes[8..32]),
                    Payload {
                        msg: &bytes[32..],
                        aad: aad.as_bytes(),
                    },
                )
                .map_err(|_| StoreError::Denied("vault authentication failed".into()))?,
        );
        let values: SecretMap =
            serde_json::from_slice(&plaintext).map_err(|e| StoreError::Canon(e.to_string()))?;
        for (name, value) in &values {
            crate::secure::name(name)?;
            if value.0.len() > 64 * 1024 {
                return Err(StoreError::Quota);
            }
        }
        if secret_wire(&values)?.as_slice() != plaintext.as_slice() {
            return Err(StoreError::Canon(
                "vault record is not StrictV1 canonical".into(),
            ));
        }
        Ok(values)
    }
    pub fn secret_digest(name: &str) -> Result<Digest, StoreError> {
        Ok(Digest::hash(&canonical(&name)?))
    }
    pub fn get(&self, cx: &CallCx, name: &str, grant: Grant) -> Result<SecretRef, StoreError> {
        if self.profile != *cx.profile() {
            return Err(StoreError::Denied("vault Profile mismatch".into()));
        }
        grant.consume(
            cx,
            &EffectRequest::Secret {
                name: name.into(),
                reveal: false,
            },
            &Self::secret_digest(name)?,
        )?;
        let mut values = self.load()?;
        let secret = values.remove(name).ok_or(StoreError::NotFound)?;
        Ok(SecretRef {
            profile: self.profile.clone(),
            principal: cx.principal().clone(),
            name: name.into(),
            bytes: secret,
        })
    }
    pub fn reveal(&self, cx: &CallCx, name: &str, grant: Grant) -> Result<Vec<u8>, StoreError> {
        if self.profile != *cx.profile() {
            return Err(StoreError::Denied("vault Profile mismatch".into()));
        }
        grant.consume(
            cx,
            &EffectRequest::Secret {
                name: name.into(),
                reveal: true,
            },
            &Self::secret_digest(name)?,
        )?;
        let mut values = self.load()?;
        let mut secret = values.remove(name).ok_or(StoreError::NotFound)?;
        // An explicitly approved reveal transfers custody to the trusted caller.
        Ok(std::mem::take(&mut secret.0))
    }
}

#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;

    struct FixtureKeys(u8);
    impl VaultKeyProvider for FixtureKeys {
        fn key(&self, _: &ProfileId) -> Result<[u8; 32], StoreError> {
            Ok([self.0; 32])
        }
    }
    fn fixture() -> (tempfile::TempDir, DataRoot, ProfileId) {
        let temporary = tempfile::Builder::new()
            .prefix(".windows-vault-")
            .tempdir_in(std::env::current_dir().unwrap())
            .unwrap();
        let root = DataRoot::open(temporary.path().join("data")).unwrap();
        let profile = ProfileId::parse("prof_windows_vault").unwrap();
        (temporary, root, profile)
    }

    #[test]
    fn concurrent_encrypted_updates_keep_every_value_under_native_profile_lock() {
        let (_temporary, root, profile) = fixture();
        let pack = tobkiri_canon::ids::PackId::parse("local.vault-test").unwrap();
        let _owner = root.owner_lease(&profile, &pack).unwrap();
        let vault = Arc::new(Vault::open(&root, &profile, Arc::new(FixtureKeys(17))).unwrap());
        let barrier = Arc::new(std::sync::Barrier::new(8));
        let threads = (0..8)
            .map(|index| {
                let vault = vault.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    vault
                        .set(
                            &format!("fixture-{index}"),
                            format!("fixture-secret-{index}").as_bytes(),
                        )
                        .unwrap();
                })
            })
            .collect::<Vec<_>>();
        for thread in threads {
            thread.join().unwrap();
        }
        let reopened = Vault::open(&root, &profile, Arc::new(FixtureKeys(17))).unwrap();
        let values = reopened.load().unwrap();
        assert_eq!(values.len(), 8);
        for index in 0..8 {
            assert_eq!(
                values[&format!("fixture-{index}")].0,
                format!("fixture-secret-{index}").as_bytes()
            );
        }
        let ciphertext = root
            .read_profile_file(&profile, "secrets.vault", 8192)
            .unwrap();
        assert!(
            !ciphertext
                .windows(b"fixture-secret-".len())
                .any(|part| part == b"fixture-secret-")
        );
        assert!(ciphertext.starts_with(MAGIC));
    }

    #[test]
    fn ciphertext_rejects_wrong_key_profile_and_tampering() {
        let (_temporary, root, profile) = fixture();
        let vault = Vault::open(&root, &profile, Arc::new(FixtureKeys(19))).unwrap();
        vault
            .set("FixtureUpperLogicalKey", b"public-test-fixture")
            .unwrap();
        assert!(matches!(
            Vault::open(&root, &profile, Arc::new(FixtureKeys(20))),
            Err(StoreError::Denied(_))
        ));
        let other = ProfileId::parse("prof_other_vault").unwrap();
        let mut bytes = root
            .read_profile_file(&profile, "secrets.vault", 8192)
            .unwrap();
        root.write_profile_file_atomic(&other, "secrets.vault", &bytes)
            .unwrap();
        assert!(matches!(
            Vault::open(&root, &other, Arc::new(FixtureKeys(19))),
            Err(StoreError::Denied(_))
        ));
        let last = bytes.len() - 1;
        bytes[last] ^= 1;
        root.write_profile_file_atomic(&profile, "secrets.vault", &bytes)
            .unwrap();
        assert!(matches!(
            Vault::open(&root, &profile, Arc::new(FixtureKeys(19))),
            Err(StoreError::Denied(_))
        ));
    }
}
