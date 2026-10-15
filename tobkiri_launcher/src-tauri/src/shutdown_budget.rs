//! Finite shutdown budgets shared by runtime stops and the outer watchdog.

use std::thread;
use std::time::{Duration, Instant};

// These are upper bounds, not mandatory waits. Defaultspack first drains HTTP
// handlers (3s), then releases its VM domains. Allow three ordinary domain
// teardowns, or one silent helper's 120s exchange plus process cleanup. A larger
// or wedged domain set can still reach the forced-stop deadline.
pub(crate) const DEFAULTSPACK_GRACE: Duration = Duration::from_secs(150);
pub(crate) const KERNEL_GRACE: Duration = Duration::from_secs(2);
pub(crate) const DEFAULTSPACK_RECOVERY_GRACE: Duration = Duration::from_secs(2);
pub(crate) const FORCE_KILL_OBSERVATION: Duration = Duration::from_millis(1_500);
pub(crate) const RUNTIME_WATCHDOG: Duration = Duration::from_secs(155);
pub(crate) const CONCURRENT_EXIT_WAIT: Duration = Duration::from_secs(160);

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum ProcessObservation {
    Running,
    Exited,
    OwnershipLost,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum GracefulWait {
    Exited,
    OwnershipLost,
    TimedOut,
}

/// Poll verified process state, returning immediately after exit or lost ownership.
pub(crate) fn wait_for_graceful_exit<E>(
    grace: Duration,
    mut inspect: impl FnMut() -> Result<ProcessObservation, E>,
) -> Result<GracefulWait, E> {
    let deadline = Instant::now() + grace;
    loop {
        match inspect()? {
            ProcessObservation::Exited => return Ok(GracefulWait::Exited),
            ProcessObservation::OwnershipLost => return Ok(GracefulWait::OwnershipLost),
            ProcessObservation::Running => {}
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(GracefulWait::TimedOut);
        }
        thread::sleep(remaining.min(Duration::from_millis(100)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn watchdog_outlasts_grace_and_force_kill_observation() {
        let drain = Duration::from_millis(500) + Duration::from_secs(3);
        let ordinary_domains = 3 * (Duration::from_secs(15) + Duration::from_secs(15));
        let silent_helper = Duration::from_secs(120) + Duration::from_secs(15);
        assert!(DEFAULTSPACK_GRACE > drain + ordinary_domains);
        assert!(DEFAULTSPACK_GRACE > drain + silent_helper);
        assert_eq!(KERNEL_GRACE, Duration::from_secs(2));
        assert_eq!(DEFAULTSPACK_RECOVERY_GRACE, Duration::from_secs(2));
        assert!(RUNTIME_WATCHDOG > DEFAULTSPACK_GRACE + FORCE_KILL_OBSERVATION);
        assert!(CONCURRENT_EXIT_WAIT > RUNTIME_WATCHDOG);
    }

    #[test]
    fn completed_shutdown_does_not_wait_for_the_grace_deadline() {
        let started = Instant::now();
        let result = wait_for_graceful_exit(DEFAULTSPACK_GRACE, || {
            Ok::<_, ()>(ProcessObservation::Exited)
        });
        assert_eq!(result, Ok(GracefulWait::Exited));
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn lost_ownership_stops_waiting_without_authorizing_a_kill() {
        assert_eq!(
            wait_for_graceful_exit(DEFAULTSPACK_GRACE, || {
                Ok::<_, ()>(ProcessObservation::OwnershipLost)
            }),
            Ok(GracefulWait::OwnershipLost)
        );
    }

    #[test]
    fn inspection_errors_are_preserved() {
        assert_eq!(
            wait_for_graceful_exit(DEFAULTSPACK_GRACE, || Err::<ProcessObservation, _>(7)),
            Err(7)
        );
    }

    #[cfg(unix)]
    mod unix {
        use super::*;
        use std::io::{BufRead, BufReader};
        use std::process::{Child, Command, Stdio};

        struct TestChild(Child);

        impl Drop for TestChild {
            fn drop(&mut self) {
                let _ = self.0.kill();
                let _ = self.0.wait();
            }
        }

        fn term_child(script: &str) -> TestChild {
            let mut child = TestChild(
                Command::new("/bin/sh")
                    .args(["-c", script])
                    .stdin(Stdio::null())
                    .stdout(Stdio::piped())
                    .spawn()
                    .unwrap(),
            );
            let mut ready = String::new();
            BufReader::new(child.0.stdout.take().unwrap())
                .read_line(&mut ready)
                .unwrap();
            assert_eq!(ready, "ready\n");
            assert!(Command::new("/bin/kill")
                .args(["-TERM", &child.0.id().to_string()])
                .status()
                .unwrap()
                .success());
            child
        }

        fn observe(child: &mut TestChild) -> std::io::Result<ProcessObservation> {
            child.0.try_wait().map(|status| {
                if status.is_some() {
                    ProcessObservation::Exited
                } else {
                    ProcessObservation::Running
                }
            })
        }

        #[test]
        fn term_cleanup_can_finish_after_the_previous_two_second_limit() {
            let mut child =
                term_child("trap 'sleep 2.2; exit 0' TERM; printf 'ready\\n'; while :; do :; done");
            let started = Instant::now();
            let result = wait_for_graceful_exit(DEFAULTSPACK_GRACE, || observe(&mut child));
            assert_eq!(result.unwrap(), GracefulWait::Exited);
            assert!(started.elapsed() >= Duration::from_secs(2));
            assert!(started.elapsed() < Duration::from_secs(10));
            assert!(child.0.wait().unwrap().success());
        }

        #[test]
        fn an_unresponsive_process_reaches_the_finite_force_stop_boundary() {
            let mut child = term_child("trap '' TERM; printf 'ready\\n'; while :; do :; done");
            let started = Instant::now();
            let result = wait_for_graceful_exit(Duration::from_millis(100), || observe(&mut child));
            assert_eq!(result.unwrap(), GracefulWait::TimedOut);
            child.0.kill().unwrap();
            assert!(!child.0.wait().unwrap().success());
            assert!(started.elapsed() < Duration::from_secs(2));
        }
    }
}
