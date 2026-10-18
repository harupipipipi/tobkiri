//! Public generation consumption with explicit cleanup custody.
//!
//! Core owns cleanup after a pull error and after delivering terminal EOF. A
//! returned error is not proof that cleanup succeeded: Core retains any unknown
//! close receipt itself. The guest must preserve the failure and pending claim,
//! rather than issue a second void cancel against a possibly consumed handle.

use crate::workflow::Failure;

/// One generation's production stream and public progress recorder. This is an
/// internal Rust boundary, not a guest-supplied cleanup or approval attestation.
pub(super) trait Driver {
    fn active(&mut self) -> Result<(), Failure>;
    fn pull(&mut self) -> Result<Vec<String>, Failure>;
    fn accept_batch(&mut self, events: Vec<String>) -> Result<(), Failure>;
    fn fail(&mut self, error: Failure) -> Failure;
    fn eof(&mut self) -> Result<(), Failure>;
    fn cancel(&mut self);
}

#[derive(Clone, Copy)]
enum CleanupCustody {
    Guest,
    Core,
}

/// Consume the actual provider once, preserving the original progress/error
/// precedence. No retry, completion inference or dispatch permit is minted.
pub(super) fn run(driver: &mut impl Driver) -> Result<(), Failure> {
    let mut cleanup = CleanupCustody::Guest;
    let outcome = loop {
        if let Err(error) = driver.active() {
            break Err(driver.fail(error));
        }
        let events = match driver.pull() {
            Ok(events) => events,
            Err(error) => {
                // Core's pull owns cleanup, including any retained unknown
                // receipt. This transfers responsibility, not a success ACK.
                cleanup = CleanupCustody::Core;
                break Err(driver.fail(error));
            }
        };
        if events.is_empty() {
            // Core delivers EOF after retiring its stream handle. A missing
            // done event or failed prefix save still fails below; neither may
            // cancel that already-consumed handle a second time.
            cleanup = CleanupCustody::Core;
            break driver.eof();
        }
        if let Err(error) = driver.accept_batch(events) {
            break Err(error);
        }
    };
    if outcome.is_err() && matches!(cleanup, CleanupCustody::Guest) {
        // Local active/parse/persistence failures still own an open stream.
        // The frozen WIT operation is void; do not fabricate a cleanup Result.
        driver.cancel();
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        public_progress::PublicProgress,
        workflow::{AttemptContext, GenerationProgress},
    };
    use serde_json::json;
    use std::collections::VecDeque;

    fn failure(code: &str) -> Failure {
        Failure {
            code: code.into(),
            message: format!("fixture {code}"),
        }
    }

    fn progress() -> GenerationProgress {
        GenerationProgress::new(AttemptContext {
            run_id: "0".repeat(64),
            run_request_digest: "1".repeat(64),
            phase: "goal_contract".into(),
            generation_key: "goal_contract@0:0#0".into(),
            attempt_id: "goal_contract@0:0#0:0".into(),
            request_digest: "2".repeat(64),
            route_digest: "3".repeat(64),
        })
    }

    fn delta(text: &str) -> String {
        json!({"kind":"delta","text":text}).to_string()
    }

    fn done(text: &str) -> String {
        json!({"kind":"done","text":text}).to_string()
    }

    struct FakeSource<'a> {
        public: PublicProgress<'a>,
        batches: VecDeque<Result<Vec<String>, Failure>>,
        active_failure_after_pulls: Option<usize>,
        persistence_failure: Option<Failure>,
        core_owns_cleanup: bool,
        pulls: usize,
        cancels: usize,
        save_attempts: usize,
        saved: Vec<GenerationProgress>,
    }

    impl<'a> FakeSource<'a> {
        fn new(
            progress: &'a mut GenerationProgress,
            batches: impl IntoIterator<Item = Result<Vec<String>, Failure>>,
        ) -> Self {
            Self {
                public: PublicProgress::new(progress),
                batches: batches.into_iter().collect(),
                active_failure_after_pulls: None,
                persistence_failure: None,
                core_owns_cleanup: false,
                pulls: 0,
                cancels: 0,
                save_attempts: 0,
                saved: vec![],
            }
        }
    }

    fn persist(
        snapshot: &GenerationProgress,
        save_attempts: &mut usize,
        saved: &mut Vec<GenerationProgress>,
        error: &Option<Failure>,
    ) -> Result<(), Failure> {
        *save_attempts += 1;
        if let Some(error) = error {
            return Err(error.clone());
        }
        saved.push(snapshot.clone());
        Ok(())
    }

    impl Driver for FakeSource<'_> {
        fn active(&mut self) -> Result<(), Failure> {
            if self.active_failure_after_pulls == Some(self.pulls) {
                Err(failure("cancelled"))
            } else {
                Ok(())
            }
        }

        fn pull(&mut self) -> Result<Vec<String>, Failure> {
            assert!(!self.core_owns_cleanup, "no read/retry after host cleanup");
            self.pulls += 1;
            let batch = self.batches.pop_front().expect("unexpected extra pull");
            if batch.as_ref().map_or(true, |events| events.is_empty()) {
                // This models custody, NOT physical cleanup success. Even an
                // unknown close receipt is still Core-owned and cannot remint
                // a stream or make a guest's second cancel safe.
                self.core_owns_cleanup = true;
            }
            batch
        }

        fn accept_batch(&mut self, events: Vec<String>) -> Result<(), Failure> {
            let Self {
                public,
                save_attempts,
                saved,
                persistence_failure,
                ..
            } = self;
            public.accept_batch(events, |snapshot| {
                persist(snapshot, save_attempts, saved, persistence_failure)
            })
        }

        fn fail(&mut self, error: Failure) -> Failure {
            let Self {
                public,
                save_attempts,
                saved,
                persistence_failure,
                ..
            } = self;
            public.fail(error, |snapshot| {
                persist(snapshot, save_attempts, saved, persistence_failure)
            })
        }

        fn eof(&mut self) -> Result<(), Failure> {
            let Self {
                public,
                save_attempts,
                saved,
                persistence_failure,
                ..
            } = self;
            public.eof(|snapshot| persist(snapshot, save_attempts, saved, persistence_failure))
        }

        fn cancel(&mut self) {
            assert!(
                !self.core_owns_cleanup,
                "the old production pump traps by cancelling a consumed handle"
            );
            self.cancels += 1;
        }
    }

    #[test]
    fn typed_pull_error_flushes_unicode_prefix_without_second_cancel() {
        let mut progress = progress();
        let text = "公開の未完本文".repeat(10);
        let mut source = FakeSource::new(
            &mut progress,
            [Ok(vec![delta(&text)]), Err(failure("unavailable"))],
        );
        let error = run(&mut source).unwrap_err();
        assert_eq!(
            (error.code.as_str(), error.message.as_str()),
            ("unavailable", "fixture unavailable")
        );
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (2, 0, 1)
        );
        assert_eq!(source.saved[0].public_text, text);
        assert!(!source.saved[0].completed);
        assert_eq!(
            source.public.snapshot().attempt.attempt_id,
            "goal_contract@0:0#0:0"
        );
    }

    #[test]
    fn unknown_core_cleanup_error_is_preserved_without_claiming_a_close_ack() {
        let mut progress = progress();
        let unknown = Failure {
            code: "core-internal".into(),
            message: "filesystem-commit-unknown: do not replay".into(),
        };
        let mut source = FakeSource::new(
            &mut progress,
            [Ok(vec![delta("accepted prefix")]), Err(unknown.clone())],
        );
        let error = run(&mut source).unwrap_err();
        assert_eq!((error.code, error.message), (unknown.code, unknown.message));
        assert!(source.core_owns_cleanup);
        assert_eq!((source.pulls, source.cancels), (2, 0));
        assert_eq!(source.saved[0].public_text, "accepted prefix");
        assert!(!source.public.snapshot().completed);
    }

    #[test]
    fn prefix_save_failure_takes_precedence_over_pull_error_without_retry() {
        let mut progress = progress();
        let mut source = FakeSource::new(
            &mut progress,
            [
                Ok(vec![delta("not yet durable")]),
                Err(failure("unavailable")),
            ],
        );
        source.persistence_failure = Some(failure("core-internal"));
        let error = run(&mut source).unwrap_err();
        assert_eq!(error.code, "checkpoint-unconfirmed");
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (2, 0, 1)
        );
        assert!(source.saved.is_empty());
        assert!(!source.public.snapshot().completed);
    }

    #[test]
    fn incomplete_eof_flushes_prefix_without_cancelling_the_consumed_handle() {
        let mut progress = progress();
        let mut source = FakeSource::new(
            &mut progress,
            [Ok(vec![delta("未完の公開本文")]), Ok(vec![])],
        );
        let error = run(&mut source).unwrap_err();
        assert_eq!(error.code, "invalid");
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (2, 0, 1)
        );
        assert_eq!(source.saved[0].public_text, "未完の公開本文");
        assert!(!source.saved[0].completed);
    }

    #[test]
    fn incomplete_eof_save_failure_is_unknown_without_a_second_cancel() {
        let mut progress = progress();
        let mut source =
            FakeSource::new(&mut progress, [Ok(vec![delta("not durable")]), Ok(vec![])]);
        source.persistence_failure = Some(failure("core-internal"));
        assert_eq!(run(&mut source).unwrap_err().code, "checkpoint-unconfirmed");
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (2, 0, 1)
        );
        assert!(source.saved.is_empty());
    }

    #[test]
    fn local_active_failure_flushes_prefix_then_cancels_the_still_open_source() {
        let mut progress = progress();
        let mut source = FakeSource::new(&mut progress, [Ok(vec![delta("retained")])]);
        source.active_failure_after_pulls = Some(1);
        let error = run(&mut source).unwrap_err();
        assert_eq!(
            (error.code.as_str(), error.message.as_str()),
            ("cancelled", "fixture cancelled")
        );
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (1, 1, 1)
        );
        assert_eq!(source.saved[0].public_text, "retained");
    }

    #[test]
    fn local_active_and_save_failure_keep_checkpoint_precedence_and_cleanup() {
        let mut progress = progress();
        let mut source = FakeSource::new(&mut progress, [Ok(vec![delta("retained")])]);
        source.active_failure_after_pulls = Some(1);
        source.persistence_failure = Some(failure("core-internal"));
        assert_eq!(run(&mut source).unwrap_err().code, "checkpoint-unconfirmed");
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (1, 1, 1)
        );
    }

    #[test]
    fn local_parse_failure_retains_accepted_prefix_and_cancels_once() {
        let mut progress = progress();
        let mut source = FakeSource::new(
            &mut progress,
            [Ok(vec![delta("accepted"), "malformed-json".into()])],
        );
        assert_eq!(run(&mut source).unwrap_err().code, "invalid");
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (1, 1, 1)
        );
        assert_eq!(source.saved[0].public_text, "accepted");
        assert!(!source.saved[0].completed);
    }

    #[test]
    fn local_batch_save_failure_does_not_pull_again_and_cancels_once() {
        let mut progress = progress();
        let mut source = FakeSource::new(
            &mut progress,
            [
                Ok(vec![delta("x".repeat(4096).as_str())]),
                Ok(vec![delta("never read")]),
            ],
        );
        source.persistence_failure = Some(failure("core-internal"));
        assert_eq!(run(&mut source).unwrap_err().code, "checkpoint-unconfirmed");
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (1, 1, 1)
        );
        assert_eq!(source.batches.len(), 1);
        assert!(source.saved.is_empty());
    }

    #[test]
    fn normal_done_and_terminal_eof_do_not_cancel_or_make_an_extra_read() {
        let mut progress = progress();
        let mut source = FakeSource::new(
            &mut progress,
            [Ok(vec![delta("391"), done("391")]), Ok(vec![])],
        );
        run(&mut source).unwrap();
        assert_eq!(
            (source.pulls, source.cancels, source.save_attempts),
            (2, 0, 1)
        );
        assert!(source.saved[0].completed);
        assert_eq!(source.public.snapshot().public_text, "391");
    }
}
