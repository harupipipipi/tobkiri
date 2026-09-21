"""Driver registry for ComputerSeat.

Manages registration and selection of drivers by platform and capability.
Drivers are tried in a defined priority order per platform.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .drivers.base import ComputerDriver

# Preferred driver order per platform (highest priority first)
MAC_DRIVER_ORDER: list[str] = [
    "browser_cdp",
    "browser_companion",
    "mac_swift_host",
    "mac_accessibility",
    "mac_apple_events",
    "mac_cgevent_pid",
    "mac_screen_capture",
    "mac_foreground",
]

WINDOWS_DRIVER_ORDER: list[str] = [
    "browser_cdp",
    "browser_companion",
    "windows_uia",
    "windows_postmessage",
    "windows_foreground",
    "local_visible",
]

LINUX_DRIVER_ORDER: list[str] = [
    "browser_cdp",
    "browser_companion",
    "linux_x11_virtual",
    "linux_visible",
    "local_visible",
]


class DriverRegistry:
    """Registry for ComputerSeat drivers.

    Drivers are registered by name and selected based on platform
    and availability.
    """

    def __init__(self) -> None:
        self._drivers: dict[str, "ComputerDriver"] = {}

    def register(self, driver: "ComputerDriver") -> None:
        """Register a driver instance.

        Args:
            driver: A ComputerDriver instance to register.
        """
        self._drivers[driver.name] = driver

    def get_driver(self, name: str) -> "ComputerDriver | None":
        """Get a specific driver by name.

        Args:
            name: The driver name.

        Returns:
            The driver instance or None if not found.
        """
        return self._drivers.get(name)

    def get_driver_chain(self, platform: str) -> list["ComputerDriver"]:
        """Get the ordered list of available drivers for a platform.

        Drivers are returned in priority order. Only drivers that report
        themselves as available are included.

        Args:
            platform: The platform identifier ("darwin", "win32", or "linux").

        Returns:
            Ordered list of available drivers for the platform.
        """
        if platform == "darwin":
            order = MAC_DRIVER_ORDER
        elif platform == "win32":
            order = WINDOWS_DRIVER_ORDER
        elif platform.startswith("linux"):
            order = LINUX_DRIVER_ORDER
        else:
            order = list(self._drivers.keys())

        chain: list["ComputerDriver"] = []
        for name in order:
            driver = self._drivers.get(name)
            if driver is not None and driver.is_available():
                chain.append(driver)
        ordered = set(order)
        for name, driver in self._drivers.items():
            if name in ordered:
                continue
            if driver.is_available():
                chain.append(driver)
        return chain

    @property
    def all_drivers(self) -> dict[str, "ComputerDriver"]:
        """Return all registered drivers."""
        return dict(self._drivers)

    def safe_ax_candidate_diagnostics(self, available_driver_names: set[str] | None = None) -> dict[str, object]:
        """Describe the fixed macOS AX type candidate without target/content data."""
        name = "mac_accessibility"
        driver = self._drivers.get(name)
        registered = driver is not None
        available = bool(registered and (available_driver_names is None or name in available_driver_names))
        background_type_capable = False
        capability_readable = False
        if driver is not None:
            try:
                background_type_capable = bool(driver.capabilities().can_background_type)
                capability_readable = True
            except Exception:
                capability_readable = False
        if not registered:
            result_code = "AX_DRIVER_NOT_REGISTERED"
        elif not available:
            result_code = "AX_DRIVER_UNAVAILABLE"
        elif not capability_readable:
            result_code = "AX_CAPABILITY_UNAVAILABLE"
        elif not background_type_capable:
            result_code = "AX_BACKGROUND_TYPE_UNSUPPORTED"
        else:
            result_code = "AX_DRIVER_ELIGIBLE"
        return {
            "driver_registered": registered,
            "driver_available": available,
            "background_type_capable": background_type_capable,
            "attempted": False,
            "result_code": result_code,
        }
