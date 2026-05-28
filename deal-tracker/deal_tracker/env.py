from __future__ import annotations

import os
from pathlib import Path

from .paths import ROOT


def load_env_value(key: str, env_path: Path | None = None) -> str:
    if os.environ.get(key):
        return os.environ[key]
    path = env_path or ROOT / ".env"
    if not path.exists():
        return ""
    prefix = f"{key}="
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        return value
    return ""
