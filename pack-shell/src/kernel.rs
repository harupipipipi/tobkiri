use anyhow::{Context, Result};
use log::{debug, info, warn};
use std::process::{Child, Command, Stdio};

/// Manages the kernel subprocess lifecycle.
pub struct KernelProcess {
    cmd: String,
    child: Option<Child>,
}

impl KernelProcess {
    pub fn new(cmd: String) -> Self {
        Self { cmd, child: None }
    }

    /// Start the kernel process.
    /// The command string is split by whitespace into program + arguments.
    /// RUMI_PORT is set in the child environment.
    pub fn start(&mut self, port: u16) -> Result<()> {
        let parts: Vec<String> = shell_words::split(&self.cmd)
            .context("Failed to parse kernel_cmd (unmatched quote?)")?;
        if parts.is_empty() {
            anyhow::bail!("kernel_cmd is empty");
        }

        let program = &parts[0];
        let args = &parts[1..];

        info!("Starting kernel: {} (port={})", self.cmd, port);

        let child = Command::new(program)
            .args(args)
            .env("RUMI_PORT", port.to_string())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .spawn()
            .context(format!("Failed to spawn kernel: {}", program))?;

        info!("Kernel process started (pid={})", child.id());
        self.child = Some(child);
        Ok(())
    }

    /// Stop the kernel process gracefully, then forcefully if needed.
    pub fn stop(&mut self) -> Result<()> {
        if let Some(ref mut child) = self.child {
            info!("Stopping kernel process (pid={})...", child.id());

            // Phase 1: Send graceful termination signal
            #[cfg(unix)]
            {
                let pid = child.id() as i32;
                // SAFETY: pid is a valid child process ID obtained from Child::id()
                let ret = unsafe { libc::kill(pid, libc::SIGTERM) };
                if ret != 0 {
                    warn!(
                        "Failed to send SIGTERM (errno={}), falling back to SIGKILL",
                        std::io::Error::last_os_error()
                    );
                    let _ = child.kill();
                }
            }
            #[cfg(not(unix))]
            {
                // Windows: TerminateProcess is the only option
                let _ = child.kill();
            }

            // Phase 2: Wait up to 5 seconds for the process to exit
            match Self::wait_with_timeout(child, std::time::Duration::from_secs(5)) {
                Ok(_) => {
                    info!("Kernel process stopped.");
                }
                Err(_) => {
                    warn!(
                        "Kernel process did not exit within 5s after SIGTERM, sending SIGKILL..."
                    );
                    Self::force_kill(child);
                }
            }

            self.child = None;
        }
        Ok(())
    }

    /// Check if the kernel process is still running.
    pub fn is_running(&mut self) -> bool {
        if let Some(ref mut child) = self.child {
            match child.try_wait() {
                Ok(Some(_)) => {
                    // Process has exited
                    false
                }
                Ok(None) => {
                    // Still running
                    true
                }
                Err(e) => {
                    debug!("Failed to check process status: {}", e);
                    false
                }
            }
        } else {
            false
        }
    }

    /// Wait for child process with a timeout.
    fn wait_with_timeout(child: &mut Child, timeout: std::time::Duration) -> Result<()> {
        let start = std::time::Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return Ok(()),
                Ok(None) => {
                    if start.elapsed() >= timeout {
                        anyhow::bail!("Process did not exit within timeout");
                    }
                    std::thread::sleep(std::time::Duration::from_millis(100));
                }
                Err(e) => return Err(e.into()),
            }
        }
    }

    /// Force kill the process (Unix).
    #[cfg(unix)]
    fn force_kill(child: &mut Child) {
        // On Unix, child.kill() sends SIGKILL.
        let _ = child.kill();
        let _ = child.wait();
    }

    /// Force kill the process (Windows).
    #[cfg(windows)]
    fn force_kill(child: &mut Child) {
        // On Windows, child.kill() calls TerminateProcess.
        let _ = child.kill();
        let _ = child.wait();
    }

    /// Force kill fallback for other platforms.
    #[cfg(not(any(unix, windows)))]
    fn force_kill(child: &mut Child) {
        let _ = child.kill();
        let _ = child.wait();
    }
}

impl Drop for KernelProcess {
    fn drop(&mut self) {
        if self.child.is_some() {
            debug!("KernelProcess dropped, stopping child process...");
            if let Err(e) = self.stop() {
                warn!("Error stopping kernel on drop: {}", e);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::KernelProcess;

    #[test]
    fn is_running_is_false_before_start() {
        let mut kernel = KernelProcess::new("unused".to_string());

        assert!(!kernel.is_running());
    }

    #[test]
    fn start_rejects_empty_kernel_command() {
        let mut kernel = KernelProcess::new("   ".to_string());

        let error = kernel
            .start(8765)
            .expect_err("empty commands must be rejected");

        assert!(error.to_string().contains("kernel_cmd is empty"));
    }

    #[cfg(unix)]
    #[test]
    fn start_and_stop_tracks_owned_kernel_process() {
        let mut kernel =
            KernelProcess::new("sh -c 'trap \"exit 0\" TERM; while :; do :; done'".to_string());

        kernel.start(4321).expect("test kernel should start");
        assert!(kernel.is_running());

        kernel.stop().expect("test kernel should stop");

        assert!(!kernel.is_running());
    }
}
