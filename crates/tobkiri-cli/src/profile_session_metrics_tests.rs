use super::*;
use std::collections::VecDeque;
use tobkiri_core::closed_metrics::{Metric, Observation};

struct Input {
    lines: VecDeque<Vec<u8>>,
    reads: usize,
}
#[async_trait(?Send)]
impl RequestInput for Input {
    async fn next(&mut self) -> Result<Option<Vec<u8>>> {
        self.reads += 1;
        Ok(self.lines.pop_front())
    }
}
struct ObservedCalls {
    observation: Option<Observation>,
    fail: bool,
    closes: usize,
    calls: usize,
}
#[async_trait(?Send)]
impl Dispatcher for ObservedCalls {
    async fn invoke(&mut self, _: &Invocation) -> Result<()> {
        self.calls += 1;
        // Unit fixture bank; real Core/Store ownership is covered separately.
        let observation = Observation::try_new(&Default::default())?;
        let timer = observation.span(Metric::RequestWall);
        let allocation = observation.span(Metric::AllocationMetered);
        allocation.add_units(12, 3);
        allocation.finish();
        self.observation = Some(observation);
        tokio::task::yield_now().await;
        timer.finish();
        if self.fail {
            return Err(CoreError::Step(tobkiri_core::StepError::new(
                "filesystem-commit-unknown",
                "fixed unknown fixture",
            ))
            .into());
        }
        Ok(())
    }
    async fn close(&mut self) -> Result<(), CoreError> {
        self.closes += 1;
        Ok(())
    }
    fn take_observation(&mut self) -> Option<Observation> {
        self.observation.take()
    }
}
fn command(id: u64) -> Vec<u8> {
    format!(r#"{{"id":{id},"shape":"local.fixture.control@1.0","op":"status","args":{{}}}}"#)
        .into_bytes()
}
fn fixture() -> (Input, ObservedCalls) {
    (
        Input {
            lines: VecDeque::from([command(1), command(2), br#"{"id":3,"close":true}"#.to_vec()]),
            reads: 0,
        },
        ObservedCalls {
            observation: None,
            fail: false,
            closes: 0,
            calls: 0,
        },
    )
}

#[tokio::test]
async fn closed_metrics_are_separate_per_request_and_only_emitted_at_terminal() {
    let (mut input, mut calls) = fixture();
    let mut output = Vec::new();
    run_loop(&mut input, &mut calls, &mut output).await.unwrap();
    let rows: Vec<serde_json::Value> = std::str::from_utf8(&output)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let metrics: Vec<_> = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row["kind"] == "request_metrics")
        .collect();
    assert_eq!(metrics.len(), 2);
    for (n, (index, row)) in metrics.iter().enumerate() {
        assert_eq!(row["id"], (n + 1) as u64);
        assert_eq!(rows[*index - 1]["kind"], "request_finished");
        assert_eq!(row["slots"][Metric::AllocationMetered as usize][0], 1);
        assert!(row["slots"][4].is_null());
        assert_eq!(row["physical_cleanup_proven"], false);
        assert_eq!(row["terminal_outcome_covered"], false);
    }
    assert_eq!(calls.closes, 1);
    assert_eq!(input.reads, 3);
}

#[tokio::test]
async fn closed_metrics_never_clear_no_replay_or_read_another_request_after_error() {
    let (mut input, mut calls) = fixture();
    calls.fail = true;
    let mut output = Vec::new();
    let error = run_loop(&mut input, &mut calls, &mut output)
        .await
        .unwrap_err();
    assert!(
        matches!(error.downcast_ref::<CoreError>(), Some(CoreError::Step(step)) if step.code == "filesystem-commit-unknown")
    );
    assert_eq!(calls.calls, 1);
    assert_eq!(input.reads, 1);
    assert_eq!(calls.closes, 1);
    let rows: Vec<serde_json::Value> = std::str::from_utf8(&output)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    let metrics = rows
        .iter()
        .find(|row| row["kind"] == "request_metrics")
        .unwrap();
    assert_eq!(metrics["request_ok"], false);
    assert_eq!(rows.last().unwrap()["ok"], false);
}

struct FailedMetricsOutput(Vec<u8>);
impl Write for FailedMetricsOutput {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.starts_with(b"{\"kind\":\"request_metrics\"") {
            return Err(std::io::Error::other("fixed metric output failure"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

#[tokio::test]
async fn closed_metrics_output_failure_preserves_original_unknown_and_stops_input() {
    let (mut input, mut calls) = fixture();
    calls.fail = true;
    let mut output = FailedMetricsOutput(Vec::new());
    let error = run_loop(&mut input, &mut calls, &mut output)
        .await
        .unwrap_err();
    assert!(
        matches!(error.downcast_ref::<CoreError>(), Some(CoreError::Step(step)) if step.code == "filesystem-commit-unknown")
    );
    assert_eq!(input.reads, 1);
    assert_eq!(calls.calls, 1);
    assert_eq!(calls.closes, 1);
}
