"""Resolve the runtime version for installed and sealed source layouts."""

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def resolve_version() -> str:
    """Return installed metadata, or the adjacent source project's version.

    Launcher stages the runtime source and its locked dependencies separately,
    without installing the project distribution. Only that source tree's own
    pyproject is a fallback; neither the working directory nor Pack files are
    version authorities. Missing or invalid source metadata fails closed.
    """
    for distribution in ("tobkiri-runtime", "rumi-ai"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    from packaging.version import Version

    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        with project_path.open("rb") as source:
            project = tomllib.load(source).get("project")
        if not isinstance(project, dict) or project.get("name") != "tobkiri-runtime":
            raise ValueError("source project identity does not match tobkiri-runtime")
        declared = project.get("version")
        if not isinstance(declared, str) or not declared or declared != declared.strip():
            raise ValueError("source project version must be a nonempty string")
        Version(declared)
    except (OSError, ValueError) as error:
        raise RuntimeError("Tobkiri runtime version metadata is unavailable or invalid") from error
    return declared
