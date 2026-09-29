"""SQLite 持久化：连接、事务与表结构。

账本（ledger_entries）只追加不修改；授予记录通过部分唯一索引
保证“同一申请人 + 同一资金来源”最多一条有效授予，从数据库层面
杜绝两个评审组并发重复授予同一来源额度。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    token TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('admin','funder','committee','applicant','auditor')),
    applicant_ref TEXT,
    funder_ref TEXT,
    reviewer_ref TEXT
);

CREATE TABLE IF NOT EXISTS fx_rate_sets (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    rounding TEXT NOT NULL CHECK (rounding IN ('DOWN','UP','HALF_UP','HALF_EVEN')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_rates (
    set_id INTEGER NOT NULL REFERENCES fx_rate_sets(id),
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    num INTEGER NOT NULL CHECK (num > 0),
    den INTEGER NOT NULL CHECK (den > 0),
    PRIMARY KEY (set_id, base, quote)
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    funder_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    total_amount INTEGER NOT NULL CHECK (total_amount >= 0),
    fx_rate_set_id INTEGER NOT NULL REFERENCES fx_rate_sets(id),
    residual_destination TEXT NOT NULL CHECK (residual_destination IN ('RESERVE','AVAILABLE')),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    committee TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','SEALED')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    applicant_id TEXT NOT NULL,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    materials TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SUBMITTED' CHECK (status IN ('SUBMITTED','ELIGIBLE','INELIGIBLE')),
    created_at TEXT NOT NULL,
    UNIQUE (applicant_id, round_id)
);

CREATE TABLE IF NOT EXISTS recusals (
    id INTEGER PRIMARY KEY,
    reviewer_id TEXT NOT NULL,
    applicant_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (reviewer_id, applicant_id)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    params TEXT NOT NULL,
    scope_batch_id INTEGER REFERENCES batches(id),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS awards (
    id INTEGER PRIMARY KEY,
    request_id TEXT UNIQUE,
    sealed_round_id INTEGER NOT NULL REFERENCES rounds(id),
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    applicant_id TEXT NOT NULL,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    award_amount INTEGER NOT NULL CHECK (award_amount > 0),
    award_currency TEXT NOT NULL,
    locked_amount INTEGER NOT NULL CHECK (locked_amount > 0),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','WITHDRAWN','DISQUALIFIED')),
    snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 防重复授予的核心约束：同一申请人在同一资金批次上最多一条有效授予。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_award
    ON awards (applicant_id, batch_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS trial_runs (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    lines TEXT NOT NULL,
    result TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('WITHDRAWAL','DISQUALIFICATION','DEFERRAL','TRANSFER')),
    award_id INTEGER REFERENCES awards(id),
    reason TEXT,
    params TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    award_id INTEGER REFERENCES awards(id),
    adjustment_id INTEGER REFERENCES adjustments(id),
    round_id INTEGER REFERENCES rounds(id),
    type TEXT NOT NULL CHECK (type IN ('LOCK','RELEASE','TRANSFER_OUT','TRANSFER_IN','RESIDUAL')),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_batch ON ledger_entries (batch_id, id);
CREATE INDEX IF NOT EXISTS idx_entries_round ON ledger_entries (round_id, id);
CREATE INDEX IF NOT EXISTS idx_entries_award ON ledger_entries (award_id, id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接：自动提交模式，写事务由 tx() 显式控制。"""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def tx(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 立即取得写锁，串行化并发写入。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
