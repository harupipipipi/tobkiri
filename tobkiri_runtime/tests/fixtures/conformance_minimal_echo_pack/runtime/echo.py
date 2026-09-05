"""Minimal Pack v4 conformance implementation marker."""


def echo(payload: dict[str, str]) -> dict[str, str]:
    """Echo the one Contract payload."""

    return {"message": payload["message"]}
