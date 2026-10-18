//! A thin, sequential native User client for one existing Core Session.
use anyhow::{Result, anyhow, bail};
use async_trait::async_trait;
use std::io::Write;
use tobkiri_core::{
    CanonValue, Core, CoreError, Session, Variant,
    ids::{OpName, ProfileId, ShapeRef},
};
use tokio_util::sync::CancellationToken;

use crate::terminal::TerminalHost;

const MAX_REQUEST_BYTES: usize = 64 * 1024;
const MAX_REQUEST_ID: i64 = 9_007_199_254_740_991;

#[derive(Debug)]
struct Invocation {
    id: u64,
    shape: ShapeRef,
    operation: OpName,
    args: CanonValue,
}
#[derive(Debug)]
enum Request {
    Invoke(Invocation),
    Close { id: u64 },
}
impl Request {
    fn id(&self) -> u64 {
        match self {
            Self::Invoke(request) => request.id,
            Self::Close { id } => *id,
        }
    }
}

fn parse_request(bytes: &[u8]) -> Result<Request> {
    if bytes.len() > MAX_REQUEST_BYTES {
        bail!("session request exceeds 65536 bytes");
    }
    let CanonValue::Obj(mut fields) = tobkiri_core::parse(bytes, Variant::StrictV1)
        .map_err(|_| anyhow!("session request must be bounded StrictV1 JSON"))?
    else {
        bail!("session request must be an object");
    };
    let Some(CanonValue::Int(id @ 1..=MAX_REQUEST_ID)) = fields.remove("id") else {
        bail!("session id must be a positive safe integer");
    };
    if fields.contains_key("close") {
        if fields.len() != 1 || !matches!(fields.remove("close"), Some(CanonValue::Bool(true))) {
            bail!("session close requires only id and close:true");
        }
        return Ok(Request::Close { id: id as u64 });
    }
    if fields.len() != 3
        || fields
            .keys()
            .any(|key| !matches!(key.as_str(), "shape" | "op" | "args"))
    {
        bail!("session request requires only id, shape, op and args");
    }
    let Some(CanonValue::Str(shape)) = fields.remove("shape") else {
        bail!("session shape must be a string");
    };
    let Some(CanonValue::Str(operation)) = fields.remove("op") else {
        bail!("session op must be a string");
    };
    let Some(args @ CanonValue::Obj(_)) = fields.remove("args") else {
        bail!("session args must be an object");
    };
    Ok(Request::Invoke(Invocation {
        id: id as u64,
        shape: ShapeRef::parse(&shape).map_err(|_| anyhow!("invalid session shape"))?,
        operation: OpName::parse(&operation).map_err(|_| anyhow!("invalid session op"))?,
        args,
    }))
}

#[async_trait(?Send)]
trait RequestInput {
    async fn next(&mut self) -> Result<Option<Vec<u8>>>;
}

#[async_trait(?Send)]
trait Dispatcher {
    async fn invoke(&mut self, request: &Invocation) -> Result<()>;
    async fn close(&mut self) -> Result<(), CoreError>;
    #[cfg(feature = "closed-request-metrics")]
    fn take_observation(&mut self) -> Option<tobkiri_core::closed_metrics::Observation> {
        None
    }
}

struct NativeRequests<'a>(&'a TerminalHost);

#[async_trait(?Send)]
impl RequestInput for NativeRequests<'_> {
    async fn next(&mut self) -> Result<Option<Vec<u8>>> {
        let cancel = CancellationToken::new();
        let mut read = Box::pin(
            self.0
                .session_request_line(MAX_REQUEST_BYTES, cancel.clone()),
        );
        tokio::select! {
            result = &mut read => Ok(result?),
            _ = tokio::signal::ctrl_c() => {
                cancel.cancel();
                // The Windows worker keeps its input turn through the actual
                // read return. Await that same read; never open another reader.
                let _ = read.await;
                bail!("session command input cancelled; no command was replayed");
            }
        }
    }
}

struct CoreDispatcher<'a> {
    session: &'a Session,
    #[cfg(feature = "closed-request-metrics")]
    observation: Option<tobkiri_core::closed_metrics::Observation>,
}

#[async_trait(?Send)]
impl Dispatcher for CoreDispatcher<'_> {
    async fn invoke(&mut self, request: &Invocation) -> Result<()> {
        #[cfg(feature = "closed-request-metrics")]
        {
            use tobkiri_core::closed_metrics::Metric;
            let observation = self.session.new_request_observation()?;
            let timer = observation.span(Metric::RequestWall);
            self.observation = Some(observation.clone());
            let result = super::user_invoke::invoke_observed(
                self.session,
                request.shape.clone(),
                request.operation.clone(),
                &request.args,
                &observation,
            )
            .await;
            timer.finish();
            return result;
        }
        #[cfg(not(feature = "closed-request-metrics"))]
        super::user_invoke::invoke(
            self.session,
            request.shape.clone(),
            request.operation.clone(),
            &request.args,
        )
        .await
    }

    async fn close(&mut self) -> Result<(), CoreError> {
        self.session.close().await
    }

    #[cfg(feature = "closed-request-metrics")]
    fn take_observation(&mut self) -> Option<tobkiri_core::closed_metrics::Observation> {
        self.observation.take()
    }
}

pub(super) async fn run(core: &Core, host: &TerminalHost, profile: &str) -> Result<()> {
    let session = core.start(&ProfileId::parse(profile)?).await?;
    let mut source = NativeRequests(host);
    let mut dispatcher = CoreDispatcher {
        session: &session,
        #[cfg(feature = "closed-request-metrics")]
        observation: None,
    };
    let mut output = std::io::stdout();
    run_loop(&mut source, &mut dispatcher, &mut output).await
}

async fn run_loop(
    source: &mut impl RequestInput,
    dispatcher: &mut impl Dispatcher,
    output: &mut impl Write,
) -> Result<()> {
    let outcome = drive(source, dispatcher, output).await;
    // EOF, parser errors, output failures and invocation failures all await the
    // exact Session close. A close failure remains visible even after idle EOF.
    let result = super::complete_with_cleanup(outcome, dispatcher.close().await);
    let marker = writeln!(
        output,
        "{{\"kind\":\"session_finished\",\"ok\":{}}}",
        result.is_ok()
    )
    .and_then(|()| output.flush());
    match (result, marker) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(error), Ok(())) => Err(error),
        (Ok(()), Err(error)) => Err(error.into()),
        (Err(error), Err(marker)) => {
            Err(error.context(format!("session completion output also failed: {marker}")))
        }
    }
}

async fn drive(
    source: &mut impl RequestInput,
    dispatcher: &mut impl Dispatcher,
    output: &mut impl Write,
) -> Result<()> {
    output.write_all(b"{\"kind\":\"session_ready\",\"protocol\":\"tobkiri.user-port-session/1\",\"max_request_bytes\":65536}\n")?;
    output.flush()?;
    let mut last_id = 0;
    loop {
        let Some(bytes) = source.next().await? else {
            return Ok(());
        };
        let request = parse_request(&bytes)?;
        if request.id() <= last_id {
            bail!("session request ids must strictly increase; no replay attempted");
        }
        last_id = request.id();
        let request = match request {
            Request::Invoke(request) => request,
            Request::Close { id } => {
                // This is a native operator command, not an EOF observation or
                // a Core port call. Only the outer awaited close can report
                // session_finished, and no later line may be consumed.
                writeln!(output, "{{\"kind\":\"session_closing\",\"id\":{id}}}")?;
                output.flush()?;
                return Ok(());
            }
        };
        writeln!(
            output,
            "{{\"kind\":\"request_started\",\"id\":{}}}",
            request.id
        )?;
        output.flush()?;
        // The same one-shot dispatcher drains raw typed values/streams and
        // awaits stream close before it returns. No next input is read yet.
        let outcome = dispatcher.invoke(&request).await;
        let marker = writeln!(
            output,
            "{{\"kind\":\"request_finished\",\"id\":{},\"ok\":{}}}",
            request.id,
            outcome.is_ok()
        )
        .and_then(|()| output.flush())
        .and_then(|()| {
            #[cfg(feature = "closed-request-metrics")]
            if let Some(observation) = dispatcher.take_observation() {
                // The bank prepays this fixed scratch and snapshot, retained
                // through the real write. No payload/error is reserialized.
                let snapshot = observation.snapshot();
                let mut bytes = [0u8; tobkiri_core::closed_metrics::ENCODER_BYTES];
                let mut encoded = std::io::Cursor::new(bytes.as_mut_slice());
                snapshot.write_json(request.id, outcome.is_ok(), &mut encoded)?;
                let size = encoded.position() as usize;
                output.write_all(&bytes[..size])?;
                output.flush()?;
            }
            Ok(())
        });
        match (outcome, marker) {
            (Ok(()), Ok(())) => {}
            (Err(error), Ok(())) => return Err(error),
            (Ok(()), Err(error)) => return Err(error.into()),
            (Err(error), Err(marker)) => {
                return Err(
                    error.context(format!("request completion output also failed: {marker}"))
                );
            }
        }
    }
}

#[cfg(test)]
#[path = "profile_session_tests.rs"]
mod tests;

#[cfg(all(test, feature = "closed-request-metrics"))]
#[path = "profile_session_metrics_tests.rs"]
mod metrics_tests;
