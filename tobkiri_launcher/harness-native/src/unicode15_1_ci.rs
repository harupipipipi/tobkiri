//! Independent CPython/UCD comparison, not an actual Setup invocation receipt.
use super::*;
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn points(value: &Value) -> Result<Vec<u32>> {
    let rows = value.as_array().context("code-point array required")?;
    ensure!(
        rows.len() <= 16384,
        "reference input exceeds fixed CI bound"
    );
    rows.iter()
        .map(|row| {
            let cp = row.as_u64().context("unsigned code point required")?;
            ensure!(cp <= 0x10ffff);
            Ok(cp as u32)
        })
        .collect()
}

fn normalized(input: &[u32], work: &mut Meter) -> Result<Vec<u32>> {
    let count = nfc_scratch_len(input.len()).context("scratch bound overflow")?;
    let mut scratch = vec![0; count];
    Ok(normalize_nfc(input, &mut scratch, work)
        .map_err(|error| anyhow::anyhow!("NFC primitive refused: {error:?}"))?
        .to_vec())
}

fn feed_sequence(hash: &mut Sha256, key: u32, output: &[u32]) {
    hash.update(key.to_le_bytes());
    hash.update((output.len() as u32).to_le_bytes());
    for &cp in output {
        hash.update(cp.to_le_bytes());
    }
}

#[test]
#[ignore = "requires fresh pinned CPython and Unicode normalization observations"]
fn original_reference_driver() -> Result<()> {
    let path = PathBuf::from(
        std::env::var_os("TOBKIRI_UNICODE15_1_FIXTURES")
            .context("fresh Unicode reference required")?,
    );
    let bytes = std::fs::read(&path)?;
    ensure!(bytes.len() <= 16 * 1024 * 1024);
    let fixture: Value = serde_json::from_slice(&bytes)?;
    ensure!(fixture["format"] == "io.tobkiri.unicode15-1-reference.v1");
    ensure!(fixture["python"] == crate::unicode15_1_data::PYTHON_VERSION);
    ensure!(fixture["unicode"] == crate::unicode15_1_data::UNICODE_VERSION);
    ensure!(fixture["scalar_count"] == 0x110000 && fixture["repr_block_size"] == 128);
    let mut work = Meter::default();
    let rows = fixture["normalization"]
        .as_array()
        .context("official corpus required")?;
    ensure!(
        rows.len()
            == fixture["normalization_cases"]
                .as_u64()
                .context("corpus count required")? as usize
    );
    ensure!(rows.len() > 19000);
    for row in rows {
        let input = points(&row["input"])?;
        let expected = points(&row["nfc"])?;
        let actual = normalized(&input, &mut work)?;
        ensure!(
            actual == expected,
            "official NFC mismatch: input={input:?} expected={expected:?} actual={actual:?}"
        );
        let mut scratch = vec![0; nfc_scratch_len(input.len()).context("scratch overflow")?];
        let same = is_nfc(&input, &mut scratch, &mut work)
            .map_err(|error| anyhow::anyhow!("NFC equality refused: {error:?}"))?;
        ensure!(same == (input == expected));
    }
    let budget = Arc::new(Budget::default());
    let erased: Arc<dyn AllocationBudget> = budget.clone();
    let contexts = fixture["contexts"]
        .as_array()
        .context("context corpus required")?;
    ensure!(contexts.len() == 32);
    for row in contexts {
        let input = points(&row["input"])?;
        ensure!(normalized(&input, &mut work)? == points(&row["nfc"])?);
        let changed = lower_changes(&input, &mut work)
            .map_err(|error| anyhow::anyhow!("lower primitive refused: {error:?}"))?;
        ensure!(Some(changed) == row["lower_changed"].as_bool());
        let mut builder = CoreBuffer::builder(&erased, 4096);
        write_python_repr(&input, &mut builder, &mut work)
            .map_err(|error| anyhow::anyhow!("repr primitive refused: {error:?}"))?;
        let actual = builder.finish()?;
        ensure!(Some(actual.as_str()?) == row["repr"].as_str());
        drop(actual);
        ensure!(budget.bytes.load(AtomicOrdering::SeqCst) == 0);
    }

    let mut nfc_hash = Sha256::new();
    let mut lower_hash = Sha256::new();
    let mut scratch = vec![0; nfc_scratch_len(1).context("single scratch overflow")?];
    for cp in 0..0x110000_u32 {
        let input = [cp];
        let output = normalize_nfc(&input, &mut scratch, &mut work)
            .map_err(|error| anyhow::anyhow!("single NFC refused at {cp:x}: {error:?}"))?;
        feed_sequence(&mut nfc_hash, cp, output);
        let changed = lower_changes(&input, &mut work)
            .map_err(|error| anyhow::anyhow!("single lower refused at {cp:x}: {error:?}"))?;
        lower_hash.update(cp.to_le_bytes());
        lower_hash.update([u8::from(changed)]);
    }
    let mut repr_hash = Sha256::new();
    let mut block = [0_u32; 128];
    for start in (0..0x110000_u32).step_by(block.len()) {
        for (offset, cp) in block.iter_mut().enumerate() {
            *cp = start + offset as u32;
        }
        let mut builder = CoreBuffer::builder(&erased, 4096);
        write_python_repr(&block, &mut builder, &mut work)
            .map_err(|error| anyhow::anyhow!("block repr refused at {start:x}: {error:?}"))?;
        let output = builder.finish()?;
        repr_hash.update(start.to_le_bytes());
        repr_hash.update((output.as_bytes().len() as u32).to_le_bytes());
        repr_hash.update(output.as_bytes());
        drop(output);
        ensure!(budget.bytes.load(AtomicOrdering::SeqCst) == 0);
    }
    let nfc_digest = format!("{:x}", nfc_hash.finalize());
    let lower_digest = format!("{:x}", lower_hash.finalize());
    let repr_digest = format!("{:x}", repr_hash.finalize());
    let matched = fixture["scalar_nfc_sha256"] == nfc_digest
        && fixture["scalar_lower_changed_sha256"] == lower_digest
        && fixture["all_codepoint_repr_blocks_sha256"] == repr_digest;
    let observation = json!({
        "format":"io.tobkiri.unicode15-1-native-observations.v1",
        "native_comparison":true,"matched":matched,"qualification":false,
        "production_enabled":false,"primitive_only":true,
        "normalization_cases":rows.len(),"context_cases":contexts.len(),
        "scalar_count":0x110000,"repr_block_size":128,
        "scalar_nfc_sha256":nfc_digest,"scalar_lower_changed_sha256":lower_digest,
        "all_codepoint_repr_blocks_sha256":repr_digest,
        "work_units":u64::MAX-work.remaining,"charged_repr_bytes_after_drop":0,
    });
    let output = serde_json::to_vec_pretty(&observation)?;
    std::fs::write(path.with_file_name("native-observations.json"), &output)?;
    ensure!(matched, "full-domain Unicode primitive digest mismatch");
    println!(
        "UNICODE15_1_NATIVE_COMPARISON cases={} contexts={} codepoints=1114112 matched=true",
        rows.len(),
        contexts.len()
    );
    Ok(())
}
