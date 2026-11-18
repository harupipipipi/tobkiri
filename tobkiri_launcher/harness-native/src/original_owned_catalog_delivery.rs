//! Ordered delivery of the verified catalog within the original Review call.
use super::*;
use crate::bootstrap_metadata::string_parts;
use native_bundled_catalog::{NativeBundleError, NativeOwnedBundle, label};
use tobkiri_canon::{AllocationError, CoreArray};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const SOFT_FRAME_BYTES: usize = 128 * 1024;

pub(super) enum OwnedCatalogDelivery {
    Armed,
    Reading {
        bundle: Funded<Box<NativeOwnedBundle>>,
        next: usize,
    },
}

impl CatalogIo {
    pub(super) fn write_owned_root(
        &self,
        out: &mut CoreBufferBuilder,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<()> {
        out.append(b"{\"kind\":\"snapshot-root\",\"binding\":")?;
        let binding = self
            .binding
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        out.append(binding.encode_guest(budget)?.as_bytes())?;
        out.append(b",\"value\":")?;
        write_path_text(out, &self.selected)?;
        out.append(b"}")?;
        Ok(())
    }

    pub(super) fn inspect_owned_delivery<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        mut out: CoreBufferBuilder,
        operation: &str,
        steps: &CoreArray<'_>,
        args: &CoreArray<'_>,
    ) -> anyhow::Result<CoreBuffer> {
        let unavailable = OriginalAuthorityError::unavailable;
        op.check(STAGE)?;
        if self.loader != Loader::RootSymlink || !steps.is_empty() {
            return Err(unavailable().into());
        }
        match (operation, self.owned_delivery.as_mut()) {
            ("snapshot-open", Some(OwnedCatalogDelivery::Armed)) if args.is_empty() => {
                // Consume the physical open before attempting it. Both domain
                // errors and failed publication must leave a terminal loader.
                self.owned_delivery = None;
                self.loader = Loader::Rejected;
                match self.compose_owned_bundle(op) {
                    Ok(bundle) => {
                        write_open(&mut out, &bundle)?;
                        let response = seal(op, out)?;
                        let capacity = op.reserve(STAGE, size_of::<NativeOwnedBundle>())?;
                        let bundle = Funded {
                            value: Box::new(bundle),
                            _capacity: capacity,
                        };
                        self.owned_delivery =
                            Some(OwnedCatalogDelivery::Reading { bundle, next: 0 });
                        self.loader = Loader::RootSymlink;
                        Ok(response)
                    }
                    Err(error) => {
                        self.write_owned_error(&mut out, error)?;
                        seal(op, out)
                    }
                }
            }
            ("snapshot-rows", Some(OwnedCatalogDelivery::Reading { bundle, next }))
                if ordinal(args) == Some(*next) && *next < bundle.entry_count() =>
            {
                let emitted = write_rows(op, &mut out, bundle, *next, &self.budget)?;
                let response = seal(op, out)?;
                *next += emitted;
                Ok(response)
            }
            ("snapshot-close", Some(OwnedCatalogDelivery::Reading { bundle, next }))
                if ordinal(args) == Some(*next) && *next == bundle.entry_count() =>
            {
                out.append(b"{\"kind\":\"unit\"}")?;
                let response = seal(op, out)?;
                let Some(OwnedCatalogDelivery::Reading { bundle, .. }) = self.owned_delivery.take()
                else {
                    unreachable!("completed catalog delivery");
                };
                self.loader = Loader::Rejected;
                self.adopt_owned_bundle(op, *bundle.value)?;
                op.check(STAGE)?;
                self.loader = Loader::Done;
                Ok(response)
            }
            _ => Err(unavailable().into()),
        }
    }

    fn write_owned_error(
        &mut self,
        out: &mut CoreBufferBuilder,
        error: NativeBundleError,
    ) -> anyhow::Result<()> {
        match error {
            NativeBundleError::Integrity(message) => {
                write_error(out, "integrity", [message.as_str()])?;
            }
            NativeBundleError::InvalidLockEntry { path, field } => {
                write_error(
                    out,
                    "integrity",
                    ["bundle entry ", path.as_str()?, " has an invalid ", field],
                )?;
            }
            NativeBundleError::TypeError(message) => write_error(out, "type-error", [message])?,
            NativeBundleError::Authority(error)
                if error.class == "builtins.ValueError"
                    && error.cause.is_none()
                    && error.context.is_none() =>
            {
                write_error(out, "value-error", [error.message.as_str()])?;
            }
            NativeBundleError::Authority(error) => return Err(error.into()),
            NativeBundleError::Lock { cause, detail, .. } => {
                let detail = detail.as_str()?;
                let message = ["cannot read bundle lock: ", detail];
                if matches!(&cause.cause, Some(OriginalAuthorityCause::Io(_))) {
                    let budget = self.budget.clone();
                    let graph = self.error_graph(&cause, &budget)?;
                    write_error(out, "lock-io", message)?;
                    out.append(b",\"cause\":{\"code\":")?;
                    string_parts(out, [graph.code().as_str()?])?;
                    out.append(b",\"message\":")?;
                    string_parts(out, [graph.message().as_str()?])?;
                    out.append(b",\"details\":")?;
                    string_parts(out, [graph.details().as_str()?])?;
                    out.append(b"}")?;
                } else if cause.class == "tobkiri_protocol.errors.CanonicalizationError"
                    && cause.cause.is_none()
                    && cause.context.is_none()
                    && !cause.suppressed
                {
                    write_error(out, "lock-json", message)?;
                    out.append(b",\"cause\":")?;
                    string_parts(out, [detail])?;
                } else {
                    return Err((*cause).into());
                }
            }
            error @ NativeBundleError::UnrepresentedDocument => return Err(error.into()),
        }
        out.append(b"}")?;
        Ok(())
    }
}

fn ordinal(args: &CoreArray<'_>) -> Option<usize> {
    if args.len() != 1 {
        return None;
    }
    args.get(0)?.as_i64()?.try_into().ok()
}

fn write_path_text(out: &mut CoreBufferBuilder, path: &Path) -> anyhow::Result<()> {
    let path = path
        .to_str()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    string_parts(out, [path])?;
    Ok(())
}

fn write_open(out: &mut CoreBufferBuilder, bundle: &NativeOwnedBundle) -> anyhow::Result<()> {
    out.append(b"{\"kind\":\"snapshot-open\",\"root\":")?;
    write_path_text(out, &bundle.root)?;
    out.append(b",\"artifact_root\":")?;
    if let Some(path) = &bundle.artifact_root {
        write_path_text(out, path)?;
    } else {
        out.append(b"null")?;
    }
    out.append(b",\"count\":")?;
    out.append(bundle.entry_count().to_string().as_bytes())?;
    out.append(b"}")?;
    Ok(())
}

fn write_rows<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    out: &mut CoreBufferBuilder,
    bundle: &NativeOwnedBundle,
    next: usize,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<usize> {
    out.append(b"{\"kind\":\"snapshot-rows\",\"rows\":[")?;
    charge_work(budget, next as u64)?;
    let mut emitted = 0;
    for (kind, id, bytes) in bundle.entries().skip(next) {
        op.check(STAGE)?;
        let kind = label(kind);
        charge_work(budget, (kind.len() + id.len() + bytes.len()) as u64)?;
        let text = std::str::from_utf8(bytes).map_err(|_| OriginalAuthorityError::unavailable())?;
        let row_bytes = [kind, id, text]
            .into_iter()
            .try_fold(4usize, |total, value| {
                total
                    .checked_add(quoted_bytes(value)?)
                    .ok_or(AllocationError::Limit)
            })?;
        // Include the next comma and the result and response suffix. A row
        // above the soft limit gets its own frame under the hard limit.
        let size = out.len() + usize::from(emitted != 0) + row_bytes + 3;
        if emitted != 0 && size > SOFT_FRAME_BYTES {
            break;
        }
        if size > MAX_BYTES {
            return Err(AllocationError::Limit.into());
        }
        out.append(if emitted == 0 { b"[" } else { b",[" })?;
        string_parts(out, [kind])?;
        out.append(b",")?;
        string_parts(out, [id])?;
        out.append(b",")?;
        string_parts(out, [text])?;
        out.append(b"]")?;
        emitted += 1;
    }
    if emitted == 0 {
        return Err(OriginalAuthorityError::unavailable().into());
    }
    out.append(b"]}")?;
    Ok(emitted)
}

fn quoted_bytes(text: &str) -> std::result::Result<usize, AllocationError> {
    text.bytes().try_fold(2usize, |count, byte| {
        let width = match byte {
            b'"' | b'\\' => 2,
            0..=31 => 6,
            _ => 1,
        };
        count.checked_add(width).ok_or(AllocationError::Limit)
    })
}

fn write_error<'a>(
    out: &mut CoreBufferBuilder,
    kind: &'static str,
    message: impl IntoIterator<Item = &'a str>,
) -> anyhow::Result<()> {
    out.append(b"{\"kind\":\"snapshot-error\",\"error\":")?;
    string_parts(out, [kind])?;
    out.append(b",\"message\":")?;
    string_parts(out, message)?;
    Ok(())
}

fn seal<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    mut out: CoreBufferBuilder,
) -> anyhow::Result<CoreBuffer> {
    op.check(STAGE)?;
    out.append(b"}")?;
    Ok(out.finish()?)
}
