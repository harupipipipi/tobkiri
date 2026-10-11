"""Public bounded denial only for authenticated exact-operation review."""

from core_runtime.authority.v4 import AuthorityDenied


class AuthenticatedPolicyReviewDenied(AuthorityDenied):
    """A current authenticated reviewer classified this exact operation dangerous."""

    def __init__(self, safe_public_reason: str) -> None:
        reason = " ".join(str(safe_public_reason).split())[:512]
        self.safe_public_reason = reason or "操作が危険と判定されました。"
        super().__init__("authenticated exact operation review denied")
