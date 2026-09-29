"""访问令牌与角色。"""
from __future__ import annotations

import sqlite3

from .errors import forbidden, unauthorized

ROLES = ("admin", "funder", "committee", "applicant", "auditor")


def authenticate(conn: sqlite3.Connection, token: str | None) -> dict | None:
    """按令牌识别操作者；识别失败返回 None。"""
    if not token:
        return None
    row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    return dict(row) if row is not None else None


def require_role(actor: dict | None, *roles: str) -> dict:
    """校验操作者角色，返回操作者本身。"""
    if actor is None:
        raise unauthorized()
    if actor["role"] not in roles:
        raise forbidden()
    return actor
