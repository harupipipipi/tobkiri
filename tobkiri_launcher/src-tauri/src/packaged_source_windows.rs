//! Windows source closure: private DACLs, pinned paths, held file identities.
use super::super::windows_packaging_fs as win;
use super::*;
use rand::RngCore;

const MANIFEST: &str = "packaged_defaultspack_source_manifest.v1.json";

#[derive(Debug)]
pub struct VerifiedSourceSnapshot {
    pub(super) owner: PathBuf,
    pub(super) root: PathBuf,
    held: BTreeMap<String, win::PinnedPath>,
    owner_pin: Option<win::PinnedPath>,
    inventory: win::Inventory,
    trusted_manifest: Vec<u8>,
    provenance: Option<Vec<u8>>,
    sid: String,
    cleanup_attempted: bool,
}

fn read_pinned(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let pinned = win::open_pinned(path, false)?;
    let before = win::identity(&pinned.file)?;
    let mut bytes = Vec::new();
    (&pinned.file).take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit || win::identity(&pinned.file)? != before {
        return Err(invalid("source file changed or exceeded size bound"));
    }
    Ok(bytes)
}

fn validate_manifest_names(manifest: &BTreeMap<String, ExpectedFile>) -> io::Result<()> {
    // Case-insensitive file/directory collision checks are independent of the
    // filesystem's current per-directory case-sensitivity setting.
    let mut names = BTreeMap::new();
    for relative in manifest.keys() {
        let mut prefix = String::new();
        for component in relative.split('/') {
            win::validate_component(std::ffi::OsStr::new(component))?;
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(component);
            let folded = prefix.to_lowercase();
            if names
                .get(&folded)
                .is_some_and(|existing| existing != &prefix)
            {
                return Err(invalid("case-aliased Windows source paths"));
            }
            names.insert(folded, prefix.clone());
        }
    }
    Ok(())
}

fn create_parents(root: &Path, relative: &str, sid: &str) -> io::Result<()> {
    let mut current = root.to_owned();
    let parts = Path::new(relative).components().collect::<Vec<_>>();
    for component in parts.iter().take(parts.len() - 1) {
        let Component::Normal(name) = component else {
            return Err(invalid("unsafe source path"));
        };
        let parent = win::open_pinned(&current, true)?;
        current.push(name);
        match win::create_private_directory(&current, sid) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e),
        }
        let child = win::open_pinned(&current, true)?;
        win::verify_acl(&child.file, sid, false, true)?;
        drop(parent);
    }
    Ok(())
}

impl VerifiedSourceSnapshot {
    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn bind_command_cwd(
        &self,
        command: &mut super::super::packaging_toolchain::VerifiedCommand<'_>,
    ) -> io::Result<()> {
        self.verify_unchanged()?;
        command.current_dir(&self.root)?;
        Ok(())
    }
    pub fn verify_unchanged(&self) -> io::Result<()> {
        let owner = self
            .owner_pin
            .as_ref()
            .ok_or_else(|| invalid("snapshot already closed"))?;
        win::verify_acl(&owner.file, &self.sid, true, true)?;
        let owner_id = win::identity(&owner.file)?;
        if self.inventory.len() != self.held.len() + 1
            || self.inventory.get("") != Some(&(owner_id.volume, owner_id.file, true))
        {
            return Err(invalid("unowned snapshot entry"));
        }
        if win::inventory(&self.owner)? != self.inventory {
            return Err(invalid(
                "Windows source snapshot identity or inventory changed",
            ));
        }
        for (relative, pinned) in &self.held {
            win::verify_acl(&pinned.file, &self.sid, true, true)?;
            let id = win::identity(&pinned.file)?;
            let key = if relative.is_empty() {
                "source".to_owned()
            } else {
                format!("source/{relative}")
            };
            if self.inventory.get(&key) != Some(&(id.volume, id.file, id.directory)) {
                return Err(invalid("held Windows source identity changed"));
            }
        }
        if read_pinned(&self.root.join(MANIFEST), MAX_MANIFEST_BYTES)? != self.trusted_manifest {
            return Err(invalid("source manifest changed"));
        }
        for (relative, expected) in parse_manifest(&self.trusted_manifest)? {
            let bytes = read_pinned(&self.root.join(relative), expected.size)?;
            if bytes.len() as u64 != expected.size
                || format!("{:x}", Sha256::digest(&bytes)) != expected.sha256
            {
                return Err(invalid("source payload digest changed"));
            }
        }
        if let Some(expected) = &self.provenance {
            if read_pinned(
                &self.root.join(PROVENANCE_FILENAME),
                MAX_PROVENANCE_BYTES as u64,
            )? != *expected
            {
                return Err(invalid("source provenance changed"));
            }
        }
        Ok(())
    }
    pub fn bind_provenance(&mut self, bytes: &[u8]) -> io::Result<PathBuf> {
        self.bind_provenance_with_hook(bytes, |_| {})
    }
    fn bind_provenance_with_hook(
        &mut self,
        bytes: &[u8],
        after_create: impl FnOnce(&Path),
    ) -> io::Result<PathBuf> {
        if self.provenance.is_some() || bytes.is_empty() || bytes.len() > MAX_PROVENANCE_BYTES {
            return Err(invalid("source provenance binding invalid"));
        }
        self.verify_unchanged()?;
        let path = self.root.join(PROVENANCE_FILENAME);
        let root = &self
            .held
            .get("")
            .ok_or_else(|| invalid("missing root handle"))?
            .file;
        win::set_private_acl(root, &self.sid, false)?;
        let result = (|| {
            let mut output = win::create_private_file(&path, &self.sid)?;
            output.write_all(bytes)?;
            output.sync_all()?;
            drop(output);
            let pinned = win::open_private_control(&path, false)?;
            win::set_private_acl(&pinned.file, &self.sid, true)?;
            self.held.insert(PROVENANCE_FILENAME.to_owned(), pinned);
            after_create(&self.root);
            Ok::<_, io::Error>(())
        })();
        let reseal = win::set_private_acl(&self.held.get("").unwrap().file, &self.sid, true);
        if let Err(error) = result {
            reseal?;
            return Err(invalid(format!(
                "provenance binding failed; residue retained: {error}"
            )));
        }
        reseal?;
        let mut candidate = self.inventory.clone();
        let id = win::identity(&self.held.get(PROVENANCE_FILENAME).unwrap().file)?;
        candidate.insert(
            format!("source/{PROVENANCE_FILENAME}"),
            (id.volume, id.file, false),
        );
        if win::inventory(&self.owner)? != candidate {
            return Err(invalid(
                "unowned entry or replacement during provenance binding; residue retained",
            ));
        }
        self.inventory = candidate;
        self.provenance = Some(bytes.to_vec());
        self.verify_unchanged()?;
        Ok(path)
    }
    pub fn cleanup(mut self) -> io::Result<()> {
        self.cleanup_attempted = true;
        self.cleanup_inner()
    }
    fn cleanup_inner(&mut self) -> io::Result<()> {
        self.verify_unchanged()?;
        let parent = win::open_pinned(
            self.owner
                .parent()
                .ok_or_else(|| invalid("missing snapshot parent"))?,
            true,
        )?;
        for pin in self.held.values() {
            win::set_private_acl(&pin.file, &self.sid, false)?;
        }
        win::set_private_acl(&self.owner_pin.as_ref().unwrap().file, &self.sid, false)?;
        self.held.clear();
        self.owner_pin.take();
        let result = win::remove_owned_tree(&self.owner, &self.inventory);
        drop(parent);
        result
    }
}
impl Drop for VerifiedSourceSnapshot {
    fn drop(&mut self) {
        if !self.cleanup_attempted {
            self.cleanup_attempted = true;
            let _ = self.cleanup_inner();
        }
    }
}

pub(super) fn verify_and_snapshot_against_manifest_with_hook(
    runtime_root: &Path,
    snapshot_parent: &Path,
    trusted_manifest: &[u8],
    before_copy: impl FnOnce(),
) -> io::Result<VerifiedSourceSnapshot> {
    if trusted_manifest.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(invalid("trusted manifest too large"));
    }
    let manifest = parse_manifest(trusted_manifest)?;
    validate_manifest_names(&manifest)?;
    let runtime = win::open_pinned(runtime_root, true)?;
    if read_pinned(&runtime_root.join(MANIFEST), MAX_MANIFEST_BYTES)? != trusted_manifest {
        return Err(invalid("working manifest differs from trusted authority"));
    }
    let mut actual = BTreeSet::new();
    for relative in ROOTS {
        let inventory = win::inventory(&runtime_root.join(relative))?;
        for (path, (_, _, directory)) in inventory {
            if !directory {
                actual.insert(format!("{relative}/{path}"));
            }
        }
    }
    for relative in FILES {
        let _pin = win::open_pinned(&runtime_root.join(relative), false)?;
        actual.insert((*relative).to_owned());
    }
    if actual != manifest.keys().cloned().collect() {
        return Err(invalid("actual source paths differ from strict manifest"));
    }
    before_copy();
    let sid = win::current_user_sid()?;
    // snapshot_parent is a fresh child of the already-private Core transaction.
    let parent = match win::open_pinned(snapshot_parent, true) {
        Ok(pin) => pin,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let grand = win::open_pinned(
                snapshot_parent
                    .parent()
                    .ok_or_else(|| invalid("missing snapshot parent"))?,
                true,
            )?;
            win::verify_acl(&grand.file, &sid, false, false)?;
            win::create_private_directory(snapshot_parent, &sid)?;
            win::open_pinned(snapshot_parent, true)?
        }
        Err(e) => return Err(e),
    };
    win::verify_acl(&parent.file, &sid, false, false)?;
    let mut nonce = [0u8; 16];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    let owner = snapshot_parent.join(format!(
        ".source-{}",
        nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
    ));
    win::create_private_directory(&owner, &sid)?;
    let owner_pin = win::open_private_control(&owner, true)?;
    let root = owner.join("source");
    win::create_private_directory(&root, &sid)?;
    // On any construction failure retain residue rather than removing a tree
    // whose complete ownership inventory was never established.
    for (relative, expected) in &manifest {
        let bytes = read_pinned(&runtime_root.join(relative), expected.size)?;
        if bytes.len() as u64 != expected.size
            || format!("{:x}", Sha256::digest(&bytes)) != expected.sha256
        {
            return Err(invalid(
                "source digest differs from trusted authority; residue retained",
            ));
        }
        create_parents(&root, relative, &sid)?;
        let mut output = win::create_private_file(&root.join(relative), &sid)?;
        output.write_all(&bytes)?;
        output.sync_all()?;
    }
    let mut output = win::create_private_file(&root.join(MANIFEST), &sid)?;
    output.write_all(trusted_manifest)?;
    output.sync_all()?;
    drop(output);
    let inventory = win::inventory(&owner)?;
    let mut expected_paths = BTreeMap::from([
        (String::new(), true),
        ("source".to_owned(), true),
        (format!("source/{MANIFEST}"), false),
    ]);
    for relative in manifest.keys() {
        let path = format!("source/{relative}");
        expected_paths.insert(path.clone(), false);
        let mut parent = Path::new(&path).parent();
        while let Some(path) = parent {
            if path.as_os_str().is_empty() {
                break;
            }
            let text = path.to_string_lossy().replace('\\', "/");
            if expected_paths.insert(text, true) == Some(false) {
                return Err(invalid("source file/directory collision"));
            }
            parent = path.parent();
        }
    }
    if inventory.len() != expected_paths.len()
        || inventory
            .iter()
            .any(|(path, (_, _, directory))| expected_paths.get(path) != Some(directory))
    {
        return Err(invalid(
            "unowned snapshot construction entry; residue retained",
        ));
    }
    let mut held = BTreeMap::new();
    for (relative, (_, _, directory)) in &inventory {
        if relative.is_empty() {
            continue;
        }
        let local = relative
            .strip_prefix("source")
            .and_then(|s| {
                if s.is_empty() {
                    Some(s)
                } else {
                    s.strip_prefix('/')
                }
            })
            .ok_or_else(|| invalid("source escaped owner"))?;
        let pin = win::open_private_control(&owner.join(relative), *directory)?;
        win::set_private_acl(&pin.file, &sid, true)?;
        held.insert(local.to_owned(), pin);
    }
    win::set_private_acl(&owner_pin.file, &sid, true)?;
    let snapshot = VerifiedSourceSnapshot {
        owner,
        root,
        held,
        owner_pin: Some(owner_pin),
        inventory,
        trusted_manifest: trusted_manifest.to_vec(),
        provenance: None,
        sid,
        cleanup_attempted: false,
    };
    snapshot.verify_unchanged()?;
    drop(runtime);
    drop(parent);
    Ok(snapshot)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn windows_provenance_rejects_unowned_extra_without_adopting_it() {
        let tree = super::super::tests::Tree::new("windows-provenance-extra");
        let root = super::super::tests::fixture(&tree);
        let manifest = read_pinned(&root.join(MANIFEST), MAX_MANIFEST_BYTES).unwrap();
        let mut snapshot = verify_and_snapshot_against_manifest_with_hook(
            &root,
            &tree.0.join("snapshots"),
            &manifest,
            || {},
        )
        .unwrap();
        let before = snapshot.inventory.clone();
        let error = snapshot
            .bind_provenance_with_hook(b"provenance", |root| {
                fs::write(root.join("unowned.py"), b"preserve").unwrap();
            })
            .unwrap_err();
        assert!(error.to_string().contains("unowned"));
        assert_eq!(snapshot.inventory, before);
        assert_eq!(
            fs::read(snapshot.root.join("unowned.py")).unwrap(),
            b"preserve"
        );
        // Restore ACLs for test teardown only, retaining all files for inspection
        // until the fixture is dropped. Production cleanup remains fail-closed.
        for pin in snapshot.held.values() {
            win::set_private_acl(&pin.file, &snapshot.sid, false).unwrap();
        }
        win::set_private_acl(
            &snapshot.owner_pin.as_ref().unwrap().file,
            &snapshot.sid,
            false,
        )
        .unwrap();
        snapshot.cleanup_attempted = true;
    }
}
