//! Revalidate the confirmed fresh Setup target before touching its journal.
use super::*;
use crate::BootstrapActivationCandidate;

pub(in crate::bootstrap_runtime) async fn verify<
    S: BootstrapInputOwner + crate::BootstrapReviewOwner,
>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    candidate: &BootstrapActivationCandidate,
) -> Result<std::result::Result<CoreValue, BootstrapReviewFailure>> {
    let caller = ReviewCaller::Publication;
    if let Err(error) = caller.check(review, pack) {
        return Ok(Err(original_failure(error)));
    }
    let request = denied::frame(pack, PUBLICATION_HOST_START_REQUEST, [])?;
    let started = match pack
        .observe_fixed_projection(&request, "bootstrap-publication-host-start")
        .await?
    {
        Ok(data) => data,
        Err(error) => {
            return Ok(Err(
                handle_call_failure(review, pack, caller, error, false).await?
            ));
        }
    };
    caller.check(review, pack)?;
    let scope = actual_call_scope(pack)?;
    publication_result(
        pack,
        &scope,
        &started,
        "io.tobkiri.native-bootstrap-publication-host-start-result.v1",
        &["format", "scope"],
    )?;
    drop((request, started));

    // host_profile_catalog may migrate existing definitions. The original
    // capture then obtains the source from an independent bundled catalog.
    let host_catalog = match materialize_host_catalog(review, pack, caller).await? {
        Ok(catalog) => catalog,
        Err(error) => return Ok(Err(error)),
    };
    if let Err(error) = load_bundle(review, pack, caller, Some(host_catalog.root)).await? {
        return Ok(Err(error));
    }
    let bundled = result_root(
        pack,
        returned(
            review
                .actual
                .catalog
                .as_ref()
                .context("reread catalog missing")?,
        )?,
        &scope,
    )?;
    let request = denied::frame(pack, PUBLICATION_SOURCE_REQUEST, [("catalog", bundled)])?;
    let source = match pack
        .observe_fixed_projection(&request, "bootstrap-publication-source")
        .await?
    {
        Ok(data) => data,
        Err(error) => {
            return Ok(Err(
                handle_call_failure(review, pack, caller, error, false).await?
            ));
        }
    };
    caller.check(review, pack)?;
    let packet = publication_result(
        pack,
        &scope,
        &source,
        "io.tobkiri.native-bootstrap-publication-source-result.v1",
        &["format", "scope", "source"],
    )?;
    let definition = denied::field(&packet, "source")?;
    if definition.object().is_none() {
        bail!("publication source is not a bundled Profile definition");
    }
    drop((packet, source, request));
    let raw_digest = {
        let work = pack.work.get();
        let mut io = work
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        io.as_mut()
            .context("post-confirmation catalog IO missing")?
            .publication_lock_digest(&work.source, &work.invocation)
    };
    let raw_digest = match raw_digest {
        Ok(digest) => digest,
        Err(error) => return Ok(Err(original_failure(error))),
    };
    if &definition.digest_guest(pack.input_budget)? != candidate.definition_digest()
        || &raw_digest != candidate.bundle_digest()
    {
        return Ok(Err(review_continuation::denied(
            review,
            pack,
            caller,
            "bootstrap target changed after confirmation",
            BootstrapDeniedRaise::Plain,
        )
        .await?));
    }
    caller.check(review, pack)?;
    Ok(Ok(definition))
}

fn publication_result<S: BootstrapInputOwner>(
    pack: &ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    data: &CoreBuffer,
    format: &str,
    fields: &[&str],
) -> Result<CoreValue> {
    let packet = CoreValue::parse_guest(data.as_bytes(), pack.input_budget)?;
    denied::exact(&packet, fields)?;
    if denied::field(&packet, "format")?.as_str() != Some(format)
        || !denied::same_scope(&denied::field(&packet, "scope")?, scope)
    {
        bail!("publication catalog result differs from the retained runtime");
    }
    Ok(packet)
}
