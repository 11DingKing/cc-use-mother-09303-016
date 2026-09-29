"""预置账号（仅用于本地演示与测试）。"""
from __future__ import annotations

import sqlite3

DEFAULT_USERS = [
    # (actor_id, role, secret, applicant_id)
    ("office", "office", "office-secret", None),
    ("auditor", "auditor", "auditor-secret", None),
    ("panel-a", "panel", "panel-a-secret", None),
    ("panel-b", "panel", "panel-b-secret", None),
    ("stu-001", "applicant", "stu-001-secret", "stu-001"),
    ("stu-002", "applicant", "stu-002-secret", "stu-002"),
    ("stu-003", "applicant", "stu-003-secret", "stu-003"),
]


def seed_users(conn: sqlite3.Connection) -> int:
    added = 0
    for actor_id, role, secret, applicant_id in DEFAULT_USERS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO users (actor_id, role, secret, applicant_id)"
            " VALUES (?,?,?,?)", (actor_id, role, secret, applicant_id))
        added += cur.rowcount
    return added
