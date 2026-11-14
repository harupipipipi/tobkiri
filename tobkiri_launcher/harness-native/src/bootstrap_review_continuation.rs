//! Original recovery and fresh Setup candidate callers on the same Pack Store.
//! Recovery and confirmed capture retain their respective publication guards.
use super::*;
use crate::app_bootstrap_identity::{BootstrapIdentityInput, validate_bootstrap_identity};
use crate::bootstrap_review::{Review, original_failure};
use crate::original_authority::{CandidateSelection, PublicationPrefix};
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

pub(super) struct Scratch {
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

    pub(super) fn ascii_identity<'a>(&self, ascii: &'a mut [u8; 128]) -> Result<&'a str> {
        for (out, point) in ascii.iter_mut().zip(&self.points) {
            *out = u8::try_from(*point)?;
        }
        Ok(std::str::from_utf8(&ascii[..self.points.len()])?)
    }
}

#[derive(Clone, Copy)]
pub(super) enum ReviewCaller {
    Recovery,
    RecoveryTemplate,
    Candidate,
    Resolver,
}

impl ReviewCaller {
    pub(super) fn check<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
        self,
        review: &Review,
        pack: &ReviewPackBorrow<'_, S>,
    ) -> std::result::Result<(), crate::OriginalAuthorityError> {
        match self {
            Self::Recovery | Self::RecoveryTemplate => review.read.check_continuation_thread(),
            Self::Candidate => review
                .read
                .check_candidate_root(&pack.work.get().source, pack.cancellation),
            Self::Resolver => review
                .read
                .check_resolver_root(&pack.work.get().source, pack.cancellation),
        }
    }

    pub(super) fn getter(self, review: &mut Review) -> &mut Option<BootstrapIdentityOutcome> {
        match self {
            Self::Recovery | Self::Candidate => &mut review.actual.getter,
            Self::RecoveryTemplate => &mut review.actual.recovery_template_getter,
            Self::Resolver => &mut review.actual.resolver_getter,
        }
    }

    pub(super) fn is_recovery(self) -> bool {
        matches!(self, Self::Recovery | Self::RecoveryTemplate)
    }
}

pub(super) async fn run<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    confirmed: bool,
) -> super::listing::Outcome {
    if review.attempted {
        return Ok(Err(BootstrapReviewFailure::Admission(
            crate::OriginalAuthorityAdmission::AlreadyAttempted,
        )));
    }
    review.attempted = true;
    if confirmed {
        // The original source-update precheck must run before lock acquisition.
        // Its active candidate path is a separate continuation.
        if review.include_source_additions {
            return Ok(Err(BootstrapReviewFailure::NotBound(
                BootstrapReviewReachpoint::ActiveCandidateAuthorityNotBound,
            )));
        }
        if let Err(error) = review.read.begin_confirmed(
            &pack.work.get().source,
            pack.cancellation,
            pack.input_budget,
        ) {
            return Ok(Err(original_failure(error)));
        }
        return fresh_candidate(review, pack).await;
    }
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
                if let Err(error) = nonstale(review, pack).await? {
                    return Ok(Err(error));
                }
                #[cfg(test)]
                if review.recovery_only {
                    return Ok(Err(BootstrapReviewFailure::NotBound(
                        BootstrapReviewReachpoint::RecoveryCompleted,
                    )));
                }
            }
        }
    }
    fresh_candidate(review, pack).await
}

async fn nonstale<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> super::catalog::RecoveryOutcome {
    let caller = ReviewCaller::Recovery;
    let identity = match validated_identity(review, pack, caller).await? {
        Ok(identity) => identity,
        Err(error) => return Ok(Err(error)),
    };
    match review.read.candidate_matches_identity(&identity.points) {
        Err(error) => return Ok(Err(original_failure(error))),
        Ok(false) => {
            return Ok(Err(denied(
                review,
                pack,
                caller,
                "Profile publication identity changed",
                BootstrapDeniedRaise::Plain,
            )
            .await?));
        }
        Ok(true) => {}
    }
    // App policy has already proved this very sequence is canonical ASCII.
    let mut ascii = [0_u8; 128];
    let profile_id = identity.ascii_identity(&mut ascii)?;
    if let Err(error) =
        review
            .read
            .construct_workspace(&pack.work.get().source, pack.cancellation, profile_id)
    {
        return Ok(Err(original_failure(error)));
    }
    pack.require_same_runtime()?;
    if review.identity_prefix {
        return Ok(Err(BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::HostProfileCatalogNotBound,
        )));
    }
    super::catalog::run_recovery(review, pack).await
}

async fn fresh_candidate<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> super::listing::Outcome {
    // The original candidate enters the exact Host user-data scope before
    // evaluating the root, the actual App loader, then the bootstrap getter.
    if let Err(error) = review
        .read
        .acquire_candidate_root(&pack.work.get().source, pack.cancellation)
    {
        return Ok(Err(original_failure(error)));
    }
    let caller = ReviewCaller::Candidate;
    if let Err(error) = super::catalog::load(review, pack, caller).await? {
        return Ok(Err(error));
    }
    let identity = match validated_identity(review, pack, caller).await? {
        Ok(identity) => identity,
        Err(error) => return Ok(Err(error)),
    };
    let mut ascii = [0_u8; 128];
    let selection = review.read.candidate_selection(
        &pack.work.get().source,
        pack.cancellation,
        identity.ascii_identity(&mut ascii)?,
        review.include_source_additions,
        pack.input_budget,
    );
    drop(identity);
    Ok(Err(match selection {
        Ok(CandidateSelection::BundledCatalog) => return resolve_bundled(review, pack).await,
        Ok(CandidateSelection::ActiveAuthority) => BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::ActiveCandidateAuthorityNotBound,
        ),
        Ok(CandidateSelection::Denied(message)) => {
            denied(review, pack, caller, message, BootstrapDeniedRaise::Plain).await?
        }
        Err(error) => original_failure(error),
    }))
}

async fn resolve_bundled<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> super::listing::Outcome {
    let source = &pack.work.get().source;
    if let Err(error) = review.read.acquire_resolver_root(source, pack.cancellation) {
        return Ok(Err(original_failure(error)));
    }
    let bundle_lock = match super::catalog::resolver_lock_digest(review, pack).await? {
        Ok(digest) => digest,
        Err(error) => return Ok(Err(error)),
    };
    let caller = ReviewCaller::Resolver;
    let snapshot = review.read.resolver_authority_snapshot(
        &pack.work.get().source,
        pack.cancellation,
        bundle_lock,
    );
    match snapshot {
        Ok(Some(snapshot)) => review.actual.resolver_authority = Some(snapshot),
        Ok(None) => {
            return Ok(Err(denied(
                review,
                pack,
                caller,
                crate::original_authority::AUTHORITY_PATH_DENIAL,
                BootstrapDeniedRaise::Plain,
            )
            .await?));
        }
        Err(error) => return Ok(Err(original_failure(error))),
    }
    match validated_identity(review, pack, caller).await? {
        // The original validator returns the same exact string. Keep that
        // getter ref, and let the original catalog do its live mapping lookup.
        Ok(identity) => {
            let mut ascii = [0_u8; 128];
            let profile_id = identity.ascii_identity(&mut ascii)?;
            drop(identity);
            match super::catalog::resolver_source_profile(review, pack).await? {
                Ok(()) => super::resolver_edges::bind_bundled_edges(review, pack, profile_id).await,
                Err(error) => Ok(Err(error)),
            }
        }
        Err(error) => Ok(Err(error)),
    }
}

pub(super) async fn validated_identity<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
) -> Result<std::result::Result<Scratch, BootstrapReviewFailure>> {
    if let Err(error) = caller.check(review, pack) {
        return Ok(Err(original_failure(error)));
    }
    // Store the actual outputs OUTSIDE this child future immediately, before
    // any fallible validation/processing or another await.
    *caller.getter(review) = Some(pack.observe_bootstrap_profile_id().await?);
    if let Err(error) = caller.check(review, pack) {
        return Ok(Err(original_failure(error)));
    }
    let getter = caller.getter(review);
    match getter.as_ref().expect("actual raw getter") {
        BootstrapIdentityOutcome::Refused(_) => {
            let Some(BootstrapIdentityOutcome::Refused(error)) = getter.take() else {
                unreachable!()
            };
            return Ok(Err(BootstrapReviewFailure::ObservationRefused(error)));
        }
        BootstrapIdentityOutcome::GetterRaised(_) => {
            let outcome = getter.take().expect("actual getter failure");
            return Ok(Err(super::catalog::handle_call_failure(
                review, pack, caller, outcome, false,
            )
            .await?));
        }
        BootstrapIdentityOutcome::Returned(_) => {}
    }
    let BootstrapIdentityOutcome::Returned(value) = getter.as_ref().expect("actual raw getter")
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
            return Ok(Err(denied(
                review,
                pack,
                caller,
                "application bootstrap Profile identity is not canonical",
                BootstrapDeniedRaise::ExplicitFrom(cause),
            )
            .await?));
        }
        Ok(identity) => identity,
    };
    meter
        .charge_work(u64::try_from(identity.len())?)
        .map_err(anyhow::Error::from)?;
    Ok(Ok(scratch))
}

pub(super) async fn denied<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    message: &str,
    cause: BootstrapDeniedRaise,
) -> Result<BootstrapReviewFailure> {
    match pack.denied(message, cause).await? {
        Ok(value) => review.actual.denied = Some(value),
        Err(error) => review.actual.factory_failure = Some(error),
    }
    caller.check(review, pack)?;
    // No fallible operation/await after transferring these real carriers to
    // the returned typed error. They remain caller-held through actual finish.
    Ok(if let Some(denied) = review.actual.denied.take() {
        BootstrapReviewFailure::ActualDenied {
            denied,
            getter: caller.getter(review).take(),
        }
    } else {
        BootstrapReviewFailure::ActualFactoryFailure {
            failure: review
                .actual
                .factory_failure
                .take()
                .expect("actual factory failure"),
            getter: caller.getter(review).take(),
        }
    })
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
    pub(super) fn require_same_runtime(&self) -> Result<()> {
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
