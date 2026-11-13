//! Finite current-bound operations. No guest reference or callback is an input.
//!
//! Descriptions are observations, not reconstructed original objects. The
//! verification sequence is not the original Setup control flow or adoption.
use super::*;
use crate::native_worker_failure::{
    IntegrationFailure, ReviewSourceRecord, SourceRecord, UnboundOperation,
};
use tobkiri_core::{CoreBuffer, CoreValue};
use tobkiri_harness_native::{BootstrapReviewFailure, BootstrapReviewOptions};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum OriginalGetter {
    BootstrapProfileId,
    HostResourceRoot,
    BundledProfileRoot,
}
impl OriginalGetter {
    fn name(self) -> &'static str {
        match self {
            Self::BootstrapProfileId => "bootstrap_profile_id",
            Self::HostResourceRoot => "host_resource_root",
            Self::BundledProfileRoot => "bundled_profile_root",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum OriginalOperation {
    /// Execute this no-argument getter, then describe its actual issued value.
    Observe(OriginalGetter),
    ReviewListing(BootstrapReviewOptions),
    /// Internal binding verification only: Profile id, host root, bundled root,
    /// each immediately followed by description in the same original Store.
    #[cfg(test)]
    VerifyBoundGetters,
    #[cfg(test)]
    VerifyDenied(bool),
    #[cfg(test)]
    VerifyDeniedCodec,
    #[cfg(test)]
    VerifyIdentity(IdentityProbe),
    #[cfg(test)]
    VerifyReview(ReviewProbe),
    /// Reject before owner/Engine construction. This is not a native binding.
    Unbound(UnboundOperation),
}

pub(super) enum AdmittedOperation {
    Observe(OriginalGetter),
    ReviewListing(BootstrapReviewOptions),
    #[cfg(test)]
    VerifyBoundGetters,
    #[cfg(test)]
    VerifyDenied(bool),
    #[cfg(test)]
    VerifyDeniedCodec,
    #[cfg(test)]
    VerifyIdentity(IdentityProbe),
    #[cfg(test)]
    VerifyReview(ReviewProbe),
}
impl OriginalOperation {
    pub(super) fn admit(self) -> std::result::Result<AdmittedOperation, DetachedFailure> {
        match self {
            Self::Observe(getter) => Ok(AdmittedOperation::Observe(getter)),
            Self::ReviewListing(options) => {
                if options
                    .denial_diagnostic
                    .as_ref()
                    .is_some_and(|text| text.len() > tobkiri_core::MAX_BYTES)
                {
                    return Err(DetachedFailure::integration(IntegrationFailure::Codec(
                        tobkiri_core::AllocationError::Limit,
                    )));
                }
                Ok(AdmittedOperation::ReviewListing(options))
            }
            #[cfg(test)]
            Self::VerifyBoundGetters => Ok(AdmittedOperation::VerifyBoundGetters),
            #[cfg(test)]
            Self::VerifyDenied(from_host) => Ok(AdmittedOperation::VerifyDenied(from_host)),
            #[cfg(test)]
            Self::VerifyDeniedCodec => Ok(AdmittedOperation::VerifyDeniedCodec),
            #[cfg(test)]
            Self::VerifyIdentity(probe) => Ok(AdmittedOperation::VerifyIdentity(probe)),
            #[cfg(test)]
            Self::VerifyReview(probe) => Ok(AdmittedOperation::VerifyReview(probe)),
            Self::Unbound(operation) => Err(DetachedFailure::integration(
                IntegrationFailure::OperationNotBound(operation),
            )),
        }
    }
}

enum Failure {
    Domain(tobkiri_wasm::NativeDataError),
    Native(anyhow::Error),
    Integration(IntegrationFailure),
    Panic,
    Review(BootstrapReviewFailure),
    #[cfg(test)]
    DeniedFunding(DeniedFunding),
}
impl Failure {
    fn detach(&self, detachment: &mut Detachment) -> DetachedFailure {
        match self {
            Self::Domain(error) => {
                // Only a borrowed projection is needed; retain the actual
                // error buffers until finish without cloning their owner.
                detachment.domain_data(error)
            }
            Self::Native(error) => detachment.native(error, Stage::Operation),
            Self::Integration(error) => DetachedFailure::integration(*error),
            Self::Panic => DetachedFailure::condition(Stage::Operation, Condition::NativePanic),
            #[cfg(test)]
            Self::DeniedFunding(value) => {
                match value {
                    DeniedFunding::Returned(value) => {
                        let _ = value.result();
                    }
                    DeniedFunding::Raised(value) => {
                        let _ = value.error();
                    }
                    DeniedFunding::CodecRejected(error) => {
                        let _ = error;
                    }
                    DeniedFunding::Identity(value) => {
                        let _ = value;
                    }
                }
                DetachedFailure::Execution {
                    stage: Stage::Operation,
                    source: SourceRecord::Unsupported(
                        crate::native_worker_failure::Unsupported::AnyhowBoundary,
                    ),
                }
            }
            Self::Review(error) => {
                let source = match error {
                    BootstrapReviewFailure::NotBound(point) => {
                        SourceRecord::Integration(IntegrationFailure::ReviewNotBound(*point))
                    }
                    BootstrapReviewFailure::Admission(error) => {
                        SourceRecord::Integration(IntegrationFailure::ReviewAdmission(*error))
                    }
                    BootstrapReviewFailure::Original(error) => {
                        match super::authority::detach_error(detachment, error, 0) {
                            Ok(error) => SourceRecord::Review(ReviewSourceRecord::Original(error)),
                            Err(error) => return error,
                        }
                    }
                    BootstrapReviewFailure::ResolutionDenied { message, cause } => {
                        let message = match detachment.authority_text(message) {
                            Ok(message) => message,
                            Err(error) => return error,
                        };
                        match super::authority::detach_cause(detachment, cause, 0) {
                            Ok(cause) => {
                                SourceRecord::Review(ReviewSourceRecord::ResolutionDenied {
                                    message,
                                    cause,
                                })
                            }
                            Err(error) => return error,
                        }
                    }
                    actual @ (BootstrapReviewFailure::ActualDenied { .. }
                    | BootstrapReviewFailure::ActualFactoryFailure { .. }
                    | BootstrapReviewFailure::GetterRaised(_)
                    | BootstrapReviewFailure::ObservationRefused(_)) => {
                        match detachment.review_actual(actual) {
                            Ok(value) => SourceRecord::Review(value),
                            Err(error) => return error,
                        }
                    }
                };
                DetachedFailure::Execution {
                    stage: Stage::Review,
                    source,
                }
            }
        }
    }
}
impl From<tobkiri_core::AllocationError> for Failure {
    fn from(error: tobkiri_core::AllocationError) -> Self {
        Self::Integration(IntegrationFailure::Codec(error))
    }
}

struct Observation {
    getter: OriginalGetter,
    // Actual funded buffers stay inside this worker through physical finish.
    _result: CoreBuffer,
    description: CoreBuffer,
}
pub(super) struct Observations([Option<Observation>; 3]);
pub(super) enum DispatchOutput {
    Observations(Observations),
    ReviewListing(CoreBuffer),
}

impl DispatchOutput {
    pub(super) fn detach(&self, detachment: &mut Detachment) -> Outcome {
        match self {
            Self::Observations(observations) => observations.detach(detachment),
            Self::ReviewListing(listing) => {
                let listing = detachment.description(listing)?;
                // Keep the existing bounded DATA detachment. The consumer
                // can parse the listing after worker cleanup and publication.
                let mut output = serde_json::Map::new();
                output.insert("listing".to_owned(), serde_json::Value::String(listing));
                Ok(serde_json::Value::Object(output))
            }
        }
    }
}

impl Observations {
    pub(super) fn detach(&self, detachment: &mut Detachment) -> Outcome {
        let mut values = Vec::with_capacity(3);
        for observation in self.0.iter().flatten() {
            let description = detachment.description(&observation.description)?;
            let mut value = serde_json::Map::new();
            value.insert(
                "getter".to_owned(),
                serde_json::Value::String(observation.getter.name().to_owned()),
            );
            // Move the one prepaid copy. A serialization macro could make
            // additional unreserved copies of this arbitrarily sized string.
            value.insert(
                "description".to_owned(),
                serde_json::Value::String(description),
            );
            values.push(serde_json::Value::Object(value));
        }
        let mut output = serde_json::Map::new();
        output.insert("observations".to_owned(), serde_json::Value::Array(values));
        Ok(serde_json::Value::Object(output))
    }
}

pub(super) struct Failed {
    failure: Failure,
    _completed: Observations,
}
impl Failed {
    pub(super) fn panic() -> Self {
        Self {
            failure: Failure::Panic,
            _completed: Observations([None, None, None]),
        }
    }
    pub(super) fn detach(&self, detachment: &mut Detachment) -> DetachedFailure {
        self.failure.detach(detachment)
    }
    /// These owned strings/causes borrow Review's prepaid diagnostic allowance.
    /// They own no IO, source or CoreBuffer; that custody stays in the runner.
    pub(super) fn owns_review_diagnostics(&self) -> bool {
        matches!(&self.failure, Failure::Review(value) if value.owns_original_diagnostics())
            && self._completed.0.iter().all(Option::is_none)
    }
}

/// Called only from the parent's owning worker. The generic owner is internal;
/// no production caller can supply code receiving Bootstrap or its budget.
pub(super) async fn execute<
    S: BootstrapInputOwner + tobkiri_harness_native::BootstrapReviewOwner,
>(
    runner: &mut NativeBootstrap<S>,
    operation: AdmittedOperation,
) -> std::result::Result<DispatchOutput, Failed> {
    let getters = match operation {
        #[cfg(test)]
        AdmittedOperation::VerifyReview(probe) => {
            let options = BootstrapReviewOptions {
                include_source_additions: probe == ReviewProbe::Boundary,
                ..Default::default()
            };
            let result = runner.setup_review_listing(&options).await;
            return Err(Failed {
                failure: match result {
                    Ok(Err(error)) => Failure::Review(error),
                    Err(error) => Failure::Native(error),
                    Ok(Ok(_)) => panic!("review boundary became listing success"),
                },
                _completed: Observations([None, None, None]),
            });
        }
        #[cfg(test)]
        AdmittedOperation::VerifyIdentity(probe) => {
            return Err(Failed {
                failure: Failure::DeniedFunding(
                    identity_funding(runner, probe)
                        .await
                        .expect("small component raw getter observation contract"),
                ),
                _completed: Observations([None, None, None]),
            });
        }
        #[cfg(test)]
        AdmittedOperation::VerifyDeniedCodec => {
            let error = match runner
                .denied(
                    WORKER_DENIED_MESSAGE,
                    tobkiri_harness_native::BootstrapDeniedRaise::Plain,
                )
                .await
            {
                Err(error) => error,
                Ok(_) => panic!("malformed factory view was not a native contract rejection"),
            };
            assert_eq!(
                error.to_string(),
                "denied factory actual exception fields differ"
            );
            assert!(
                runner.fuel_remaining().is_err(),
                "codec rejection left the same Store live"
            );
            return Err(Failed {
                failure: Failure::DeniedFunding(DeniedFunding::CodecRejected(error)),
                _completed: Observations([None, None, None]),
            });
        }
        #[cfg(test)]
        AdmittedOperation::VerifyDenied(from_host) => {
            return Err(Failed {
                // A trap/codec failure must fail this verification, rather
                // than accidentally match its deliberate Unsupported result.
                failure: Failure::DeniedFunding(
                    denied_funding(runner, from_host)
                        .await
                        .expect("small component denied binding contract"),
                ),
                _completed: Observations([None, None, None]),
            });
        }
        AdmittedOperation::ReviewListing(options) => {
            let failure = match runner.setup_review_listing(&options).await {
                Ok(Ok(listing)) => return Ok(DispatchOutput::ReviewListing(listing)),
                Ok(Err(error)) => Failure::Review(error),
                Err(error) => Failure::Native(error),
            };
            return Err(Failed {
                failure,
                _completed: Observations([None, None, None]),
            });
        }
        AdmittedOperation::Observe(getter) => [Some(getter), None, None],
        #[cfg(test)]
        AdmittedOperation::VerifyBoundGetters => [
            Some(OriginalGetter::BootstrapProfileId),
            Some(OriginalGetter::HostResourceRoot),
            Some(OriginalGetter::BundledProfileRoot),
        ],
    };
    let mut output = Observations([None, None, None]);
    for (index, getter) in getters.into_iter().enumerate() {
        let Some(getter) = getter else { break };
        match observe(runner, getter).await {
            Ok(value) => output.0[index] = Some(value),
            Err(failure) => {
                return Err(Failed {
                    failure,
                    _completed: output,
                });
            }
        }
    }
    Ok(DispatchOutput::Observations(output))
}

#[cfg(test)]
const WORKER_DENIED_MESSAGE: &str = "native worker factory value";

// This deliberately unsupported test-only variant keeps the real funded carrier
// through ordinary detachment/finish/drop/Join. It opens no production route.
#[cfg(test)]
#[derive(Debug)]
enum DeniedFunding {
    Returned(tobkiri_harness_native::BootstrapDenied),
    Raised(tobkiri_harness_native::BootstrapDeniedFailure),
    CodecRejected(anyhow::Error),
    Identity(tobkiri_harness_native::BootstrapIdentityOutcome),
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum IdentityProbe {
    String,
    NonString,
    ValueError,
    OsError,
    FactoryRaised,
    CodecRejected,
    Refused,
}
#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ReviewProbe {
    Boundary,
    Canonical,
    RawValue,
    RawType,
    Mismatch,
    FactoryFailure,
}

#[cfg(test)]
async fn identity_funding<S: BootstrapInputOwner>(
    runner: &mut NativeBootstrap<S>,
    probe: IdentityProbe,
) -> anyhow::Result<DeniedFunding> {
    use tobkiri_harness_native::{
        BootstrapBuiltinKind, BootstrapDeniedRaise, BootstrapIdentityOutcome,
        BootstrapIdentityStage, BootstrapRecoveryCatch,
    };
    let outcome = runner.observe_bootstrap_profile_id().await;
    if probe == IdentityProbe::CodecRejected {
        let error = match outcome {
            Err(error) => error,
            Ok(_) => panic!("malformed getter view was not a contract rejection"),
        };
        assert_eq!(error.to_string(), "raw getter codepoint count differs");
        assert!(runner.fuel_remaining().is_err());
        return Ok(DeniedFunding::CodecRejected(error));
    }
    let outcome = outcome?;
    match (&outcome, probe) {
        (BootstrapIdentityOutcome::Returned(value), IdentityProbe::String) => {
            assert_eq!(value.builtin_kind(), BootstrapBuiltinKind::Str);
            assert_eq!(value.string_length(), Some(2));
            let points = value.codepoints().unwrap();
            let points = points.array().unwrap();
            assert_eq!(points.get(0).unwrap().as_i64(), Some(0xd800));
            assert_eq!(points.get(1).unwrap().as_i64(), Some(0x10400));
            return Ok(DeniedFunding::Identity(outcome));
        }
        (BootstrapIdentityOutcome::Returned(value), IdentityProbe::NonString) => {
            assert_eq!(value.builtin_kind(), BootstrapBuiltinKind::Dict);
            assert_eq!(value.string_length(), None);
            assert!(value.codepoints().is_none());
            return Ok(DeniedFunding::Identity(outcome));
        }
        (BootstrapIdentityOutcome::Refused(value), IdentityProbe::Refused) => {
            assert_eq!(value.stage(), BootstrapIdentityStage::Describe);
            assert_eq!(value.error().code().as_str()?, "unsupported-value");
            assert_eq!(
                value.getter_error().unwrap().code().as_str()?,
                "method-raised"
            );
            assert!(
                runner.fuel_remaining().is_ok(),
                "ABI refusal retired a healthy Store"
            );
            return Ok(DeniedFunding::Identity(outcome));
        }
        (
            BootstrapIdentityOutcome::GetterRaised(value),
            IdentityProbe::ValueError | IdentityProbe::OsError | IdentityProbe::FactoryRaised,
        ) => {
            assert_eq!(
                value.recovery_catch(),
                if probe == IdentityProbe::OsError {
                    BootstrapRecoveryCatch::OsError
                } else {
                    BootstrapRecoveryCatch::ValueError
                }
            );
        }
        _ => panic!("unexpected raw getter outcome for controlled fixture"),
    }
    let BootstrapIdentityOutcome::GetterRaised(error) = outcome else {
        unreachable!()
    };
    let actual_view = error.view().as_bytes().as_ptr() as usize;
    let actual_ref = error.error().details().unwrap().as_bytes().as_ptr() as usize;
    // This fixed test raise site proves Host carrier identity only. It does
    // not put raw getter errors through canonical validation/recovery yet.
    match runner
        .denied(
            WORKER_DENIED_MESSAGE,
            BootstrapDeniedRaise::OpaqueGetter(error),
        )
        .await?
    {
        Ok(value) => {
            assert_ne!(probe, IdentityProbe::FactoryRaised);
            let cause = value.opaque_cause().unwrap();
            assert!(std::ptr::eq(cause, value.opaque_context().unwrap()));
            assert_eq!(cause.view().as_bytes().as_ptr() as usize, actual_view);
            assert_eq!(
                cause.error().details().unwrap().as_bytes().as_ptr() as usize,
                actual_ref
            );
            assert!(value.cause().is_none() && value.context().is_none());
            assert!(value.suppress_context());
            Ok(DeniedFunding::Returned(value))
        }
        Err(value) => {
            assert_eq!(probe, IdentityProbe::FactoryRaised);
            assert!(value.factory_raised());
            assert!(value.host_except_context().is_none());
            assert!(value
                .error()
                .details()
                .unwrap()
                .as_str()?
                .contains("\"object\":\"ee5\""));
            let context = value.opaque_except_context().unwrap();
            assert_eq!(context.view().as_bytes().as_ptr() as usize, actual_view);
            assert_eq!(
                context.error().details().unwrap().as_bytes().as_ptr() as usize,
                actual_ref
            );
            Ok(DeniedFunding::Raised(value))
        }
    }
}
#[cfg(test)]
async fn denied_funding<S: BootstrapInputOwner>(
    runner: &mut NativeBootstrap<S>,
    from_host: bool,
) -> anyhow::Result<DeniedFunding> {
    use tobkiri_harness_native::{BootstrapDeniedRaise, NativeErrorGraph};
    let host_raise = if from_host {
        let budget = runner.data_budget();
        let code = CoreBuffer::copy_from(b"protocol-error", budget)?;
        let message = CoreBuffer::copy_from(b"original Host cause", budget)?;
        let details = CoreBuffer::copy_from(br#"{"format":"tobkiri.native-exception/1","graph_id":1,"root":0,"nodes":[{"kind":"protocol-error","args":["original Host cause"],"fields":{},"cause":null,"context":null,"suppress_context":false}]}"#, budget)?;
        BootstrapDeniedRaise::ExplicitFrom(NativeErrorGraph::decode(
            code, message, details, budget,
        )?)
    } else {
        BootstrapDeniedRaise::Plain
    };
    match runner.denied(WORKER_DENIED_MESSAGE, host_raise).await? {
        Ok(value) => {
            assert_eq!(value.message().as_str()?, WORKER_DENIED_MESSAGE);
            assert_eq!(value.suppress_context(), from_host);
            if from_host {
                assert!(std::ptr::eq(
                    value.cause().unwrap(),
                    value.context().unwrap()
                ));
                assert_eq!(
                    value.cause().unwrap().root().kind(),
                    tobkiri_harness_native::NativeErrorKind::ProtocolError
                );
            } else {
                assert!(value.cause().is_none() && value.context().is_none());
            }
            Ok(DeniedFunding::Returned(value))
        }
        Err(value) => {
            assert_eq!(
                value.stage(),
                tobkiri_harness_native::BootstrapDeniedStage::Factory
            );
            assert_eq!(value.error().code().as_str()?, "method-raised");
            assert_eq!(value.host_except_context().is_some(), from_host);
            // No explicit-from/suppression is applied to this thrown factory;
            // the original opaque method error and ambient context are kept.
            Ok(DeniedFunding::Raised(value))
        }
    }
}

async fn observe<S: BootstrapInputOwner>(
    runner: &mut NativeBootstrap<S>,
    getter: OriginalGetter,
) -> std::result::Result<Observation, Failure> {
    // These methods create the original args=[]/kwargs=[] call frame and
    // retain their own phase/source checks. No hand-built call bypasses it.
    let result = match getter {
        OriginalGetter::BootstrapProfileId => runner.bootstrap_profile_id().await,
        OriginalGetter::HostResourceRoot => runner.host_resource_root().await,
        OriginalGetter::BundledProfileRoot => runner.bundled_profile_root().await,
    }
    .map_err(Failure::Native)?
    .map_err(Failure::Domain)?;
    let request = description_request(runner, &result)?;
    let description = runner
        .describe_value(&request)
        .await
        .map_err(Failure::Native)?
        .map_err(Failure::Domain)?;
    Ok(Observation {
        getter,
        _result: result,
        description,
    })
}

fn description_request<S: BootstrapInputOwner>(
    runner: &NativeBootstrap<S>,
    result: &CoreBuffer,
) -> std::result::Result<CoreBuffer, Failure> {
    let budget = runner.data_budget();
    let result = CoreValue::parse_guest(result.as_bytes(), budget)?;
    if result.object().is_none_or(|object| object.len() != 4)
        || !result
            .get_key("format")
            .is_some_and(|value| value.as_str() == Some("io.tobkiri.native-value-result.v1"))
        || result.get_key("owned").is_none()
    {
        return Err(Failure::Integration(IntegrationFailure::InvalidValueResult));
    }
    let scope = result
        .get_key("scope")
        .ok_or(Failure::Integration(IntegrationFailure::InvalidValueResult))?;
    let reference = result
        .get_key("root")
        .ok_or(Failure::Integration(IntegrationFailure::InvalidValueResult))?;
    // Reuse the actual result packet as DATA. The original Registry checks its
    // actual scope/reference; this codec does not issue or manufacture a hold.
    Ok(CoreValue::object_from(
        [
            Ok((
                "format",
                CoreValue::string("io.tobkiri.native-value-describe.v1", budget)?,
            )),
            Ok(("scope", scope)),
            Ok(("root", reference)),
        ]
        .into_iter(),
        budget,
    )?
    .encode_guest(budget)?)
}

#[cfg(all(test, target_os = "macos"))]
#[path = "native_harness_dispatch_tests.rs"]
mod tests;
