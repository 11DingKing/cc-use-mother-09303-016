"""资金占用分录与派生余额。

账本只追加不修改：任何额度变化（锁定、释放、转移、尾差）都是一条
分录；批次的可用/锁定/准备金余额全部由分录求和派生，审计人员因此
可以对任一批次、任一轮次逐笔复算。

批次恒等式：total + 转入 - 转出 = 可用 + 锁定 + 准备金
"""
from __future__ import annotations

import sqlite3


def post(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    type: str,
    amount: int,
    currency: str,
    created_at: str,
    award_id: int | None = None,
    adjustment_id: int | None = None,
    round_id: int | None = None,
    note: str | None = None,
) -> int:
    """追加一条分录，返回分录编号。必须在做完业务校验后于事务内调用。"""
    cur = conn.execute(
        """INSERT INTO ledger_entries
           (batch_id, award_id, adjustment_id, round_id, type, amount, currency, note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (batch_id, award_id, adjustment_id, round_id, type, amount, currency, note, created_at),
    )
    return cur.lastrowid


def batch_totals(conn: sqlite3.Connection, batch_id: int) -> dict:
    """由分录派生批次余额。尾差按批次固定去向计入准备金或留在可用。"""
    batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    row = conn.execute(
        """SELECT
               COALESCE(SUM(CASE WHEN type='LOCK' THEN amount ELSE 0 END),0)
             - COALESCE(SUM(CASE WHEN type='RELEASE' THEN amount ELSE 0 END),0) AS locked,
             COALESCE(SUM(CASE WHEN type='TRANSFER_IN' THEN amount ELSE 0 END),0) AS transferred_in,
             COALESCE(SUM(CASE WHEN type='TRANSFER_OUT' THEN amount ELSE 0 END),0) AS transferred_out,
             COALESCE(SUM(CASE WHEN type='RESIDUAL' THEN amount ELSE 0 END),0) AS residual
           FROM ledger_entries WHERE batch_id=?""",
        (batch_id,),
    ).fetchone()
    to_reserve = batch["residual_destination"] == "RESERVE"
    reserve = row["residual"] if to_reserve else 0
    available = (
        batch["total_amount"] + row["transferred_in"] - row["transferred_out"] - row["locked"] - reserve
    )
    return {
        "total": batch["total_amount"],
        "locked": row["locked"],
        "reserve": reserve,
        "residual_returned": 0 if to_reserve else row["residual"],
        "transferred_in": row["transferred_in"],
        "transferred_out": row["transferred_out"],
        "available": available,
    }


def round_occupancy(conn: sqlite3.Connection, round_id: int) -> list[dict]:
    """某轮次内各批次的净锁定（LOCK - RELEASE），用于轮次复算。"""
    rows = conn.execute(
        """SELECT e.batch_id AS batch_id, b.currency AS currency,
                  COALESCE(SUM(CASE WHEN e.type='LOCK' THEN e.amount ELSE -e.amount END), 0) AS net_locked
           FROM ledger_entries e JOIN batches b ON b.id = e.batch_id
           WHERE e.round_id=? AND e.type IN ('LOCK','RELEASE')
           GROUP BY e.batch_id, b.currency ORDER BY e.batch_id""",
        (round_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def entries_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    """按分录顺序返回批次全部分录，供台账与复算使用。"""
    return conn.execute(
        "SELECT * FROM ledger_entries WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()
