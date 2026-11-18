//! Bounded, read-only inspection at the later resolved-application handoff.
//!
//! Only the actual signed resolver selection implements the Core source
//! contract here. Core binds its selected root and declared member at the
//! reached open, verifies identity and digest, and retains source custody until
//! physical cleanup finishes. The Launcher still parses JSON with its original
//! native allocations; this is not the complete nativeKit transform boundary.
//! The original Launcher can use its own registered native invocation at Launch.

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use serde_json::Value;
use tobkiri_core::{
    Core, NativeArtifactDeclaration, NativeArtifactError, NativeArtifactInspection,
    NativeDeclaredArtifactSource,
};

use crate::defaultspack_authority::{
    NativeApplicationSources, NativeSourceRoot, VerifiedPackArtifact,
};
use crate::host_contract_contributions::{
    declared_artifact_for_role, parse_json_artifact, prepare_contribution,
    HostContractContributionValues, CONTRIBUTION_ARTIFACT_ROLE, CONTRIBUTION_PATH,
    UPDATE_METADATA_ARTIFACT_ROLE,
};

impl NativeArtifactDeclaration for VerifiedPackArtifact {
    fn digest(&self) -> &str {
        &self.digest
    }
}

impl NativeDeclaredArtifactSource for NativeApplicationSources {
    type Declaration = VerifiedPackArtifact;

    fn root(&self) -> &Path {
        self.root(NativeSourceRoot::MaterializedPack)
    }

    fn declarations(&self) -> &BTreeMap<String, Self::Declaration> {
        &self.authority().verified_artifacts
    }
}

/// Move the original resolver selection into Core and recover it after cleanup.
///
/// An undeclared optional contribution returns without starting an inspection.
/// Callers supply an already composed Core in its actual Tokio runtime; this
/// function neither constructs another Core nor blocks on an async operation.
pub(crate) async fn collect_for_resolved_application(
    core: &Arc<Core>,
    sources: NativeApplicationSources,
) -> Result<(NativeApplicationSources, HostContractContributionValues)> {
    collect(sources, |sources| core.inspect_declared_artifacts(sources))
        .await
        .0
}

#[cfg(all(target_os = "macos", feature = "native-harness-bootstrap"))]
pub(crate) async fn collect_for_native_launch(
    sources: NativeApplicationSources,
    invocation: tobkiri_core::NativeSetupInvocation,
    cancellation: &tobkiri_gate::CoreCancellation,
) -> (Collection, bool) {
    collect(sources, |sources| {
        NativeArtifactInspection::from_invocation(sources, invocation, cancellation)
    })
    .await
}

type Collection = Result<(NativeApplicationSources, HostContractContributionValues)>;

async fn collect(
    sources: NativeApplicationSources,
    inspect: impl FnOnce(
        NativeApplicationSources,
    ) -> Result<
        NativeArtifactInspection<NativeApplicationSources>,
        NativeArtifactError,
    >,
) -> (Collection, bool) {
    match declared_artifact_for_role(
        &sources.authority().verified_artifacts,
        CONTRIBUTION_PATH,
        CONTRIBUTION_ARTIFACT_ROLE,
        "Host contract contribution",
    ) {
        Ok(Some(_)) => {}
        Ok(None) => return (Ok((sources, HostContractContributionValues::empty())), true),
        Err(error) => return (Err(error), true),
    }
    let mut inspection = match inspect(sources) {
        Ok(inspection) => inspection,
        Err(error) => return (Err(error.into()), true),
    };
    let values = collect_from_inspection(&mut inspection).await;
    // Retire and join before reporting either the value or the parser error.
    let sources = inspection.finish().await;
    let cleanup_verified = match &sources {
        Ok(_) => true,
        #[cfg(all(target_os = "macos", feature = "native-harness-bootstrap"))]
        Err(NativeArtifactError::Execution(
            tobkiri_wasm::NativeDataIoError::Cancelled | tobkiri_wasm::NativeDataIoError::Deadline,
        )) => true,
        Err(_) => false,
    };
    (
        finish_collection(values, sources.map_err(Into::into)),
        cleanup_verified,
    )
}

async fn collect_from_inspection(
    inspection: &mut NativeArtifactInspection<NativeApplicationSources>,
) -> Result<HostContractContributionValues> {
    let contribution = read_declared_json_artifact(
        inspection,
        CONTRIBUTION_PATH,
        CONTRIBUTION_ARTIFACT_ROLE,
        "Host contract contribution",
    )
    .await?
    .context("Host contract contribution is no longer declared")?;
    let request = inspection.with_source(|sources| {
        prepare_contribution(
            sources.root(NativeSourceRoot::MaterializedPack),
            &sources.authority().materialized_pack_id,
            &contribution,
        )
    })??;
    let metadata = match request.version_path() {
        Some(version_path) => {
            read_declared_json_artifact(
                inspection,
                version_path,
                UPDATE_METADATA_ARTIFACT_ROLE,
                "Pack update metadata",
            )
            .await?
        }
        None => None,
    };
    request.finish(metadata.as_ref())
}

async fn read_declared_json_artifact(
    inspection: &mut NativeArtifactInspection<NativeApplicationSources>,
    relative: &str,
    expected_role: &str,
    label: &str,
) -> Result<Option<Value>> {
    let declared = inspection.with_source(|sources| {
        declared_artifact_for_role(
            &sources.authority().verified_artifacts,
            relative,
            expected_role,
            label,
        )
        .map(|artifact| artifact.is_some())
    })??;
    if !declared {
        return Ok(None);
    }
    // Role validation above precedes every read. Core independently selects
    // the actual declaration and returns only identity/digest-verified bytes.
    let bytes = inspection.read(relative).await?;
    parse_json_artifact(bytes.as_bytes(), label).map(Some)
}

fn finish_collection(
    values: Result<HostContractContributionValues>,
    sources: Result<NativeApplicationSources>,
) -> Result<(NativeApplicationSources, HostContractContributionValues)> {
    match (values, sources) {
        (Ok(values), Ok(sources)) => Ok((sources, values)),
        (Err(error), Ok(_)) => Err(error),
        (Ok(_), Err(error)) => Err(error),
        (Err(error), Err(cleanup_error)) => Err(error).with_context(|| {
            format!("declared artifact inspection cleanup also failed: {cleanup_error}")
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::{finish_collection, HostContractContributionValues};

    #[test]
    fn read_failure_remains_the_source_when_cleanup_also_fails() {
        let error = finish_collection(
            Err(anyhow::anyhow!("original declared artifact read failure")),
            Err(anyhow::anyhow!("physical cleanup failure")),
        )
        .err()
        .expect("both failures must be reported");
        assert_eq!(
            error.root_cause().to_string(),
            "original declared artifact read failure"
        );
        assert!(error.to_string().contains("physical cleanup failure"));
    }

    #[test]
    fn cleanup_failure_prevents_publishing_parsed_contributions() {
        let error = finish_collection(
            Ok(HostContractContributionValues::empty()),
            Err(anyhow::anyhow!("physical cleanup failure")),
        )
        .err()
        .expect("cleanup must finish before source custody can return");
        assert_eq!(error.to_string(), "physical cleanup failure");
    }
}
