"""Runtime configuration (all overridable via environment variables)."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_VAULT = "~/CortexVault"
DEFAULT_HTTP_PORT = 8765


def vault_path() -> Path:
    """Vault root. Set CORTEX_VAULT_PATH to move it (e.g. into a Google Drive folder)."""
    return Path(os.environ.get("CORTEX_VAULT_PATH", DEFAULT_VAULT)).expanduser().resolve()


def http_port() -> int:
    return int(os.environ.get("CORTEX_HTTP_PORT", DEFAULT_HTTP_PORT))
