//! The original nonstale recovery body, under its SAME pending journal guard.
//! This fixed Host-only continuation stops at host_profile_catalog's entry.
use super::*;
use crate::app_bootstrap_identity::{BootstrapIdentityInput, validate_bootstrap_identity};
use crate::bootstrap_review::{Review, original_failure};
use crate::original_authority::PublicationPrefix;
use crate::unicode15_1::UnicodeWork as _;
use crate::{BootstrapReviewFailure, BootstrapReviewReachpoint, NativeErrorGraph, NativeErrorKind};
use tobkiri_canon::{AllocationError, AllocationKind, AllocationRequest};

struct Meter<'a>(&'a Arc<dyn AllocationBudget>);
impl crate::unicode15_1::UnicodeWork for Meter<'_> {
    type Error = AllocationError;
    fn charge_work(&mut self, work: u64) -> std::result::Result<(), Self::Error> {
        // Same Store's original 2B native work account, cancellation and
        // absolute deadline (including preparation). No local/reset meter.
        self.0.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work,
        })
    }
}

struct Scratch {
    points: Vec<u32>,
    normalized: Vec<u32>,
    _capacity: NativeSetupReservation,
}
impl Scratch {
    fn new<S: BootstrapInputOwner>(
        pack: &ReviewPackBorrow<'_, S>,
        value: &BootstrapIdentityValue,
    ) -> Result<Self> {
        let len = value
            .string_length()
            .filter(|length| *length <= 128)
            .unwrap_or(0);
        let scratch = crate::unicode15_1::nfc_scratch_len(len)
            .context("bootstrap identity scratch overflow")?;
        let bytes = len
            .checked_add(scratch)
            .and_then(|v| v.checked_mul(std::mem::size_of::<u32>()))
            .and_then(|v| v.checked_add(1024))
            .context("bootstrap identity scratch overflow")?;
        let capacity = pack
            .work
            .get()
            .invocation
            .reserve(CapacityKind::VfsBuffer, u64::try_from(bytes)?)?;
        let mut points = Vec::new();
        points.try_reserve_exact(len)?;
        if points.capacity() != len {
            bail!("bootstrap identity input capacity differs");
        }
        if len != 0 {
            let observed = value
                .codepoints()
                .context("raw identity codepoints missing")?;
            let observed = observed
                .array()
                .context("raw identity codepoints invalid")?;
            if observed.len() != len {
                bail!("raw identity codepoint count differs");
            }
            for index in 0..len {
                let point = observed
                    .get(index)
                    .and_then(|v| v.as_i64())
                    .context("raw identity codepoint missing")?;
                points.push(u32::try_from(point)?);
            }
        }
        let mut normalized = Vec::new();
        normalized.try_reserve_exact(scratch)?;
        if normalized.capacity() != scratch {
            bail!("bootstrap identity scratch capacity differs");
        }
        normalized.resize(scratch, 0);
        Ok(Self {
            points,
            normalized,
            _capacity: capacity,
        })
    }
}

pub(super) async fn run<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> Result<std::result::Result<CoreBuffer, BootstrapReviewFailure>> {
    if review.attempted {
        return Ok(Err(BootstrapReviewFailure::Admission(
            crate::OriginalAuthorityAdmission::AlreadyAttempted,
        )));
    }
    review.attempted = true;
    for discard in [true, false] {
        match review
            .read
            .enter_recovery(&pack.work.get().source, pack.cancellation, discard)
        {
            Err(error) => return Ok(Err(original_failure(error))),
            Ok(
                PublicationPrefix::NoJournal
                | PublicationPrefix::NoIntent
                | PublicationPrefix::RecoveredNone,
            ) => {}
            Ok(PublicationPrefix::BootstrapProfileIdNotBound) => {
                return nonstale(review, pack).await;
            }
        }
    }
    Ok(Err(BootstrapReviewFailure::NotBound(
        BootstrapReviewReachpoint::CatalogNotBound,
    )))
}

async fn nonstale<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> Result<std::result::Result<CoreBuffer, BootstrapReviewFailure>> {
    if let Err(error) = review.read.check_continuation_thread() {
        return Ok(Err(original_failure(error)));
    }
    // Store the actual outputs OUTSIDE this child future immediately, before
    // any fallible validation/processing or another await.
    review.actual.getter = Some(pack.observe_bootstrap_profile_id().await?);
    if let Err(error) = review.read.check_continuation_thread() {
        return Ok(Err(original_failure(error)));
    }
    match review.actual.getter.as_ref().expect("actual raw getter") {
        BootstrapIdentityOutcome::Refused(_) => {
            let Some(BootstrapIdentityOutcome::Refused(error)) = review.actual.getter.take() else {
                unreachable!()
            };
            return Ok(Err(BootstrapReviewFailure::ObservationRefused(error)));
        }
        BootstrapIdentityOutcome::GetterRaised(error) => {
            if error.recovery_catch() == BootstrapRecoveryCatch::None {
                let Some(BootstrapIdentityOutcome::GetterRaised(error)) =
                    review.actual.getter.take()
                else {
                    unreachable!()
                };
                return Ok(Err(BootstrapReviewFailure::GetterRaised(error)));
            }
            // The raw getter precedes the canonical try. Only the original
            // recovery catch wraps its ACTUAL opaque ValueError/OSError here.
            let Some(BootstrapIdentityOutcome::GetterRaised(error)) = review.actual.getter.take()
            else {
                unreachable!()
            };
            return denied(
                review,
                pack,
                "Profile publication recovery failed closed",
                BootstrapDeniedRaise::OpaqueGetter(error),
            )
            .await;
        }
        BootstrapIdentityOutcome::Returned(_) => {}
    }
    let BootstrapIdentityOutcome::Returned(value) =
        review.actual.getter.as_ref().expect("actual raw getter")
    else {
        unreachable!()
    };
    let mut scratch = Scratch::new(pack, value)?;
    let input = if value.builtin_kind() == BootstrapBuiltinKind::Str {
        BootstrapIdentityInput::String {
            length: value.string_length().context("raw string length missing")?,
            codepoints: &scratch.points,
        }
    } else {
        BootstrapIdentityInput::NonString
    };
    let mut meter = Meter(pack.input_budget);
    let validated = validate_bootstrap_identity(
        input,
        &mut scratch.normalized,
        pack.input_budget,
        &mut meter,
    )
    .map_err(|error| anyhow::anyhow!("bootstrap identity processing refused: {error:?}"))?;
    let identity = match validated {
        Err(error) => {
            let cause = protocol_graph(&error.message, pack.input_budget)?;
            return denied(
                review,
                pack,
                "application bootstrap Profile identity is not canonical",
                BootstrapDeniedRaise::ExplicitFrom(cause),
            )
            .await;
        }
        Ok(identity) => identity,
    };
    meter
        .charge_work(u64::try_from(identity.len())?)
        .map_err(anyhow::Error::from)?;
    match review.read.candidate_matches_identity(identity) {
        Err(error) => return Ok(Err(original_failure(error))),
        Ok(false) => {
            return denied(
                review,
                pack,
                "Profile publication identity changed",
                BootstrapDeniedRaise::Plain,
            )
            .await;
        }
        Ok(true) => {}
    }
    // App policy has already proved this very sequence is canonical ASCII.
    // This is a bounded, funded pure Path spelling, never a normalization IO.
    let mut ascii = [0_u8; 128];
    for (out, point) in ascii.iter_mut().zip(identity) {
        *out = u8::try_from(*point)?;
    }
    let profile_id = std::str::from_utf8(&ascii[..identity.len()])?;
    if let Err(error) =
        review
            .read
            .construct_workspace(&pack.work.get().source, pack.cancellation, profile_id)
    {
        return Ok(Err(original_failure(error)));
    }
    pack.require_same_runtime()?;
    // The original next call is host_profile_catalog(base_dir). Its body,
    // Authority constructor and workspace.exists are not executed by this unit.
    Ok(Err(BootstrapReviewFailure::NotBound(
        BootstrapReviewReachpoint::HostProfileCatalogNotBound,
    )))
}

async fn denied<S: BootstrapInputOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    message: &str,
    cause: BootstrapDeniedRaise,
) -> Result<std::result::Result<CoreBuffer, BootstrapReviewFailure>> {
    match pack.denied(message, cause).await? {
        Ok(value) => review.actual.denied = Some(value),
        Err(error) => review.actual.factory_failure = Some(error),
    }
    if review.read.check_continuation_thread().is_err() {
        bail!("publication factory continuation changed logical worker thread");
    }
    // No fallible operation/await after transferring these real carriers to
    // the returned typed error. They remain caller-held through actual finish.
    Ok(Err(if let Some(denied) = review.actual.denied.take() {
        BootstrapReviewFailure::ActualDenied {
            denied,
            getter: review.actual.getter.take(),
        }
    } else {
        BootstrapReviewFailure::ActualFactoryFailure {
            failure: review
                .actual
                .factory_failure
                .take()
                .expect("actual factory failure"),
            getter: review.actual.getter.take(),
        }
    }))
}

fn protocol_graph(
    message: &CoreBuffer,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<NativeErrorGraph> {
    let code = CoreBuffer::copy_from(NativeErrorKind::ProtocolError.tag().as_bytes(), budget)?;
    let mut out = CoreBuffer::builder(budget, MAX_BYTES);
    out.append(br#"{"format":"tobkiri.native-exception/1","graph_id":1,"root":0,"nodes":[{"kind":"protocol-error","args":["#)?;
    bootstrap_metadata::string_parts(&mut out, [message.as_str()?])?;
    out.append(br#"],"fields":{},"cause":null,"context":null,"suppress_context":false}]}"#)?;
    NativeErrorGraph::decode(code, message.clone(), out.finish()?, budget).map_err(Into::into)
}

impl<S: BootstrapInputOwner> ReviewPackBorrow<'_, S> {
    fn require_same_runtime(&self) -> Result<()> {
        if self.work.get().phase.load(Ordering::Acquire) != REVIEW {
            bail!("original review runtime is no longer live");
        }
        self.work.get().source.check_live()?;
        // Native composition replaces the immutable Host port registry with
        // this one initialized program/binding table. Frames cannot register,
        // replace or select another runtime. Retrieve that same actual owner;
        // no getter value, constant, metadata hash or path confers this state.
        self.program
            .as_ref()
            .context("original review runtime is unavailable")?
            .fuel_remaining()?;
        Meter(self.input_budget).charge_work(0)?;
        Ok(())
    }
}
