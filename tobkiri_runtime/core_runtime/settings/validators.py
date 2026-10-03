from __future__ import annotations

from collections.abc import Mapping

from .models import SettingContribution, SettingSectionId

BLOCKED_RAW_LABELS = {"mimo", "computer_use_gradient", "openrouter_auto"}


def localized_to_text(value: str | Mapping[str, str]) -> str:
    if isinstance(value, str):
        return value
    return value.get("en") or value.get("ja") or next(iter(value.values()), "")


def validate_contribution(contribution: SettingContribution) -> list[str]:
    errors: list[str] = []
    if not contribution.id:
        errors.append("missing id")
    if not contribution.owner:
        errors.append(f"{contribution.id}: missing owner")
    if contribution.priority is None:
        errors.append(f"{contribution.id}: missing priority")

    title = localized_to_text(contribution.title).strip()
    if not title:
        errors.append(f"{contribution.id}: missing title")
    if title.lower() in BLOCKED_RAW_LABELS:
        errors.append(f"{contribution.id}: raw/internal label '{title}' cannot be shown in normal Settings UI")
    if title == contribution.id or title == contribution.component:
        errors.append(f"{contribution.id}: title must not equal internal id or component path")

    if contribution.audience == "developer" and contribution.section not in {SettingSectionId.ADVANCED, SettingSectionId.DIAGNOSTICS}:
        errors.append(f"{contribution.id}: developer setting must live under Advanced or Diagnostics")
    if contribution.frequency == "debug" and contribution.section != SettingSectionId.DIAGNOSTICS:
        errors.append(f"{contribution.id}: debug setting must live under Diagnostics")
    if contribution.owner != "core" and contribution.section == SettingSectionId.QUICK_SETUP and contribution.priority < 20:
        errors.append(f"{contribution.id}: pack cannot take core setup priority range 0-19")
    return errors


def assert_valid_contributions(contributions: list[SettingContribution]) -> None:
    errors: list[str] = []
    for contribution in contributions:
        errors.extend(validate_contribution(contribution))
    if errors:
        raise ValueError("Invalid Settings contributions:\n" + "\n".join(errors))
