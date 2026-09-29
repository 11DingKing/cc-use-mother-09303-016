"""SQLite 持久化：连接与表结构。

账本分录（ledger）是资金占用的唯一事实来源：所有批次余额都由分录
重放得出，审计人员可据此复算任一轮次、每笔资金的占用变化。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  actor_id TEXT PRIMARY KEY,
  role TEXT NOT NULL CHECK (role IN ('office','panel','applicant','auditor')),
  secret TEXT NOT NULL,
  applicant_id TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  actor_id TEXT NOT NULL REFERENCES users(actor_id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fund_batches (
  id TEXT PRIMARY KEY,
  funder TEXT NOT NULL,
  name TEXT NOT NULL,
  currency TEXT NOT NULL,
  total_minor INTEGER NOT NULL CHECK (total_minor >= 0),
  is_residual_destination INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
-- 尾差固定去向：每个币种至多一个尾差归集批次
CREATE UNIQUE INDEX IF NOT EXISTS uq_residual_per_currency
  ON fund_batches(currency) WHERE is_residual_destination = 1;

CREATE TABLE IF NOT EXISTS exchange_rates (
  id TEXT PRIMARY KEY,
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL,
  num INTEGER NOT NULL CHECK (num > 0),
  den INTEGER NOT NULL CHECK (den > 0),
  effective_date TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','SEALED')),
  created_at TEXT NOT NULL,
  sealed_at TEXT
);

-- 轮次锁定的汇率口径：一轮之内所有换算只用这一套口径
CREATE TABLE IF NOT EXISTS round_rates (
  round_id TEXT NOT NULL REFERENCES rounds(id),
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL,
  num INTEGER NOT NULL CHECK (num > 0),
  den INTEGER NOT NULL CHECK (den > 0),
  PRIMARY KEY (round_id, base_currency, quote_currency)
);

CREATE TABLE IF NOT EXISTS materials (
  applicant_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'SUBMITTED' CHECK (status IN ('SUBMITTED','VERIFIED')),
  submitted_at TEXT NOT NULL,
  verified_by TEXT,
  PRIMARY KEY (applicant_id, version)
);

CREATE TABLE IF NOT EXISTS recusals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reviewer_id TEXT NOT NULL,
  applicant_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (reviewer_id, applicant_id)
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  round_id TEXT NOT NULL REFERENCES rounds(id),
  panel_id TEXT NOT NULL,
  reviewer_id TEXT NOT NULL,
  applicant_id TEXT NOT NULL,
  score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
  comment TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (round_id, reviewer_id, applicant_id)
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT REFERENCES fund_batches(id),
  type TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS awards (
  id TEXT PRIMARY KEY,
  round_id TEXT NOT NULL REFERENCES rounds(id),
  panel_id TEXT NOT NULL,
  applicant_id TEXT NOT NULL,
  batch_id TEXT NOT NULL REFERENCES fund_batches(id),
  award_currency TEXT NOT NULL,
  award_amount_minor INTEGER NOT NULL CHECK (award_amount_minor > 0),
  lock_amount_minor INTEGER NOT NULL CHECK (lock_amount_minor >= 0),
  rate_num INTEGER NOT NULL DEFAULT 1,
  rate_den INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','WITHDRAWN','FORFEITED','DEFERRED')),
  deferred_to_round_id TEXT,
  idempotency_key TEXT UNIQUE,
  snapshot_id TEXT,
  created_at TEXT NOT NULL
);
-- 防双重授予：同一轮次内同一申请人对同一资金批次至多一笔有效授予
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_award
  ON awards(round_id, applicant_id, batch_id) WHERE status IN ('ACTIVE','DEFERRED');

CREATE TABLE IF NOT EXISTS snapshots (
  id TEXT PRIMARY KEY,
  award_id TEXT NOT NULL UNIQUE REFERENCES awards(id),
  payload TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  sealed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY,
  from_batch_id TEXT NOT NULL REFERENCES fund_batches(id),
  to_batch_id TEXT NOT NULL REFERENCES fund_batches(id),
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  converted_minor INTEGER NOT NULL CHECK (converted_minor >= 0),
  rate_num INTEGER NOT NULL DEFAULT 1,
  rate_den INTEGER NOT NULL DEFAULT 1,
  round_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id TEXT NOT NULL REFERENCES fund_batches(id),
  round_id TEXT,
  award_id TEXT,
  transfer_id TEXT,
  type TEXT NOT NULL CHECK (type IN (
    'LOCK','RELEASE','FORFEIT','DEFER_RELEASE','DEFER_LOCK',
    'TRANSFER_OUT','TRANSFER_IN','ROUNDING')),
  amount_num INTEGER NOT NULL,
  amount_den INTEGER NOT NULL DEFAULT 1 CHECK (amount_den > 0),
  currency TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_batch ON ledger(batch_id, entry_id);
CREATE INDEX IF NOT EXISTS idx_ledger_round ON ledger(round_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接：自动提交模式，事务由业务层显式控制。"""
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: str | Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
