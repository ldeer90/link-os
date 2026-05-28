from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import connect, init_db
from .deals import sync_replies
from .env import load_env_value
from .utils import now_iso


def sync_interval_days() -> int:
    raw = load_env_value("SYNC_INTERVAL_DAYS") or "3"
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def latest_successful_sync() -> dict[str, Any] | None:
    init_db()
    with connect() as connection:
        row = connection.execute("select * from sync_runs where status='success' order by finished_at desc, id desc limit 1").fetchone()
    return dict(row) if row else None


def sync_due() -> bool:
    latest = latest_successful_sync()
    if not latest:
        return True
    finished_at = _parse_iso(str(latest.get("finished_at") or ""))
    if not finished_at:
        return True
    return finished_at + timedelta(days=sync_interval_days()) <= datetime.now(timezone.utc)


def next_sync_at() -> str:
    latest = latest_successful_sync()
    if not latest:
        return now_iso()
    finished_at = _parse_iso(str(latest.get("finished_at") or ""))
    if not finished_at:
        return now_iso()
    return (finished_at + timedelta(days=sync_interval_days())).isoformat()


def record_setting(key: str, value: str) -> None:
    init_db()
    with connect() as connection:
        connection.execute(
            """
            insert into app_settings (key, value, updated_at) values (?, ?, ?)
            on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_iso()),
        )
        connection.commit()


def run_sync_recorded(trigger_type: str = "manual") -> dict[str, Any]:
    init_db()
    started_at = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            "insert into sync_runs (started_at, status, trigger_type) values (?, 'running', ?)",
            (started_at, trigger_type),
        )
        run_id = int(cursor.lastrowid)
        connection.execute(
            "insert into sync_events (sync_run_id, event_type, message, created_at) values (?, 'started', ?, ?)",
            (run_id, f"{trigger_type} sync started", started_at),
        )
        connection.commit()
    try:
        summary = sync_replies()
        finished_at = now_iso()
        with connect() as connection:
            connection.execute(
                """
                update sync_runs
                set finished_at=?, status='success', inbound_reply_count=?, new_deal_count=?, summary_json=?
                where id=?
                """,
                (
                    finished_at,
                    int(summary.get("inbound_replies") or 0),
                    int(summary.get("new_or_updated_deals") or 0),
                    json.dumps(summary, sort_keys=True),
                    run_id,
                ),
            )
            connection.execute(
                "insert into sync_events (sync_run_id, event_type, message, created_at) values (?, 'finished', 'sync completed', ?)",
                (run_id, finished_at),
            )
            connection.commit()
        record_setting("last_successful_sync_at", finished_at)
        record_setting("next_sync_at", next_sync_at())
        return {"sync_run_id": run_id, **summary}
    except Exception as exc:
        finished_at = now_iso()
        message = str(exc)
        with connect() as connection:
            connection.execute(
                "update sync_runs set finished_at=?, status='failed', error_message=? where id=?",
                (finished_at, message[:2000], run_id),
            )
            connection.execute(
                "insert into sync_events (sync_run_id, event_type, message, created_at) values (?, 'failed', ?, ?)",
                (run_id, message[:2000], finished_at),
            )
            connection.commit()
        return {"sync_run_id": run_id, "error": message}
