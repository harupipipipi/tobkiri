//! WIT adapter for descriptor-confined filesystem capabilities.
use super::{get_resource, push, resource_rep};
use crate::{
    InvocationState,
    effect_values::{EffectValues, text, uint},
    filesystem::{FsCall, FsDirectory, FsOutput},
    host::{PackHost, PackState},
    owned_values::ValueOwners,
};
use anyhow::{Context, Result, bail};
use tobkiri_gate::CallCx;
use wasmtime::StoreContextMut;
use wasmtime::component::{BudgetedVecBuilder, Val};

pub(super) async fn invoke(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    host: &PackHost,
    cx: &CallCx,
    version: &str,
    operation: &str,
    params: &[Val],
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    if operation == "open-grant" {
        let [token] = params else {
            bail!("invalid filesystem open argument count")
        };
        let registry = host
            .shared
            .filesystem
            .get()
            .context("Core filesystem registry is not installed")?;
        let prepared = registry.prepare_open(cx, text(token)?)?;
        let grant = host
            .shared
            .gate
            .authorize(
                cx,
                prepared.request().clone(),
                prepared.args_digest().clone(),
            )
            .await?;
        let id = grant.id().clone();
        let result = prepared.consume(grant, cx, host.shared.gate.clone()).await;
        let completion = match result {
            Ok(completion) => completion,
            Err(error) => {
                let result: Result<()> = Err(error.into());
                super::outcome(host, id, &result, cx).await?;
                return result.map(|_| unreachable!());
            }
        };
        let result = (|| {
            cx.check_active()?;
            codec.ok(Some(push(store, version, completion.directory.clone())?))
        })();
        super::outcome(host, id, &result, completion.context()).await?;
        completion.context().check_active()?;
        return result;
    }
    let resource = params
        .first()
        .context("filesystem directory argument missing")?;
    let rep = resource_rep::<FsDirectory>(store, version, resource)?;
    let directory = get_resource::<FsDirectory>(store, version, rep)?.clone();
    let call = parse_call(operation, &params[1..], &codec)?;
    let prepared = directory.prepare(cx, call)?;
    let grant = host
        .shared
        .gate
        .authorize(
            cx,
            prepared.request().clone(),
            prepared.args_digest().clone(),
        )
        .await?;
    let id = grant.id().clone();
    let work = prepared
        .consume(grant, cx, host.shared.gate.clone(), owners.graph().clone())
        .await;
    let work = match work {
        Ok(work) => work,
        Err(error) => {
            let result: Result<()> = Err(error.into());
            super::outcome(host, id, &result, cx).await?;
            return result.map(|_| unreachable!());
        }
    };
    let delivery = work.dispatch().await?;
    let result = delivery.convert(|output| encode(output, &codec));
    delivery.finish(result).await
}

fn parse_call<'a>(
    operation: &str,
    params: &'a [Val],
    codec: &EffectValues<'_>,
) -> Result<FsCall<'a>> {
    Ok(match (operation, params) {
        ("[method]dir.read", [path, offset, len]) => FsCall::Read {
            path: text(path)?,
            offset: uint(offset)?,
            len: u32::try_from(uint(len)?)?,
        },
        ("[method]dir.write", [path, data, Val::Bool(append)]) => FsCall::Write {
            path: text(path)?,
            data: codec.copy_bytes(data)?,
            append: *append,
        },
        ("[method]dir.list", [path]) => FsCall::List { path: text(path)? },
        ("[method]dir.stat", [path]) => FsCall::Stat { path: text(path)? },
        ("[method]dir.rename", [from, to]) => FsCall::Rename {
            from: text(from)?,
            to: text(to)?,
        },
        ("[method]dir.remove", [path]) => FsCall::Remove { path: text(path)? },
        ("[method]dir.mkdir", [path]) => FsCall::Mkdir { path: text(path)? },
        _ => bail!("unknown filesystem operation or invalid argument count/type"),
    })
}

fn entry(codec: &EffectValues<'_>, name: &str, is_dir: bool, size: u64) -> Result<Val> {
    codec.record(
        [
            ("name", codec.string(name)?),
            ("is-dir", Val::Bool(is_dir)),
            ("size", Val::U64(size)),
        ]
        .into_iter(),
    )
}

fn encode(output: FsOutput, codec: &EffectValues<'_>) -> Result<Vec<Val>> {
    let value = match output {
        FsOutput::Unit => None,
        FsOutput::Bytes(value) => Some(codec.bytes(value.as_bytes())?),
        FsOutput::Stat(stat) => Some(entry(
            codec,
            stat.name
                .as_str()
                .context("invalid native filesystem name")?,
            stat.is_directory,
            stat.size,
        )?),
        FsOutput::Entries(entries) => {
            let entries = entries
                .array()
                .context("invalid native filesystem entries")?;
            let mut values = BudgetedVecBuilder::new(entries.len(), codec.0.component(), 0)?;
            for item in entries.iter() {
                let name = item
                    .get_key("name")
                    .context("filesystem entry name missing")?;
                let is_dir = item
                    .get_key("is-dir")
                    .context("filesystem entry kind missing")?;
                let size = item
                    .get_key("size")
                    .context("filesystem entry size missing")?;
                values.push(entry(
                    codec,
                    name.as_str().context("invalid filesystem entry name")?,
                    is_dir.as_bool().context("invalid filesystem entry kind")?,
                    size.as_str()
                        .context("invalid filesystem entry size")?
                        .parse()?,
                )?)?;
            }
            Some(Val::List(values.finish()?))
        }
    };
    codec.ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    };
    use tobkiri_canon::{AllocationBudget, AllocationError, AllocationRequest, CoreValue};
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

    #[derive(Default)]
    struct Meter(AtomicU64);
    impl AllocationBudget for Meter {
        fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
            self.0.fetch_add(request.bytes, Ordering::AcqRel);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.0.fetch_sub(bytes, Ordering::AcqRel);
        }
    }
    impl HostAllocationBudget for Meter {
        fn reserve(
            &self,
            request: HostAllocationRequest,
        ) -> std::result::Result<(), HostAllocationError> {
            self.0.fetch_add(request.bytes, Ordering::AcqRel);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.0.fetch_sub(bytes, Ordering::AcqRel);
        }
    }

    #[test]
    fn list_encoding_preserves_complete_wit_u64_and_owned_unicode_names() {
        let meter = Arc::new(Meter::default());
        let owners = ValueOwners::from_budgets(meter.clone(), meter.clone());
        let codec = EffectValues(&owners);
        let entry = CoreValue::object_shared(
            [
                Ok((
                    "name",
                    CoreValue::string("資料\".txt", owners.graph()).unwrap(),
                )),
                Ok(("is-dir", CoreValue::bool(false, owners.graph()).unwrap())),
                Ok((
                    "size",
                    CoreValue::string("18446744073709551615", owners.graph()).unwrap(),
                )),
            ]
            .into_iter(),
            owners.graph(),
        )
        .unwrap();
        let entries = CoreValue::array_from([Ok(entry)].into_iter(), owners.graph()).unwrap();
        let result = encode(FsOutput::Entries(entries), &codec).unwrap();
        let [Val::Result(Ok(Some(value)))] = result.as_slice() else {
            panic!("list result expected")
        };
        let Val::List(entries) = &**value else {
            panic!("list expected")
        };
        let first = &entries[0];
        assert_eq!(
            text(crate::effect_values::field(first, "name").unwrap()).unwrap(),
            "資料\".txt"
        );
        assert!(matches!(
            crate::effect_values::field(first, "size").unwrap(),
            Val::U64(u64::MAX)
        ));
        drop(result);
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }

    #[test]
    fn malformed_write_bytes_and_oversize_read_are_rejected_before_authorization() {
        let meter = Arc::new(Meter::default());
        let owners = ValueOwners::from_budgets(meter.clone(), meter.clone());
        let codec = EffectValues(&owners);
        {
            let params = [
                codec.string("path").unwrap(),
                Val::U64(0),
                Val::U64(u64::from(u32::MAX) + 1),
            ];
            assert!(parse_call("[method]dir.read", &params, &codec).is_err());
        }
        {
            let data = codec
                .list([codec.string("not a byte").unwrap()].into_iter())
                .unwrap();
            let params = [codec.string("path").unwrap(), data, Val::Bool(false)];
            assert!(parse_call("[method]dir.write", &params, &codec).is_err());
        }
        assert_eq!(meter.0.load(Ordering::Acquire), 0);
    }
}
