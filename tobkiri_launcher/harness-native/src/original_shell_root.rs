//! The original Shell root and selected path checks after the Pack lock read.
use super::shell_inputs::{MAX_NAME, single_leaf};
use super::*;

const RESOLVED_STEPS: &[(&str, &str)] = &[("shell-resolved-root", "")];

impl CatalogIo {
    pub(super) fn inspect_shell_root<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        scope: &CoreValue,
        steps: &tobkiri_canon::CoreArray<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        let selected = match self.pack_resolve {
            PackResolve::ShellLeafSymlink => Some(self.selected_shell_leaf()?),
            PackResolve::ShellLeafParentSymlink
            | PackResolve::ShellTreeSymlink
            | PackResolve::ShellTreeFile
            | PackResolve::ShellTreeDirectory => self.shell_leaf.clone(),
            _ => None,
        };
        let selected_name = selected.as_ref().and_then(CoreValue::as_str);
        let valid_steps = match self.pack_resolve {
            PackResolve::ShellDirectory => exact_steps(steps, RESOLVED_STEPS),
            PackResolve::ShellLeafSymlink
            | PackResolve::ShellTreeSymlink
            | PackResolve::ShellTreeFile
            | PackResolve::ShellTreeDirectory => {
                let name = selected_name.ok_or_else(OriginalAuthorityError::unavailable)?;
                exact_steps(steps, &[RESOLVED_STEPS[0], ("join", name)])
            }
            PackResolve::ShellLeafParentSymlink => {
                let name = selected_name.ok_or_else(OriginalAuthorityError::unavailable)?;
                exact_steps(steps, &[RESOLVED_STEPS[0], ("join", name), ("parent", "")])
            }
            _ => exact_steps(steps, ARTIFACT_ROOT_STEPS),
        };
        if !valid_steps {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let original = self
            .artifact_root
            .as_deref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let parent_path = self
            .selected
            .parent()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if original.parent() != Some(parent_path)
            || !parent_path.is_absolute()
            || original.as_os_str().len() > 8192
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let original_name = original
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let parent_identity = self
            .artifact_parent_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let mut response = inspection_result(scope, budget)?;
        let stage = std::mem::replace(&mut self.pack_resolve, PackResolve::Spent);
        op.check(OriginalAuthorityStage::Source)?;
        charge_work(
            budget,
            parent_path.as_os_str().len() as u64 + MAX_NAME as u64,
        )?;
        // Follow no ambient parent symlink or guest-derived path. A failed
        // walk cannot be reported as a leaf FileNotFoundError.
        let parent = open_parent(op, parent_path)?;
        let outcome = (|| -> anyhow::Result<PackResolve> {
            let current = parent
                .metadata()
                .map_err(|_| OriginalAuthorityError::unavailable())?;
            if identity(&current) != parent_identity {
                return Err(OriginalAuthorityError::unavailable().into());
            }
            Ok(match stage {
                PackResolve::ShellResolve => {
                    let name = resolve_leaf(op, &parent, parent_path, original_name)?;
                    let resolved = parent_path.join(&name);
                    if resolved.as_os_str().len() > 8192 {
                        return Err(
                            OriginalAuthorityError::capacity(OriginalAuthorityStage::Path).into(),
                        );
                    }
                    let steps = CoreValue::parse_guest(br#"[["shell-resolved-root",""]]"#, budget)?;
                    let binding = self
                        .binding
                        .as_ref()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    write_path(&mut response, binding, &resolved, Some(&steps), budget)?;
                    self.shell_resolved_name = Some(name);
                    PackResolve::ShellSymlink
                }
                PackResolve::ShellSymlink => {
                    let symlink = optional_stat(op, &parent, parent_path, original_name)?
                        .as_ref()
                        .is_some_and(|stat| stat_mode(stat) == mode_bits(libc::S_IFLNK));
                    write_bool(&mut response, symlink)?;
                    if symlink {
                        PackResolve::Spent
                    } else {
                        PackResolve::ShellDirectory
                    }
                }
                PackResolve::ShellDirectory => {
                    let name = self
                        .shell_resolved_name
                        .as_deref()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let stat = optional_stat(op, &parent, parent_path, name)?;
                    let mode = stat.as_ref().map(stat_mode);
                    // A newly substituted symlink cannot extend the held
                    // occurrence into the target of that link.
                    if mode == Some(mode_bits(libc::S_IFLNK)) {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                    let directory = mode == Some(mode_bits(libc::S_IFDIR));
                    write_bool(&mut response, directory)?;
                    if directory {
                        self.shell_root_identity = stat.as_ref().map(stat_identity);
                        PackResolve::ShellLeafSymlink
                    } else {
                        PackResolve::Spent
                    }
                }
                PackResolve::ShellLeafSymlink
                | PackResolve::ShellTreeSymlink
                | PackResolve::ShellTreeFile
                | PackResolve::ShellTreeDirectory => {
                    let name = self
                        .shell_resolved_name
                        .as_deref()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let leaf = selected_name.ok_or_else(OriginalAuthorityError::unavailable)?;
                    let leaf_name = checked_name(leaf)?;
                    let expected = self
                        .shell_root_identity
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let root_path = parent_path.join(name);
                    let root = open_shell_directory(op, &parent, &root_path, name, expected)?;
                    let result = optional_stat_at(op, &root, &leaf_name, || root_path.join(leaf));
                    close_parent(root)?;
                    let mode = result?.as_ref().map(stat_mode);
                    match stage {
                        PackResolve::ShellLeafSymlink | PackResolve::ShellTreeSymlink => {
                            let symlink = mode == Some(mode_bits(libc::S_IFLNK));
                            write_bool(&mut response, symlink)?;
                            if stage == PackResolve::ShellLeafSymlink {
                                self.shell_inputs = None;
                                if !symlink {
                                    self.shell_leaf = selected.clone();
                                }
                            }
                            match (symlink, stage) {
                                (true, _) => PackResolve::Spent,
                                (false, PackResolve::ShellLeafSymlink) => {
                                    PackResolve::ShellLeafParentSymlink
                                }
                                (false, _) => PackResolve::ShellTreeFile,
                            }
                        }
                        PackResolve::ShellTreeFile | PackResolve::ShellTreeDirectory => {
                            if mode == Some(mode_bits(libc::S_IFLNK)) {
                                // The preceding predicate saw a different entry.
                                // Do not follow the new link during type checks.
                                return Err(OriginalAuthorityError::unavailable().into());
                            }
                            let file = stage == PackResolve::ShellTreeFile;
                            let expected = if file { libc::S_IFREG } else { libc::S_IFDIR };
                            let found = mode == Some(mode_bits(expected));
                            write_bool(&mut response, found)?;
                            match (found, file) {
                                (true, _) => PackResolve::ShellReady,
                                (false, true) => PackResolve::ShellTreeDirectory,
                                (false, false) => PackResolve::Spent,
                            }
                        }
                        _ => unreachable!("selected Shell predicate"),
                    }
                }
                PackResolve::ShellLeafParentSymlink => {
                    let name = self
                        .shell_resolved_name
                        .as_deref()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let stat = optional_stat(op, &parent, parent_path, name)?;
                    let symlink = stat
                        .as_ref()
                        .is_some_and(|stat| stat_mode(stat) == mode_bits(libc::S_IFLNK));
                    if !symlink && stat.as_ref().map(stat_identity) != self.shell_root_identity {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                    write_bool(&mut response, symlink)?;
                    if symlink {
                        PackResolve::Spent
                    } else {
                        PackResolve::ShellTreeSymlink
                    }
                }
                _ => return Err(OriginalAuthorityError::unavailable().into()),
            })
        })();
        // Unconfirmed native close is an integration failure, not original
        // Path/OSError DATA. CleanupState keeps its physical uncertainty.
        close_parent(parent)?;
        let next = outcome?;
        op.check(OriginalAuthorityStage::Source)?;
        response.append(b"}")?;
        let response = response.finish()?;
        self.pack_resolve = next;
        Ok(response)
    }
}

pub(super) fn artifact_parent_identity<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    selected: &Path,
    root: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<Identity>> {
    let Some(path) = root
        .parent()
        .filter(|path| Some(*path) == selected.parent())
    else {
        return Ok(None);
    };
    charge_work(budget, path.as_os_str().len() as u64)?;
    let parent = open_parent(op, path)?;
    let id = parent
        .metadata()
        .map(|metadata| identity(&metadata))
        .map_err(|_| OriginalAuthorityError::unavailable());
    close_parent(parent)?;
    Ok(Some(id?))
}

fn open_parent<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    path: &Path,
) -> Result<TrackedFile> {
    op.check(OriginalAuthorityStage::Source)?;
    let parent =
        Parent::walk(path, &op.cleanup).map_err(|_| OriginalAuthorityError::unavailable())?;
    if op.cleanup.uncertain.load(Ordering::Acquire) {
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(parent)
}

fn close_parent(parent: TrackedFile) -> Result<()> {
    parent
        .close()
        .map_err(|_| OriginalAuthorityError::unavailable())
}

fn open_shell_directory<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    name: &str,
    expected: Identity,
) -> Result<TrackedFile> {
    let name = checked_name(name)?;
    // The first leaf predicate needs directory search access. Keep the
    // descriptor relative and avoid requiring permission to enumerate it.
    #[cfg(target_os = "macos")]
    let access = libc::O_SEARCH;
    #[cfg(target_os = "linux")]
    let access = libc::O_PATH;
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    let access = libc::O_RDONLY;
    let descriptor = loop {
        op.check(OriginalAuthorityStage::Source)?;
        let descriptor = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                name.as_ptr(),
                access | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if descriptor >= 0 {
            break descriptor;
        }
        let error = std::io::Error::last_os_error();
        if matches!(error.raw_os_error(), Some(libc::EACCES | libc::EPERM)) {
            return Err(raw_io(error, path.to_str()));
        }
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(OriginalAuthorityError::unavailable());
        }
    };
    let root = TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
    if !root
        .metadata()
        .is_ok_and(|meta| identity(&meta) == expected)
    {
        close_parent(root)?;
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(root)
}

fn checked_name(name: &str) -> Result<CString> {
    if !single_leaf(name) {
        return Err(OriginalAuthorityError::unavailable());
    }
    CString::new(name).map_err(|_| OriginalAuthorityError::unavailable())
}

fn stat_mode(stat: &libc::stat) -> u32 {
    mode_bits(stat.st_mode & libc::S_IFMT)
}

fn stat_identity(stat: &libc::stat) -> Identity {
    Identity {
        device: device_bits(stat.st_dev),
        inode: stat.st_ino,
        owner: stat.st_uid,
        kind: stat_mode(stat),
    }
}

fn stat_at<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    name: &CString,
    path: impl FnOnce() -> PathBuf,
) -> Result<libc::stat> {
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    loop {
        op.check(OriginalAuthorityStage::Source)?;
        if unsafe {
            libc::fstatat(
                parent.as_raw_fd(),
                name.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } == 0
        {
            return Ok(unsafe { stat.assume_init() });
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(raw_io(error, path().to_str()));
        }
    }
}

fn optional_stat_at<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    name: &CString,
    path: impl FnOnce() -> PathBuf,
) -> Result<Option<libc::stat>> {
    match stat_at(op, parent, name, path) {
        Ok(stat) => Ok(Some(stat)),
        Err(error)
            if matches!(&error.cause, Some(OriginalAuthorityCause::Io(io))
                if io.errno.is_some_and(pathlib_suppressed_stat_errno)) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn optional_stat<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    name: &str,
) -> Result<Option<libc::stat>> {
    optional_stat_at(op, parent, &checked_name(name)?, || path.join(name))
}

fn resolve_leaf<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    original: &str,
) -> Result<String> {
    let original_c = checked_name(original)?;
    if stat_mode(&stat_at(op, parent, &original_c, || path.join(original))?)
        != mode_bits(libc::S_IFLNK)
    {
        return Ok(original.to_owned());
    }
    let mut target = [0_u8; MAX_NAME + 1];
    let size = loop {
        op.check(OriginalAuthorityStage::Source)?;
        let size = unsafe {
            libc::readlinkat(
                parent.as_raw_fd(),
                original_c.as_ptr(),
                target.as_mut_ptr().cast(),
                target.len(),
            )
        };
        if size >= 0 {
            let size = size as usize;
            if size >= target.len() {
                return Err(OriginalAuthorityError::unavailable());
            }
            break size;
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(raw_io(error, path.join(original).to_str()));
        }
    };
    let name =
        std::str::from_utf8(&target[..size]).map_err(|_| OriginalAuthorityError::unavailable())?;
    // Resolve only an immediate sibling. Validate before any target IO and
    // refuse further links. This supports the original staged root-replaced
    // case without allowing arbitrary realpath or a child file operation.
    if stat_mode(&stat_at(op, parent, &checked_name(name)?, || {
        path.join(name)
    })?) == mode_bits(libc::S_IFLNK)
    {
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(name.to_owned())
}
