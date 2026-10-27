//! Same-task canonical reentry constraint, not independent task/CPython proof.
use super::*;
use anyhow::{Context, ensure};
use std::{fs::File, io::Read};
use tobkiri_gate::{CapacityKind, CapacityLedger};
use wasmtime::component::{BudgetedFuture, Component, Func, Linker, Val};
use wasmtime::{AsContextMut, OptLevel, Trap};

#[derive(Clone, Copy, Debug)]
enum Outcome {
    NotAttempted,
    Returned,
    CannotEnter,
    AllocationUnavailable,
    OtherError,
}
struct Reentry {
    second: Option<Func>,
    attempted: bool,
    outcome: Outcome,
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "isolated CI constraint observation; not a portable/native root substitute"]
async fn actual_same_task_sync_lift_reentry_constraint() -> Result<()> {
    ensure!(
        std::env::var("TOBKIRI_TEST_SYNC_REENTRY_CI").as_deref() == Ok("1"),
        "explicit isolated constraint CI required"
    );
    let _serial = crate::allocation::TEST_CUSTODY.lock().await;
    let _census = crate::child_census::lock_census_fixture().await;
    let temporary = tempfile::tempdir()?;
    // Fixture authoring precedes the real operation, just as the producer does.
    // Engine creation, actual source read and Wasmtime compilation do not.
    let bytes = wat::parse_str(
        r#"
      (component
        (type $t (func (result u32)))
        (import "tobkiri-native:reentry/host@1.0.0"
          (instance $host (export "observe" (func (type $t)))))
        (core func $host (canon lower (func $host "observe")))
        (core module $m
          (import "" "host" (func $host (result i32)))
          (func (export "first") (result i32) call $host)
          (func (export "second")))
        (core instance $i (instantiate $m
          (with "" (instance (export "host" (func $host))))))
        (func (export "first") (result u32) (canon lift (core func $i "first")))
        (func (export "second") (canon lift (core func $i "second"))))
    "#,
    )?;
    let path = temporary.path().join("authored-component");
    std::fs::write(&path, &bytes)?;
    let expected = tobkiri_canon::Digest::hash(&bytes);
    let size = bytes.len();
    drop(bytes);
    let control = CoreControl::default();
    let root = tobkiri_store::DataRoot::open(temporary.path().join("data"))?;
    let physical = root.native_setup_owner_lease(&control)?;
    let account =
        CapacityLedger::new().native_setup_account(&control, || Ok(Arc::new(physical.clone())))?;
    let invocation = account.begin()?;
    let original_deadline = invocation.deadline()?;
    let engine = WasmEngine::new_with_control_and_optimization(&control, OptLevel::None)?;
    let source_funds = invocation.reserve(CapacityKind::VfsBuffer, u64::try_from(size)?)?;
    let source_control = control.reserve(ControlKind::Frame, u64::try_from(size)?)?;
    let mut bytes = Vec::new();
    bytes.try_reserve_exact(size)?;
    ensure!(
        bytes.capacity() == size,
        "source allocator capacity differs"
    );
    bytes.resize(size, 0);
    File::open(path)?.read_exact(&mut bytes)?;
    ensure!(
        tobkiri_canon::Digest::hash(&bytes) == expected,
        "authored source changed"
    );
    let component = Component::new(engine.engine(), &bytes)?;
    drop(bytes);
    drop(source_funds);
    drop(source_control);
    ensure!(
        invocation.deadline()? == original_deadline && Instant::now() < original_deadline,
        "original preparation deadline exhausted"
    );
    let mut owned = engine.native_setup_store(
        invocation.clone(),
        Reentry {
            second: None,
            attempted: false,
            outcome: Outcome::NotAttempted,
        },
    )?;
    let component_owner = owned.allocation_owners().component().clone();
    let mut linker = Linker::<NativeState<Reentry>>::new(engine.engine());
    linker.instance("tobkiri-native:reentry/host@1.0.0")?.func_new_async_owned(
        "observe", &component_owner,
        |mut store, _ty, _params, results| {
            let owner = store.data().owners.component().clone();
            BudgetedFuture::new(async move {
                let operation: Result<()> = async {
                ensure!(!store.data().data.attempted, "second call attempted more than once");
                store.data_mut().data.attempted = true;
                let second = store.data().data.second.context("same-instance second export missing")?;
                // Dynamic owned calls are the production path. TypedFunc's
                // legacy call_async is an allocation-Unavailable stub here.
                println!("NATIVE_SYNC_REENTRY attempt=begin same_task=true independent_task=false");
                let result = second.call_async_owned(store.as_context_mut(), &[]).await;
                store.data_mut().data.outcome = match result {
                    Ok(values) => {
                        ensure!(values.as_slice().is_empty(), "second export returned data");
                        second.post_return_async(store.as_context_mut()).await?;
                        Outcome::Returned
                    }
                    Err(error) if matches!(error.downcast_ref::<Trap>(), Some(Trap::CannotEnterComponent)) => Outcome::CannotEnter,
                    Err(error) if matches!(error.downcast_ref::<wasmtime::component::HostAllocationError>(), Some(wasmtime::component::HostAllocationError::Unavailable)) => Outcome::AllocationUnavailable,
                    Err(_) => Outcome::OtherError,
                };
                results[0] = Val::U32(7);
                Ok(())
                }.await;
                operation.map_err(wasmtime::Error::from_anyhow)
            }, &owner, 0)
        },
    )?;
    let store = owned.store.as_mut().context("funded Store missing")?;
    store.host_allocation_budget(component_owner);
    let instance = linker.instantiate_async(&mut *store, &component).await?;
    let first = instance
        .get_func(&mut *store, "first")
        .context("first export missing")?;
    let second = instance
        .get_func(&mut *store, "second")
        .context("second export missing")?;
    store.data_mut().data.second = Some(second);
    let result = first.call_async_owned(&mut *store, &[]).await;
    let first_returned = match result {
        Ok(values) => {
            ensure!(
                matches!(values.as_slice(), [Val::U32(7)]),
                "first return differs"
            );
            first.post_return_async(&mut *store).await.is_ok()
        }
        Err(_) => false,
    };
    let state = &store.data().data;
    ensure!(
        state.attempted && !matches!(state.outcome, Outcome::NotAttempted),
        "same-task attempt not observed"
    );
    let fuel = store.get_fuel()?;
    let snapshot = invocation.capacity_snapshot()?;
    println!(
        "NATIVE_SYNC_REENTRY diagnostic_only=true same_task=true independent_task=false nested={:?} first_completed={} fuel_remaining={} elapsed_us={} peak_charged_bytes={} deadline_unchanged={}",
        state.outcome,
        first_returned,
        fuel,
        invocation.started_at().elapsed().as_micros(),
        snapshot.peak_charged_bytes,
        invocation.deadline()? == original_deadline
    );
    ensure!(
        fuel < 2_000_000_000 && snapshot.peak_charged_bytes <= NATIVE_SETUP_MEMORY_BYTES,
        "original resource bounds differ"
    );
    ensure!(
        invocation.deadline()? == original_deadline && Instant::now() < original_deadline,
        "original constraint deadline exhausted"
    );
    // No new entry, retry, independent task or state reuse after this result.
    drop(owned);
    drop(component);
    drop(linker);
    drop(engine);
    crate::reap_allocation_owners()?;
    ensure!(
        account.snapshot()?.charged_bytes == 0,
        "constraint physical payload still retained"
    );
    Ok(())
}
