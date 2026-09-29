"""账号、令牌与角色。"""
from __future__ import annotations

import secrets
import sqlite3

from .errors import ApiError
from .services import now_iso


def issue_token(conn: sqlite3.Connection, actor_id: str, secret: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE actor_id=?", (actor_id,)).fetchone()
    if row is None or row["secret"] != secret:
        raise ApiError(401, "BAD_CREDENTIALS", "账号或口令错误")
    token = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO tokens (token, actor_id, created_at) VALUES (?,?,?)",
        (token, actor_id, now_iso()),
    )
    return {"token": token, "actor_id": actor_id, "role": row["role"]}


def authenticate(conn: sqlite3.Connection, token: str):
    """按令牌解析操作者；无效返回 None。"""
    if not token:
        return None
    return conn.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.actor_id = t.actor_id WHERE t.token=?",
        (token,),
    ).fetchone()
