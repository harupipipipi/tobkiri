use std::{collections::BTreeMap, path::Path, sync::Arc};
use tobkiri_canon::{
    Digest, Variant,
    ids::{PackHash, PackId, ProfileId},
};
use tobkiri_manifest::{VerifiedPack, verify_directory, verify_tpk_file};
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
                verify_tpk_file(path, &profile.publishers)?
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

#[cfg(test)]
mod tests {
    use super::*;
    use tobkiri_gate::PermissionSet;
    use tobkiri_manifest::{FileEntry, PackManifest, Ports, RuntimeSpec, build_tpk, verify_tpk};
    use tobkiri_profile::{PackPin, Startup};

    fn profile(path: &Path) -> Profile {
        Profile {
            format: "tobkiri.profile/1".into(),
            id: ProfileId::parse("bounded_tpk").unwrap(),
            name: "Bounded package fixture".into(),
            packs: vec![PackPin {
                id: PackId::parse("local.bounded-tpk").unwrap(),
                version: "1.0.0".into(),
                sha256: Digest::hash(&[]).into(),
                source: format!("file:{}", path.display()),
            }],
            publishers: BTreeMap::new(),
            ceiling: PermissionSet::default(),
            user_permissions: None,
            pack_limits: BTreeMap::new(),
            bindings: BTreeMap::new(),
            overlay_order: vec![],
            user_overlays: vec![],
            startup: Startup {
                inputs: vec![],
                ui: None,
            },
        }
    }

    #[test]
    fn load_packs_rejects_a_sparse_oversized_tpk_before_parsing_or_pin_validation() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("oversized.tpk");
        let file = std::fs::File::create(&path).unwrap();
        file.set_len(512 * 1024 * 1024 + 1).unwrap();
        let error = load_packs(&profile(&path)).unwrap_err();
        assert!(error.to_string().contains("package exceeds 512 MiB"));
    }

    #[test]
    fn load_packs_preserves_valid_tpk_bytes_hash_and_profile_pin_checks() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("valid.tpk");
        let mut profile = profile(&path);
        let files = BTreeMap::from([("a.txt".into(), b"hello".to_vec())]);
        let manifest = PackManifest {
            format: "tobkiri.pack/1".into(),
            id: profile.packs[0].id.clone(),
            version: profile.packs[0].version.clone(),
            publisher_key: None,
            runtime: RuntimeSpec::Data,
            shapes: vec![],
            ports: Ports::default(),
            inputs: vec![],
            flows: vec![],
            overlays: vec![],
            stores: vec![],
            permissions: PermissionSet::default(),
            ui: None,
            files: vec![FileEntry {
                path: "a.txt".into(),
                sha256: Digest::hash(b"hello"),
                size: 5,
            }],
        };
        let bytes = build_tpk(&manifest, &files, None).unwrap();
        let expected = verify_tpk(&bytes, &profile.publishers).unwrap();
        std::fs::write(&path, &bytes).unwrap();
        profile.packs[0].sha256 = expected.hash.clone();
        let loaded = load_packs(&profile).unwrap();
        assert_eq!(loaded[0].hash, expected.hash);
        assert_eq!(loaded[0].files, expected.files);
        assert_eq!(std::fs::read(&path).unwrap(), bytes);

        #[cfg(unix)]
        {
            let alias = root.path().join("alias.tpk");
            std::os::unix::fs::symlink(&path, &alias).unwrap();
            profile.packs[0].source = alias.to_str().unwrap().into();
            assert_eq!(load_packs(&profile).unwrap()[0].hash, expected.hash);
        }

        profile.packs[0].version = "2.0.0".into();
        let error = load_packs(&profile).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("Pack bytes do not match the Profile pin")
        );
    }
}
