//! The three original Shell root observations after the Pack lock read.
use super::*;

const RESOLVED_STEPS: &[(&str, &str)] = &[("shell-resolved-root", "")];
const MAX_NAME: usize = 255;

impl CatalogIo {
    pub(super) fn inspect_shell_root<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        scope: &CoreValue,
        steps: &tobkiri_canon::CoreArray<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        let expected_steps = if self.pack_resolve == PackResolve::ShellDirectory {
            RESOLVED_STEPS
        } else {
            ARTIFACT_ROOT_STEPS
        };
        if !exact_steps(steps, expected_steps) {
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
                    let symlink = optional_mode(op, &parent, parent_path, original_name)?
                        .is_some_and(|mode| mode == mode_bits(libc::S_IFLNK));
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
                    let mode = optional_mode(op, &parent, parent_path, name)?;
                    // A newly substituted symlink cannot extend the held
                    // occurrence into the target of that link.
                    if mode == Some(mode_bits(libc::S_IFLNK)) {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                    let directory = mode == Some(mode_bits(libc::S_IFDIR));
                    write_bool(&mut response, directory)?;
                    if directory {
                        PackResolve::ShellReady
                    } else {
                        PackResolve::Spent
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

fn checked_name(name: &str) -> Result<CString> {
    if name.is_empty() || name.len() > MAX_NAME || name == "." || name == ".." || name.contains('/')
    {
        return Err(OriginalAuthorityError::unavailable());
    }
    CString::new(name).map_err(|_| OriginalAuthorityError::unavailable())
}

fn leaf_mode<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    name: &str,
) -> Result<u32> {
    let name_c = checked_name(name)?;
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    loop {
        op.check(OriginalAuthorityStage::Source)?;
        if unsafe {
            libc::fstatat(
                parent.as_raw_fd(),
                name_c.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } == 0
        {
            return Ok(mode_bits(
                unsafe { stat.assume_init() }.st_mode & libc::S_IFMT,
            ));
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(raw_io(error, path.join(name).to_str()));
        }
    }
}

fn optional_mode<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    name: &str,
) -> Result<Option<u32>> {
    match leaf_mode(op, parent, path, name) {
        Ok(mode) => Ok(Some(mode)),
        Err(error)
            if matches!(&error.cause, Some(OriginalAuthorityCause::Io(io))
                if io.errno.is_some_and(pathlib_suppressed_stat_errno)) =>
        {
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

fn resolve_leaf<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    original: &str,
) -> Result<String> {
    if leaf_mode(op, parent, path, original)? != mode_bits(libc::S_IFLNK) {
        return Ok(original.to_owned());
    }
    let original_c = checked_name(original)?;
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
    if leaf_mode(op, parent, path, name)? == mode_bits(libc::S_IFLNK) {
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(name.to_owned())
}
