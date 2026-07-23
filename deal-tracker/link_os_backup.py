#!/usr/bin/env python3
"""Create and verify a rotating native PostgreSQL backup for LINK OS."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


POSTGRES_BIN = Path("/opt/homebrew/opt/postgresql@17/bin")
BACKUP_DIR = Path.home() / "Library" / "Application Support" / "LinkOS" / "backups"
DATABASE = os.environ.get("LINK_OS_DATABASE_NAME", "link_os")
KEEP = max(2, int(os.environ.get("LINK_OS_BACKUP_RETENTION", "30")))


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = BACKUP_DIR / f"link-os-{stamp}.dump"
    partial_path = final_path.with_suffix(".dump.partial")
    partial_path.unlink(missing_ok=True)
    try:
        subprocess.run(
            [str(POSTGRES_BIN / "pg_dump"), "-Fc", "-d", DATABASE, "-f", str(partial_path)],
            check=True,
        )
        subprocess.run(
            [str(POSTGRES_BIN / "pg_restore"), "-l", str(partial_path)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        os.chmod(partial_path, 0o600)
        partial_path.replace(final_path)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise

    backups = sorted(BACKUP_DIR.glob("link-os-*.dump"), key=lambda path: path.stat().st_mtime, reverse=True)
    pruned = 0
    for stale in backups[KEEP:]:
        stale.unlink()
        pruned += 1
    print(json.dumps({"ok": True, "path": str(final_path), "size_bytes": final_path.stat().st_size, "pruned": pruned}))


if __name__ == "__main__":
    main()
