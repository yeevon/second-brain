from __future__ import annotations

import os
from pathlib import Path


def check_vault_writable(vault_path: str) -> bool:
    path = Path(vault_path)
    return path.exists() and path.is_dir() and os.access(path, os.W_OK)
