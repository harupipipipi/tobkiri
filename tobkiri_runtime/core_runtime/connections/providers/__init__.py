"""Host-owned compatibility catalog for built-in connection providers."""

from .cloudflare import CLOUDFLARE_PROVIDER
from .codex import CODEX_PROVIDER
from .github import GITHUB_PROVIDER
from .google import GOOGLE_PROVIDER

__all__ = [
    "CLOUDFLARE_PROVIDER",
    "CODEX_PROVIDER",
    "GITHUB_PROVIDER",
    "GOOGLE_PROVIDER",
]
