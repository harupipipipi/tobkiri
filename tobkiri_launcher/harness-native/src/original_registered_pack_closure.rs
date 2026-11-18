//! Resolve the admitted Pack inventory for one registered Setup candidate.
use super::*;
use catalog::CatalogIo;
use external_catalog::{ClosureExecutable, ClosureManifest};
use registered_snapshot::RegisteredCatalogPolicy;
use tobkiri_canon::{AllocationBudget, AllocationKind, AllocationRequest, CoreValue, MAX_BYTES};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

#[derive(Clone, Copy)]
pub(crate) enum RegisteredPackRoots {
    Active,
    Declared,
}

pub(crate) enum RegisteredPackClosure {
    Rows(CoreValue),
    BundledSource,
    HostExtension,
}

impl PublicationRead {
    pub(crate) fn read_registered_pack_closure<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn AllocationBudget>,
        loader: &Mutex<Option<CatalogIo>>,
        records: &RegisteredCatalogPolicy,
        roots: RegisteredPackRoots,
    ) -> Result<RegisteredPackClosure> {
        let root = self.registered_pack_closure_root(owner, cancellation)?;
        let op = self.candidate_operation(owner, cancellation)?;
        let io = loader.lock().unwrap_or_else(|error| error.into_inner());
        let io = io
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        io.require_recovery_catalog(&op)?;
        owner.with_original_catalog_source(|source| {
            PackWalk {
                op: &op,
                budget,
                io,
                runtime: source.runtime,
                user_data: root,
            }
            .resolve(records, roots)
        })?
    }

    pub(crate) fn record_registered_review_packs<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
        loader: &Mutex<Option<CatalogIo>>,
        rows: &CoreValue,
    ) -> Result<()> {
        self.registered_pack_closure_root(owner, cancellation)?;
        let op = self.candidate_operation(owner, cancellation)?;
        loader
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_mut()
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .record_registered_review_packs(&op, rows)
    }
}

struct PackWalk<'a, 'host, S> {
    op: &'a Operation<'host, S>,
    budget: &'a Arc<dyn AllocationBudget>,
    io: &'a CatalogIo,
    runtime: &'a Path,
    user_data: &'a Path,
}

impl<S: crate::BootstrapReviewOwner> PackWalk<'_, '_, S> {
    fn resolve(
        &self,
        records: &RegisteredCatalogPolicy,
        roots: RegisteredPackRoots,
    ) -> Result<RegisteredPackClosure> {
        let (mut queue, pins) = self.inputs(records, roots)?;
        let mut manifests =
            CoreValue::array_builder_incremental(self.budget, MAX_BYTES).map_err(capacity)?;
        let mut cursor = 0;
        while cursor < queue.items.len() {
            self.op.check(STAGE)?;
            let id_value = queue.items[cursor].share();
            cursor += 1;
            let id = as_text(&id_value)?;
            let pin = selected_pin(&pins, id, self.budget)?;
            let selected = pin.as_ref().map(as_text).transpose()?;
            let present = self.io.review_pack_digest(id)?;
            if present.is_some_and(|digest| selected.is_none_or(|selected| selected == digest)) {
                self.enqueue_keys(&mut queue, self.io.review_pack_dependencies(id)?, false)?;
                continue;
            }
            let fetched = external_catalog::read_registered_closure_manifest(
                self.op,
                self.runtime,
                self.user_data,
                self.budget,
                id,
                &pins,
            )?;
            match fetched {
                ClosureManifest::External {
                    manifest,
                    artifact_digest,
                } => {
                    let requirements = field(&manifest, "requirements")?;
                    let dependencies = field(&requirements, "pack_dependencies")?;
                    self.enqueue_keys(&mut queue, &dependencies, true)?;
                    manifests
                        .push(row(self.budget, id_value, manifest, artifact_digest)?)
                        .map_err(capacity)?;
                }
                ClosureManifest::BundledSource => return Ok(RegisteredPackClosure::BundledSource),
                ClosureManifest::HostExtension => return Ok(RegisteredPackClosure::HostExtension),
            }
        }
        drop((queue, pins));
        self.executables(&manifests.finish().map_err(capacity)?)
    }

    fn inputs(
        &self,
        records: &RegisteredCatalogPolicy,
        roots: RegisteredPackRoots,
    ) -> Result<(Queue, CoreValue)> {
        let declared = field(&records.registered_profile, "packs")?;
        let declared = declared
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let _pins = reserve_slots::<(CoreValue, CoreValue), _>(self.op, declared.len())?;
        let mut pins: Vec<(CoreValue, CoreValue)> = checked_vec(declared.len())?;
        let mut queue = Queue::new(self.op)?;
        for row in declared.iter() {
            self.op.check(STAGE)?;
            let id = field(&row, "pack_id")?;
            let id_text = as_text(&id)?;
            if matches!(roots, RegisteredPackRoots::Declared) {
                queue.push(id.share(), self.budget)?;
            }
            let Some(pin) = row.get_key("artifact_digest") else {
                continue;
            };
            if matches!(pin.inspect(), tobkiri_canon::CoreRef::Null) {
                continue;
            }
            as_text(&pin)?;
            charge(
                self.budget,
                (id_text.len() + 1).saturating_mul(pins.len() + 1),
            )?;
            if let Some((_, previous)) = pins
                .iter_mut()
                .find(|(key, _)| key.as_str() == Some(id_text))
            {
                *previous = pin;
            } else {
                pins.push((id, pin));
            }
        }
        if matches!(roots, RegisteredPackRoots::Active) {
            let active = &records.active_profile;
            let selected = field(active, "packs")?;
            for row in selected
                .array()
                .ok_or_else(OriginalAuthorityError::unavailable)?
                .iter()
            {
                queue.push(field(&row, "pack_id")?, self.budget)?;
            }
            for role in ["base", "shell"] {
                queue.push(field(&field(active, role)?, "pack_id")?, self.budget)?;
            }
            sort_ids(&mut queue.items, self.budget)?;
        }
        let mut ordered = CoreValue::array_builder(self.budget, pins.len()).map_err(capacity)?;
        for (id, pin) in pins {
            let entry = CoreValue::array_shared([Ok(id), Ok(pin)].into_iter(), self.budget)
                .map_err(capacity)?;
            ordered.push(entry).map_err(capacity)?;
        }
        Ok((queue, ordered.finish().map_err(capacity)?))
    }

    fn enqueue_keys(
        &self,
        queue: &mut Queue,
        dependencies: &CoreValue,
        manifest: bool,
    ) -> Result<()> {
        let count = if manifest {
            dependencies
                .object()
                .ok_or_else(OriginalAuthorityError::unavailable)?
                .len()
        } else {
            dependencies
                .array()
                .ok_or_else(OriginalAuthorityError::unavailable)?
                .len()
        };
        let _keys = reserve_slots::<CoreValue, _>(self.op, count)?;
        let mut keys = checked_vec(count)?;
        if manifest {
            for (id, _) in dependencies
                .object()
                .expect("verified dependency map")
                .iter()
            {
                keys.push(CoreValue::string(id, self.budget).map_err(capacity)?);
            }
        } else {
            keys.extend(
                dependencies
                    .array()
                    .expect("verified dependency keys")
                    .iter(),
            );
        }
        sort_ids(&mut keys, self.budget)?;
        for id in keys {
            self.op.check(STAGE)?;
            queue.push(id, self.budget)?;
        }
        Ok(())
    }

    fn executables(&self, manifests: &CoreValue) -> Result<RegisteredPackClosure> {
        let manifests = manifests
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let _order = reserve_slots::<(usize, CoreValue), _>(self.op, manifests.len())?;
        let mut order = checked_vec(manifests.len())?;
        for (position, row) in manifests.iter().enumerate() {
            order.push((
                position,
                row.get_index(0).expect("verified external Pack ID"),
            ));
        }
        sort_cost(self.budget, order.len(), order.iter().map(|(_, id)| id))?;
        order.sort_unstable_by(|(_, left), (_, right)| left.as_str().cmp(&right.as_str()));
        let mut verified =
            CoreValue::array_builder_slots(self.budget, order.len()).map_err(capacity)?;
        for (position, id) in order {
            self.op.check(STAGE)?;
            let manifest = manifests.get(position).expect("verified Pack row");
            let digest = manifest
                .get_index(2)
                .expect("verified external Pack digest");
            let manifest = manifest
                .get_index(1)
                .expect("verified external Pack manifest");
            let executable = external_catalog::read_registered_closure_executable(
                self.op,
                self.runtime,
                self.user_data,
                self.budget,
                as_text(&id)?,
                &manifest,
                as_text(&digest)?,
            )?;
            match executable {
                ClosureExecutable::External(executable) => {
                    verified
                        .set(position, row(self.budget, id, manifest, executable)?)
                        .map_err(capacity)?;
                }
                ClosureExecutable::HostExtension => {
                    return Ok(RegisteredPackClosure::HostExtension);
                }
            }
        }
        Ok(RegisteredPackClosure::Rows(
            verified.finish().map_err(capacity)?,
        ))
    }
}

struct Queue {
    items: Vec<CoreValue>,
    funding: NativeSetupReservation,
}

impl Queue {
    fn new<S: OriginalAuthorityOwner>(op: &Operation<'_, S>) -> Result<Self> {
        Ok(Self {
            items: Vec::new(),
            funding: op.reserve(STAGE, 1)?,
        })
    }

    fn push(&mut self, value: CoreValue, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
        let id = as_text(&value)?;
        if id.is_empty() || !id.is_ascii() {
            return Err(OriginalAuthorityError::unavailable());
        }
        charge(budget, (id.len() + 1).saturating_mul(self.items.len() + 1))?;
        if self.items.iter().any(|seen| seen.as_str() == Some(id)) {
            return Ok(());
        }
        let old = self.items.capacity();
        if self.items.len() == old {
            let next = old.max(4).checked_mul(2).ok_or_else(|| capacity(()))?;
            self.resize(old.checked_add(next).ok_or_else(|| capacity(()))?)?;
            self.items.try_reserve_exact(next - old).map_err(capacity)?;
            if self.items.capacity() != next {
                return Err(capacity(()));
            }
            self.resize(next)?;
        }
        self.items.push(value);
        Ok(())
    }

    fn resize(&self, slots: usize) -> Result<()> {
        let bytes = slots
            .checked_mul(size_of::<CoreValue>())
            .ok_or_else(|| capacity(()))?;
        self.funding.resize(bytes as u64).map_err(capacity)
    }
}

fn sort_ids(ids: &mut [CoreValue], budget: &Arc<dyn AllocationBudget>) -> Result<()> {
    sort_cost(budget, ids.len(), ids.iter())?;
    ids.sort_unstable_by(|left, right| left.as_str().cmp(&right.as_str()));
    Ok(())
}

fn sort_cost<'a>(
    budget: &Arc<dyn AllocationBudget>,
    count: usize,
    ids: impl Iterator<Item = &'a CoreValue>,
) -> Result<()> {
    let longest = ids
        .filter_map(CoreValue::as_str)
        .map(str::len)
        .max()
        .unwrap_or(0);
    charge(
        budget,
        count
            .saturating_mul(usize::BITS as usize)
            .saturating_mul(longest + 1),
    )
}

fn checked_vec<T>(size: usize) -> Result<Vec<T>> {
    let mut values = Vec::new();
    values.try_reserve_exact(size).map_err(capacity)?;
    if values.capacity() != size {
        return Err(capacity(()));
    }
    Ok(values)
}

fn reserve_slots<T, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    slots: usize,
) -> Result<NativeSetupReservation> {
    op.reserve(
        STAGE,
        slots
            .checked_mul(size_of::<T>())
            .ok_or_else(|| capacity(()))?,
    )
}

fn row(
    budget: &Arc<dyn AllocationBudget>,
    id: CoreValue,
    manifest: CoreValue,
    field: CoreValue,
) -> Result<CoreValue> {
    CoreValue::array_shared([Ok(id), Ok(manifest), Ok(field)].into_iter(), budget).map_err(capacity)
}

fn field(value: &CoreValue, key: &str) -> Result<CoreValue> {
    value
        .get_key(key)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn as_text(value: &CoreValue) -> Result<&str> {
    value
        .as_str()
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn selected_pin(
    pins: &CoreValue,
    id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreValue>> {
    let pins = pins
        .array()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    charge(budget, pins.len().saturating_mul(id.len() + 1))?;
    for row in pins.iter() {
        if row.get_index(0).as_ref().and_then(CoreValue::as_str) == Some(id) {
            return Ok(row.get_index(1));
        }
    }
    Ok(None)
}

fn charge(budget: &Arc<dyn AllocationBudget>, work: usize) -> Result<()> {
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: work as u64,
        })
        .map_err(capacity)
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::{Value, json};

    #[tokio::test(flavor = "current_thread")]
    async fn registered_closure_preserves_pin_order_and_projects_an_exact_signed_revision() {
        with_source(|owner, read, cancellation, budget| {
            let op = operation(read, owner, cancellation);
            let mut io = CatalogIo::new(
                owner,
                &read.invocation,
                cancellation,
                read.cleanup.clone(),
                &owner.root,
                budget,
            )
            .unwrap();
            io.retain_review_pack_dependencies();
            let parse = |value: Value| {
                CoreValue::parse_guest(&serde_json::to_vec(&value).unwrap(), budget).unwrap()
            };
            let walk = PackWalk {
                op: &op,
                budget,
                io: &io,
                runtime: &owner.root,
                user_data: &owner.root,
            };
            let mut records = RegisteredCatalogPolicy {
                active_profile: parse(json!({
                    "packs": [{"pack_id": "qa.z"}, {"pack_id": "qa.a"}, {"pack_id": "qa.a"}],
                    "base": {"pack_id": "qa.base"}, "shell": {"pack_id": "qa.shell"},
                })),
                registered_profile: parse(json!({"packs": [
                    {"pack_id":"qa.z", "artifact_digest": "old"},
                    {"pack_id":"qa.unvisited", "artifact_digest": "unrelated"},
                    {"pack_id":"qa.z", "artifact_digest": "new"},
                    {"pack_id":"qa.a", "artifact_digest": null},
                ]})),
                active_plan: None,
                verified_definition_digest: CoreValue::null(budget).unwrap(),
            };
            for (roots, expected) in [
                (
                    RegisteredPackRoots::Active,
                    &["qa.a", "qa.base", "qa.shell", "qa.z"][..],
                ),
                (
                    RegisteredPackRoots::Declared,
                    &["qa.z", "qa.unvisited", "qa.a"][..],
                ),
            ] {
                let (queue, pins) = walk.inputs(&records, roots).unwrap();
                let ids: Vec<_> = queue.items.iter().map(|id| id.as_str().unwrap()).collect();
                assert_eq!(ids, expected);
                assert!(
                    pins.eq_guest(
                        &parse(json!([["qa.z", "new"], ["qa.unvisited", "unrelated"]])),
                        budget
                    )
                    .unwrap()
                );
            }

            let Some(reference) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
                return;
            };
            let bytes = fs::read(reference).unwrap();
            assert!(bytes.len() <= MAX_BYTES);
            let fixture: Value = serde_json::from_slice(&bytes).unwrap();
            let case = fixture["cases"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["name"] == "retained-version")
                .unwrap();
            let pin = &case["retained_probe"]["ordered"][0];
            let id = pin["key"].as_str().unwrap();
            let digest = pin["artifact_digest"].as_str().unwrap();
            records.registered_profile =
                parse(json!({"packs": [{"pack_id": id, "artifact_digest": digest}]}));
            let signed = PackWalk {
                runtime: Path::new(case["runtime_dir"].as_str().unwrap()),
                user_data: Path::new(case["native_user_data"].as_str().unwrap()),
                ..walk
            };
            let RegisteredPackClosure::Rows(rows) = signed
                .resolve(&records, RegisteredPackRoots::Declared)
                .unwrap()
            else {
                panic!("expected an independently verified signed Pack row");
            };
            let rows = rows.array().unwrap();
            assert_eq!(rows.len(), 1);
            let row = rows.get(0).unwrap();
            assert_eq!(row.get_index(0).unwrap().as_str(), Some(id));
            assert_eq!(
                row.get_index(1)
                    .unwrap()
                    .get_key("pack")
                    .unwrap()
                    .get_key("artifact_digest")
                    .unwrap()
                    .as_str(),
                Some(digest)
            );
            assert_eq!(
                row.get_index(2)
                    .unwrap()
                    .get_key("pack_id")
                    .unwrap()
                    .as_str(),
                Some(id)
            );
            assert!(read.receipt().verified());
        });
    }
}
