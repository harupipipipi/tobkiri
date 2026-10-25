//! Shared aggregate Store limits; execution authority remains in the caller.
use std::sync::Arc;
use tobkiri_gate::GateError;
use wasmtime::ResourceLimiter;

// Component composition routinely creates more core instances than exported
// Pack instances. These are finite native structure bounds, independent of the
// approved aggregate guest memory/table-element budgets below. Keep the
// memory/table object bounds unchanged until a supported component needs more.
pub(crate) const MAX_CORE_INSTANCES: usize = 1024;
pub(crate) const MAX_MEMORIES: usize = 16;
pub(crate) const MAX_TABLES: usize = 16;

/// Physical occupancy only. Implementers retain their own real funding proof;
/// this interface supplies no Profile, Gate, frame or execution authority.
pub(crate) trait PhysicalCapacity: Send + Sync {
    fn memory_growth(&self, current: usize, desired: usize) -> Result<u64, GateError>;
    fn table_growth(&self, current: usize, desired: usize) -> Result<u64, GateError>;
    fn failed_growth(&self, bytes: u64) -> Result<(), GateError>;
}

// Limits apply to the sum of all guest memories/tables in a store.
pub(crate) struct AggregateLimits {
    pub(crate) memory_cap: usize,
    pub(crate) table_cap: usize,
    pub(crate) memory_used: usize,
    pub(crate) table_used: usize,
    pub(crate) last_memory_growth: usize,
    pub(crate) last_table_growth: usize,
    pub(crate) physical: Option<Arc<dyn PhysicalCapacity>>,
    pub(crate) last_physical_memory: u64,
    pub(crate) last_physical_table: u64,
}
impl ResourceLimiter for AggregateLimits {
    fn memory_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        let growth = desired.saturating_sub(current);
        if maximum.is_some_and(|max| desired > max)
            || self
                .memory_used
                .checked_add(growth)
                .is_none_or(|n| n > self.memory_cap)
        {
            return Err(wasmtime::Error::msg(
                "aggregate guest memory limit exceeded",
            ));
        }
        if let Some(physical) = &self.physical {
            self.last_physical_memory = physical
                .memory_growth(current, desired)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
        }
        self.memory_used += growth;
        self.last_memory_growth = growth;
        Ok(true)
    }
    fn memory_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        if let Some(physical) = &self.physical {
            physical
                .failed_growth(self.last_physical_memory)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
            self.last_physical_memory = 0;
        }
        self.memory_used = self.memory_used.saturating_sub(self.last_memory_growth);
        self.last_memory_growth = 0;
        Err(error)
    }
    fn table_growing(
        &mut self,
        current: usize,
        desired: usize,
        maximum: Option<usize>,
    ) -> wasmtime::Result<bool> {
        let growth = desired.saturating_sub(current);
        if maximum.is_some_and(|max| desired > max)
            || self
                .table_used
                .checked_add(growth)
                .is_none_or(|n| n > self.table_cap)
        {
            return Err(wasmtime::Error::msg("aggregate guest table limit exceeded"));
        }
        if let Some(physical) = &self.physical {
            self.last_physical_table = physical
                .table_growth(current, desired)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
        }
        self.table_used += growth;
        self.last_table_growth = growth;
        Ok(true)
    }
    fn table_grow_failed(&mut self, error: wasmtime::Error) -> wasmtime::Result<()> {
        if let Some(physical) = &self.physical {
            physical
                .failed_growth(self.last_physical_table)
                .map_err(|error| wasmtime::Error::msg(error.to_string()))?;
            self.last_physical_table = 0;
        }
        self.table_used = self.table_used.saturating_sub(self.last_table_growth);
        self.last_table_growth = 0;
        Err(error)
    }
    fn instances(&self) -> usize {
        MAX_CORE_INSTANCES
    }
    fn memories(&self) -> usize {
        MAX_MEMORIES
    }
    fn tables(&self) -> usize {
        MAX_TABLES
    }
}
