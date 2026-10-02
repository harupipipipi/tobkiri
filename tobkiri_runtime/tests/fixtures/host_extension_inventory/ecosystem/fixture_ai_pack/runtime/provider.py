"""Static scanner fixture; this module is never imported by the test."""

from pathlib import Path


def read_fixture(path: Path) -> str:
    """Return fixture text to expose a statically observable I/O call."""

    return path.read_text(encoding="utf-8")


HOST_PROVIDER_FACTORY = {"fixture_ai_pack.provider": read_fixture}
