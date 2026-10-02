"""Read-only Linux KVM capability probe, independent of any product Pack."""

import os
import platform
import stat
from pathlib import Path


def kvm_capability() -> tuple[bool, str | None]:
    """Probe KVM read-only configuration; never enable or change permissions."""
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        return False, "Linux PackVM currently requires Linux x86_64"
    if os.geteuid() == 0:
        return False, "Linux PackVM must run as an unprivileged user"
    import fcntl

    try:
        info = Path("/dev/kvm").lstat()
        if not stat.S_ISCHR(info.st_mode):
            return False, "Linux PackVM requires the /dev/kvm character device"
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if fcntl.ioctl(fd, 0xAE00, 0) != 12:  # KVM_GET_API_VERSION
                return False, "Linux PackVM requires KVM API version 12"
        finally:
            os.close(fd)
    except FileNotFoundError:
        return False, "Linux PackVM requires /dev/kvm; TCG is not a production isolation fallback"
    except PermissionError:
        return False, "Linux PackVM cannot access /dev/kvm; host setup must grant access"
    except OSError:
        return False, "Linux PackVM KVM capability could not be verified"
    return True, None


