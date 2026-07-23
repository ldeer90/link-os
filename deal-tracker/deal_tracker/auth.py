from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from .db import connect, init_db
from .env import load_env_value
from .utils import now_iso


SESSION_COOKIE = "gpdt_session"
SESSION_DAYS = 14
ROLES = {"admin", "agency"}
VISIBILITY_TIERS = {"basic", "trusted", "full"}
TIER_RANK = {"basic": 1, "trusted": 2, "full": 3}


def _platform_enabled() -> bool:
    if os.getenv("LINK_OS_USE_POSTGRES_AUTH", "true").strip().lower() in {"0", "false", "no"}:
        return False
    from .platform_runtime import platform_configured

    return platform_configured()


def _platform_user_dict(user: Any) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "password_hash": user.password_hash,
        "role": user.role,
        "visibility_tier": user.visibility_tier,
        "is_active": 1 if user.is_active else 0,
        "created_at": user.created_at.isoformat() if user.created_at else "",
        "updated_at": user.updated_at.isoformat() if user.updated_at else "",
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else "",
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = 260_000) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_raw)
        expected = base64.b64decode(digest_raw)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def ensure_bootstrap_admin() -> None:
    if _platform_enabled():
        from sqlalchemy import select

        from .platform.models import UserAccount
        from .platform_runtime import platform_session

        email = (load_env_value("APP_BOOTSTRAP_ADMIN_EMAIL") or "admin@link-os.local").strip().lower()
        password = load_env_value("APP_BOOTSTRAP_ADMIN_PASSWORD") or "change-me-now"
        with platform_session() as session:
            existing = session.scalar(select(UserAccount.id).where(UserAccount.role == "admin").limit(1))
            if existing is None:
                session.add(
                    UserAccount(
                        email=email,
                        password_hash=hash_password(password),
                        role="admin",
                        visibility_tier="full",
                        is_active=True,
                        source_metadata={"source": "bootstrap"},
                    )
                )
        return
    init_db()
    email = load_env_value("APP_BOOTSTRAP_ADMIN_EMAIL") or "admin@guest-post.local"
    password = load_env_value("APP_BOOTSTRAP_ADMIN_PASSWORD") or "change-me-now"
    with connect() as connection:
        existing = connection.execute("select id from users where role='admin' limit 1").fetchone()
        if existing:
            return
        timestamp = now_iso()
        connection.execute(
            """
            insert into users (email, password_hash, role, visibility_tier, is_active, created_at, updated_at)
            values (?, ?, 'admin', 'full', 1, ?, ?)
            """,
            (email.lower(), hash_password(password), timestamp, timestamp),
        )
        connection.commit()


def create_user(email: str, password: str, role: str = "agency", visibility_tier: str = "basic", is_active: bool = True) -> int:
    if _platform_enabled():
        from .platform.models import UserAccount
        from .platform_runtime import platform_session

        role = role if role in ROLES else "agency"
        visibility_tier = visibility_tier if visibility_tier in VISIBILITY_TIERS else "basic"
        with platform_session() as session:
            user = UserAccount(
                email=email.strip().lower(),
                password_hash=hash_password(password),
                role=role,
                visibility_tier=visibility_tier,
                is_active=is_active,
                source_metadata={"source": "link_os"},
            )
            session.add(user)
            session.flush()
            return int(user.id)
    init_db()
    role = role if role in ROLES else "agency"
    visibility_tier = visibility_tier if visibility_tier in VISIBILITY_TIERS else "basic"
    timestamp = now_iso()
    with connect() as connection:
        cursor = connection.execute(
            """
            insert into users (email, password_hash, role, visibility_tier, is_active, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (email.strip().lower(), hash_password(password), role, visibility_tier, 1 if is_active else 0, timestamp, timestamp),
        )
        connection.commit()
        return int(cursor.lastrowid)


def update_user(user_id: int, fields: dict[str, Any]) -> None:
    if _platform_enabled():
        from .platform.models import UserAccount
        from .platform_runtime import platform_session

        with platform_session() as session:
            user = session.get(UserAccount, user_id)
            if user is None:
                return
            if "email" in fields:
                user.email = str(fields["email"]).strip().lower()
            if fields.get("role") in ROLES:
                user.role = str(fields["role"])
            if fields.get("visibility_tier") in VISIBILITY_TIERS:
                user.visibility_tier = str(fields["visibility_tier"])
            if "is_active" in fields:
                user.is_active = str(fields["is_active"]).lower() in {"1", "true", "on", "yes"}
            if fields.get("password"):
                user.password_hash = hash_password(str(fields["password"]))
        return
    allowed: dict[str, Any] = {}
    if "email" in fields:
        allowed["email"] = str(fields["email"]).strip().lower()
    if "role" in fields and fields["role"] in ROLES:
        allowed["role"] = fields["role"]
    if "visibility_tier" in fields and fields["visibility_tier"] in VISIBILITY_TIERS:
        allowed["visibility_tier"] = fields["visibility_tier"]
    if "is_active" in fields:
        allowed["is_active"] = 1 if str(fields["is_active"]) in {"1", "true", "on", "yes"} else 0
    if fields.get("password"):
        allowed["password_hash"] = hash_password(str(fields["password"]))
    if not allowed:
        return
    allowed["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in allowed)
    with connect() as connection:
        connection.execute(f"update users set {assignments} where id=?", [*allowed.values(), user_id])
        connection.commit()


def list_users(role: str | None = None) -> list[dict[str, Any]]:
    if _platform_enabled():
        from sqlalchemy import select

        from .platform.models import UserAccount
        from .platform_runtime import platform_session

        with platform_session() as session:
            statement = select(UserAccount)
            if role:
                statement = statement.where(UserAccount.role == role)
            rows = list(session.scalars(statement.order_by(UserAccount.role, UserAccount.email)))
            return [_platform_user_dict(row) for row in rows]
    init_db()
    where = "where role=?" if role else ""
    values = [role] if role else []
    with connect() as connection:
        rows = connection.execute(f"select * from users {where} order by role, email", values).fetchall()
    return [dict(row) for row in rows]


def get_user(user_id: int) -> dict[str, Any] | None:
    if _platform_enabled():
        from .platform.models import UserAccount
        from .platform_runtime import platform_session

        with platform_session() as session:
            user = session.get(UserAccount, user_id)
            return _platform_user_dict(user) if user else None
    init_db()
    with connect() as connection:
        row = connection.execute("select * from users where id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    if _platform_enabled():
        from sqlalchemy import select

        from .platform.models import UserAccount
        from .platform_runtime import platform_session

        ensure_bootstrap_admin()
        with platform_session() as session:
            user = session.scalar(
                select(UserAccount).where(
                    UserAccount.email == email.strip().lower(),
                    UserAccount.is_active.is_(True),
                )
            )
            if user is None or not verify_password(password, user.password_hash):
                return None
            user.last_login_at = _utcnow()
            session.flush()
            return _platform_user_dict(user)
    ensure_bootstrap_admin()
    with connect() as connection:
        row = connection.execute("select * from users where email=? and is_active=1", (email.strip().lower(),)).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            return None
        timestamp = now_iso()
        connection.execute("update users set last_login_at=?, updated_at=? where id=?", (timestamp, timestamp, row["id"]))
        connection.commit()
    return get_user(int(row["id"]))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    now = _utcnow()
    expires = now + timedelta(days=SESSION_DAYS)
    if _platform_enabled():
        from .platform.models import UserSession
        from .platform_runtime import platform_session

        with platform_session() as session:
            session.add(
                UserSession(
                    user_id=user_id,
                    token_hash=_hash_token(token),
                    created_at=now,
                    expires_at=expires,
                )
            )
        return token
    with connect() as connection:
        connection.execute(
            "insert into user_sessions (user_id, token_hash, created_at, expires_at) values (?, ?, ?, ?)",
            (user_id, _hash_token(token), now.isoformat(), expires.isoformat()),
        )
        connection.commit()
    return token


def delete_session(token: str) -> None:
    if not token:
        return
    if _platform_enabled():
        from sqlalchemy import delete

        from .platform.models import UserSession
        from .platform_runtime import platform_session

        with platform_session() as session:
            session.execute(delete(UserSession).where(UserSession.token_hash == _hash_token(token)))
        return
    with connect() as connection:
        connection.execute("delete from user_sessions where token_hash=?", (_hash_token(token),))
        connection.commit()


def current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    if _platform_enabled():
        from sqlalchemy import select

        from .platform.models import UserAccount, UserSession
        from .platform_runtime import platform_session

        with platform_session() as session:
            user = session.scalar(
                select(UserAccount)
                .join(UserSession, UserSession.user_id == UserAccount.id)
                .where(
                    UserSession.token_hash == _hash_token(token),
                    UserSession.expires_at > _utcnow(),
                    UserAccount.is_active.is_(True),
                )
            )
            return _platform_user_dict(user) if user else None
    init_db()
    now = _utcnow().isoformat()
    with connect() as connection:
        row = connection.execute(
            """
            select users.* from user_sessions
            join users on users.id=user_sessions.user_id
            where user_sessions.token_hash=? and user_sessions.expires_at>? and users.is_active=1
            """,
            (_hash_token(token), now),
        ).fetchone()
    return dict(row) if row else None


def can_view_tier(user: dict[str, Any], required_tier: str) -> bool:
    if user.get("role") == "admin":
        return True
    return TIER_RANK.get(str(user.get("visibility_tier") or "basic"), 1) >= TIER_RANK.get(required_tier or "basic", 1)
