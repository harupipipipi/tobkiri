# Python component producer

The SDK compilation target is a typed outer component. The unchanged
componentize-py output is its private implementation, including that compiler's
private initialization interface. That initialization interface is never an
outer export and is unavailable to a Pack caller.

The reviewed producer stages captured source bytes, generates bindings, and
preinitializes with componentize-py 0.25.0 and `--stub-wasi`. The producer does
not pass Pack config, secrets, inherited HOME or credentials. Runtime config is
supplied later by the gated lifecycle call. The required deterministic,
machine-independent initialization has not yet been established: the pinned
CLI constructs its own WASI context and bakes a build-time PRNG seed into the
snapshot. Two captured clean builds currently differ. The adviser approved a
narrow source patch to the original pinned producer for initialization before
CPython, rather than another initialization layer. Its source/API and capacity
plan is prepared; compilation and the proof matrix remain pending while the
disk floor and tool-build hold apply.

The planned composition tool is Bytecode Alliance WAC 0.12.0, published on
2026-09-30. It instantiates the original inner component, forwards only the
explicitly allowed core imports, and selects only lifecycle and the declared
Pack interfaces as exports. Interface types are forwarded through aliases;
resource identity must survive composition rather than being copied into
structurally similar definitions. No component bytes are deleted or patched.

The build verifies the exact outer import/export sets and the declared WIT
world. Provenance records captured sources, WIT, compiler/runtime versions,
composition source and tool hash, flags, and both inner and outer hashes. A
clean repeat build compares those artifacts and reports any difference. The
separate resource fixture executes own, borrow, drop, cross-interface identity
and rejection of a foreign nominal resource, and verifies that the private
initializer is inaccessible. This producer fixture does not substitute for
Core/Gate runtime acceptance of a Pack.

The shell-policy pilot retains its 91 frozen legacy cases and source hashes.
Its earlier extra-export failure remains in `m4-pending.json`; a later successful
outer-component replay does not erase that attempt or complete the migration
inventory. The pinned WAC tool and resource proof have recorded executions;
the patched compiler build and final runtime acceptance remain pending.

`deterministic_plan.py` prepares public declaration-derived inputs: the
versioned seed domain, canonical declared-plan digest, explicit epoch,
monotonic progression, guest environment and path-derived metadata. Its native
tests do not establish enforcement by the producer. The source patch must
constrain filesystem, network, ordering and scheduling before Python starts,
then prove independence from host paths, environment, time and entropy.
Only explicitly staged Python paths are admitted; venv/pipenv discovery and
inherited environment are disabled. The input mounts are read-only and guest
bytecode writes are disabled; host inode metadata is not a normalized input.
The public seed must never provide runtime cryptography. Snapshot RNG caches
need an audit and live Core randomness/reseeding before security use.
