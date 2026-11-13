//! Sparse operation markers for the feature-gated cold-schema diagnostic.
use super::FuelBucket;
use pyo3::prelude::*;
use std::cell::Cell;

const BUCKETS: [&str; 4] = ["uri", "registry", "pointer", "anchor"];
const STRIDE: u32 = 257;
const MAX_PER_SCHEMA: u32 = 4;
const OFFSET: [u32; 4] = [0, 53, 107, 161];

pub(super) struct Sampler {
    observer: Option<Py<PyAny>>,
    counts: [Cell<u32>; BUCKETS.len()],
    first: [u32; BUCKETS.len()],
    next: [Cell<u32>; BUCKETS.len()],
}

impl Sampler {
    pub(super) fn new(observer: Option<(&Bound<'_, PyAny>, u32)>) -> Self {
        let (observer, seed) = observer.map_or((None, 0), |(value, seed)| {
            (Some(value.clone().unbind()), seed)
        });
        let first = OFFSET.map(|offset| (seed % STRIDE + offset) % STRIDE + 1);
        Self {
            observer,
            counts: Default::default(),
            first,
            next: first.map(Cell::new),
        }
    }

    pub(super) fn measure<T>(
        &self,
        py: Python<'_>,
        bucket: FuelBucket,
        operation: impl FnOnce() -> PyResult<T>,
    ) -> PyResult<T> {
        let Some(observer) = self.observer.as_ref() else {
            return operation();
        };
        let index = bucket as usize;
        let name = BUCKETS[index];
        let count = self.counts[index].get().saturating_add(1);
        self.counts[index].set(count);
        if count != self.next[index].get() {
            return operation();
        }
        let next = if count == self.first[index] + (MAX_PER_SCHEMA - 1) * STRIDE {
            0
        } else {
            count + STRIDE
        };
        self.next[index].set(next);
        Self::mark(py, observer, "sample-begin", name, count)?;
        let result = operation();
        Self::mark(py, observer, "sample-end", name, count)?;
        result
    }

    fn mark(
        py: Python<'_>,
        observer: &Py<PyAny>,
        phase: &'static str,
        bucket: &'static str,
        ordinal: u32,
    ) -> PyResult<()> {
        observer
            .bind(py)
            .call1((phase, bucket, ordinal))
            .map(|_| ())
    }

    pub(super) fn calibrate(&self, py: Python<'_>) -> PyResult<()> {
        if let Some(observer) = self.observer.as_ref() {
            Self::mark(py, observer, "sample-begin", "marker", 1)?;
            Self::mark(py, observer, "sample-end", "marker", 1)?;
        }
        Ok(())
    }

    pub(super) fn counts(&self, py: Python<'_>) -> PyResult<()> {
        if let Some(observer) = self.observer.as_ref() {
            for (index, bucket) in BUCKETS.iter().enumerate() {
                Self::mark(
                    py,
                    observer,
                    "sample-count",
                    bucket,
                    self.counts[index].get(),
                )?;
            }
        }
        Ok(())
    }
}
