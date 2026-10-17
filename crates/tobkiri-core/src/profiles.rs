use std::{collections::BTreeMap, path::Path, sync::Arc};
use tobkiri_canon::{
    Digest, Variant,
    ids::{PackHash, PackId, ProfileId},
};
use tobkiri_manifest::{VerifiedPack, verify_directory, verify_tpk};
use tobkiri_profile::{Profile, PublishedProfile, parse_profile, parse_published};
use tobkiri_store::DataRoot;

use crate::CoreError;

pub(crate) struct Profiles {
    pub root: Arc<DataRoot>,
}

impl Profiles {
    pub fn load(&self, id: &ProfileId) -> Result<(Profile, Digest), CoreError> {
        let head = self.root.read_profile_file(id, "head", 128)?;
        let digest = Digest::parse(
            std::str::from_utf8(&head)
                .map_err(|_| CoreError::Invalid("Profile head is not UTF-8".into()))?
                .trim(),
        )?;
        Ok((self.load_revision(id, &digest)?, digest))
    }

    pub fn load_revision(&self, id: &ProfileId, digest: &Digest) -> Result<Profile, CoreError> {
        let bytes = self.root.read_profile_file(
            id,
            &format!("revisions/{}.json", digest.hex()),
            tobkiri_canon::MAX_BYTES,
        )?;
        if &Digest::hash(&bytes) != digest {
            return Err(CoreError::Invalid(
                "Profile revision digest mismatch".into(),
            ));
        }
        let profile = parse_profile(&bytes)?;
        if profile.id != *id {
            return Err(CoreError::Invalid(
                "Profile revision belongs to another Profile".into(),
            ));
        }
        Ok(profile)
    }

    pub fn head(&self, id: &ProfileId) -> Result<Option<Digest>, CoreError> {
        match self.root.read_profile_file(id, "head", 128) {
            Ok(head) => Ok(Some(Digest::parse(
                std::str::from_utf8(&head)
                    .map_err(|_| CoreError::Invalid("Profile head is not UTF-8".into()))?
                    .trim(),
            )?)),
            Err(tobkiri_store::StoreError::Io(error))
                if error.kind() == std::io::ErrorKind::NotFound =>
            {
                Ok(None)
            }
            Err(error) => Err(error.into()),
        }
    }

    pub fn write_revision(&self, profile: &Profile) -> Result<Digest, CoreError> {
        let bytes = strict_bytes(profile)?;
        let digest = Digest::hash(&bytes);
        self.root.write_profile_file_atomic(
            &profile.id,
            &format!("revisions/{}.json", digest.hex()),
            &bytes,
        )?;
        Ok(digest)
    }

    pub fn export(&self, id: &ProfileId) -> Result<Vec<u8>, CoreError> {
        let (profile, _) = self.load(id)?;
        strict_bytes(&profile.published())
    }
}

pub(crate) fn strict_bytes<T: serde::Serialize>(document: &T) -> Result<Vec<u8>, CoreError> {
    let json = serde_json::to_vec(document)?;
    let value = tobkiri_canon::parse(&json, Variant::StrictV1)?;
    Ok(tobkiri_canon::encode(&value, Variant::StrictV1)?)
}

pub(crate) fn published(bytes: &[u8]) -> Result<PublishedProfile, CoreError> {
    Ok(parse_published(bytes)?)
}

pub(crate) fn load_packs(profile: &Profile) -> Result<Vec<VerifiedPack>, CoreError> {
    profile
        .packs
        .iter()
        .map(|pin| {
            let source = pin
                .source
                .strip_prefix("file:")
                .or_else(|| pin.source.strip_prefix("path:"))
                .unwrap_or(&pin.source);
            if source.contains("://") || source.starts_with("bundled:") {
                return Err(CoreError::Invalid(
                    "Pack source must resolve to a verified local directory or .tpk artifact"
                        .into(),
                ));
            }
            let path = Path::new(source);
            let pack = if path.is_dir() {
                verify_directory(path, &profile.publishers)?
            } else {
                verify_tpk(&std::fs::read(path)?, &profile.publishers)?
            };
            if pack.manifest.id != pin.id
                || pack.hash != pin.sha256
                || pack.manifest.version != pin.version
            {
                return Err(CoreError::Invalid(
                    "Pack bytes do not match the Profile pin".into(),
                ));
            }
            Ok(pack)
        })
        .collect()
}

pub(crate) fn old_hashes(profile: Option<&Profile>) -> BTreeMap<PackId, PackHash> {
    profile
        .into_iter()
        .flat_map(|profile| {
            profile
                .packs
                .iter()
                .map(|pin| (pin.id.clone(), pin.sha256.clone()))
        })
        .collect()
}
