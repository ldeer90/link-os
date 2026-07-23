#!/usr/bin/env python3
"""Run LINK OS natively on macOS without Docker.

The launcher loads the repository's private .env file into the process, applies
safe local-service defaults, and then replaces itself with the API, worker, or
operator CLI process. It never prints environment values.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "deal-tracker"
ENV_FILE = REPO_ROOT / ".env"
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_private_environment(path: Path = ENV_FILE) -> None:
    """Load a simple dotenv file without logging any values."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def configure_native_defaults() -> None:
    load_private_environment()
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg:///link_os")
    os.environ.setdefault("LINK_OS_DATA_DIR", str(BACKEND_ROOT / "data" / "platform"))
    os.environ.setdefault("LINK_OS_USE_POSTGRES_AUTH", "true")


def run_api() -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        check=True,
        env=os.environ,
    )
    host = os.environ.get("LINK_OS_BIND_HOST", "127.0.0.1")
    port = os.environ.get("LINK_OS_PORT", "8766")
    os.execvpe(
        sys.executable,
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", port],
        os.environ,
    )


def run_worker() -> None:
    os.execvpe(
        sys.executable,
        [sys.executable, "-m", "deal_tracker.worker"],
        os.environ,
    )


def run_cli(arguments: list[str]) -> None:
    os.execvpe(
        sys.executable,
        [sys.executable, str(BACKEND_ROOT / "link_os_cli.py"), *arguments],
        os.environ,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=("api", "worker", "cli"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    configure_native_defaults()
    os.chdir(BACKEND_ROOT)
    if args.service == "api":
        run_api()
    if args.service == "worker":
        run_worker()
    run_cli(args.arguments)


if __name__ == "__main__":
    main()
