"""Emit the reviewed source patch from exact pinned upstream text, without builds."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def replace(text: str, old: str, new: str) -> str:
    """Require one exact original anchor; never fuzz-apply source changes."""
    if text.count(old) != 1:
        raise ValueError(f"source anchor is not unique: {old[:80]!r}")
    return text.replace(old, new, 1)


def insert_body(text: str, signature: str, addition: str) -> str:
    """Keep the original Argument Clinic signature and hash comment intact."""
    if text.count(signature) != 1:
        raise ValueError(f"function signature differs: {signature}")
    start = text.index(signature) + len(signature)
    end = text.index("{\n", start) + 2
    between = text[start:end]
    if "/*[clinic end generated code:" not in between or len(between) > 160:
        raise ValueError("original Argument Clinic boundary differs")
    return text[:end] + addition + text[end:]


def producer_lib(text: str) -> str:
    text = replace(text, "mod prelink;\n", "mod prelink;\npub mod tobkiri_preinit;\nmod captured_inputs;\nmod captured_wasi;\n")
    text = replace(text, "    table: ResourceTable,\n", "    table: ResourceTable,\n    captured: captured_wasi::CapturedAdapter,\n")
    text = replace(text, "    pub async fn generate(&self) -> Result<()> {\n",
                   "    pub async fn generate(&self) -> Result<()> {\n"
                   "        anyhow::bail!(\"Unavailable: explicit deterministic preinit declaration required (PLAN-01)\")\n    }\n\n"
                   "    /// Source-patched original initializer, never a second initializer.\n"
                   "    pub async fn generate_deterministic(&self, plan: &tobkiri_preinit::PreinitPlan) -> Result<()> {\n"
                   "        ensure!(self.stub_wasi && self.add_to_linker.is_none(), \"deterministic preinit requires closed original linker\");\n"
                   "        plan.verify_options(self)?;\n        plan.require_input_admission(self.python_path, self.wit_paths)?;\n"
                   "        self.generate_with_plan(Some(plan)).await\n    }\n\n"
                   "    async fn generate_with_plan(&self, plan: Option<&tobkiri_preinit::PreinitPlan>) -> Result<()> {\n")
    text = replace(text, "        let mut wasi = WasiCtxBuilder::new();\n",
                   "        let mut wasi = WasiCtxBuilder::new();\n"
                   "        let mut capture_mounts = Vec::new();\n")
    text = replace(text, '        let python_path = &self\n            .python_path\n            .iter()\n            .filter_map(|&s| Path::new(s).exists().then_some(s))\n            .collect::<Vec<_>>();',
                   '        let python_path = self.python_path;\n'
                   '        let scratch = plan.ok_or_else(|| anyhow!("PLAN-01 missing declaration"))?.scratch()?;')
    text = replace(text, "prelink::embedded_python_standard_library()?", "prelink::embedded_python_standard_library(scratch)?")
    text = replace(text, "prelink::embedded_helper_utils()?", "prelink::embedded_helper_utils(scratch)?")
    text = text.replace("tempfile::tempdir()?", "tempfile::tempdir_in(scratch)?")
    text = replace(text, '            .env("PYTHONHOME", "/python")\n            .preopened_dir(\n                embedded_python_standard_lib.path(),\n                "python",\n                DirPerms::all(),\n                FilePerms::all(),\n            )?\n            .preopened_dir(\n                embedded_helper_utils.path(),\n                "bundled",\n                DirPerms::all(),\n                FilePerms::all(),\n            )?;',
                   '            .env("PYTHONHOME", "/python");\n'
                   '        capture_mounts.push(captured_inputs::Mount { guest_path: "python".into(), source: embedded_python_standard_lib.path().to_owned() });\n'
                   '        capture_mounts.push(captured_inputs::Mount { guest_path: "bundled".into(), source: embedded_helper_utils.path().to_owned() });')
    text = replace(text, '            wasi.preopened_dir(path, index.to_string(), DirPerms::all(), FilePerms::all())?;',
                   '            capture_mounts.push(captured_inputs::Mount { guest_path: index.to_string(), source: Path::new(path).to_owned() });')
    text = replace(text, '                wasi.preopened_dir(world_dir.path(), mount, DirPerms::all(), FilePerms::all())?;',
                   '                capture_mounts.push(captured_inputs::Mount { guest_path: mount.to_owned(), source: world_dir.path().to_owned() });')
    text = replace(text, "        let table = ResourceTable::new();\n",
                   "        let plan = plan.ok_or_else(|| anyhow!(\"PLAN-01 missing declaration\"))?;\n"
                   "        let captured = captured_wasi::CapturedAdapter::new(plan.capture_inputs(&capture_mounts)?, scratch)?;\n"
                   "        plan.configure(&mut wasi)?;\n        let table = ResourceTable::new();\n")
    text = replace(text, "Ctx { wasi, table }", "Ctx { wasi, table, captured }")
    text = replace(text, "                    let pre = InitPre::new(linker.instantiate_pre(component)?)?;\n",
                   "                    captured_wasi::install(&mut linker)?;\n"
                   "                    let pre = InitPre::new(linker.instantiate_pre(component)?)?;\n")
    text = replace(text, "        DirPerms, FilePerms, WasiCtx, WasiCtxBuilder, WasiCtxView, WasiView,\n",
                   "        WasiCtx, WasiCtxBuilder, WasiCtxView, WasiView,\n")
    text = replace(text, "        config.wasm_component_model(true);\n",
                   "        config.parallel_compilation(false);\n        config.wasm_component_model(true);\n")
    text = replace(text, "                    for entry in fs::read_dir(path)? {\n                        replace(&entry?.path(), pattern, replacement)?;\n                    }",
                   "                    let mut entries = fs::read_dir(path)?.collect::<std::io::Result<Vec<_>>>()?;\n"
                   "                    entries.sort_by_key(|entry| entry.file_name());\n"
                   "                    for entry in entries {\n                        replace(&entry.path(), pattern, replacement)?;\n                    }")
    return text


def producer_command(text: str) -> str:
    text = replace(text, "    tokio::runtime::Runtime,\n", "")
    text = replace(text, "pub struct Componentize {\n", "pub struct Componentize {\n"
                   "    /// Explicit reviewed declaration; disables all ambient package discovery.\n"
                   "    #[arg(long)]\n    pub deterministic_preinit: Option<PathBuf>,\n\n")
    text = replace(text, "    for site_packages in find_site_packages()? {\n",
                   "    let plan = componentize.deterministic_preinit.as_deref()\n"
                   "        .map(crate::tobkiri_preinit::PreinitPlan::read).transpose()?;\n"
                   "    anyhow::ensure!(plan.is_some(), \"Unavailable: --deterministic-preinit required (PLAN-01)\");\n"
                   "    let site_packages = if plan.is_some() { Vec::new() } else { find_site_packages()? };\n"
                   "    for site_packages in site_packages {\n")
    text = replace(text, "    Runtime::new()?.block_on(\n",
                   "    tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {\n")
    text = replace(text, "        ComponentGenerator {\n", "        let generator = ComponentGenerator {\n")
    text = replace(text, "        }\n        .generate(),\n    )?;\n",
                   "        };\n        if let Some(plan) = plan.as_ref() { generator.generate_deterministic(plan).await }\n"
                   "        else { generator.generate().await }\n    })?;\n")
    return text


def python_api(text: str) -> str:
    text = replace(text, "    tokio::runtime::Runtime,\n", "")
    text = replace(text, "full_names, intersect_world))]",
                   "full_names, intersect_world, deterministic_preinit=None))]")
    text = replace(text, "    intersect_world: Option<&str>,\n",
                   "    intersect_world: Option<&str>,\n    deterministic_preinit: Option<PathBuf>,\n")
    text = replace(text, "        Runtime::new()?.block_on(\n",
                   "        let plan = deterministic_preinit.as_deref()\n"
                   "            .map(crate::tobkiri_preinit::PreinitPlan::read).transpose()?;\n"
                   "        anyhow::ensure!(plan.is_some(), \"Unavailable: deterministic_preinit required (PLAN-01)\");\n"
                   "        tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {\n")
    text = replace(text, "            ComponentGenerator {\n", "            let generator = ComponentGenerator {\n")
    text = replace(text, "            }\n            .generate(),\n        )\n",
                   "            };\n            if let Some(plan) = plan.as_ref() { generator.generate_deterministic(plan).await }\n"
                   "            else { generator.generate().await }\n        })\n")
    return text


def runtime(text: str) -> str:
    text = replace(text, "mod bindings {\n", "mod tobkiri_preinit;\nmod bindings {\n")
    text = replace(text, "static SEED: OnceCell<Py<PyAny>> = OnceCell::new();\n", "")
    text = replace(text, "    Python::initialize();\n", "    tobkiri_preinit::begin_build()?;\n    Python::initialize();\n")
    text = replace(text, "    let init = |py: Python| {\n        let app = py.import(app_name.as_str())?;\n",
                   "    let init = |py: Python| {\n"
                   "        let flags = py.import(\"sys\")?.getattr(\"flags\")?;\n"
                   "        if flags.getattr(\"hash_randomization\")?.extract::<u32>()? != 0 ||\n"
                   "           flags.getattr(\"dont_write_bytecode\")?.extract::<u32>()? != 1 {\n"
                   "            return Err(anyhow::anyhow!(\"guest preinit hash/bytecode flags mismatch\"));\n"
                   "        }\n"
                   "        for module in tobkiri_preinit::audited_stdlib().map_err(anyhow::Error::msg)? { py.import(module.as_str())?; }\n"
                   "        let app = py.import(app_name.as_str())?;\n")
    text = replace(text, "        SEED.set(py.import(\"random\")?.getattr(\"seed\")?.into())\n            .unwrap();\n", "")
    text = replace(text, "        let result = do_init(app_name, symbols, stub_wasi);\n",
                   "        let result = do_init(app_name, symbols, stub_wasi);\n"
                   "        let result = result.and_then(|()| tobkiri_preinit::seal());\n"
                   "        if result.is_err() { tobkiri_preinit::abort(); }\n")
    text = replace(text, "    fn export_call_(func: ExportFunction, cx: &mut MyCall<'_>, async_: bool) -> u32 {\n",
                   "    fn export_call_(func: ExportFunction, cx: &mut MyCall<'_>, async_: bool) -> u32 {\n"
                   "        tobkiri_preinit::require_active();\n")
    text = replace(text, "    fn export_start<'a>(_: Wit, _: ExportFunction) -> Box<MyCall<'a>> {\n",
                   "    fn export_start<'a>(_: Wit, _: ExportFunction) -> Box<MyCall<'a>> {\n"
                   "        tobkiri_preinit::require_active();\n")
    text = replace(text, "    fn export_async_callback(event0: u32, event1: u32, event2: u32) -> u32 {\n",
                   "    fn export_async_callback(event0: u32, event1: u32, event2: u32) -> u32 {\n"
                   "        tobkiri_preinit::require_active();\n")
    text = replace(text, "                    // Call `random.seed()` to ensure we get a fresh seed rather\n"
                   "                    // than the one that got baked in during pre-init.\n"
                   "                    SEED.get().unwrap().call0(py).unwrap();\n", "")
    # Frozen Pack runtime never inherits host environment or argv. Legacy
    # upstream branch is removed, including the now-unused STUB_WASI static.
    start = text.index("            if !*STUB_WASI.get().unwrap() {\n")
    end = text.index("\n            if async_ {", start)
    text = text[:start] + text[end:]
    text = replace(text, "static STUB_WASI: OnceCell<bool> = OnceCell::new();\n", "")
    text = replace(text, "        STUB_WASI.set(stub_wasi).unwrap();\n", "")
    text = replace(text, "fn do_init(app_name: String, symbols: Symbols, stub_wasi: bool) -> Result<(), String> {",
                   "fn do_init(app_name: String, symbols: Symbols, _stub_wasi: bool) -> Result<(), String> {")
    text = text.replace("        wasi::cli0_2_0::environment,\n", "")
    text = replace(text, "        sync::Once,\n", "")
    text = text.replace("Python::attach(", "tobkiri_preinit::attach(")
    # PyO3 entry points already hold Python's lock and therefore need a scope
    # check of their own (not just the explicit attach call sites). This covers
    # saved drop/finalizer/import helpers and async support functions as well.
    matches = list(re.finditer(r"#\[pyo3::pyfunction\]\n(?:[ \t]*#\[[^\n]*\]\n)*[ \t]*(?:pub )?fn \w+", text))
    if len(matches) != text.count("#[pyo3::pyfunction]"):
        raise ValueError("an original PyO3 entry point was not scope-guarded")
    for match in reversed(matches):
        body = text.index("{", match.end()) + 1
        text = text[:body] + "\n    crate::tobkiri_preinit::require_scope();" + text[body:]
    return text


RNG_REGISTRY = r'''
#if defined(__wasi__)
static RandomObject *tobkiri_rng_head;
enum { RNG_UNKNOWN, RNG_IMPLICIT_BUILD, RNG_IMPLICIT_LIVE, RNG_EXPLICIT_LIVE, RNG_STATELESS };
static int random_seed_urandom(RandomObject *self);
static int tobkiri_rng_audit(void)
{
    for (RandomObject *rng = tobkiri_rng_head; rng; rng = rng->tobkiri_next) {
        if (rng->tobkiri_provenance != RNG_IMPLICIT_BUILD &&
            rng->tobkiri_provenance != RNG_STATELESS) return -1;
    }
    return 0;
}
static int tobkiri_rng_reseed(void)
{
    for (RandomObject *rng = tobkiri_rng_head; rng; rng = rng->tobkiri_next) {
        if (rng->tobkiri_provenance == RNG_STATELESS) continue;
        if (rng->tobkiri_provenance != RNG_IMPLICIT_BUILD) return -1;
        if (random_seed_urandom(rng) < 0) return -1;
    }
    return 0;
}
static PyObject *tobkiri_mark_system_random(RandomObject *self, PyObject *unused)
{
    (void)unused;
    if (!_PyTobkiri_EntropyIsBuild() && _PyTobkiri_EntropyRequireActive() != 0) {
        PyErr_SetString(PyExc_RuntimeError, "stateless RNG outside active Core epoch");
        return NULL;
    }
    if (self->tobkiri_provenance != RNG_UNKNOWN &&
        self->tobkiri_provenance != RNG_STATELESS) {
        PyErr_SetString(PyExc_RuntimeError, "cannot relabel existing RNG state");
        return NULL;
    }
    memset(self->state, 0, sizeof(self->state));
    self->index = 0;
    self->tobkiri_provenance = RNG_STATELESS;
    Py_RETURN_NONE;
}
static PyObject *tobkiri_random_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    PyObject *object = PyType_GenericNew(type, args, kwds);
    if (object == NULL) return NULL;
    RandomObject *self = (RandomObject *)object;
    self->tobkiri_provenance = RNG_UNKNOWN;
    self->tobkiri_previous = NULL;
    self->tobkiri_next = tobkiri_rng_head;
    if (tobkiri_rng_head) tobkiri_rng_head->tobkiri_previous = self;
    tobkiri_rng_head = self;
    return object;
}
static void tobkiri_random_dealloc(PyObject *object)
{
    RandomObject *self = (RandomObject *)object;
    if (self->tobkiri_previous) self->tobkiri_previous->tobkiri_next = self->tobkiri_next;
    else tobkiri_rng_head = self->tobkiri_next;
    if (self->tobkiri_next) self->tobkiri_next->tobkiri_previous = self->tobkiri_previous;
    PyTypeObject *type = Py_TYPE(object);
    type->tp_free(object);
    Py_DECREF(type);
}
#endif
'''


def random_module(text: str) -> str:
    text = replace(text, '#include "Python.h"\n', '#include "Python.h"\n#if defined(__wasi__)\n#include "pycore_tobkiri_entropy.h"\n#endif\n')
    text = replace(text, "typedef struct {\n    PyObject_HEAD\n    int index;\n    uint32_t state[N];\n} RandomObject;",
                   "typedef struct RandomObject {\n    PyObject_HEAD\n    int index;\n    uint32_t state[N];\n"
                   "#if defined(__wasi__)\n    struct RandomObject *tobkiri_previous, *tobkiri_next;\n"
                   "    int tobkiri_provenance;\n#endif\n} RandomObject;\n" + RNG_REGISTRY)
    for signature in ("random_seed_urandom(RandomObject *self)", "random_seed(RandomObject *self, PyObject *arg)"):
        text = replace(text, signature + "\n{\n", signature + "\n{\n"
                       "#if defined(__wasi__)\n"
                       "    if (self->tobkiri_provenance == RNG_STATELESS) {\n"
                       "        PyErr_SetString(PyExc_RuntimeError, \"stateless RNG cannot be reseeded or retagged\"); return -1;\n    }\n"
                       "#endif\n")
    text = replace(text, "    if (_PyOS_URandomNonblock(key, sizeof(key)) < 0) {\n        return -1;\n    }\n",
                   "#if defined(__wasi__)\n    if (_PyTobkiri_EntropySeedEnter() < 0) return -1;\n#endif\n"
                   "    int entropy_result = _PyOS_URandomNonblock(key, sizeof(key));\n"
                   "#if defined(__wasi__)\n    _PyTobkiri_EntropySeedLeave();\n#endif\n"
                   "    if (entropy_result < 0) return -1;\n")
    text = replace(text, "    if (entropy_result < 0) return -1;\n    init_by_array(self, key, Py_ARRAY_LENGTH(key));\n    return 0;\n",
                   "    if (entropy_result < 0) return -1;\n    init_by_array(self, key, Py_ARRAY_LENGTH(key));\n"
                   "#if defined(__wasi__)\n    self->tobkiri_provenance = _PyTobkiri_EntropyIsBuild() ? RNG_IMPLICIT_BUILD : RNG_IMPLICIT_LIVE;\n#endif\n"
                   "    memset(key, 0, sizeof(key));\n    return 0;\n")
    text = replace(text, "static int\nrandom_seed_time_pid(RandomObject *self)",
                   "#if !defined(__wasi__)\nstatic int\nrandom_seed_time_pid(RandomObject *self)")
    text = replace(text, "static int\nrandom_seed(RandomObject *self, PyObject *arg)",
                   "#endif\n\nstatic int\nrandom_seed(RandomObject *self, PyObject *arg)")
    old = """       if (random_seed_urandom(self) < 0) {
            PyErr_Clear();

            /* Reading system entropy failed, fall back on the worst entropy:
               use the current time and process identifier. */
            if (random_seed_time_pid(self) < 0) {
                return -1;
            }
        }
        return 0;
"""
    new = """#if defined(__wasi__)
        /* Denial never falls back to time, PID, public seed or ambient WASI. */
        return random_seed_urandom(self);
#else
""" + old + "#endif\n"
    text = replace(text, old, new)
    text = replace(text, "    /* This algorithm relies on the number being unsigned.\n",
                   "#if defined(__wasi__)\n"
                   "    if (_PyTobkiri_EntropyIsBuild()) {\n"
                   "        PyErr_SetString(PyExc_RuntimeError, \"Unavailable: preinit explicit seed provenance (SEED-01)\");\n"
                   "        return -1;\n    }\n"
                   "    if (_PyTobkiri_EntropyRequireActive() != 0) {\n"
                   "        PyErr_SetString(PyExc_RuntimeError, \"explicit seed outside active Core epoch\");\n"
                   "        return -1;\n    }\n#endif\n\n"
                   "    /* This algorithm relies on the number being unsigned.\n")
    text = replace(text, "    result = 0;\n\nDone:\n", "    result = 0;\n#if defined(__wasi__)\n    self->tobkiri_provenance = RNG_EXPLICIT_LIVE;\n#endif\n\nDone:\n")
    for signature in ("_random_Random_random_impl(RandomObject *self)",
                      "_random_Random_getstate_impl(RandomObject *self)",
                      "_random_Random_getrandbits_impl(RandomObject *self, uint64_t k)"):
        text = insert_body(text, signature, "#if defined(__wasi__)\n"
                       "    if (self->tobkiri_provenance == RNG_STATELESS) {\n"
                       "        PyErr_SetString(PyExc_RuntimeError, \"stateless RNG has no base C state\"); return NULL;\n    }\n"
                       "    if (_PyTobkiri_EntropyRequireRng(self->tobkiri_provenance != RNG_EXPLICIT_LIVE) < 0) return NULL;\n"
                       "#endif\n")
    text = insert_body(text, "_random_Random_setstate_impl(RandomObject *self, PyObject *state)", "#if defined(__wasi__)\n"
                   "    PyErr_SetString(PyExc_RuntimeError, \"Unavailable: unaudited restored RNG state (SEED-02)\");\n"
                   "    return NULL;\n#endif\n")
    text = replace(text, "    {Py_tp_new, PyType_GenericNew},\n",
                   "#if defined(__wasi__)\n    {Py_tp_new, tobkiri_random_new},\n    {Py_tp_dealloc, tobkiri_random_dealloc},\n"
                   "#else\n    {Py_tp_new, PyType_GenericNew},\n#endif\n")
    text = replace(text, "static PyMethodDef random_methods[] = {\n",
                   "static PyMethodDef random_methods[] = {\n#if defined(__wasi__)\n"
                   "    {\"_tobkiri_mark_system_random\", (PyCFunction)tobkiri_mark_system_random, METH_NOARGS, NULL},\n#endif\n")
    text = replace(text, "    _randomstate *state = get_random_state(module);\n\n    state->Random_Type = PyType_FromModuleAndSpec(\n",
                   "    _randomstate *state = get_random_state(module);\n"
                   "#if defined(__wasi__)\n"
                   "    if (_PyTobkiri_EntropySetReseed(tobkiri_rng_reseed) != 0 ||\n"
                   "        _PyTobkiri_EntropySetRngAudit(tobkiri_rng_audit) != 0) {\n"
                   "        PyErr_SetString(PyExc_RuntimeError, \"Unavailable: stdlib random must be declared and preinitialized (MODULE-01)\");\n"
                   "        return -1;\n    }\n#endif\n\n"
                   "    state->Random_Type = PyType_FromModuleAndSpec(\n")
    return text


def bootstrap(text: str) -> str:
    text = replace(text, '#include "Python.h"\n', '#include "Python.h"\n#if defined(__wasi__)\n#include "pycore_tobkiri_entropy.h"\n#endif\n')
    return replace(text, "pyurandom(void *buffer, Py_ssize_t size, int blocking, int raise)\n{\n",
                   "pyurandom(void *buffer, Py_ssize_t size, int blocking, int raise)\n{\n"
                   "#if defined(__wasi__)\n    return _PyTobkiri_EntropyFill(buffer, size, raise);\n#endif\n")


def stdlib_random(text: str) -> str:
    return replace(text, '    def seed(self, *args, **kwds):\n        "Stub method.  Not used for a system random number generator."\n        return None\n',
                   '    def seed(self, *args, **kwds):\n        "Stub method.  Not used for a system random number generator."\n'
                   '        if hasattr(_random.Random, "_tobkiri_mark_system_random"):\n'
                   '            _random.Random._tobkiri_mark_system_random(self)\n        return None\n')


def build_script(text: str) -> str:
    text = replace(text, '    let cpython_dir = repo_dir.join("cpython");\n',
                   '    let cpython_dir = repo_dir.join("cpython");\n'
                   '    anyhow::ensure!(cpython_dir.exists(), "patched producer requires explicitly pinned, patched local CPython source; download disabled");\n')
    text = replace(text, 'fn package_all_the_things(out_dir: &Path) -> Result<()> {\n',
                   'fn package_all_the_things(out_dir: &Path) -> Result<()> {\n'
                   '    tobkiri_require_runtime_stage()?;\n')
    return text + '\nfn tobkiri_require_runtime_stage() -> Result<()> {\n' \
        '    bail!("Unavailable: verified sequential shared-target runtime stage is not implemented (BUILD-02); original nested sync/async target builds are disabled")\n}\n'


def prelink(text: str) -> str:
    for name in ("embedded_python_standard_library", "embedded_helper_utils"):
        text = replace(text, f"pub fn {name}() -> Result<TempDir> {{", f"pub fn {name}(scratch: &Path) -> Result<TempDir> {{")
    text = text.replace("tempfile::tempdir()?", "tempfile::tempdir_in(scratch)?")
    return replace(text, "        for entry in fs::read_dir(path).with_context(|| path.display().to_string())? {\n"
                   "            search_directory(root, &entry?.path(), libraries, configs, modules_seen)?;\n        }",
                   "        let mut entries = fs::read_dir(path).with_context(|| path.display().to_string())?\n"
                   "            .collect::<std::io::Result<Vec<_>>>()?;\n"
                   "        entries.sort_by_key(|entry| entry.file_name());\n"
                   "        for entry in entries {\n            search_directory(root, &entry.path(), libraries, configs, modules_seen)?;\n        }")


def summary(text: str) -> str:
    for direction in ("imported", "exported"):
        old = f"            me.{direction}_interfaces.keys().copied(),"
        new = f"            {{ let mut ids = me.{direction}_interfaces.keys().copied().collect::<Vec<_>>();\n" \
              "              ids.sort_by_key(|id| id.index()); ids.into_iter() },"
        text = replace(text, old, new)
    return text


def cargo(text: str) -> str:
    text = replace(text, '[dependencies]\n', '[dependencies]\nsha2 = "=0.10.9"\nserde_json = "=1.0.154"\nlibc = "=0.2.190"\n')
    return text + '\n[patch.crates-io]\n# Producer-only original WASI46 ctor exposure; root host47 is unchanged.\nwasmtime-wasi = { path = "../wasmtime-wasi-46.0.1" }\n'


def makefile(text: str) -> str:
    return replace(text, '\t\tPython/bootstrap_hash.o \\\n', '\t\tPython/bootstrap_hash.o \\\n\t\tPython/tobkiri_entropy.o \\\n')


def wasi_constructor(text: str) -> str:
    return replace(text, "    pub(crate) fn new(\n        i: impl Iterator<Item = FsResult<types::DirectoryEntry>> + Send + 'static,\n",
                   "    /// Producer patch: accept bounded captured entries, keeping this nominal type.\n"
                   "    pub fn new(\n        i: impl Iterator<Item = FsResult<types::DirectoryEntry>> + Send + 'static,\n")


TRANSFORMS = {
    "componentize-py/src/lib.rs": producer_lib,
    "componentize-py/src/command.rs": producer_command,
    "componentize-py/src/prelink.rs": prelink,
    "componentize-py/src/summary.rs": summary,
    "componentize-py/src/python.rs": python_api,
    "componentize-py/runtime/src/lib.rs": runtime,
    "componentize-py/build.rs": build_script,
    "componentize-py/Cargo.toml": cargo,
    "cpython/Python/bootstrap_hash.c": bootstrap,
    "cpython/Modules/_randommodule.c": random_module,
    "cpython/Lib/random.py": stdlib_random,
    "cpython/Makefile.pre.in": makefile,
    "wasmtime-wasi/src/p2/filesystem.rs": wasi_constructor,
}


def patches(source: Path) -> tuple[dict[str, str], dict]:
    """Calculate exact diffs and before/after hashes; do not modify upstream."""
    pins = json.loads((HERE / "pins.json").read_text())
    diff = {"componentize-py": "", "cpython": "", "wasmtime-wasi": ""}
    report = {}
    for path, transform in TRANSFORMS.items():
        data = (source / path).read_bytes()
        before = hashlib.sha256(data).hexdigest()
        if before != pins["sources"][path]["sha256"]:
            raise ValueError(f"source pin differs: {path}")
        output = transform(data.decode())
        report[path] = {"before": before, "after": hashlib.sha256(output.encode()).hexdigest(),
                        "before_bytes": len(data), "after_bytes": len(output.encode())}
        producer, relative = path.split("/", 1)
        diff[producer] += "".join(difflib.unified_diff(data.decode().splitlines(True), output.splitlines(True),
                                                     fromfile=f"a/{relative}", tofile=f"b/{relative}"))
    for path in sorted((HERE / "overlay").rglob("*")):
        if not path.is_file():
            continue
        producer, relative = path.relative_to(HERE / "overlay").as_posix().split("/", 1)
        data = path.read_bytes()
        diff[producer] += "".join(difflib.unified_diff([], data.decode().splitlines(True),
                                                     fromfile="/dev/null", tofile=f"b/{relative}"))
        report[f"{producer}/{relative}"] = {"before": None, "after": hashlib.sha256(data).hexdigest(),
                                            "before_bytes": 0, "after_bytes": len(data)}
    return diff, report


def main() -> None:
    """Generate review artifacts from a read-only source checkout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args()
    diff, report = patches(args.source)
    for producer, text in diff.items():
        (HERE / "patches" / f"{producer}-0001.patch").write_text(text)
    (HERE / "source-delta.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
