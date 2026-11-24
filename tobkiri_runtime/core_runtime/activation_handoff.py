"""Distinguish durable activation from validation of the next Host capture."""


class ActivationCommittedError(RuntimeError):
    """Signal that activation committed but the cold Host still needs validation."""
