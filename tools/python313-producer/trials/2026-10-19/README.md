# Isolated CPython 3.13 build trial

These scripts are exact executed stage commands, not a production installer.
They require the reviewed source at `/source`, SDK33 at `/sdk`, and a private
writable `/work` inside the separately controlled networkless sandbox. Do not
run them directly against arbitrary host paths. The receipt describes enforced
resource limits and observed results. No binary, secret, or full internal log
is included.

The native executable ran and reported CPython3.13.16 / Unicode15.1.0. The WASI
artifacts only compiled/linked; export inspection is not execution or ABI proof.
Optional native module absences, experimental dynamic linking and unchanged
SQLite restrictions remain explicit. This does not complete Issue1520.
