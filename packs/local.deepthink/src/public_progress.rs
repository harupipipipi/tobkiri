//! Bounded persistence of accepted public model evidence.
//!
//! Only text deltas are coalesced: after each accepted event, fewer than 64
//! delta records and fewer than 4096 UTF-8 bytes may remain dirty. The incoming
//! provider event/batch retains its separate existing bounds; accepting one
//! event may temporarily cross either threshold before its synchronous flush.
//! Identity changes, usage, completion and stream/error boundaries flush the
//! accepted prefix. Content-free heartbeats never add public evidence.
//!
//! A process crash or an inactive/deadline-bound storage call can lose this
//! unflushed tail. Such a tail is not durable, complete, or safe to regenerate:
//! the attempt claim/pending checkpoint still requires reconciliation. A failed
//! save poisons this recorder, including when its commit status is unknown.

use crate::workflow::{Failure, GenerationProgress, invalid};
use serde_json::Value;

const MAX_DIRTY_DELTAS: usize = 64;
const MAX_DIRTY_BYTES: usize = 4096;

/// Per-generation recorder; never retained across exports or persisted itself.
pub(super) struct PublicProgress<'a> {
    progress: &'a mut GenerationProgress,
    dirty: bool,
    dirty_deltas: usize,
    dirty_bytes: usize,
    poisoned: Option<Failure>,
}

impl<'a> PublicProgress<'a> {
    /// `progress` must already have its initial empty snapshot confirmed before
    /// the caller starts any model dispatch. Construction performs no write.
    pub(super) fn new(progress: &'a mut GenerationProgress) -> Self {
        Self {
            progress,
            dirty: false,
            dirty_deltas: 0,
            dirty_bytes: 0,
            poisoned: None,
        }
    }

    pub(super) fn snapshot(&self) -> &GenerationProgress {
        self.progress
    }

    /// Process a bounded provider batch without copying its growing text prefix.
    pub(super) fn accept_batch(
        &mut self,
        events: Vec<String>,
        persist: impl FnMut(&GenerationProgress) -> Result<(), Failure>,
    ) -> Result<(), Failure> {
        self.batch(events, persist, false)
    }

    fn batch(
        &mut self,
        events: Vec<String>,
        mut persist: impl FnMut(&GenerationProgress) -> Result<(), Failure>,
        eager: bool,
    ) -> Result<(), Failure> {
        self.ready()?;
        for raw in events {
            let accepted = serde_json::from_str::<Value>(&raw)
                .map_err(|_| invalid("provider returned an invalid stream event"))
                .and_then(|event| self.accept(&event));
            let accepted = match accepted {
                Ok(accepted) => accepted,
                Err(error) => return Err(self.fail(error, &mut persist)),
            };
            self.dirty |= accepted.changed;
            if let Some(bytes) = accepted.delta_bytes {
                self.dirty_deltas = self.dirty_deltas.saturating_add(1);
                self.dirty_bytes = self.dirty_bytes.saturating_add(bytes);
            }
            if !eager
                && (accepted.boundary
                    || self.dirty_deltas >= MAX_DIRTY_DELTAS
                    || self.dirty_bytes >= MAX_DIRTY_BYTES)
            {
                self.flush(&mut persist)?;
            }
        }
        if eager {
            self.flush(&mut persist)?;
        }
        Ok(())
    }

    // accept() can partly update usage/identity before rejecting a later field.
    // Roll back those small fields and append length, not the full public text.
    fn accept(&mut self, event: &Value) -> Result<Accepted, Failure> {
        let length = self.progress.public_text.len();
        let completed = self.progress.completed;
        let usage = self.progress.usage.clone();
        let metadata = self.progress.metadata.clone();
        if let Err(error) = self.progress.accept(event) {
            self.progress.public_text.truncate(length);
            self.progress.completed = completed;
            self.progress.usage = usage;
            self.progress.metadata = metadata;
            return Err(error);
        }
        let metadata_changed = self.progress.metadata != metadata;
        let changed = self.progress.public_text.len() != length
            || self.progress.completed != completed
            || self.progress.usage != usage
            || metadata_changed;
        let kind = event["kind"].as_str();
        Ok(Accepted {
            changed,
            delta_bytes: (kind == Some("delta"))
                .then_some(self.progress.public_text.len().saturating_sub(length)),
            boundary: metadata_changed || matches!(kind, Some("usage" | "done" | "progress")),
        })
    }

    fn ready(&self) -> Result<(), Failure> {
        self.poisoned.clone().map_or(Ok(()), Err)
    }

    fn poison(&mut self, error: Failure) -> Failure {
        let error = if error.code == "checkpoint-unconfirmed" {
            error
        } else {
            Failure {
                code: "checkpoint-unconfirmed".into(),
                message: "Generation snapshot write was not confirmed; do not automatically repeat the request."
                    .into(),
            }
        };
        self.poisoned = Some(error.clone());
        error
    }

    fn flush(
        &mut self,
        mut persist: impl FnMut(&GenerationProgress) -> Result<(), Failure>,
    ) -> Result<(), Failure> {
        self.ready()?;
        if self.dirty {
            if let Err(error) = self.progress.next_snapshot() {
                return Err(self.poison(error));
            }
            if let Err(error) = persist(self.progress) {
                // No retry, reset, subsequent event, or further snapshot write.
                return Err(self.poison(error));
            }
            self.dirty = false;
        }
        self.dirty_deltas = 0;
        self.dirty_bytes = 0;
        Ok(())
    }

    /// Flush accepted evidence before surfacing the original read/validation
    /// error. A failed or already-unconfirmed save takes precedence.
    pub(super) fn fail(
        &mut self,
        error: Failure,
        persist: impl FnMut(&GenerationProgress) -> Result<(), Failure>,
    ) -> Failure {
        if let Some(poisoned) = &self.poisoned {
            return poisoned.clone();
        }
        if error.code == "checkpoint-unconfirmed" {
            return self.poison(error);
        }
        match self.flush(persist) {
            Ok(()) => error,
            Err(error) => error,
        }
    }

    /// Empty provider batches are EOF, never a synthetic successful completion.
    pub(super) fn eof(
        &mut self,
        persist: impl FnMut(&GenerationProgress) -> Result<(), Failure>,
    ) -> Result<(), Failure> {
        self.flush(persist)?;
        if !self.progress.completed {
            return Err(invalid(
                "provider stream ended before successful completion",
            ));
        }
        Ok(())
    }
}

struct Accepted {
    changed: bool,
    delta_bytes: Option<usize>,
    boundary: bool,
}

/// Preserve the existing eager batch test contract through the same acceptance
/// and rollback path. Production calls `accept_batch` with coalescing enabled.
#[cfg(test)]
pub(super) fn record_eager_batch(
    progress: &mut GenerationProgress,
    events: Vec<String>,
    persist: impl FnOnce(&GenerationProgress) -> Result<(), Failure>,
) -> Result<(), Failure> {
    let mut persist = Some(persist);
    PublicProgress::new(progress).batch(
        events,
        |snapshot| persist.take().expect("eager batch saves at most once")(snapshot),
        true,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::workflow::AttemptContext;
    use serde_json::json;
    use std::cell::Cell;

    fn progress() -> GenerationProgress {
        GenerationProgress::new(AttemptContext {
            run_id: "0".repeat(64),
            run_request_digest: "1".repeat(64),
            phase: "goal_intake".into(),
            generation_key: "goal_intake@0:0#0".into(),
            attempt_id: "goal_intake@0:0#0:0".into(),
            request_digest: "2".repeat(64),
            route_digest: "3".repeat(64),
        })
    }

    fn delta(text: &str) -> String {
        json!({"kind":"delta","text":text}).to_string()
    }

    fn metadata(id: &str) -> String {
        json!({"kind":"metadata","text":json!({
            "version":"tobkiri.chat.metadata.v1","generation_id":id
        }).to_string()})
        .to_string()
    }

    fn unavailable() -> Failure {
        Failure {
            code: "unavailable".into(),
            message: "fixture transport stopped".into(),
        }
    }

    #[test]
    fn one_event_stream_coalesces_text_with_early_identity_and_full_prefix() {
        let mut progress = progress();
        // Runtime confirms this initial empty snapshot before POST.
        progress.next_snapshot().unwrap();
        let mut saved = vec![progress.clone()];
        let mut recorder = PublicProgress::new(&mut progress);
        recorder
            .accept_batch(vec![metadata("gen-fixture")], |snapshot| {
                saved.push(snapshot.clone());
                Ok(())
            })
            .unwrap();
        assert_eq!(saved.len(), 2);
        assert_eq!(saved[1].metadata["generation_id"], "gen-fixture");
        for _ in 0..218 {
            recorder
                .accept_batch(vec![delta("x")], |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                })
                .unwrap();
            assert!(recorder.dirty_deltas < MAX_DIRTY_DELTAS);
            assert!(recorder.dirty_bytes < MAX_DIRTY_BYTES);
        }
        assert_eq!(saved.len(), 5); // empty + metadata + three 64-record flushes.
        recorder
            .accept_batch(
                vec![json!({"kind":"usage","prompt-tokens":13,"completion-tokens":7}).to_string()],
                |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                },
            )
            .unwrap();
        recorder
            .accept_batch(
                vec![json!({"kind":"done","text":"x".repeat(218)}).to_string()],
                |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                },
            )
            .unwrap();
        recorder
            .eof(|_| panic!("EOF after confirmed done has nothing to save"))
            .unwrap();
        assert_eq!(saved.len(), 7);
        assert_eq!(saved.last().unwrap().sequence, 7);
        assert_eq!(saved.last().unwrap().public_text, "x".repeat(218));
        assert!(saved.last().unwrap().completed);
        assert_eq!(saved.last().unwrap().usage["input_tokens"], 13);
        assert!(
            saved
                .windows(2)
                .all(|pair| pair[1].public_text.starts_with(&pair[0].public_text))
        );
    }

    #[test]
    fn utf8_bytes_flush_before_record_limit_and_large_single_delta_is_bounded() {
        let mut progress = progress();
        let mut recorder = PublicProgress::new(&mut progress);
        let mut lengths = Vec::new();
        let text = "あ".repeat(341); // 1023 UTF-8 bytes per accepted record.
        for index in 0..5 {
            recorder
                .accept_batch(vec![delta(&text)], |snapshot| {
                    lengths.push(snapshot.public_text.len());
                    Ok(())
                })
                .unwrap();
            assert!(recorder.dirty_bytes < MAX_DIRTY_BYTES);
            assert!(recorder.dirty_deltas < MAX_DIRTY_DELTAS);
            assert_eq!(lengths.len(), usize::from(index == 4));
        }
        assert_eq!(lengths, vec![5115]);
        recorder
            .accept_batch(vec![delta(&"z".repeat(8192))], |snapshot| {
                lengths.push(snapshot.public_text.len());
                Ok(())
            })
            .unwrap();
        assert_eq!(lengths, vec![5115, 13307]);
        assert_eq!((recorder.dirty_deltas, recorder.dirty_bytes), (0, 0));
    }

    #[test]
    fn usage_done_and_changed_metadata_flush_but_duplicate_identity_does_not() {
        let mut progress = progress();
        let mut recorder = PublicProgress::new(&mut progress);
        let mut saved = Vec::new();
        recorder
            .accept_batch(
                vec![delta("3"), metadata("gen-fixture"), delta("9")],
                |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                },
            )
            .unwrap();
        assert_eq!(saved.len(), 1);
        assert_eq!(saved[0].public_text, "3");
        recorder
            .accept_batch(vec![metadata("gen-fixture")], |_| {
                panic!("unchanged identity must not force a text flush")
            })
            .unwrap();
        recorder
            .accept_batch(
                vec![json!({"kind":"usage","prompt-tokens":5}).to_string()],
                |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                },
            )
            .unwrap();
        assert_eq!(saved.len(), 2);
        assert_eq!(saved[1].public_text, "39");
        recorder
            .accept_batch(
                vec![delta("1"), json!({"kind":"done","text":"391"}).to_string()],
                |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                },
            )
            .unwrap();
        assert_eq!(saved.len(), 3);
        assert_eq!(saved[2].public_text, "391");
        assert!(saved[2].completed);
    }

    #[test]
    fn transport_error_and_eof_flush_prefix_without_making_it_complete() {
        for eof in [false, true] {
            let mut progress = progress();
            let mut recorder = PublicProgress::new(&mut progress);
            recorder
                .accept_batch(vec![delta("public prefix")], |_| {
                    panic!("text is coalesced")
                })
                .unwrap();
            let mut saved = Vec::new();
            let mut persist = |snapshot: &GenerationProgress| {
                saved.push(snapshot.clone());
                Ok(())
            };
            let error = if eof {
                recorder.eof(&mut persist).unwrap_err()
            } else {
                recorder.fail(unavailable(), &mut persist)
            };
            assert_eq!(saved.len(), 1);
            assert_eq!(saved[0].public_text, "public prefix");
            assert!(!saved[0].completed);
            assert_eq!(error.code, if eof { "invalid" } else { "unavailable" });
        }
    }

    #[test]
    fn later_invalid_event_flushes_accepted_prefix_and_rolls_back_partial_fields() {
        for invalid_event in [
            "malformed-json".into(),
            metadata("gen-different"),
            json!({"kind":"usage","prompt-tokens":6,"completion-tokens":8}).to_string(),
            json!({"kind":"done","text":"wrong"}).to_string(),
        ] {
            let mut progress = progress();
            progress.metadata = json!({"generation_id":"gen-original"});
            progress.usage = json!({"input_tokens":5,"output_tokens":9});
            let mut recorder = PublicProgress::new(&mut progress);
            let mut saved = Vec::new();
            let error = recorder
                .accept_batch(vec![delta("391"), invalid_event], |snapshot| {
                    saved.push(snapshot.clone());
                    Ok(())
                })
                .unwrap_err();
            assert_eq!(error.code, "invalid");
            assert_eq!(saved.len(), 1);
            assert_eq!(saved[0].public_text, "391");
            assert_eq!(saved[0].metadata, json!({"generation_id":"gen-original"}));
            assert_eq!(saved[0].usage, json!({"input_tokens":5,"output_tokens":9}));
            assert!(!saved[0].completed);
        }
    }

    #[test]
    fn failed_save_poison_prevents_retry_next_pull_and_sequence_reuse() {
        let mut progress = progress();
        let mut recorder = PublicProgress::new(&mut progress);
        let writes = Cell::new(0);
        let pulls = Cell::new(0);
        let mut batches =
            [vec![delta(&"x".repeat(4096))], vec![delta("must not pull")]].into_iter();
        let error = (|| -> Result<(), Failure> {
            while let Some(events) = batches.next() {
                pulls.set(pulls.get() + 1);
                recorder.accept_batch(events, |_| {
                    writes.set(writes.get() + 1);
                    Err(Failure {
                        code: "core-internal".into(),
                        message: "unknown commit".into(),
                    })
                })?;
            }
            Ok(())
        })()
        .unwrap_err();
        assert_eq!(error.code, "checkpoint-unconfirmed");
        assert_eq!((pulls.get(), writes.get()), (1, 1));
        let sequence = recorder.snapshot().sequence;
        for error in [
            recorder.fail(unavailable(), |_| panic!("poison forbids another save")),
            recorder
                .eof(|_| panic!("poison forbids EOF save"))
                .unwrap_err(),
            recorder
                .accept_batch(vec![delta("rejected")], |_| {
                    panic!("poison forbids event save")
                })
                .unwrap_err(),
        ] {
            assert_eq!(error.code, "checkpoint-unconfirmed");
        }
        assert_eq!(recorder.snapshot().sequence, sequence);
        assert_eq!(recorder.snapshot().public_text, "x".repeat(4096));
    }

    #[test]
    fn private_heartbeat_excludes_text_and_only_flushes_existing_public_evidence() {
        let mut progress = progress();
        let mut recorder = PublicProgress::new(&mut progress);
        let heartbeat =
            json!({"kind":"progress","text":"private marker must never persist"}).to_string();
        recorder
            .accept_batch(vec![heartbeat.clone()], |_| {
                panic!("private-only batch cannot write")
            })
            .unwrap();
        assert_eq!(recorder.snapshot().sequence, 0);
        recorder
            .accept_batch(vec![delta("public")], |_| panic!("coalesced text"))
            .unwrap();
        let mut saved = None;
        recorder
            .accept_batch(vec![heartbeat], |snapshot| {
                saved = Some(snapshot.clone());
                Ok(())
            })
            .unwrap();
        let saved = saved.unwrap();
        assert_eq!(saved.public_text, "public");
        assert!(
            !serde_json::to_string(&saved)
                .unwrap()
                .contains("private marker")
        );
        assert_eq!(saved.sequence, 1);
        assert_eq!((recorder.dirty_deltas, recorder.dirty_bytes), (0, 0));
    }
}
