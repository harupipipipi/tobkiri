//! Dynamic canonical ABI byte lists keep their work and physical custody while
//! admitting scalar traversal once, including after reentrant realloc work.
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use wasmtime::component::{
    BudgetedVec, Component, HostAllocationBudget, HostAllocationError, HostAllocationKind,
    HostAllocationOwner, HostAllocationRequest, Linker, Val,
};
use wasmtime::{Engine, ResourceLimiter, Store};

#[derive(Default)]
struct Meter {
    live: AtomicU64,
    deny_scalars: AtomicBool,
    requests: Mutex<Vec<HostAllocationRequest>>,
}
impl HostAllocationBudget for Meter {
    fn reserve(&self, request: HostAllocationRequest) -> Result<(), HostAllocationError> {
        self.requests.lock().unwrap().push(request);
        if self.deny_scalars.load(Ordering::Acquire)
            && request.kind == HostAllocationKind::Temporary
            && request.bytes == 0
            && request.elements > 1
        {
            return Err(HostAllocationError::Limit);
        }
        self.live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_add(request.bytes)
                    .filter(|next| *next <= 4 * 1024 * 1024)
            })
            .map(|_| ())
            .map_err(|_| HostAllocationError::Limit)
    }
    fn release(&self, bytes: u64) {
        self.live
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                live.checked_sub(bytes)
            })
            .expect("release only retired physical storage");
    }
}
struct State {
    meter: Arc<Meter>,
    deny_after_growth: bool,
    refuse_growth: bool,
    growths: usize,
}
impl ResourceLimiter for State {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        _: Option<usize>,
    ) -> wasmtime::Result<bool> {
        if current > 0 {
            self.growths += 1;
            if self.deny_after_growth {
                self.meter.deny_scalars.store(true, Ordering::Release);
            }
            if self.refuse_growth {
                return Err(wasmtime::Error::msg("fixture memory growth denied"));
            }
        }
        Ok(desired <= 2 * 65536)
    }
    fn table_growing(
        &mut self,
        _: usize,
        desired: usize,
        _: Option<usize>,
    ) -> wasmtime::Result<bool> {
        Ok(desired <= 16)
    }
}

fn fixture(
    element: &str,
    tuples: usize,
    realloc: &str,
    echo: &str,
) -> (Store<State>, wasmtime::component::Func, Arc<Meter>) {
    let mut types = format!("(type $t0 (list {element}))");
    for index in 1..=tuples {
        types.push_str(&format!("(type $t{index} (tuple $t{}))", index - 1));
    }
    let text = format!(
        r#"(component
        {types}
        (core module $m
            (memory (export "memory") 1 2)
            (func (export "realloc") (param i32 i32 i32 i32) (result i32) {realloc})
            (func (export "echo") (param $ptr i32) (param $len i32) (result i32) {echo}))
        (core instance $i (instantiate $m))
        (func (export "echo") (param "bytes" $t{tuples}) (result $t{tuples})
            (canon lift (core func $i "echo")
                (memory $i "memory") (realloc (func $i "realloc")))))"#
    );
    let engine = Engine::default();
    let component = Component::new(&engine, wat::parse_str(text).unwrap()).unwrap();
    let meter = Arc::new(Meter::default());
    let mut store = Store::new(
        &engine,
        State {
            meter: meter.clone(),
            deny_after_growth: false,
            refuse_growth: false,
            growths: 0,
        },
    );
    store.host_allocation_budget(meter.clone());
    store.limiter(|state| state);
    let instance = Linker::<State>::new(&engine)
        .instantiate(&mut store, &component)
        .unwrap();
    let function = instance.get_func(&mut store, "echo").unwrap();
    (store, function, meter)
}
const ECHO: &str =
    "i32.const 0 local.get $ptr i32.store i32.const 4 local.get $len i32.store i32.const 0";

fn input(count: usize, element: &str, tuples: usize) -> Val {
    let owner: HostAllocationOwner = Arc::new(Meter::default());
    let mut value = Val::List(
        BudgetedVec::from_iter(
            (0..count).map(|index| match element {
                "u8" => Val::U8((index % 256) as u8),
                "u16" => Val::U16((index % 65536) as u16),
                _ => unreachable!(),
            }),
            &owner,
            0,
        )
        .unwrap(),
    );
    for _ in 0..tuples {
        value = Val::Tuple(BudgetedVec::from_iter([value].into_iter(), &owner, 0).unwrap());
    }
    value
}
fn scalar(request: &&HostAllocationRequest) -> bool {
    request.kind == HostAllocationKind::Temporary && request.bytes == 0 && request.depth == 2
}

#[test]
fn byte_list_abi_preserves_values_total_work_constant_admissions_and_alias_custody() {
    let (mut store, function, meter) = fixture("u8", 0, "i32.const 1024", ECHO);
    drop(
        function
            .call_owned(&mut store, &[input(0, "u8", 0)])
            .unwrap(),
    );
    let mut prior: Option<(usize, usize, u64)> = None;
    for count in [0, 1, 1024, 6144] {
        let value = input(count, "u8", 0);
        meter.requests.lock().unwrap().clear();
        let result = function.call_owned(&mut store, &[value.clone()]).unwrap();
        assert_eq!(&result.as_slice()[0], &value);
        let requests = meter.requests.lock().unwrap().clone();
        let work = requests
            .iter()
            .map(|request| request.work.max(request.elements))
            .sum::<u64>();
        let entries = requests.iter().filter(scalar).collect::<Vec<_>>();
        assert_eq!(entries.len(), if count == 0 { 0 } else { 2 });
        assert!(
            entries
                .iter()
                .all(|request| request.work == count as u64 && request.elements == count as u64)
        );
        if count > 0 {
            if let Some((previous_count, admissions, previous_work)) = prior {
                assert_eq!(requests.len(), admissions);
                // Existing lower realloc + lower scalar + lift storage + lift
                // scalar work all remain charged; batching does not erase work.
                assert_eq!(work - previous_work, 4 * (count - previous_count) as u64);
            }
            prior = Some((count, requests.len(), work));
        }
        drop(result);
    }
    let result = function
        .call_owned(&mut store, &[input(6144, "u8", 0)])
        .unwrap();
    let alias = result.clone();
    drop(result);
    drop(store);
    assert!(meter.live.load(Ordering::Acquire) >= 6144 * std::mem::size_of::<Val>() as u64);
    drop(alias);
    assert_eq!(meter.live.load(Ordering::Acquire), 0);
}

#[test]
fn byte_list_abi_non_u8_path_keeps_each_scalar_admission() {
    let (mut store, function, meter) = fixture("u16", 0, "i32.const 1024", ECHO);
    let value = input(64, "u16", 0);
    let result = function.call_owned(&mut store, &[value.clone()]).unwrap();
    assert_eq!(&result.as_slice()[0], &value);
    let requests = meter.requests.lock().unwrap().clone();
    let entries = requests.iter().filter(scalar).collect::<Vec<_>>();
    assert_eq!(entries.len(), 128);
    assert!(
        entries
            .iter()
            .all(|request| request.work == 1 && request.elements == 1)
    );
    drop((result, store));
    assert_eq!(meter.live.load(Ordering::Acquire), 0);
}

#[test]
fn byte_list_abi_rechecks_scalar_admission_after_realloc_and_keeps_memory_limiter() {
    for refuse_growth in [false, true] {
        let (mut store, function, meter) =
            fixture("u8", 0, "i32.const 1 memory.grow drop i32.const 1024", ECHO);
        store.data_mut().deny_after_growth = true;
        store.data_mut().refuse_growth = refuse_growth;
        let error = function
            .call_owned(&mut store, &[input(64, "u8", 0)])
            .unwrap_err();
        assert_eq!(store.data().growths, 1);
        let requests = meter.requests.lock().unwrap().clone();
        let entries = requests.iter().filter(scalar).collect::<Vec<_>>();
        if refuse_growth {
            assert!(format!("{error:#}").contains("fixture memory growth denied"));
            assert!(
                entries.is_empty(),
                "failed realloc must not reach scalar traversal"
            );
        } else {
            assert_eq!(
                error.downcast_ref::<HostAllocationError>(),
                Some(&HostAllocationError::Limit)
            );
            assert_eq!(
                entries.len(),
                1,
                "new denial must stop before result lifting"
            );
            assert_eq!(entries[0].work, 64);
        }
        drop(store);
        assert_eq!(meter.live.load(Ordering::Acquire), 0);
    }
}

#[test]
fn byte_list_abi_preserves_child_depth_limit_including_empty_lists() {
    for (tuples, count, succeeds) in [(62, 1, true), (63, 0, true), (63, 1, false)] {
        let (mut store, function, meter) = fixture("u8", tuples, "i32.const 1024", ECHO);
        let value = input(count, "u8", tuples);
        let result = function.call_owned(&mut store, &[value.clone()]);
        if succeeds {
            assert_eq!(&result.as_ref().unwrap().as_slice()[0], &value);
        } else {
            assert_eq!(
                result
                    .as_ref()
                    .unwrap_err()
                    .downcast_ref::<HostAllocationError>(),
                Some(&HostAllocationError::Depth)
            );
        }
        drop((result, store));
        assert_eq!(meter.live.load(Ordering::Acquire), 0);
    }
}

#[test]
fn byte_list_abi_keeps_bounds_fuel_and_scalar_type_validation() {
    for (realloc, echo, fuel, invalid_type, message) in [
        (
            "i32.const 65535",
            ECHO,
            usize::MAX,
            false,
            "realloc return: beyond end of memory",
        ),
        (
            "i32.const 1024",
            "i32.const 0 i32.const 65535 i32.store i32.const 4 i32.const 2 i32.store i32.const 0",
            usize::MAX,
            false,
            "list pointer/length out of bounds",
        ),
        (
            "i32.const 1024",
            ECHO,
            0,
            false,
            "fuel allocated for hostcalls has been exhausted",
        ),
        ("i32.const 1024", ECHO, usize::MAX, true, "type mismatch"),
    ] {
        let (mut store, function, meter) = fixture("u8", 0, realloc, echo);
        store.set_hostcall_fuel(fuel);
        let value = input(2, if invalid_type { "u16" } else { "u8" }, 0);
        let error = function.call_owned(&mut store, &[value]).unwrap_err();
        assert!(format!("{error:#}").contains(message), "{error:#}");
        drop(store);
        assert_eq!(meter.live.load(Ordering::Acquire), 0);
    }
}
