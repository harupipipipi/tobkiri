//! Fixed, non-nested fuel intervals within an original App schema evaluation.
use anyhow::{Context, Result, ensure};
use serde::{Serialize, Serializer, ser::SerializeSeq};
use sha2::Digest as _;

const BUCKETS: [&str; 5] = ["descend", "keyword", "lookup", "scalar", "marker"];
const MEASURED_BUCKETS: usize = BUCKETS.len() - 1;
const MARKER: usize = MEASURED_BUCKETS;
const STRIDE: u32 = 257;
const PER_BUCKET: usize = 4;
const OFFSETS: [u32; MEASURED_BUCKETS] = [0, 53, 107, 161];
pub(super) const MAX_SAMPLES: usize = super::SCHEMAS * (MEASURED_BUCKETS * PER_BUCKET + 1);

#[derive(Clone, Copy)]
struct Point {
    schema: usize,
    bucket: usize,
    ordinal: u32,
    begin: u64,
}
fn first_ordinal(schema: &str, bucket: usize) -> u32 {
    let bytes = sha2::Sha256::digest(schema.as_bytes());
    let seed = u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]);
    (seed % STRIDE + OFFSETS[bucket]) % STRIDE + 1
}

#[derive(Clone, Copy)]
struct Pair {
    begin: Point,
    end: u64,
}
const EMPTY: Pair = Pair {
    begin: Point {
        schema: 0,
        bucket: 0,
        ordinal: 0,
        begin: 0,
    },
    end: 0,
};

pub(super) struct Samples {
    open: Option<Point>,
    rows: [Pair; MAX_SAMPLES],
    len: usize,
    last: [u32; BUCKETS.len()],
    next_count: usize,
    counts: [[Option<u32>; MEASURED_BUCKETS]; super::SCHEMAS],
}

impl Samples {
    pub(super) fn new() -> Self {
        Self {
            open: None,
            rows: [EMPTY; MAX_SAMPLES],
            len: 0,
            last: [0; BUCKETS.len()],
            next_count: 0,
            counts: [[None; MEASURED_BUCKETS]; super::SCHEMAS],
        }
    }

    pub(super) fn phase_start(&mut self) -> Result<()> {
        ensure!(
            self.open.is_none(),
            "new schema with an unfinished interval"
        );
        self.last = [0; BUCKETS.len()];
        self.next_count = 0;
        Ok(())
    }

    pub(super) fn count(&self) -> usize {
        self.len
    }

    pub(super) fn latest_fuel(&self) -> Option<u64> {
        self.open
            .map(|point| point.begin)
            .or_else(|| self.len.checked_sub(1).map(|last| self.rows[last].end))
    }

    pub(super) fn verify_return(&self) -> Result<()> {
        ensure!(
            self.open.is_none() && self.last[MARKER] == 1 && self.next_count == MEASURED_BUCKETS,
            "return missing sampled evaluation counters"
        );
        Ok(())
    }

    pub(super) fn record(
        &mut self,
        schema: usize,
        name: &str,
        event: &str,
        bucket: &str,
        ordinal: u32,
        fuel: u64,
    ) -> Result<()> {
        let bucket = BUCKETS
            .iter()
            .position(|known| *known == bucket)
            .context("unknown operation sample bucket")?;
        match event {
            "sample-begin" => {
                ensure!(
                    self.open.is_none() && self.len < MAX_SAMPLES,
                    "overlapping or excess operation sample"
                );
                if bucket == MARKER {
                    ensure!(
                        ordinal == 1 && self.last[bucket] == 0,
                        "invalid calibration"
                    );
                } else {
                    let first = first_ordinal(name, bucket);
                    let expected = if self.last[bucket] == 0 {
                        first
                    } else {
                        self.last[bucket] + STRIDE
                    };
                    ensure!(
                        self.last[MARKER] == 1
                            && self.next_count == 0
                            && ordinal == expected
                            && ordinal <= first + (PER_BUCKET as u32 - 1) * STRIDE,
                        "out-of-sequence operation sample"
                    );
                }
                self.open = Some(Point {
                    schema,
                    bucket,
                    ordinal,
                    begin: fuel,
                });
            }
            "sample-end" => {
                let begin = self.open.take().context("sample without a start")?;
                ensure!(
                    (begin.schema, begin.bucket, begin.ordinal) == (schema, bucket, ordinal)
                        && fuel <= begin.begin,
                    "mismatched operation sample pair"
                );
                self.rows[self.len] = Pair { begin, end: fuel };
                self.len += 1;
                self.last[bucket] = ordinal;
            }
            "sample-count" => {
                ensure!(
                    bucket < MEASURED_BUCKETS
                        && bucket == self.next_count
                        && self.open.is_none()
                        && self.last[MARKER] == 1,
                    "invalid completed operation count"
                );
                let first = first_ordinal(name, bucket);
                let expected_last = if ordinal >= first {
                    let step = ((ordinal - first) / STRIDE).min(PER_BUCKET as u32 - 1);
                    first + step * STRIDE
                } else {
                    0
                };
                ensure!(
                    expected_last == self.last[bucket],
                    "missing operation samples"
                );
                self.counts[schema][bucket] = Some(ordinal);
                self.next_count += 1;
            }
            _ => anyhow::bail!("unknown operation sample event"),
        }
        Ok(())
    }

    pub(super) fn unmatched<'a>(
        &self,
        names: &'a [super::SchemaName; super::SCHEMAS],
    ) -> Option<Row<'a>> {
        self.open.map(|point| Row {
            schema: names[point.schema].as_str(),
            bucket: BUCKETS[point.bucket],
            ordinal: point.ordinal,
            begin_fuel: point.begin,
            end_fuel: None,
            interval_fuel: None,
        })
    }
}

#[derive(Serialize)]
pub(super) struct Row<'a> {
    schema: &'a str,
    bucket: &'static str,
    ordinal: u32,
    begin_fuel: u64,
    end_fuel: Option<u64>,
    interval_fuel: Option<u64>,
}

pub(super) struct Rows<'a>(pub(super) &'a super::Trace);

impl Serialize for Rows<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let trace = self.0;
        let mut seq = serializer.serialize_seq(Some(trace.samples.len))?;
        for pair in &trace.samples.rows[..trace.samples.len] {
            let point = pair.begin;
            seq.serialize_element(&Row {
                schema: trace.names[point.schema].as_str(),
                bucket: BUCKETS[point.bucket],
                ordinal: point.ordinal,
                begin_fuel: point.begin,
                end_fuel: Some(pair.end),
                interval_fuel: Some(point.begin - pair.end),
            })?;
        }
        seq.end()
    }
}

pub(super) struct Counts<'a>(pub(super) &'a super::Trace);

impl Serialize for Counts<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        #[derive(Serialize)]
        struct Count<'a> {
            schema: &'a str,
            bucket: &'static str,
            operations: u32,
        }
        let trace = self.0;
        let mut seq = serializer.serialize_seq(None)?;
        for (schema, counts) in trace.samples.counts.iter().enumerate() {
            if !trace.rows[..trace.len]
                .iter()
                .flatten()
                .any(|row| row.schema == schema && row.phase == super::Phase::Return)
            {
                continue;
            }
            for (bucket, count) in counts.iter().enumerate() {
                if let Some(operations) = *count {
                    seq.serialize_element(&Count {
                        schema: trace.names[schema].as_str(),
                        bucket: BUCKETS[bucket],
                        operations,
                    })?;
                }
            }
        }
        seq.end()
    }
}

#[test]
fn operation_samples_require_ordered_pairs_and_all_completed_counts() -> Result<()> {
    let mut samples = Samples::new();
    let name = "fixture.schema.json";
    let first = first_ordinal(name, 0);
    assert!(
        samples
            .record(0, name, "sample-begin", "descend", first, 90)
            .is_err()
    );
    samples.record(0, name, "sample-begin", "marker", 1, 100)?;
    assert!(samples.verify_return().is_err());
    samples.record(0, name, "sample-end", "marker", 1, 95)?;
    assert!(
        samples
            .record(0, name, "sample-count", "keyword", 0, 90)
            .is_err()
    );
    assert!(
        samples
            .record(0, name, "sample-count", "descend", first, 90)
            .is_err()
    );
    samples.record(0, name, "sample-begin", "descend", first, 90)?;
    assert!(samples.phase_start().is_err());
    samples.record(0, name, "sample-end", "descend", first, 80)?;
    assert!(
        samples
            .record(0, name, "sample-begin", "descend", first, 75)
            .is_err()
    );
    for (index, bucket) in BUCKETS[..MEASURED_BUCKETS].iter().enumerate() {
        let count = if index == 0 { first } else { 0 };
        samples.record(0, name, "sample-count", bucket, count, 75)?;
    }
    samples.verify_return()?;
    assert_eq!(samples.count(), 2);
    samples.phase_start()?;
    samples.record(1, name, "sample-begin", "marker", 1, 70)?;
    assert!(samples.verify_return().is_err());
    assert_eq!(samples.latest_fuel(), Some(70));
    Ok(())
}
