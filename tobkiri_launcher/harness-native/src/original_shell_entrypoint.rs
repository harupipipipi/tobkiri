//! Fresh entrypoint resolution and digest for the original Setup Shell.
use super::shell_file::{ShellFile, open_shell_file, write_descriptor};
use super::shell_root::{
    bounded_member, checked_name, close_parent, open_parent, open_shell_directory,
    optional_stat_at, resolve_leaf_stat, stat_at, stat_identity, stat_mode,
};
use super::*;

const ROOT_STEP: (&str, &str) = ("shell-resolved-root", "");
const PATH_BYTES: usize = libc::PATH_MAX as usize;

pub(super) struct ResolvedEntrypoint {
    identity: Identity,
    link: bool,
    parents: Funded<Vec<Identity>>,
}

impl CatalogIo {
    pub(super) fn inspect_shell_entrypoint<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        scope: &CoreValue,
        steps: &tobkiri_canon::CoreArray<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        let entry = admitted(&self.shell_entrypoint)?;
        let artifact = admitted(&self.shell_leaf)?;
        let stage = self.pack_resolve;
        let artifact_call = stage == PackResolve::ShellEntryArtifactResolve;
        let relative = if artifact_call { artifact } else { entry };
        if !exact_steps(steps, &[ROOT_STEP, ("join", relative)])
            || (entry != artifact
                && !entry
                    .strip_prefix(artifact)
                    .is_some_and(|s| s.starts_with('/')))
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let root_name = self
            .shell_resolved_name
            .as_deref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let parent_path = self
            .selected
            .parent()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let root_path = bounded_member(parent_path, root_name)?;
        let root_identity = self
            .shell_root_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let artifact_identity = self
            .shell_artifact_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let parent_identity = self
            .artifact_parent_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let binding = self
            .binding
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let _scratch = op.reserve(OriginalAuthorityStage::Path, PATH_BYTES * 6)?;
        let mut out = inspection_result(scope, budget)?;
        self.pack_resolve = PackResolve::Spent;
        op.check(OriginalAuthorityStage::Source)?;
        charge_work(
            budget,
            root_path.as_os_str().len() as u64 + relative.len() as u64,
        )?;
        let parent = open_parent(op, parent_path)?;
        let root = (|| {
            if !parent
                .metadata()
                .is_ok_and(|meta| identity(&meta) == parent_identity)
            {
                return Err(OriginalAuthorityError::unavailable());
            }
            open_shell_directory(op, &parent, &root_path, root_name, root_identity, false)
        })();
        close_parent(parent)?;
        let root = root?;
        let mut components = relative.rsplitn(2, '/');
        let leaf = components
            .next()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let directories = components.next();
        let first = stage == PackResolve::ShellEntryResolve;
        let depth = directories.map_or(0, |parents| parents.split('/').count());
        let mut recorded = if first {
            Some(reserve_parents(op, depth)?)
        } else {
            None
        };
        let (directory, path) = walk_parents(
            op,
            (root, root_path),
            directories,
            artifact_identity,
            self.shell_entrypoint_resolution
                .as_ref()
                .map(|entry| entry.parents.value.as_slice()),
            recorded.as_mut(),
            budget,
        )?;
        let absolute = bounded_member(&path, leaf)?;
        let result = (|| -> anyhow::Result<PackResolve> {
            let resolved = self.shell_entrypoint_resolution.as_ref();
            match stage {
                PackResolve::ShellEntryResolve | PackResolve::ShellEntryArtifactResolve => {
                    let (name, identity, link) = if artifact_call && entry != artifact {
                        let stat =
                            stat_at(op, &directory, &checked_name(leaf)?, || absolute.clone())?;
                        (
                            leaf.to_owned(),
                            stat_identity(&stat),
                            stat_mode(&stat) == mode_bits(libc::S_IFLNK),
                        )
                    } else {
                        resolve_leaf_stat(op, &directory, &path, leaf)?
                    };
                    if relative == artifact {
                        // A single-file artifact's two resolves can report the same
                        // replacement symlink. Only metadata is observed, and the
                        // following original symlink predicate must reject it.
                        if (entry != artifact || !link) && identity != artifact_identity {
                            return Err(OriginalAuthorityError::unavailable().into());
                        }
                        if entry != artifact && link {
                            return Err(OriginalAuthorityError::unavailable().into());
                        }
                    }
                    if !first
                        && entry == artifact
                        && !resolved.is_some_and(|old| old.identity == identity && old.link == link)
                    {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                    let lexical = if link {
                        bounded_member(&path, &name)?
                    } else {
                        absolute
                    };
                    write_resolved(&mut out, steps, binding, &lexical, budget)?;
                    if first {
                        self.shell_entrypoint_resolution = Some(ResolvedEntrypoint {
                            identity,
                            link,
                            parents: recorded.expect("funded first resolution"),
                        });
                        Ok(PackResolve::ShellEntryArtifactResolve)
                    } else {
                        Ok(PackResolve::ShellEntrySymlink)
                    }
                }
                PackResolve::ShellEntrySymlink | PackResolve::ShellEntryFile => {
                    let resolved = resolved.ok_or_else(OriginalAuthorityError::unavailable)?;
                    let stat = optional_stat_at(op, &directory, &checked_name(leaf)?, || absolute)?;
                    let link = stat
                        .as_ref()
                        .is_some_and(|stat| stat_mode(stat) == mode_bits(libc::S_IFLNK));
                    if stage == PackResolve::ShellEntrySymlink {
                        if resolved.link && !link {
                            return Err(OriginalAuthorityError::unavailable().into());
                        }
                        write_bool(&mut out, link)?;
                        return Ok(if link {
                            PackResolve::Spent
                        } else {
                            PackResolve::ShellEntryFile
                        });
                    }
                    if link {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                    let Some(stat) =
                        stat.filter(|stat| stat_mode(stat) == mode_bits(libc::S_IFREG))
                    else {
                        write_bool(&mut out, false)?;
                        return Ok(PackResolve::Spent);
                    };
                    if stat_identity(&stat) != resolved.identity {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                    write_bool(&mut out, true)?;
                    Ok(PackResolve::ShellEntryOpen)
                }
                PackResolve::ShellEntryOpen | PackResolve::ShellHeaderOpen => {
                    let resolved = resolved.ok_or_else(OriginalAuthorityError::unavailable)?;
                    let file = open_shell_file(
                        op,
                        &directory,
                        &path,
                        &checked_name(leaf)?,
                        resolved.identity,
                    )?;
                    write_descriptor(&mut out, binding, budget)?;
                    if stage == PackResolve::ShellHeaderOpen {
                        self.shell_file = Some(ShellFile::header(file));
                        Ok(PackResolve::ShellFileStat)
                    } else {
                        self.shell_file = Some(ShellFile::Entrypoint(file));
                        Ok(PackResolve::ShellFileTell)
                    }
                }
                _ => Err(OriginalAuthorityError::unavailable().into()),
            }
        })();
        close_parent(directory)?;
        let next = result?;
        op.check(OriginalAuthorityStage::Source)?;
        out.append(b"}")?;
        let response = out.finish()?;
        self.pack_resolve = next;
        Ok(response)
    }

    pub(super) fn inspect_shell_plist<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        scope: &CoreValue,
        steps: &tobkiri_canon::CoreArray<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        let artifact = admitted(&self.shell_leaf)?;
        if !self.shell_macos
            || !exact_steps(
                steps,
                &[
                    ROOT_STEP,
                    ("join", artifact),
                    ("join", "Contents"),
                    ("join", "Info.plist"),
                ],
            )
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let root_name = self
            .shell_resolved_name
            .as_deref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let parent_path = self
            .selected
            .parent()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let root_identity = self
            .shell_root_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let artifact_identity = self
            .shell_artifact_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let parent_identity = self
            .artifact_parent_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let _scratch = op.reserve(OriginalAuthorityStage::Path, PATH_BYTES * 6)?;
        let root_path = bounded_member(parent_path, root_name)?;
        let artifact_path = bounded_member(&root_path, artifact)?;
        let contents_path = bounded_member(&artifact_path, "Contents")?;
        let plist_path = bounded_member(&contents_path, "Info.plist")?;
        charge_work(budget, plist_path.as_os_str().len() as u64)?;
        let mut out = inspection_result(scope, budget)?;
        let stage = std::mem::replace(&mut self.pack_resolve, PackResolve::Spent);
        let parent = open_parent(op, parent_path)?;
        let root = (|| {
            if !parent
                .metadata()
                .is_ok_and(|meta| identity(&meta) == parent_identity)
            {
                return Err(OriginalAuthorityError::unavailable());
            }
            open_shell_directory(op, &parent, &root_path, root_name, root_identity, false)
        })();
        close_parent(parent)?;
        let root = root?;
        let app = open_shell_directory(
            op,
            &root,
            &artifact_path,
            artifact,
            artifact_identity,
            false,
        );
        close_parent(root)?;
        let app = app?;
        let contents = (|| {
            let name = checked_name("Contents")?;
            let stat = optional_stat_at(op, &app, &name, || contents_path.clone())?;
            let Some(stat) = stat else { return Ok(None) };
            if stat_mode(&stat) == mode_bits(libc::S_IFLNK) {
                return Err(OriginalAuthorityError::unavailable());
            }
            if stat_mode(&stat) != mode_bits(libc::S_IFDIR) {
                return Ok(None);
            }
            let actual = stat_identity(&stat);
            if self.shell_plist_parent.is_some_and(|prior| prior != actual) {
                return Err(OriginalAuthorityError::unavailable());
            }
            let file = open_shell_directory(op, &app, &contents_path, "Contents", actual, false)?;
            self.shell_plist_parent = Some(actual);
            Ok(Some(file))
        })();
        close_parent(app)?;
        let contents = contents?;
        let result = (|| -> anyhow::Result<PackResolve> {
            if stage == PackResolve::ShellPlistRead {
                let parent = contents
                    .as_ref()
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                let expected = self
                    .shell_plist_file
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                let mut file = open_shell_file(
                    op,
                    parent,
                    &contents_path,
                    &checked_name("Info.plist")?,
                    expected,
                )?;
                let read = (|| {
                    let size = file.metadata().map_err(|error| raw_io(error, None))?.len();
                    // Base64 and the inspection envelope must fit the existing 4 MiB frame.
                    let bytes =
                        read_resource(op, &mut file, size, (MAX_BYTES - 4096) / 4 * 3, budget)?;
                    Ok::<_, anyhow::Error>(bytes)
                })();
                close_parent(file)?;
                write_bytes(&mut out, &read?.value)?;
                return Ok(PackResolve::ShellHeaderOpen);
            }
            let stat = if let Some(parent) = &contents {
                optional_stat_at(op, parent, &checked_name("Info.plist")?, || plist_path)?
            } else {
                None
            };
            let mode = stat.as_ref().map(stat_mode);
            if stage == PackResolve::ShellPlistSymlink {
                let link = mode == Some(mode_bits(libc::S_IFLNK));
                write_bool(&mut out, link)?;
                return Ok(if link {
                    PackResolve::Spent
                } else {
                    PackResolve::ShellPlistFile
                });
            }
            if mode == Some(mode_bits(libc::S_IFLNK)) {
                return Err(OriginalAuthorityError::unavailable().into());
            }
            let regular = mode == Some(mode_bits(libc::S_IFREG));
            write_bool(&mut out, regular)?;
            if regular {
                self.shell_plist_file = stat.as_ref().map(stat_identity);
                Ok(PackResolve::ShellPlistRead)
            } else {
                Ok(PackResolve::Spent)
            }
        })();
        if let Some(contents) = contents {
            close_parent(contents)?;
        }
        let next = result?;
        op.check(OriginalAuthorityStage::Source)?;
        out.append(b"}")?;
        let response = out.finish()?;
        self.pack_resolve = next;
        Ok(response)
    }
}

fn admitted(value: &Option<CoreValue>) -> Result<&str> {
    value
        .as_ref()
        .and_then(CoreValue::as_str)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn reserve_parents<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    depth: usize,
) -> Result<Funded<Vec<Identity>>> {
    let exhausted = || OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let capacity = op.reserve(
        OriginalAuthorityStage::Source,
        depth
            .checked_mul(size_of::<Identity>())
            .ok_or_else(exhausted)?,
    )?;
    let mut value = Vec::new();
    value.try_reserve_exact(depth).map_err(|_| exhausted())?;
    if value.capacity() != depth {
        return Err(exhausted());
    }
    Ok(Funded {
        value,
        _capacity: capacity,
    })
}

fn walk_parents<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    root: (TrackedFile, PathBuf),
    directories: Option<&str>,
    artifact: Identity,
    expected: Option<&[Identity]>,
    mut recorded: Option<&mut Funded<Vec<Identity>>>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(TrackedFile, PathBuf)> {
    let (mut directory, mut path) = root;
    for (index, name) in directories
        .into_iter()
        .flat_map(|value| value.split('/'))
        .enumerate()
    {
        let child = bounded_member(&path, name)?;
        charge_work(budget, child.as_os_str().len() as u64)?;
        let next = (|| {
            let stat = stat_at(op, &directory, &checked_name(name)?, || child.clone())?;
            let identity = stat_identity(&stat);
            if stat_mode(&stat) != mode_bits(libc::S_IFDIR) {
                if stat_mode(&stat) == mode_bits(libc::S_IFLNK) {
                    return Err(OriginalAuthorityError::unavailable());
                }
                return Err(raw_io(
                    std::io::Error::from_raw_os_error(libc::ENOTDIR),
                    child.to_str(),
                ));
            }
            if (index == 0 && identity != artifact)
                || expected.is_some_and(|identities| identities.get(index) != Some(&identity))
            {
                return Err(OriginalAuthorityError::unavailable());
            }
            let next = open_shell_directory(op, &directory, &child, name, identity, false)?;
            if let Some(ref mut recorded) = recorded {
                recorded.value.push(identity);
            }
            Ok(next)
        })();
        close_parent(directory)?;
        directory = next?;
        path = child;
    }
    Ok((directory, path))
}

fn write_resolved(
    out: &mut CoreBufferBuilder,
    steps: &tobkiri_canon::CoreArray<'_>,
    binding: &CoreValue,
    path: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<()> {
    let mut encoded = CoreValue::array_builder(budget, steps.len())?;
    for step in steps.iter() {
        encoded.push(step)?;
    }
    write_path(out, binding, path, Some(&encoded.finish()?), budget)
}
