//! Private implementation symbols, not a new WIT interface or Python API.
//! No Core activation bridge is installed by this patch. Runtime entries deny.
use super::bindings::wasi::cli0_2_0::environment;
use pyo3::Python;

unsafe extern "C" {
    fn _PyTobkiri_EntropyBeginBuild(seed: *const u8) -> i32;
    fn _PyTobkiri_EntropySeal() -> i32;
    fn _PyTobkiri_EntropyRequireActive() -> i32;
    fn _PyTobkiri_EntropyInvalidate();
    fn _PyTobkiri_EntropyIsBuild() -> i32;
}

pub fn begin_build() -> Result<(), String> {
    let values = environment::get_environment();
    let seed = values
        .iter()
        .find(|(key, _)| key == "TOBKIRI_PUBLIC_PREINIT_SEED")
        .ok_or("missing explicit public preinit seed")?
        .1
        .as_bytes();
    if seed.len() != 64
        || !seed
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
    {
        return Err("invalid public preinit seed".into());
    }
    let mut bytes = [0u8; 32];
    for (output, pair) in bytes.iter_mut().zip(seed.chunks_exact(2)) {
        *output = u8::from_str_radix(std::str::from_utf8(pair).map_err(|e| e.to_string())?, 16)
            .map_err(|e| e.to_string())?;
    }
    if unsafe { _PyTobkiri_EntropyBeginBuild(bytes.as_ptr()) } != 0 {
        return Err("preinit cannot reopen sealed entropy mode".into());
    }
    Ok(())
}

pub fn seal() -> Result<(), String> {
    if unsafe { _PyTobkiri_EntropySeal() } != 0 {
        return Err("unproved retained RNG state prevents snapshot admission".into());
    }
    Ok(())
}

pub fn require_active() {
    // The missing owner/epoch/Core callback bridge cannot silently activate.
    assert_eq!(
        unsafe { _PyTobkiri_EntropyRequireActive() },
        0,
        "Unavailable: private Core entropy activation bridge is not connected (ACT-01)"
    );
}

pub fn abort() {
    unsafe { _PyTobkiri_EntropyInvalidate() };
}

pub fn audited_stdlib() -> Result<Vec<String>, String> {
    let values = environment::get_environment();
    let value = values
        .iter()
        .find(|(key, _)| key == "TOBKIRI_PREINIT_STDLIB")
        .ok_or("missing audited preinit module declaration")?
        .1
        .as_str();
    if value.is_empty() {
        return Ok(Vec::new());
    }
    let modules = value.split(',').map(str::to_owned).collect::<Vec<_>>();
    if modules
        .iter()
        .any(|module| !["random", "secrets"].contains(&module.as_str()))
        || modules.windows(2).any(|pair| pair[0] >= pair[1])
    {
        return Err("invalid audited preinit module declaration".into());
    }
    Ok(modules)
}

/// Every original attach path, including lifting/drop/finalization, uses this
/// boundary. Build mode is only available in the original init invocation;
/// sealed runtime states must pass the private owner/epoch live check.
pub fn attach<F, R>(function: F) -> R
where
    F: for<'py> FnOnce(Python<'py>) -> R,
{
    require_scope();
    Python::attach(function)
}

pub fn require_scope() {
    if unsafe { _PyTobkiri_EntropyIsBuild() } != 1 {
        require_active();
    }
}
