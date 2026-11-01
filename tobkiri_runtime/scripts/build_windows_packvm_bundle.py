"""Stage pinned Windows QEMU/WHPX assets and the complete adjacent DLL closure.

Pass --qemu-dll NAME PATH SHA256 for every redistributable DLL. Missing PE
normal/delay imports fail the build; no binary is executed and no downloads
are performed. Guest assets are the same amd64 Linux guest used by QEMU/KVM.
"""

from __future__ import annotations

try:
    from scripts.build_linux_packvm_bundle import main
except ModuleNotFoundError:
    from build_linux_packvm_bundle import main


if __name__ == "__main__":
    raise SystemExit(main(accelerator="whpx"))
