//! Development golden cases use the authoritative Rust declared-width formatter.
fn main() {
    let mut cases=Vec::new();
    let mut bits=0x9e3779b97f4a7c15u64;
    for _ in 0..512 {
        bits=bits.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let value=f64::from_bits(bits);
        if value.is_finite(){cases.push(serde_json::json!({"width":"f64","bits":format!("{bits:016x}"),"expect":tobkiri_guest_sdk::canon_f64(value).unwrap()}));}
        let narrowed=bits as u32;let value=f32::from_bits(narrowed);
        if value.is_finite(){cases.push(serde_json::json!({"width":"f32","bits":format!("{narrowed:08x}"),"expect":tobkiri_guest_sdk::canon_f32(value).unwrap()}));}
    }
    println!("{}",serde_json::to_string_pretty(&cases).unwrap());
}
