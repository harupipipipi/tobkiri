//! Windows packaging leases and suspended, Job-contained child processes.
use super::super::windows_packaging_fs as win;
use super::*;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Write,
    mem::{size_of, zeroed},
    os::windows::{
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
        process::CommandExt,
    },
    ptr::null,
    sync::Arc,
};
use windows_sys::Win32::{
    Foundation::INVALID_HANDLE_VALUE,
    System::{Diagnostics::ToolHelp::*, JobObjects::*, Threading::*},
};
const MANIFEST: &str = "sealed-environment.v1.json";
const MAX_MANIFEST: u64 = 64 * 1024 * 1024;
const MAX_CAPTURE: u64 = 64 * 1024 * 1024;

pub(super) struct PythonLease {
    pub root: PathBuf,
    pub executable: PathBuf,
    held: BTreeMap<String, win::PinnedPath>,
    inventory: win::Inventory,
    records: BTreeMap<String, (u64, String)>,
    sid: String,
}
impl PythonLease {
    pub fn verify_unchanged(&self) -> io::Result<()> {
        if win::inventory(&self.root)? != self.inventory || self.held.len() != self.inventory.len()
        {
            return Err(invalid("Windows Python lease inventory changed"));
        }
        for (relative, pin) in &self.held {
            let id = win::identity(&pin.file)?;
            if self.inventory.get(relative) != Some(&(id.volume, id.file, id.directory)) {
                return Err(invalid("Windows Python lease identity changed"));
            }
            win::verify_acl(&pin.file, &self.sid, true, true)?;
            if let Some((size, digest)) = self.records.get(relative) {
                verify_bytes(&pin.file, *size, digest)?;
            }
        }
        Ok(())
    }
}
impl Drop for PythonLease {
    fn drop(&mut self) {
        let result = (|| -> io::Result<()> {
            self.verify_unchanged()?;
            let parent = win::open_pinned(
                self.root
                    .parent()
                    .ok_or_else(|| invalid("Python snapshot parent missing"))?,
                true,
            )?;
            for pin in self.held.values() {
                win::set_private_acl(&pin.file, &self.sid, false)?;
            }
            self.held.clear();
            let result = win::remove_owned_tree(&self.root, &self.inventory);
            drop(parent);
            result
        })();
        if let Err(error) = result {
            eprintln!("Windows Python snapshot residue retained: {error}");
        }
    }
}
fn read_bytes(file: &File, limit: u64) -> io::Result<Vec<u8>> {
    use std::os::windows::fs::FileExt;
    let mut bytes = Vec::new();
    let mut buffer = [0u8; 65536];
    loop {
        let count = file.seek_read(&mut buffer, bytes.len() as u64)?;
        if count == 0 {
            break;
        }
        if (bytes.len() as u64).saturating_add(count as u64) > limit {
            return Err(invalid("Windows Python closure file exceeds bound"));
        }
        bytes.extend_from_slice(&buffer[..count]);
    }
    Ok(bytes)
}
fn verify_bytes(file: &File, size: u64, digest: &str) -> io::Result<Vec<u8>> {
    let bytes = read_bytes(file, size)?;
    if bytes.len() as u64 != size || format!("{:x}", Sha256::digest(&bytes)) != digest {
        return Err(invalid("Windows Python closure digest mismatch"));
    }
    Ok(bytes)
}
fn parse_inventory(bytes: &[u8]) -> io::Result<BTreeMap<String, (u64, String)>> {
    let document: serde_json::Value = serde_json::from_slice(bytes)
        .map_err(|e| invalid(format!("Python inventory invalid: {e}")))?;
    if document.get("schema").and_then(|v| v.as_str())
        != Some("io.tobkiri.sealed-python-environment.v1")
        || document.get("platform").and_then(|v| v.as_str()) != Some("windows")
    {
        return Err(invalid("Windows Python inventory schema/platform invalid"));
    }
    let files = document
        .get("files")
        .and_then(|v| v.as_array())
        .ok_or_else(|| invalid("Python inventory files missing"))?;
    if files.is_empty() || files.len() as u64 > SEALED_RESEAL_MAX_FILE_COUNT {
        return Err(invalid("Python inventory file count invalid"));
    }
    let mut records = BTreeMap::new();
    let mut aliases = BTreeMap::new();
    let mut total = 0u64;
    let mut previous = None;
    for entry in files {
        let fields = entry
            .as_object()
            .ok_or_else(|| invalid("Python inventory entry invalid"))?;
        if fields.keys().map(String::as_str).collect::<BTreeSet<_>>()
            != BTreeSet::from(["path", "size", "sha256", "executable"])
        {
            return Err(invalid("Python inventory fields invalid"));
        }
        let path = entry
            .get("path")
            .and_then(|v| v.as_str())
            .ok_or_else(|| invalid("Python inventory path missing"))?;
        if path == MANIFEST || previous.is_some_and(|p: &str| p >= path) {
            return Err(invalid("Python inventory paths unsorted or repeated"));
        }
        previous = Some(path);
        let mut prefix = String::new();
        for part in path.split('/') {
            win::validate_component(std::ffi::OsStr::new(part))?;
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(part);
            if aliases
                .get(&prefix.to_lowercase())
                .is_some_and(|old| old != &prefix)
            {
                return Err(invalid("case-aliased Python inventory"));
            }
            aliases.insert(prefix.to_lowercase(), prefix.clone());
        }
        let size = entry
            .get("size")
            .and_then(|v| v.as_u64())
            .ok_or_else(|| invalid("Python inventory size invalid"))?;
        total = total
            .checked_add(size)
            .filter(|n| *n <= SEALED_RESEAL_MAX_INVENTORY_BYTES)
            .ok_or_else(|| invalid("Python closure exceeds size limit"))?;
        let digest = entry
            .get("sha256")
            .and_then(|v| v.as_str())
            .filter(|v| valid_raw_sha256(v))
            .ok_or_else(|| invalid("Python inventory digest invalid"))?;
        if entry.get("executable").and_then(|v| v.as_bool()).is_none() {
            return Err(invalid("Python executable metadata missing"));
        }
        records.insert(path.to_owned(), (size, digest.to_owned()));
    }
    Ok(records)
}
fn expected_tree(records: &BTreeMap<String, (u64, String)>) -> io::Result<BTreeMap<String, bool>> {
    let mut tree = BTreeMap::from([(String::new(), true)]);
    for path in records.keys() {
        if tree.insert(path.clone(), false).is_some() {
            return Err(invalid("Python file/directory collision"));
        }
        let mut current = Path::new(path).parent();
        while let Some(parent) = current {
            if parent.as_os_str().is_empty() {
                break;
            }
            let text = parent.to_string_lossy().replace('\\', "/");
            if tree.insert(text, true) == Some(false) {
                return Err(invalid("Python file/directory collision"));
            }
            current = parent.parent();
        }
    }
    Ok(tree)
}
fn validate_venv_config(bytes: &[u8]) -> io::Result<()> {
    let text =
        std::str::from_utf8(bytes).map_err(|_| invalid("venv configuration must be UTF-8"))?;
    let mut home = None;
    let mut system_site = None;
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        match key.trim().to_ascii_lowercase().as_str() {
            "home" => {
                if home.replace(value.trim()).is_some() {
                    return Err(invalid("duplicate venv home"));
                }
            }
            "include-system-site-packages" => {
                if system_site
                    .replace(value.trim().to_ascii_lowercase())
                    .is_some()
                {
                    return Err(invalid("duplicate venv site policy"));
                }
            }
            _ => {}
        }
    }
    if home != Some("runtime") || system_site.as_deref() != Some("false") {
        return Err(invalid(
            "Windows venv home or system-site policy escapes its sealed closure",
        ));
    }
    Ok(())
}

pub(super) fn python_lease(path: &Path, expected_executable: &str) -> io::Result<Arc<PythonLease>> {
    let root = PathBuf::from(
        env::var_os(PYTHON_SNAPSHOT_ENV)
            .ok_or_else(|| invalid("TOBKIRI_PACKAGING_PYTHON_SNAPSHOT is required"))?,
    );
    let expected = env::var(PYTHON_INVENTORY_SHA256_ENV)
        .map_err(|_| invalid("Python inventory digest is required"))?;
    if !valid_raw_sha256(&expected) {
        return Err(invalid("Python inventory digest invalid"));
    }
    python_lease_at(path, &root, &expected, expected_executable)
}
fn python_lease_at(
    path: &Path,
    root: &Path,
    expected: &str,
    expected_executable: &str,
) -> io::Result<Arc<PythonLease>> {
    let source_root = win::open_pinned(root, true)?;
    let executable_relative = path
        .strip_prefix(root)
        .map_err(|_| invalid("Python executable escapes closure"))?
        .to_string_lossy()
        .replace('\\', "/");
    if executable_relative != "venv/Scripts/python.exe" {
        return Err(invalid(
            "Windows packaging Python must be venv/Scripts/python.exe inside its closure",
        ));
    }
    let manifest = win::open_pinned(&root.join(MANIFEST), false)?;
    let bytes = read_bytes(&manifest.file, MAX_MANIFEST)?;
    if format!("{:x}", Sha256::digest(&bytes)) != expected {
        return Err(invalid("Python inventory authority digest mismatch"));
    }
    let mut records = parse_inventory(&bytes)?;
    if records
        .get(&executable_relative)
        .map(|(_, hash)| hash.as_str())
        != Some(expected_executable)
    {
        return Err(invalid(
            "Python executable is not bound by closure authority",
        ));
    }
    records.insert(
        MANIFEST.to_owned(),
        (bytes.len() as u64, expected.to_owned()),
    );
    let wanted = expected_tree(&records)?;
    let inventory = win::inventory(root)?;
    if inventory.len() != wanted.len()
        || inventory
            .iter()
            .any(|(p, (_, _, d))| wanted.get(p) != Some(d))
    {
        return Err(invalid("Python installation has missing or extra entries"));
    }
    // Acquire and hash every source file before creating any destination.
    // These handles deny mutation for the entire copy operation.
    let mut source_files = BTreeMap::new();
    for (relative, (size, digest)) in &records {
        let source = win::open_pinned(&root.join(relative), false)?;
        let id = win::identity(&source.file)?;
        if inventory.get(relative) != Some(&(id.volume, id.file, false)) {
            return Err(invalid("Python source identity changed"));
        }
        verify_bytes(&source.file, *size, digest)?;
        source_files.insert(relative.clone(), source);
    }
    if win::inventory(root)? != inventory {
        return Err(invalid("Python source inventory changed while pinned"));
    }
    let config = source_files
        .get("venv/pyvenv.cfg")
        .ok_or_else(|| invalid("sealed Windows venv configuration missing"))?;
    validate_venv_config(&read_bytes(&config.file, 64 * 1024)?)?;
    let sid = win::current_user_sid()?;
    let temp = env::temp_dir();
    let temp_pin = win::open_pinned(&temp, true)?;
    win::verify_acl(&temp_pin.file, &sid, false, false)?;
    let mut nonce = [0u8; 16];
    rand::RngCore::fill_bytes(&mut rand::rngs::OsRng, &mut nonce);
    let target = temp.join(format!(
        "tobkiri-python-{}",
        nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
    ));
    win::create_private_directory(&target, &sid)?;
    let target_root = win::open_pinned(&target, true)?;
    for (relative, directory) in &wanted {
        if relative.is_empty() {
            continue;
        }
        let destination = target.join(relative);
        if *directory {
            win::create_private_directory(&destination, &sid)?;
        } else {
            let source = &source_files[relative];
            let (size, digest) = &records[relative];
            let payload = verify_bytes(&source.file, *size, digest)?;
            let mut output = win::create_private_file(&destination, &sid)?;
            output.write_all(&payload)?;
            output.sync_all()?;
        }
    }
    let inventory = win::inventory(&target)?;
    if inventory.len() != wanted.len()
        || inventory
            .iter()
            .any(|(p, (_, _, d))| wanted.get(p) != Some(d))
    {
        return Err(invalid("unowned Python snapshot residue"));
    }
    let mut held = BTreeMap::new();
    for (relative, (_, _, directory)) in &inventory {
        let pin = win::open_private_control(&target.join(relative), *directory)?;
        win::set_private_acl(&pin.file, &sid, true)?;
        held.insert(relative.clone(), pin);
    }
    let executable = target.join(executable_relative);
    let lease = Arc::new(PythonLease {
        root: target,
        executable,
        held,
        inventory,
        records,
        sid,
    });
    lease.verify_unchanged()?;
    drop(target_root);
    drop(temp_pin);
    drop(source_root);
    Ok(lease)
}

pub struct WindowsChild {
    child: Child,
    job: OwnedHandle,
    assigned: bool,
    executable: Option<Arc<win::PinnedPath>>,
    cwd: Option<Arc<win::PinnedPath>>,
    reaped: bool,
    // Must drop after both path guards to permit handle-based cleanup.
    lease: Option<Arc<PythonLease>>,
}
fn job() -> io::Result<OwnedHandle> {
    let handle = unsafe { CreateJobObjectW(null(), null()) };
    if handle.is_null() {
        return Err(io::Error::last_os_error());
    }
    let job = unsafe { OwnedHandle::from_raw_handle(handle) };
    let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if unsafe {
        SetInformationJobObject(
            job.as_raw_handle(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(job)
}
fn resume_primary(child: &Child) -> io::Result<()> {
    let raw = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if raw == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(raw) };
    let mut entry: THREADENTRY32 = unsafe { zeroed() };
    entry.dwSize = size_of::<THREADENTRY32>() as u32;
    let mut found = None;
    let mut ok = unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) };
    while ok != 0 {
        if entry.th32OwnerProcessID == child.id() {
            if found.is_some() {
                return Err(invalid("suspended child has multiple threads"));
            }
            found = Some(entry.th32ThreadID);
        }
        ok = unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) };
    }
    let thread = unsafe {
        OpenThread(
            THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
            0,
            found.ok_or_else(|| invalid("suspended primary thread missing"))?,
        )
    };
    if thread.is_null() {
        return Err(io::Error::last_os_error());
    }
    let thread = unsafe { OwnedHandle::from_raw_handle(thread) };
    if unsafe { GetProcessIdOfThread(thread.as_raw_handle()) } != child.id() {
        return Err(invalid("suspended primary thread ownership changed"));
    }
    if unsafe { ResumeThread(thread.as_raw_handle()) } != 1 {
        return Err(invalid("unexpected primary thread suspend count"));
    }
    Ok(())
}
impl WindowsChild {
    fn active(&self) -> io::Result<u32> {
        let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { zeroed() };
        if unsafe {
            QueryInformationJobObject(
                self.job.as_raw_handle(),
                JobObjectBasicAccountingInformation,
                (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(info.ActiveProcesses)
    }
    pub fn kill(&mut self) -> io::Result<()> {
        if !self.assigned {
            return self.child.kill();
        }
        if unsafe { TerminateJobObject(self.job.as_raw_handle(), 1) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    pub fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        if let Some(status) = self.child.try_wait()? {
            if self.active()? != 0 {
                self.kill()?;
                return Ok(None);
            }
            if let Some(lease) = &self.lease {
                lease.verify_unchanged()?;
            }
            self.reaped = true;
            return Ok(Some(status));
        }
        Ok(None)
    }
    pub fn wait(&mut self) -> io::Result<ExitStatus> {
        loop {
            if let Some(status) = self.try_wait()? {
                return Ok(status);
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }
    fn stop_until(&mut self, deadline: std::time::Instant) -> io::Result<()> {
        if self.reaped {
            return Ok(());
        }
        self.kill()?;
        while std::time::Instant::now() < deadline {
            if self.try_wait()?.is_some() {
                return Ok(());
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
        Err(invalid(
            "Windows child Job termination unconfirmed; lease retained",
        ))
    }
    pub fn output(mut self, budget: Option<VerifiedOutputBudget>) -> io::Result<Output> {
        let result = self.output_inner(budget);
        if let Err(primary) = result {
            return match self
                .stop_until(std::time::Instant::now() + std::time::Duration::from_secs(2))
            {
                Ok(()) => Err(primary),
                Err(containment) => Err(invalid(format!("{primary}; {containment}"))),
            };
        }
        result
    }
    fn output_inner(&mut self, budget: Option<VerifiedOutputBudget>) -> io::Result<Output> {
        let (tx, rx) = std::sync::mpsc::channel();
        let readers = [
            output_reader(
                self.child
                    .stdout
                    .take()
                    .ok_or_else(|| invalid("stdout unavailable"))?,
                0,
                tx.clone(),
                MAX_CAPTURE,
            )?,
            output_reader(
                self.child
                    .stderr
                    .take()
                    .ok_or_else(|| invalid("stderr unavailable"))?,
                1,
                tx,
                MAX_CAPTURE,
            )?,
        ];
        let mut readers = Some(readers);
        let deadline = budget.map(|b| std::time::Instant::now() + b.duration);
        let mut output = [None, None];
        let mut status = None;
        loop {
            loop {
                match rx.try_recv() {
                    Ok((index, result)) => output[index] = Some(result?),
                    Err(std::sync::mpsc::TryRecvError::Empty) => break,
                    Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                        if output.iter().any(Option::is_none) {
                            return Err(invalid(
                                "Windows output reader disconnected before completion",
                            ));
                        }
                        break;
                    }
                }
            }
            if status.is_none() {
                status = self.try_wait()?;
            }
            if let Some(status) = status {
                if output.iter().all(Option::is_some) {
                    for reader in readers.take().unwrap() {
                        reader
                            .join()
                            .map_err(|_| invalid("Windows output reader panicked"))?;
                    }
                    return Ok(Output {
                        status,
                        stdout: output[0].take().unwrap(),
                        stderr: output[1].take().unwrap(),
                    });
                }
            }
            if deadline.is_some_and(|d| std::time::Instant::now() >= d) {
                self.stop_until(std::time::Instant::now() + std::time::Duration::from_secs(2))?;
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Windows packaging output budget exceeded",
                ));
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }
}
fn output_reader<T: Read + Send + 'static>(
    input: T,
    which: usize,
    tx: std::sync::mpsc::Sender<(usize, io::Result<Vec<u8>>)>,
    cap: u64,
) -> io::Result<std::thread::JoinHandle<()>> {
    std::thread::Builder::new()
        .name("windows-packaging-output".into())
        .spawn(move || {
            let mut bytes = Vec::new();
            let result = input.take(cap + 1).read_to_end(&mut bytes).and_then(|_| {
                if bytes.len() as u64 > cap {
                    Err(invalid("Windows child output exceeded cap"))
                } else {
                    Ok(bytes)
                }
            });
            let _ = tx.send((which, result));
        })
}

impl Drop for WindowsChild {
    fn drop(&mut self) {
        if !self.reaped
            && self
                .stop_until(std::time::Instant::now() + std::time::Duration::from_secs(2))
                .is_err()
        {
            if let Some(lease) = self.lease.take() {
                std::mem::forget(lease);
            }
            if let Some(pin) = self.executable.take() {
                std::mem::forget(pin);
            }
            if let Some(pin) = self.cwd.take() {
                std::mem::forget(pin);
            }
        }
    }
}

#[derive(Clone, Copy)]
enum SpawnFault {
    None,
    #[cfg(test)]
    Assign,
    #[cfg(test)]
    Resume,
}
pub(super) fn spawn(command: &VerifiedCommand<'_>, capture: bool) -> VerifiedSpawnOutcome {
    spawn_inner(command, capture, SpawnFault::None)
}
fn spawn_inner(
    command: &VerifiedCommand<'_>,
    capture: bool,
    fault: SpawnFault,
) -> VerifiedSpawnOutcome {
    let lease = command.tool.windows_python.clone();
    if let Some(lease) = &lease {
        let checked = (|| -> io::Result<()> {
            lease.verify_unchanged()?;
            let cwd = command
                .windows_cwd
                .as_ref()
                .ok_or_else(|| invalid("Python command lacks pinned runtime cwd"))?;
            if win::identity(&cwd.file)? != win::identity(&lease.held[""].file)? {
                return Err(invalid("Python command cwd escapes its leased runtime"));
            }
            Ok(())
        })();
        if let Err(error) = checked {
            return VerifiedSpawnOutcome::NoChild(error);
        }
    }
    let job = match job() {
        Ok(job) => job,
        Err(error) => return VerifiedSpawnOutcome::NoChild(error),
    };
    let mut process = match command.command_with_stdio(capture) {
        Ok(command) => command,
        Err(error) => return VerifiedSpawnOutcome::NoChild(error),
    };
    process.creation_flags(CREATE_SUSPENDED);
    if capture {
        process.stdin(Stdio::null());
    }
    let child = match process.spawn() {
        Ok(child) => child,
        Err(error) => return VerifiedSpawnOutcome::NoChild(error),
    };
    let mut child = WindowsChild {
        child,
        job,
        assigned: false,
        lease,
        executable: Some(command.tool.windows_executable.clone()),
        cwd: command.windows_cwd.clone(),
        reaped: false,
    };
    #[cfg(test)]
    let reject_assign = matches!(fault, SpawnFault::Assign);
    #[cfg(not(test))]
    let reject_assign = false;
    let assigned = !reject_assign
        && unsafe {
            AssignProcessToJobObject(child.job.as_raw_handle(), child.child.as_raw_handle())
        } != 0;
    if !assigned {
        let error = io::Error::last_os_error();
        let stopped = (|| -> io::Result<()> {
            child.child.kill()?;
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
            while std::time::Instant::now() < deadline {
                if child.child.try_wait()?.is_some() {
                    return Ok(());
                }
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            Err(invalid("suspended child termination deadline exceeded"))
        })();
        if stopped.is_ok() {
            child.reaped = true;
            return VerifiedSpawnOutcome::ReapedFailure(error);
        }
        return VerifiedSpawnOutcome::Uncontained(invalid(format!(
            "Job assignment failed: {error}; child termination unconfirmed"
        )));
    }
    child.assigned = true;
    let _ = fault;
    #[cfg(test)]
    let reject_resume = matches!(fault, SpawnFault::Resume);
    #[cfg(not(test))]
    let reject_resume = false;
    let resumed = if reject_resume {
        Err(invalid("injected primary resume failure"))
    } else {
        resume_primary(&child.child)
    };
    if let Err(error) = resumed {
        return match child.stop_until(std::time::Instant::now() + std::time::Duration::from_secs(2))
        {
            Ok(()) => VerifiedSpawnOutcome::ReapedFailure(error),
            Err(containment) => {
                VerifiedSpawnOutcome::Uncontained(invalid(format!("{error}; {containment}")))
            }
        };
    }
    VerifiedSpawnOutcome::Running(VerifiedChild::Windows(child))
}

pub(super) fn configure_environment(command: &mut Command) -> io::Result<()> {
    use std::os::windows::ffi::OsStringExt;
    let mut buffer = vec![0u16; 32768];
    let count = unsafe {
        windows_sys::Win32::System::SystemInformation::GetWindowsDirectoryW(
            buffer.as_mut_ptr(),
            buffer.len() as u32,
        )
    };
    if count == 0 || count as usize >= buffer.len() {
        return Err(io::Error::last_os_error());
    }
    let root = PathBuf::from(std::ffi::OsString::from_wide(&buffer[..count as usize]));
    command
        .env("SystemRoot", &root)
        .env("WINDIR", &root)
        .env("PATH", root.join("System32"));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(label: &str) -> (PathBuf, PathBuf, String, String) {
        let sid = win::current_user_sid().unwrap();
        let root = env::temp_dir().join(format!(
            "tobkiri-python-test-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        win::create_private_directory(&root, &sid).unwrap();
        let contents = [
            ("runtime/Lib/os.py", b"stdlib".as_slice()),
            ("venv/Scripts/python.exe", b"exe".as_slice()),
            (
                "venv/pyvenv.cfg",
                b"home = runtime\ninclude-system-site-packages = false\n".as_slice(),
            ),
            ("runtime/python313.dll", b"dll".as_slice()),
            ("venv/Lib/site-packages/example.py", b"package".as_slice()),
        ];
        let mut files = Vec::new();
        for (relative, bytes) in contents {
            let path = root.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, bytes).unwrap();
            files.push(serde_json::json!({"path":relative,"size":bytes.len(),"sha256":format!("{:x}",Sha256::digest(bytes)),"executable":false}));
        }
        files.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        let manifest=serde_json::to_vec(&serde_json::json!({"schema":"io.tobkiri.sealed-python-environment.v1","platform":"windows","files":files})).unwrap();
        fs::write(root.join(MANIFEST), &manifest).unwrap();
        let executable = root.join("venv/Scripts/python.exe");
        let exe_hash = format!("{:x}", Sha256::digest(b"exe"));
        let hash = format!("{:x}", Sha256::digest(&manifest));
        (root, executable, hash, exe_hash)
    }
    fn remove(root: &Path) {
        win::remove_owned_tree(root, &win::inventory(root).unwrap()).unwrap();
    }
    #[test]
    fn windows_python_lease_copies_seals_and_reaps_complete_closure() {
        let (root, exe, hash, exe_hash) = fixture("valid");
        let lease = python_lease_at(&exe, &root, &hash, &exe_hash).unwrap();
        let copied = lease.root.clone();
        assert_eq!(
            fs::read(copied.join("runtime/python313.dll")).unwrap(),
            b"dll"
        );
        assert!(fs::write(copied.join("runtime/Lib/os.py"), b"tampered").is_err());
        fs::write(root.join("runtime/python313.dll"), b"changed source").unwrap();
        lease.verify_unchanged().unwrap();
        drop(lease);
        assert!(!copied.exists());
        remove(&root);
    }
    #[test]
    fn windows_python_lease_rejects_dll_tampering_extras_and_wrong_authority() {
        let (root, exe, hash, exe_hash) = fixture("tamper");
        assert!(python_lease_at(&exe, &root, &"0".repeat(64), &exe_hash).is_err());
        fs::write(root.join("extra.dll"), b"extra").unwrap();
        assert!(python_lease_at(&exe, &root, &hash, &exe_hash).is_err());
        fs::remove_file(root.join("extra.dll")).unwrap();
        fs::write(root.join("runtime/python313.dll"), b"bad").unwrap();
        assert!(python_lease_at(&exe, &root, &hash, &exe_hash).is_err());
        remove(&root);
    }
    fn native_tool(name: &str) -> (VerifiedTool, PathBuf) {
        use std::os::windows::ffi::OsStringExt;
        let mut buffer = vec![0u16; 32768];
        let n = unsafe {
            windows_sys::Win32::System::SystemInformation::GetWindowsDirectoryW(
                buffer.as_mut_ptr(),
                buffer.len() as u32,
            )
        };
        assert!(n > 0);
        let path = PathBuf::from(std::ffi::OsString::from_wide(&buffer[..n as usize]))
            .join("System32")
            .join(name);
        // System32 binaries can legitimately be WinSxS hardlinks. Copy a
        // trusted OS binary into this test's private unique-file fixture.
        let root = env::temp_dir().join(format!(
            "tobkiri-job-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        win::create_private_directory(&root, &win::current_user_sid().unwrap()).unwrap();
        let target = root.join(name);
        fs::copy(&path, &target).unwrap();
        let path = target;
        let pin = Arc::new(win::open_pinned(&path, false).unwrap());
        let metadata = pin.file.metadata().unwrap();
        (
            VerifiedTool {
                kind: "native-test".into(),
                original_path: path,
                identity: file_identity(&metadata),
                windows_python: None,
                lock: pin.file.try_clone().unwrap(),
                windows_executable: pin,
            },
            root,
        )
    }
    #[test]
    fn windows_job_captures_output_after_reaping() {
        let (tool, root) = native_tool("cmd.exe");
        let mut command = VerifiedCommand::new(&tool);
        command.args(["/D", "/C", "echo success"]);
        let output = command
            .output_with_budget(VerifiedOutputBudget {
                duration: std::time::Duration::from_secs(10),
            })
            .unwrap();
        assert!(output.status.success());
        assert!(String::from_utf8_lossy(&output.stdout).contains("success"));
        drop(command);
        drop(tool);
        remove(&root);
    }
    #[test]
    fn windows_job_honors_timeout_and_reaps_process() {
        let (tool, root) = native_tool("ping.exe");
        let mut command = VerifiedCommand::new(&tool);
        command.args(["-n", "60", "127.0.0.1"]);
        let start = std::time::Instant::now();
        let error = command
            .output_with_budget(VerifiedOutputBudget {
                duration: std::time::Duration::from_millis(150),
            })
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        assert!(start.elapsed() < std::time::Duration::from_secs(5));
        drop(command);
        drop(tool);
        remove(&root);
    }
    #[test]
    fn windows_job_reaps_descendant_inheriting_output() {
        let (tool, root) = native_tool("cmd.exe");
        let mut command = VerifiedCommand::new(&tool);
        command.args([
            "/D",
            "/C",
            "start /B ping.exe -n 60 127.0.0.1 & echo parent-complete",
        ]);
        let start = std::time::Instant::now();
        let output = command
            .output_with_budget(VerifiedOutputBudget {
                duration: std::time::Duration::from_secs(10),
            })
            .unwrap();
        assert!(String::from_utf8_lossy(&output.stdout).contains("parent-complete"));
        assert!(start.elapsed() < std::time::Duration::from_secs(5));
        drop(command);
        drop(tool);
        remove(&root);
    }
    #[test]
    fn windows_python_rejects_external_venv_home_and_system_site() {
        assert!(validate_venv_config(
            b"home = C:\\external\ninclude-system-site-packages = false\n"
        )
        .is_err());
        assert!(
            validate_venv_config(b"home = runtime\ninclude-system-site-packages = true\n").is_err()
        );
        assert!(validate_venv_config(
            b"home = runtime\nhome = runtime\ninclude-system-site-packages = false\n"
        )
        .is_err());
    }
    #[test]
    #[ignore = "requires independently bound real Windows sealed Python environment"]
    fn windows_bound_python_reports_private_import_paths() {
        let tool = verified_tool("python").unwrap();
        let root = tool.windows_python.as_ref().unwrap().root.clone();
        let mut command = tool.command().unwrap();
        command.args(["-c", "import sys, os, json, _ssl, _hashlib, importlib.util; cryptography=__import__('cryptography' if importlib.util.find_spec('cryptography') else 'tobkiri_lease_smoke'); root=os.path.normcase(os.path.realpath(sys.argv[1])); paths=[*sys.path,sys.executable,sys.prefix,sys.base_prefix,_ssl.__file__,_hashlib.__file__,cryptography.__file__]; assert all(os.path.commonpath([root,os.path.normcase(os.path.realpath(p))])==root for p in paths if p), paths; assert os.path.normcase(os.path.realpath(sys.prefix))==os.path.join(root,'venv'); assert os.path.normcase(os.path.realpath(sys.base_prefix))==os.path.join(root,'runtime'); print(json.dumps({'prefix':sys.prefix,'base_prefix':sys.base_prefix,'paths':paths}))"]).arg(&root);
        let output = command
            .output_with_budget(VerifiedOutputBudget {
                duration: std::time::Duration::from_secs(30),
            })
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(report["paths"].as_array().unwrap().len() >= 7);
        drop(command);
        drop(tool);
        assert!(!root.exists());
    }
    #[test]
    fn windows_job_reaps_injected_assignment_and_resume_failures() {
        let (tool, root) = native_tool("cmd.exe");
        for fault in [SpawnFault::Assign, SpawnFault::Resume] {
            let mut command = VerifiedCommand::new(&tool);
            command.args(["/D", "/C", "echo must-not-run"]);
            assert!(matches!(
                spawn_inner(&command, true, fault),
                VerifiedSpawnOutcome::ReapedFailure(_)
            ));
        }
        drop(tool);
        remove(&root);
    }
    #[test]
    fn windows_output_reader_enforces_cap_and_reports_read_failure() {
        let (tx, rx) = std::sync::mpsc::channel();
        let thread = output_reader(std::io::Cursor::new(vec![0u8; 32]), 0, tx, 16).unwrap();
        assert!(rx
            .recv()
            .unwrap()
            .1
            .unwrap_err()
            .to_string()
            .contains("cap"));
        thread.join().unwrap();
        struct Broken;
        impl Read for Broken {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("injected read failure"))
            }
        }
        let (tx, rx) = std::sync::mpsc::channel();
        let thread = output_reader(Broken, 0, tx, 16).unwrap();
        assert!(rx.recv().unwrap().1.is_err());
        thread.join().unwrap();
    }
}
