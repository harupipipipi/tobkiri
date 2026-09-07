"""Explicit data dependencies for projecting the existing settings controls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SettingsCatalogInputs:
    """Owner-resolved display data, not authorization or credential material.

    All fields are required: an unavailable dependency must not accidentally
    trigger discovery against an ambient registry or credential store.
    """

    input_templates: list[dict[str, Any]]
    output_templates: list[dict[str, Any]]
    input_profile_options: list[dict[str, Any]]
    output_profile_options: list[dict[str, Any]]
    model_options: list[dict[str, Any]]
    model_route_options: list[dict[str, Any]]
    api_key_status: list[dict[str, Any]]
