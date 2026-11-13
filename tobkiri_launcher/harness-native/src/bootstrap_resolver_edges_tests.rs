use super::*;
use std::io::Read;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Default)]
struct Meter {
    live: AtomicU64,
    work: AtomicU64,
}
impl AllocationBudget for Meter {
    fn reserve(
        &self,
        request: AllocationRequest,
    ) -> std::result::Result<(), tobkiri_canon::AllocationError> {
        self.work
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value
                    .checked_add(request.work.max(request.elements))
                    .filter(|value| *value <= 4 * 1024 * 1024)
            })
            .map_err(|_| tobkiri_canon::AllocationError::Limit)?;
        self.live
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                value
                    .checked_add(request.bytes)
                    .filter(|value| *value <= 1024 * 1024)
            })
            .map_err(|_| tobkiri_canon::AllocationError::Limit)?;
        Ok(())
    }
    fn release(&self, bytes: u64) {
        assert!(self.live.fetch_sub(bytes, Ordering::Relaxed) >= bytes);
    }
}

#[test]
fn original_static_edge_bindings_keep_python_order_duplicates_and_canonical_bytes() -> Result<()> {
    let meter = Arc::new(Meter::default());
    let budget: Arc<dyn AllocationBudget> = meter.clone();
    let snapshot =
        Digest::parse("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")?;
    // Frozen Python json.dumps + SHA-256 results for the source preimage;
    // joined duplicate keys keep their first insertion positions.
    let input = CoreValue::parse_guest(
        br#"[
        ["z","p","c.v1","op"],
        ["a","p","c.v1","op"],
        ["z","p","c.v1","op"],
        ["q\"\\\b\f\n\r\t\u00e9","p","c.v1","op"],
        ["x|","p","y","z"],
        ["x","|p","y","z"]
    ]"#,
        &budget,
    )?;
    let actual = derive_bindings(&input, &snapshot, &budget)?;
    let expected = CoreValue::parse_guest(br#"[
        ["z|p|c.v1|op","authority-ref:13f1bbf010df2ed08582e4d6639a77a1c2cdf00451a4d8eeb925483982860ecc"],
        ["a|p|c.v1|op","authority-ref:a545a772a2a7651f96c16b0b7616e27e87e434ed289d4a20b09db5ce1fe63584"],
        ["q\"\\\b\f\n\r\t\u00e9|p|c.v1|op","authority-ref:bdc193a689a6bcefe989ddfd7244e38f38bb398d78de0b3be53f1664aa92efee"],
        ["x||p|y|z","authority-ref:93ad4965b7d8130e68385f34a88c1918d215effd6396f5f98b015f42ed351a65"]
    ]"#, &budget)?;
    assert!(actual.eq_guest(&expected, &budget)?);
    drop((actual, expected, input));
    assert_eq!(meter.live.load(Ordering::Relaxed), 0);

    let invalid = CoreValue::parse_guest(
        br#"[["z","p","c.v1","op"],["invalid",2,"c.v1","op"]]"#,
        &budget,
    )?;
    let held = meter.live.load(Ordering::Relaxed);
    assert!(derive_bindings(&invalid, &snapshot, &budget).is_err());
    assert_eq!(meter.live.load(Ordering::Relaxed), held);
    drop(invalid);
    assert_eq!(meter.live.load(Ordering::Relaxed), 0);
    Ok(())
}

#[test]
#[ignore = "requires a fresh CI execution of the pinned original Setup"]
fn original_static_edge_reference_driver() -> Result<()> {
    anyhow::ensure!(std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true"));
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_ORIGINAL_STATIC_EDGES_REFERENCE")
            .context("missing pinned original Setup summary")?,
    );
    anyhow::ensure!(path.is_absolute() && path.canonicalize()? == path);
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    anyhow::ensure!(bytes.len() <= 1024 * 1024);
    let reference: serde_json::Value = serde_json::from_slice(&bytes)?;
    anyhow::ensure!(
        reference["format"] == "io.tobkiri.original-setup-resolver-prefix-reference.v1"
            && reference["original_head"] == "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
            && reference["case_count"] == 12
            && reference["native_comparison"] == false
            && reference["qualification"] == false
    );
    let cases = reference["cases"]
        .as_array()
        .context("original cases missing")?;
    let mut matched = cases
        .iter()
        .filter(|case| case["case"] == "source-profile-requested-bindings");
    let case = matched
        .next()
        .context("original requested edge case missing")?;
    anyhow::ensure!(
        matched.next().is_none() && case["expected"]["outcome"] == "static-edges-bound"
    );
    let facts = &case["facts"];
    let snapshot = Digest::parse(
        facts["authority_snapshot_digest"]
            .as_str()
            .context("original snapshot missing")?,
    )?;
    let observed = &facts["requested_bindings"];
    anyhow::ensure!(observed["source_edge_count"] == 310);
    let meter = Arc::new(Meter::default());
    let budget: Arc<dyn AllocationBudget> = meter.clone();
    let rows = CoreValue::parse_guest(
        &serde_json::to_vec(&observed["source_edge_fields"])?,
        &budget,
    )?;
    let bindings = derive_bindings(&rows, &snapshot, &budget)?;
    let expected =
        CoreValue::parse_guest(&serde_json::to_vec(&observed["ordered_bindings"])?, &budget)?;
    anyhow::ensure!(rows.array().context("original ordered rows missing")?.len() == 310);
    anyhow::ensure!(
        expected
            .array()
            .context("original ordered bindings missing")?
            .len()
            == 310
    );
    anyhow::ensure!(bindings.eq_guest(&expected, &budget)?);
    drop((rows, bindings, expected));
    anyhow::ensure!(meter.live.load(Ordering::Relaxed) == 0);
    Ok(())
}
