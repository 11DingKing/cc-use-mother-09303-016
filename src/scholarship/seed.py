"""演示数据：多资助方资金池、汇率口径、限制规则、回避关系与候选。

仅用于本地演示与端到端测试；演示令牌写死在此，生产部署必须替换。
"""
from __future__ import annotations

import sqlite3

from . import service

DEMO_TOKENS = {
    "admin（奖学金办公室）": "demo-admin",
    "funder（资助方）": "demo-funder",
    "reviewer-chen（评审）": "demo-rev1",
    "reviewer-wang（评审）": "demo-rev2",
    "applicant-li（申请人 S001）": "demo-app1",
    "applicant-wang（申请人 S002）": "demo-app2",
    "auditor（审计）": "demo-auditor",
}

_USERS = [
    ("office", "demo-admin", "admin", None, None, None),
    ("riverside-funder", "demo-funder", "funder", None, "FUNDER-RIVERSIDE", None),
    ("rev-chen", "demo-rev1", "committee", None, None, "rev-chen"),
    ("rev-wang", "demo-rev2", "committee", None, None, "rev-wang"),
    ("app-li", "demo-app1", "applicant", "S001", None, None),
    ("app-wang", "demo-app2", "applicant", "S002", None, None),
    ("audit-li", "demo-auditor", "auditor", None, None, None),
]

_ADMIN = {"username": "seed", "role": "admin", "applicant_ref": None, "funder_ref": None, "reviewer_ref": None}


def seed(conn: sqlite3.Connection) -> dict:
    """写入演示数据；已有用户时跳过，保证可重复执行。"""
    if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]:
        return {"seeded": False}
    conn.executemany(
        "INSERT INTO users (username, token, role, applicant_ref, funder_ref, reviewer_ref)"
        " VALUES (?,?,?,?,?,?)",
        _USERS,
    )
    fx = service.create_fx_rate_set(
        conn,
        _ADMIN,
        {
            "name": "2026-秋季汇率口径",
            "rounding": "HALF_EVEN",
            "rates": [
                {"base": "EUR", "quote": "USD", "num": 108, "den": 100},
                {"base": "USD", "quote": "EUR", "num": 100, "den": 108},
                {"base": "CNY", "quote": "USD", "num": 14, "den": 100},
            ],
        },
    )
    usd = service.create_batch(
        conn,
        _ADMIN,
        {
            "name": "里弗赛德联合基金-美元池",
            "funder_id": "FUNDER-RIVERSIDE",
            "currency": "USD",
            "total_amount": "1000000.00",
            "fx_rate_set_id": fx["id"],
            "residual_destination": "RESERVE",
        },
    )
    eur = service.create_batch(
        conn,
        _ADMIN,
        {
            "name": "里弗赛德联合基金-欧元池",
            "funder_id": "FUNDER-RIVERSIDE",
            "currency": "EUR",
            "total_amount": "250000.00",
            "fx_rate_set_id": fx["id"],
            "residual_destination": "RESERVE",
        },
    )
    round_row = service.create_round(
        conn, _ADMIN, {"name": "2026-秋季评审", "committee": ["rev-chen", "rev-wang"]}
    )
    service.create_rule(
        conn, _ADMIN, {"type": "MAX_ACTIVE_AWARDS_PER_APPLICANT", "params": {"count": 1}}
    )
    service.create_rule(
        conn,
        _ADMIN,
        {"type": "MAX_AMOUNT_PER_AWARD", "params": {"amount": "20000.00", "currency": "USD"}},
    )
    service.create_rule(conn, _ADMIN, {"type": "MIN_GPA", "params": {"value": "3.0"}})
    service.create_rule(
        conn,
        _ADMIN,
        {
            "type": "ROUND_BUDGET_CAP",
            "params": {"amount": "600000.00"},
            "scope_batch_id": usd["id"],
        },
    )
    service.create_recusal(
        conn,
        _ADMIN,
        {"reviewer_id": "rev-wang", "applicant_id": "S002", "reason": "亲属关系"},
    )
    first = service.create_candidate(
        conn,
        _ADMIN,
        {
            "applicant_id": "S001",
            "round_id": round_row["id"],
            "materials": {"name": "李同学", "nationality": "CN", "gpa": "3.8", "major": "计算机"},
        },
    )
    second = service.create_candidate(
        conn,
        _ADMIN,
        {
            "applicant_id": "S002",
            "round_id": round_row["id"],
            "materials": {"name": "王同学", "nationality": "CN", "gpa": "3.5", "major": "经济学"},
        },
    )
    return {
        "seeded": True,
        "fx_rate_set_id": fx["id"],
        "usd_batch_id": usd["id"],
        "eur_batch_id": eur["id"],
        "round_id": round_row["id"],
        "candidate_ids": [first["id"], second["id"]],
    }
