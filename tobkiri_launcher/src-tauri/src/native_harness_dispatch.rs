//! Finite current-bound operations. No guest reference or callback is an input.
//!
//! Descriptions are observations, not reconstructed original objects. The
//! verification sequence is not the original Setup control flow or adoption.
use super::*;
use crate::native_worker_failure::{IntegrationFailure, UnboundOperation};
use tobkiri_core::{CoreBuffer, CoreValue};

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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum OriginalOperation {
    /// Execute this no-argument getter, then describe its actual issued value.
    Observe(OriginalGetter),
    /// Internal binding verification only: Profile id, host root, bundled root,
    /// each immediately followed by description in the same original Store.
    #[cfg(test)]
    VerifyBoundGetters,
    /// Reject before owner/Engine construction. This is not a native binding.
    Unbound(UnboundOperation),
}

pub(super) enum AdmittedOperation {
    Observe(OriginalGetter),
    #[cfg(test)]
    VerifyBoundGetters,
}
impl OriginalOperation {
    pub(super) fn admit(self) -> std::result::Result<AdmittedOperation, DetachedFailure> {
        match self {
            Self::Observe(getter) => Ok(AdmittedOperation::Observe(getter)),
            #[cfg(test)]
            Self::VerifyBoundGetters => Ok(AdmittedOperation::VerifyBoundGetters),
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
}

/// Called only from the parent's owning worker. The generic owner is internal;
/// no production caller can supply code receiving Bootstrap or its budget.
pub(super) async fn execute<S: BootstrapInputOwner>(
    runner: &mut NativeBootstrap<S>,
    operation: AdmittedOperation,
) -> std::result::Result<Observations, Failed> {
    let getters = match operation {
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
    Ok(output)
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
