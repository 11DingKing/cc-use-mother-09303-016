"""奖学金配置核心业务。

覆盖：资金批次、限制规则、候选材料、回避关系、汇率口径、方案试算、
正式授予（单事务原子锁定 + 评审快照封存）、放弃/资格丧失/延期/
资金转移的分录结算，以及面向审计的轮次复算与批次台账。

关键设计：
- 正式授予在一个 BEGIN IMMEDIATE 事务内完成“校验 + 写入 + 锁定”，
  并由 (申请人, 批次) 有效授予唯一索引兜底，两个评审组并发也不会
  重复授予同一来源额度；request_id 提供幂等重试。
- 汇率口径（fx_rate_sets）创建后不可变，批次创建时绑定；换算只在
  锁定时发生一次，释放永远按原锁定额，杜绝换算回漂造成的对账差异。
- 结算尾差/扣留以 RESIDUAL 分录进入批次固定去向（RESERVE 准备金或
  AVAILABLE 回流可用），批次恒等式始终成立。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import ledger
from . import rules as rule_engine
from .auth import require_role
from .db import tx
from .errors import bad_request, conflict, forbidden, not_found, unprocessable
from .money import ROUNDING_MODES, convert_minor, parse_minor

RESIDUAL_DESTINATIONS = ("RESERVE", "AVAILABLE")
ADJUSTMENT_TYPES = ("WITHDRAWAL", "DISQUALIFICATION", "DEFERRAL")


# ---------------------------------------------------------------- 基础工具


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pos_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise bad_request(f"{field} 必须是正整数")
    return value


def _minor(value: object, field: str, minimum: int = 0) -> int:
    try:
        result = parse_minor(value)
    except ValueError as exc:
        raise bad_request(f"{field}：{exc}") from exc
    if result < minimum:
        raise bad_request(f"{field} 不能小于 {minimum}")
    return result


def _currency(value: object, field: str = "currency") -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise bad_request(f"{field} 必须是三字母币种代码")
    return value.upper()


def _non_empty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"{field} 必须是非空字符串")
    return value.strip()


def _get_round(conn: sqlite3.Connection, round_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
    if row is None:
        raise not_found(f"评审轮次不存在：{round_id}")
    return row


def _get_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise not_found(f"资金批次不存在：{batch_id}")
    return row


# ---------------------------------------------------------------- 视图


def _round_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "committee": json.loads(row["committee"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _batch_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "funder_id": row["funder_id"],
        "currency": row["currency"],
        "total_amount": row["total_amount"],
        "fx_rate_set_id": row["fx_rate_set_id"],
        "residual_destination": row["residual_destination"],
        "status": row["status"],
        "created_at": row["created_at"],
        "totals": ledger.batch_totals(conn, row["id"]),
    }


def _candidate_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "applicant_id": row["applicant_id"],
        "round_id": row["round_id"],
        "materials": json.loads(row["materials"]),
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _award_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "request_id": row["request_id"],
        "round_id": row["round_id"],
        "sealed_round_id": row["sealed_round_id"],
        "applicant_id": row["applicant_id"],
        "candidate_id": row["candidate_id"],
        "batch_id": row["batch_id"],
        "award_amount": row["award_amount"],
        "award_currency": row["award_currency"],
        "locked_amount": row["locked_amount"],
        "status": row["status"],
        "snapshot": json.loads(row["snapshot"]),
        "created_at": row["created_at"],
    }


def _adjustment_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "award_id": row["award_id"],
        "reason": row["reason"],
        "params": json.loads(row["params"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _check_batch_visible(actor: dict, batch: sqlite3.Row) -> None:
    if actor["role"] in ("admin", "auditor"):
        return
    if actor["role"] == "funder" and actor.get("funder_ref") == batch["funder_id"]:
        return
    raise forbidden("只能查看本资助方的资金批次")


# ---------------------------------------------------------------- 汇率口径


def create_fx_rate_set(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    """定义汇率口径：名称 + 舍入方式 + 一组有理数汇率。创建后不可变。"""
    require_role(actor, "admin")
    name = _non_empty_str(payload.get("name"), "name")
    rounding = payload.get("rounding")
    if rounding not in ROUNDING_MODES:
        raise bad_request(f"rounding 必须是 {'/'.join(ROUNDING_MODES)}")
    rates = payload.get("rates")
    if not isinstance(rates, list) or not rates:
        raise bad_request("rates 必须是非空列表")
    parsed, seen = [], set()
    for item in rates:
        if not isinstance(item, dict):
            raise bad_request("rates 元素必须是对象")
        base = _currency(item.get("base"), "base")
        quote = _currency(item.get("quote"), "quote")
        if base == quote:
            raise bad_request("同一币种无需汇率")
        num = _pos_int(item.get("num"), "num")
        den = _pos_int(item.get("den"), "den")
        if (base, quote) in seen:
            raise bad_request(f"重复汇率 {base}/{quote}")
        seen.add((base, quote))
        parsed.append((base, quote, num, den))
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO fx_rate_sets (name, rounding, created_at) VALUES (?,?,?)",
            (name, rounding, _now()),
        )
        set_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO fx_rates (set_id, base, quote, num, den) VALUES (?,?,?,?,?)",
            [(set_id, b, q, n, d) for b, q, n, d in parsed],
        )
    return get_fx_rate_set(conn, actor, set_id)


def get_fx_rate_set(conn: sqlite3.Connection, actor: dict, set_id: int) -> dict:
    require_role(actor, "admin", "auditor", "committee", "funder")
    row = conn.execute("SELECT * FROM fx_rate_sets WHERE id=?", (set_id,)).fetchone()
    if row is None:
        raise not_found(f"汇率口径不存在：{set_id}")
    rates = conn.execute(
        "SELECT base, quote, num, den FROM fx_rates WHERE set_id=? ORDER BY base, quote",
        (set_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "rounding": row["rounding"],
        "created_at": row["created_at"],
        "rates": [dict(rate) for rate in rates],
    }


# ---------------------------------------------------------------- 资金批次


def create_batch(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    """开设资金批次。资助方只能开设本资助方批次。"""
    require_role(actor, "admin", "funder")
    name = _non_empty_str(payload.get("name"), "name")
    currency = _currency(payload.get("currency"))
    total = _minor(payload.get("total_amount"), "total_amount")
    fx_set_id = _pos_int(payload.get("fx_rate_set_id"), "fx_rate_set_id")
    if conn.execute("SELECT 1 FROM fx_rate_sets WHERE id=?", (fx_set_id,)).fetchone() is None:
        raise bad_request(f"汇率口径不存在：{fx_set_id}")
    destination = payload.get("residual_destination", "RESERVE")
    if destination not in RESIDUAL_DESTINATIONS:
        raise bad_request(f"residual_destination 必须是 {'/'.join(RESIDUAL_DESTINATIONS)}")
    if actor["role"] == "funder":
        funder_id = actor.get("funder_ref")
    else:
        funder_id = _non_empty_str(payload.get("funder_id"), "funder_id")
    with tx(conn):
        cur = conn.execute(
            """INSERT INTO batches
               (name, funder_id, currency, total_amount, fx_rate_set_id, residual_destination, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (name, funder_id, currency, total, fx_set_id, destination, _now()),
        )
        batch_id = cur.lastrowid
    return _batch_view(conn, _get_batch(conn, batch_id))


def list_batches(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    require_role(actor, "admin", "funder", "auditor")
    if actor["role"] == "funder":
        rows = conn.execute(
            "SELECT * FROM batches WHERE funder_id=? ORDER BY id", (actor.get("funder_ref"),)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    return [_batch_view(conn, row) for row in rows]


def get_batch(conn: sqlite3.Connection, actor: dict, batch_id: int) -> dict:
    require_role(actor, "admin", "funder", "auditor")
    row = _get_batch(conn, batch_id)
    _check_batch_visible(actor, row)
    return _batch_view(conn, row)


# ---------------------------------------------------------------- 限制规则


def _validate_rule_params(rule_type: str, params: object) -> dict:
    if not isinstance(params, dict):
        raise bad_request("params 必须是对象")
    if rule_type == "MAX_AMOUNT_PER_AWARD":
        return {
            "amount": _minor(params.get("amount"), "params.amount"),
            "currency": _currency(params.get("currency"), "params.currency"),
        }
    if rule_type == "MAX_ACTIVE_AWARDS_PER_APPLICANT":
        return {"count": _pos_int(params.get("count"), "params.count")}
    if rule_type == "ROUND_BUDGET_CAP":
        return {"amount": _minor(params.get("amount"), "params.amount")}
    if rule_type == "EXCLUDED_NATIONALITY":
        nationalities = params.get("nationalities")
        if (
            not isinstance(nationalities, list)
            or not nationalities
            or not all(isinstance(n, str) and n for n in nationalities)
        ):
            raise bad_request("params.nationalities 必须是非空字符串列表")
        return {"nationalities": sorted(set(nationalities))}
    if rule_type == "MIN_GPA":
        value = params.get("value")
        try:
            from decimal import Decimal

            Decimal(str(value))
        except Exception as exc:
            raise bad_request("params.value 必须是数值") from exc
        return {"value": str(value)}
    raise bad_request(f"未知规则类型：{rule_type}")


def create_rule(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    require_role(actor, "admin")
    rule_type = payload.get("type")
    if rule_type not in rule_engine.RULE_TYPES:
        raise bad_request(f"type 必须是 {'/'.join(rule_engine.RULE_TYPES)}")
    params = _validate_rule_params(rule_type, payload.get("params"))
    scope_batch_id = payload.get("scope_batch_id")
    if rule_type == "ROUND_BUDGET_CAP" and scope_batch_id is None:
        raise bad_request("ROUND_BUDGET_CAP 必须指定 scope_batch_id")
    if scope_batch_id is not None:
        scope_batch_id = _pos_int(scope_batch_id, "scope_batch_id")
        _get_batch(conn, scope_batch_id)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO rules (type, params, scope_batch_id, created_at) VALUES (?,?,?,?)",
            (rule_type, json.dumps(params, ensure_ascii=False), scope_batch_id, _now()),
        )
        rule_id = cur.lastrowid
    row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
    return _rule_view(row)


def _rule_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "params": json.loads(row["params"]),
        "scope_batch_id": row["scope_batch_id"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
    }


def list_rules(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    require_role(actor, "admin", "auditor", "committee")
    rows = conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
    return [_rule_view(row) for row in rows]


# ---------------------------------------------------------------- 评审轮次


def create_round(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    require_role(actor, "admin")
    name = _non_empty_str(payload.get("name"), "name")
    committee = payload.get("committee")
    if (
        not isinstance(committee, list)
        or not committee
        or not all(isinstance(member, str) and member for member in committee)
    ):
        raise bad_request("committee 必须是非空的评审人列表")
    if len(set(committee)) != len(committee):
        raise bad_request("committee 内评审人不能重复")
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO rounds (name, committee, created_at) VALUES (?,?,?)",
            (name, json.dumps(sorted(committee), ensure_ascii=False), _now()),
        )
        round_id = cur.lastrowid
    return _round_view(_get_round(conn, round_id))


def list_rounds(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    require_role(actor, "admin", "committee", "auditor")
    rows = conn.execute("SELECT * FROM rounds ORDER BY id").fetchall()
    return [_round_view(row) for row in rows]


def get_round(conn: sqlite3.Connection, actor: dict, round_id: int) -> dict:
    require_role(actor, "admin", "committee", "auditor")
    row = _get_round(conn, round_id)
    award_count = conn.execute(
        "SELECT COUNT(*) AS c FROM awards WHERE round_id=?", (round_id,)
    ).fetchone()["c"]
    view = _round_view(row)
    view["occupancy"] = ledger.round_occupancy(conn, round_id)
    view["award_count"] = award_count
    return view


def seal_round(conn: sqlite3.Connection, actor: dict, round_id: int) -> dict:
    """封存轮次：之后不能再在该轮次试算、授予或延期进入。"""
    require_role(actor, "admin")
    with tx(conn):
        row = _get_round(conn, round_id)
        if row["status"] != "OPEN":
            raise conflict("ROUND_NOT_OPEN", "轮次已封存")
        conn.execute("UPDATE rounds SET status='SEALED' WHERE id=?", (round_id,))
    return _round_view(_get_round(conn, round_id))


# ---------------------------------------------------------------- 候选材料


def create_candidate(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    require_role(actor, "admin", "committee")
    applicant_id = _non_empty_str(payload.get("applicant_id"), "applicant_id")
    round_id = _pos_int(payload.get("round_id"), "round_id")
    materials = payload.get("materials")
    if not isinstance(materials, dict):
        raise bad_request("materials 必须是对象")
    with tx(conn):
        round_row = _get_round(conn, round_id)
        if round_row["status"] != "OPEN":
            raise conflict("ROUND_NOT_OPEN", "轮次已封存，不能再登记候选")
        try:
            cur = conn.execute(
                "INSERT INTO candidates (applicant_id, round_id, materials, created_at) VALUES (?,?,?,?)",
                (applicant_id, round_id, json.dumps(materials, ensure_ascii=False), _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise conflict("CANDIDATE_EXISTS", "该申请人在本轮次已登记候选材料") from exc
        candidate_id = cur.lastrowid
    row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    return _candidate_view(row)


def list_candidates(conn: sqlite3.Connection, actor: dict, round_id: int | None) -> list[dict]:
    require_role(actor, "admin", "committee", "auditor")
    if round_id is None:
        rows = conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE round_id=? ORDER BY id", (round_id,)
        ).fetchall()
    return [_candidate_view(row) for row in rows]


def get_candidate(conn: sqlite3.Connection, actor: dict, candidate_id: int) -> dict:
    require_role(actor, "admin", "committee", "auditor")
    row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if row is None:
        raise not_found(f"候选材料不存在：{candidate_id}")
    return _candidate_view(row)


def set_eligibility(conn: sqlite3.Connection, actor: dict, candidate_id: int, payload: dict) -> dict:
    require_role(actor, "admin", "committee")
    status = payload.get("status")
    if status not in ("SUBMITTED", "ELIGIBLE", "INELIGIBLE"):
        raise bad_request("status 必须是 SUBMITTED/ELIGIBLE/INELIGIBLE")
    with tx(conn):
        row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise not_found(f"候选材料不存在：{candidate_id}")
        conn.execute("UPDATE candidates SET status=? WHERE id=?", (status, candidate_id))
    row = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    return _candidate_view(row)


# ---------------------------------------------------------------- 回避关系


def create_recusal(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    require_role(actor, "admin")
    reviewer_id = _non_empty_str(payload.get("reviewer_id"), "reviewer_id")
    applicant_id = _non_empty_str(payload.get("applicant_id"), "applicant_id")
    reason = _non_empty_str(payload.get("reason"), "reason")
    with tx(conn):
        try:
            cur = conn.execute(
                "INSERT INTO recusals (reviewer_id, applicant_id, reason, created_at) VALUES (?,?,?,?)",
                (reviewer_id, applicant_id, reason, _now()),
            )
        except sqlite3.IntegrityError as exc:
            raise conflict("RECUSAL_EXISTS", "该评审人与申请人的回避关系已存在") from exc
        recusal_id = cur.lastrowid
    row = conn.execute("SELECT * FROM recusals WHERE id=?", (recusal_id,)).fetchone()
    return {
        "id": row["id"],
        "reviewer_id": row["reviewer_id"],
        "applicant_id": row["applicant_id"],
        "reason": row["reason"],
        "created_at": row["created_at"],
    }


def list_recusals(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    require_role(actor, "admin", "auditor", "committee")
    rows = conn.execute("SELECT * FROM recusals ORDER BY id").fetchall()
    return [
        {
            "id": row["id"],
            "reviewer_id": row["reviewer_id"],
            "applicant_id": row["applicant_id"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------- 授予明细构造


def _build_line(conn: sqlite3.Connection, round_row: sqlite3.Row, item: dict) -> dict:
    """把一条授予意向换算成批次币种锁定额，并收集规则评估所需上下文。"""
    if not isinstance(item, dict):
        raise bad_request("授予明细必须是对象")
    candidate_id = _pos_int(item.get("candidate_id"), "candidate_id")
    candidate = conn.execute(
        "SELECT * FROM candidates WHERE id=?", (candidate_id,)
    ).fetchone()
    if candidate is None:
        raise not_found(f"候选材料不存在：{candidate_id}")
    if candidate["round_id"] != round_row["id"]:
        raise unprocessable(
            "CANDIDATE_ROUND_MISMATCH", f"候选 {candidate_id} 不属于轮次 {round_row['id']}"
        )
    if candidate["status"] == "INELIGIBLE":
        raise unprocessable("CANDIDATE_INELIGIBLE", f"候选 {candidate_id} 已标记为不合格")
    batch = _get_batch(conn, _pos_int(item.get("batch_id"), "batch_id"))
    if batch["status"] != "OPEN":
        raise conflict("BATCH_NOT_OPEN", f"资金批次 {batch['id']} 已关闭")
    amount = _minor(item.get("award_amount"), "award_amount", minimum=1)
    award_currency = _currency(item.get("award_currency"), "award_currency")
    fx_set = conn.execute(
        "SELECT * FROM fx_rate_sets WHERE id=?", (batch["fx_rate_set_id"],)
    ).fetchone()
    rate = None
    if award_currency == batch["currency"]:
        locked = amount
    else:
        rate = conn.execute(
            "SELECT * FROM fx_rates WHERE set_id=? AND base=? AND quote=?",
            (batch["fx_rate_set_id"], award_currency, batch["currency"]),
        ).fetchone()
        if rate is None:
            raise unprocessable(
                "NO_FX_RATE",
                f"汇率口径 {batch['fx_rate_set_id']} 缺少 "
                f"{award_currency}→{batch['currency']} 汇率",
            )
        locked = convert_minor(amount, rate["num"], rate["den"], fx_set["rounding"])
        if locked <= 0:
            raise bad_request("换算后锁定额为零，无法授予")
    return {
        "candidate": candidate,
        "applicant_id": candidate["applicant_id"],
        "materials": json.loads(candidate["materials"]),
        "batch": batch,
        "fx_set": fx_set,
        "rate": rate,
        "award_amount": amount,
        "award_currency": award_currency,
        "locked_amount": locked,
    }


def _line_view(line: dict) -> dict:
    rate = line["rate"]
    return {
        "candidate_id": line["candidate"]["id"],
        "applicant_id": line["applicant_id"],
        "batch_id": line["batch"]["id"],
        "award_amount": line["award_amount"],
        "award_currency": line["award_currency"],
        "locked_amount": line["locked_amount"],
        "batch_currency": line["batch"]["currency"],
        "rate": (
            {"base": rate["base"], "quote": rate["quote"], "num": rate["num"], "den": rate["den"]}
            if rate is not None
            else None
        ),
    }


# ---------------------------------------------------------------- 方案试算


def create_trial(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    """方案试算：与正式授予同一套换算与规则判定，但不锁定任何额度。"""
    require_role(actor, "admin", "committee")
    round_id = _pos_int(payload.get("round_id"), "round_id")
    round_row = _get_round(conn, round_id)
    if round_row["status"] != "OPEN":
        raise conflict("ROUND_NOT_OPEN", "轮次已封存，不能再试算")
    items = payload.get("lines")
    if not isinstance(items, list) or not items:
        raise bad_request("lines 必须是非空列表")
    lines = [_build_line(conn, round_row, item) for item in items]
    violations = rule_engine.evaluate(conn, lines=lines, round_id=round_id)
    projection = []
    by_batch: dict[int, int] = {}
    for line in lines:
        by_batch[line["batch"]["id"]] = by_batch.get(line["batch"]["id"], 0) + line["locked_amount"]
    for batch_id, incoming in sorted(by_batch.items()):
        totals = ledger.batch_totals(conn, batch_id)
        batch = _get_batch(conn, batch_id)
        projection.append(
            {
                "batch_id": batch_id,
                "currency": batch["currency"],
                "available_now": totals["available"],
                "locked_by_trial": incoming,
                "available_after": totals["available"] - incoming,
                "sufficient": totals["available"] - incoming >= 0,
            }
        )
    result = {
        "lines": [_line_view(line) for line in lines],
        "violations": violations,
        "projection": projection,
    }
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO trial_runs (round_id, lines, result, created_by, created_at) VALUES (?,?,?,?,?)",
            (
                round_id,
                json.dumps(items, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                actor["username"],
                _now(),
            ),
        )
        trial_id = cur.lastrowid
    return {"id": trial_id, "round_id": round_id, **result}


def get_trial(conn: sqlite3.Connection, actor: dict, trial_id: int) -> dict:
    require_role(actor, "admin", "committee", "auditor")
    row = conn.execute("SELECT * FROM trial_runs WHERE id=?", (trial_id,)).fetchone()
    if row is None:
        raise not_found(f"试算方案不存在：{trial_id}")
    return {
        "id": row["id"],
        "round_id": row["round_id"],
        "lines": json.loads(row["lines"]),
        "result": json.loads(row["result"]),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 正式授予


def create_award(conn: sqlite3.Connection, actor: dict, payload: dict) -> tuple[dict, bool]:
    """正式授予：单事务内完成校验、写入与额度锁定，并封存评审快照。

    返回 (授予视图, 是否新建)；request_id 重试时返回既有授予。
    """
    require_role(actor, "admin")
    request_id = payload.get("request_id")
    if request_id is not None:
        request_id = _non_empty_str(request_id, "request_id")
        existing = conn.execute(
            "SELECT * FROM awards WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing is not None:
            return _award_view(existing), False
    round_id = _pos_int(payload.get("round_id"), "round_id")
    panel = payload.get("panel")
    if (
        not isinstance(panel, list)
        or not panel
        or not all(isinstance(member, str) and member for member in panel)
    ):
        raise bad_request("panel 必须是非空的授奖评审人列表")
    try:
        with tx(conn):
            round_row = _get_round(conn, round_id)
            if round_row["status"] != "OPEN":
                raise conflict("ROUND_NOT_OPEN", "轮次已封存，不能再授予")
            committee = set(json.loads(round_row["committee"]))
            if not set(panel) <= committee:
                raise unprocessable("PANEL_NOT_IN_COMMITTEE", "授奖评审人必须来自轮次评审组")
            line = _build_line(conn, round_row, payload)
            recused = conn.execute(
                "SELECT reviewer_id FROM recusals WHERE applicant_id=? AND reviewer_id IN (%s)"
                % ",".join("?" * len(set(panel))),
                (line["applicant_id"], *sorted(set(panel))),
            ).fetchall()
            if recused:
                names = "、".join(row["reviewer_id"] for row in recused)
                raise unprocessable(
                    "RECUSAL_CONFLICT",
                    f"评审人 {names} 与申请人 {line['applicant_id']} 存在回避关系",
                )
            duplicate = conn.execute(
                "SELECT id FROM awards WHERE applicant_id=? AND batch_id=? AND status='ACTIVE'",
                (line["applicant_id"], line["batch"]["id"]),
            ).fetchone()
            if duplicate is not None:
                raise conflict(
                    "AWARD_CONFLICT",
                    f"申请人 {line['applicant_id']} 已持有批次 {line['batch']['id']} 的有效授予"
                    f"（授予编号 {duplicate['id']}）",
                )
            violations = rule_engine.evaluate(conn, lines=[line], round_id=round_id)
            if violations:
                raise unprocessable("RULE_VIOLATION", "违反限制规则", violations)
            totals = ledger.batch_totals(conn, line["batch"]["id"])
            if line["locked_amount"] > totals["available"]:
                raise conflict(
                    "INSUFFICIENT_FUNDS",
                    f"批次可用额度 {totals['available']} 不足以锁定 {line['locked_amount']}",
                )
            now = _now()
            snapshot = _build_snapshot(conn, round_row, panel, line, now)
            cur = conn.execute(
                """INSERT INTO awards
                   (request_id, sealed_round_id, round_id, applicant_id, candidate_id,
                    batch_id, award_amount, award_currency, locked_amount, snapshot, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    round_id,
                    round_id,
                    line["applicant_id"],
                    line["candidate"]["id"],
                    line["batch"]["id"],
                    line["award_amount"],
                    line["award_currency"],
                    line["locked_amount"],
                    json.dumps(snapshot, ensure_ascii=False),
                    now,
                ),
            )
            award_id = cur.lastrowid
            ledger.post(
                conn,
                batch_id=line["batch"]["id"],
                type="LOCK",
                amount=line["locked_amount"],
                currency=line["batch"]["currency"],
                award_id=award_id,
                round_id=round_id,
                note="正式授予原子锁定",
                created_at=now,
            )
    except sqlite3.IntegrityError as exc:
        # 唯一索引兜底：并发重复授予或 request_id 撞车。
        if request_id and "request_id" in str(exc):
            existing = conn.execute(
                "SELECT * FROM awards WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                return _award_view(existing), False
        raise conflict(
            "AWARD_CONFLICT", "同一申请人已持有该来源额度的有效授予"
        ) from exc
    row = conn.execute("SELECT * FROM awards WHERE id=?", (award_id,)).fetchone()
    return _award_view(row), True


def _build_snapshot(conn, round_row, panel, line, now) -> dict:
    """封存评审快照：材料摘要、评审组、汇率口径与规则判定，事后不可变。"""
    materials_json = json.dumps(
        line["materials"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    rate = line["rate"]
    active_rules = conn.execute(
        "SELECT id, type FROM rules WHERE active=1 ORDER BY id"
    ).fetchall()
    return {
        "sealed_at": now,
        "round": {
            "id": round_row["id"],
            "name": round_row["name"],
            "committee": sorted(json.loads(round_row["committee"])),
        },
        "panel": sorted(set(panel)),
        "applicant_id": line["applicant_id"],
        "candidate_id": line["candidate"]["id"],
        "materials_sha256": hashlib.sha256(materials_json.encode("utf-8")).hexdigest(),
        "batch": {
            "id": line["batch"]["id"],
            "currency": line["batch"]["currency"],
            "fx_rate_set_id": line["batch"]["fx_rate_set_id"],
        },
        "fx": {
            "set_id": line["fx_set"]["id"],
            "rounding": line["fx_set"]["rounding"],
            "rate": (
                {
                    "base": rate["base"],
                    "quote": rate["quote"],
                    "num": rate["num"],
                    "den": rate["den"],
                }
                if rate is not None
                else None
            ),
        },
        "award_amount": line["award_amount"],
        "award_currency": line["award_currency"],
        "locked_amount": line["locked_amount"],
        "rules": [{"rule_id": r["id"], "type": r["type"], "passed": True} for r in active_rules],
    }


def get_award(conn: sqlite3.Connection, actor: dict, award_id: int) -> dict:
    require_role(actor, "admin", "auditor")
    row = conn.execute("SELECT * FROM awards WHERE id=?", (award_id,)).fetchone()
    if row is None:
        raise not_found(f"授予不存在：{award_id}")
    return _award_view(row)


def list_awards(
    conn: sqlite3.Connection,
    actor: dict,
    round_id: int | None = None,
    applicant_id: str | None = None,
) -> list[dict]:
    require_role(actor, "admin", "auditor")
    sql = "SELECT * FROM awards"
    clauses, args = [], []
    if round_id is not None:
        clauses.append("round_id=?")
        args.append(round_id)
    if applicant_id is not None:
        clauses.append("applicant_id=?")
        args.append(applicant_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [_award_view(row) for row in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- 调整与结算


def adjust_award(conn: sqlite3.Connection, actor: dict, award_id: int, payload: dict) -> dict:
    """放弃 / 资格丧失 / 延期：全部通过分录结算，账本可追溯。"""
    require_role(actor, "admin")
    kind = payload.get("type")
    if kind not in ADJUSTMENT_TYPES:
        raise bad_request(f"type 必须是 {'/'.join(ADJUSTMENT_TYPES)}")
    now = _now()
    with tx(conn):
        award = conn.execute("SELECT * FROM awards WHERE id=?", (award_id,)).fetchone()
        if award is None:
            raise not_found(f"授予不存在：{award_id}")
        if award["status"] != "ACTIVE":
            raise conflict("AWARD_NOT_ACTIVE", "只有有效授予可以调整")
        batch = _get_batch(conn, award["batch_id"])
        if kind == "DEFERRAL":
            target_id = _pos_int(payload.get("target_round_id"), "target_round_id")
            if target_id == award["round_id"]:
                raise bad_request("延期目标轮次与当前轮次相同")
            target_round = _get_round(conn, target_id)
            if target_round["status"] != "OPEN":
                raise conflict("ROUND_NOT_OPEN", "目标轮次已封存")
            params = {"target_round_id": target_id}
            reason = payload.get("reason")
            cur = conn.execute(
                "INSERT INTO adjustments (type, award_id, reason, params, created_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (kind, award_id, reason, json.dumps(params), actor["username"], now),
            )
            adjustment_id = cur.lastrowid
            common = dict(
                batch_id=award["batch_id"],
                amount=award["locked_amount"],
                currency=batch["currency"],
                award_id=award_id,
                adjustment_id=adjustment_id,
                created_at=now,
            )
            ledger.post(
                conn, type="RELEASE", round_id=award["round_id"], note="延期释放原轮次占用", **common
            )
            ledger.post(conn, type="LOCK", round_id=target_id, note="延期锁定新轮次占用", **common)
            conn.execute("UPDATE awards SET round_id=? WHERE id=?", (target_id, award_id))
        else:
            reason = _non_empty_str(payload.get("reason"), "reason")
            retain = _minor(payload.get("retain_amount", 0), "retain_amount")
            if retain > award["locked_amount"]:
                raise bad_request("扣留金额不能超过锁定额度")
            new_status = "WITHDRAWN" if kind == "WITHDRAWAL" else "DISQUALIFIED"
            params = {"retain_amount": retain}
            cur = conn.execute(
                "INSERT INTO adjustments (type, award_id, reason, params, created_by, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (kind, award_id, reason, json.dumps(params), actor["username"], now),
            )
            adjustment_id = cur.lastrowid
            ledger.post(
                conn,
                batch_id=award["batch_id"],
                type="RELEASE",
                amount=award["locked_amount"],
                currency=batch["currency"],
                award_id=award_id,
                adjustment_id=adjustment_id,
                round_id=award["round_id"],
                note="放弃结算释放" if kind == "WITHDRAWAL" else "资格丧失结算释放",
                created_at=now,
            )
            if retain:
                ledger.post(
                    conn,
                    batch_id=award["batch_id"],
                    type="RESIDUAL",
                    amount=retain,
                    currency=batch["currency"],
                    award_id=award_id,
                    adjustment_id=adjustment_id,
                    round_id=award["round_id"],
                    note=f"结算尾差/扣留，固定去向：{batch['residual_destination']}",
                    created_at=now,
                )
            conn.execute("UPDATE awards SET status=? WHERE id=?", (new_status, award_id))
    award = conn.execute("SELECT * FROM awards WHERE id=?", (award_id,)).fetchone()
    adjustment = conn.execute(
        "SELECT * FROM adjustments WHERE id=?", (adjustment_id,)
    ).fetchone()
    return {"award": _award_view(award), "adjustment": _adjustment_view(adjustment)}


def transfer(conn: sqlite3.Connection, actor: dict, payload: dict) -> dict:
    """资金转移：批次间划转可用额度，跨币种按源批次绑定的汇率口径换算。"""
    require_role(actor, "admin")
    from_id = _pos_int(payload.get("from_batch_id"), "from_batch_id")
    to_id = _pos_int(payload.get("to_batch_id"), "to_batch_id")
    if from_id == to_id:
        raise bad_request("转出与转入批次不能相同")
    amount = _minor(payload.get("amount"), "amount", minimum=1)
    currency = _currency(payload.get("currency"))
    now = _now()
    with tx(conn):
        source = _get_batch(conn, from_id)
        target = _get_batch(conn, to_id)
        for batch in (source, target):
            if batch["status"] != "OPEN":
                raise conflict("BATCH_NOT_OPEN", f"资金批次 {batch['id']} 已关闭")
        if currency not in (source["currency"], target["currency"]):
            raise bad_request("currency 必须是转出或转入批次的币种")
        rate_used = None
        if source["currency"] == target["currency"]:
            out_amount = in_amount = amount
        else:
            fx_set = conn.execute(
                "SELECT * FROM fx_rate_sets WHERE id=?", (source["fx_rate_set_id"],)
            ).fetchone()
            if currency == source["currency"]:
                base, quote = source["currency"], target["currency"]
                out_amount = amount
            else:
                base, quote = target["currency"], source["currency"]
                in_amount = amount
            rate = conn.execute(
                "SELECT * FROM fx_rates WHERE set_id=? AND base=? AND quote=?",
                (source["fx_rate_set_id"], base, quote),
            ).fetchone()
            if rate is None:
                raise unprocessable(
                    "NO_FX_RATE", f"源批次汇率口径缺少 {base}→{quote} 汇率"
                )
            converted = convert_minor(amount, rate["num"], rate["den"], fx_set["rounding"])
            if currency == source["currency"]:
                in_amount = converted
            else:
                out_amount = converted
            rate_used = {"base": base, "quote": quote, "num": rate["num"], "den": rate["den"]}
        totals = ledger.batch_totals(conn, from_id)
        if out_amount > totals["available"]:
            raise conflict(
                "INSUFFICIENT_FUNDS",
                f"批次可用额度 {totals['available']} 不足以转出 {out_amount}",
            )
        params = {
            "from_batch_id": from_id,
            "to_batch_id": to_id,
            "amount": amount,
            "currency": currency,
            "out_amount": out_amount,
            "in_amount": in_amount,
            "rate": rate_used,
        }
        cur = conn.execute(
            "INSERT INTO adjustments (type, award_id, reason, params, created_by, created_at)"
            " VALUES ('TRANSFER', NULL, ?, ?, ?, ?)",
            (payload.get("reason"), json.dumps(params), actor["username"], now),
        )
        adjustment_id = cur.lastrowid
        ledger.post(
            conn,
            batch_id=from_id,
            type="TRANSFER_OUT",
            amount=out_amount,
            currency=source["currency"],
            adjustment_id=adjustment_id,
            note=f"转移至批次 {to_id}",
            created_at=now,
        )
        ledger.post(
            conn,
            batch_id=to_id,
            type="TRANSFER_IN",
            amount=in_amount,
            currency=target["currency"],
            adjustment_id=adjustment_id,
            note=f"来自批次 {from_id}",
            created_at=now,
        )
    return {
        "adjustment_id": adjustment_id,
        "from_batch_id": from_id,
        "to_batch_id": to_id,
        "out_amount": out_amount,
        "out_currency": source["currency"],
        "in_amount": in_amount,
        "in_currency": target["currency"],
        "rate": rate_used,
    }


def list_adjustments(conn: sqlite3.Connection, actor: dict, award_id: int) -> list[dict]:
    require_role(actor, "admin", "auditor")
    rows = conn.execute(
        "SELECT * FROM adjustments WHERE award_id=? ORDER BY id", (award_id,)
    ).fetchall()
    return [_adjustment_view(row) for row in rows]


# ---------------------------------------------------------------- 申请人自查


def my_awards(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    """申请人只能查询自己的授予结果。"""
    require_role(actor, "applicant")
    rows = conn.execute(
        """SELECT a.*, b.name AS batch_name, r.name AS round_name
           FROM awards a
           JOIN batches b ON b.id = a.batch_id
           JOIN rounds r ON r.id = a.round_id
           WHERE a.applicant_id=? ORDER BY a.id""",
        (actor.get("applicant_ref"),),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "round_id": row["round_id"],
            "round_name": row["round_name"],
            "batch_id": row["batch_id"],
            "batch_name": row["batch_name"],
            "award_amount": row["award_amount"],
            "award_currency": row["award_currency"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def my_candidates(conn: sqlite3.Connection, actor: dict) -> list[dict]:
    """申请人只能查询自己的候选材料与状态。"""
    require_role(actor, "applicant")
    rows = conn.execute(
        "SELECT * FROM candidates WHERE applicant_id=? ORDER BY id",
        (actor.get("applicant_ref"),),
    ).fetchall()
    return [_candidate_view(row) for row in rows]


# ---------------------------------------------------------------- 审计复算


def audit_batch_ledger(conn: sqlite3.Connection, actor: dict, batch_id: int) -> dict:
    """批次台账：逐笔分录 + 每笔之后的锁定/可用余额，可逐笔复算。"""
    require_role(actor, "admin", "auditor")
    batch = _get_batch(conn, batch_id)
    to_reserve = batch["residual_destination"] == "RESERVE"
    running_locked = 0
    running_available = batch["total_amount"]
    items = []
    for entry in ledger.entries_for_batch(conn, batch_id):
        amount = entry["amount"]
        if entry["type"] == "LOCK":
            running_locked += amount
            running_available -= amount
        elif entry["type"] == "RELEASE":
            running_locked -= amount
            running_available += amount
        elif entry["type"] == "TRANSFER_OUT":
            running_available -= amount
        elif entry["type"] == "TRANSFER_IN":
            running_available += amount
        elif entry["type"] == "RESIDUAL" and to_reserve:
            running_available -= amount
        items.append(
            {
                "id": entry["id"],
                "type": entry["type"],
                "amount": amount,
                "currency": entry["currency"],
                "award_id": entry["award_id"],
                "adjustment_id": entry["adjustment_id"],
                "round_id": entry["round_id"],
                "note": entry["note"],
                "created_at": entry["created_at"],
                "locked_after": running_locked,
                "available_after": running_available,
            }
        )
    return {
        "batch": _batch_view(conn, batch),
        "entries": items,
    }


def audit_batch_reconcile(conn: sqlite3.Connection, actor: dict, batch_id: int) -> dict:
    """批次对账：分录派生余额与授予记录交叉核对，并回放检查可用不为负。"""
    require_role(actor, "admin", "auditor")
    batch = _get_batch(conn, batch_id)
    totals = ledger.batch_totals(conn, batch_id)
    locked_per_awards = conn.execute(
        "SELECT COALESCE(SUM(locked_amount),0) AS s FROM awards WHERE batch_id=? AND status='ACTIVE'",
        (batch_id,),
    ).fetchone()["s"]
    to_reserve = batch["residual_destination"] == "RESERVE"
    running_available = batch["total_amount"]
    available_never_negative = True
    for entry in ledger.entries_for_batch(conn, batch_id):
        amount = entry["amount"]
        if entry["type"] in ("LOCK", "TRANSFER_OUT"):
            running_available -= amount
        elif entry["type"] in ("RELEASE", "TRANSFER_IN"):
            running_available += amount
        elif entry["type"] == "RESIDUAL" and to_reserve:
            running_available -= amount
        if running_available < 0:
            available_never_negative = False
    locked_matches = totals["locked"] == locked_per_awards
    return {
        "batch_id": batch_id,
        "currency": batch["currency"],
        "totals": totals,
        "locked_per_ledger": totals["locked"],
        "locked_per_awards": locked_per_awards,
        "locked_matches": locked_matches,
        "available_never_negative": available_never_negative,
        "balanced": locked_matches and available_never_negative and totals["available"] >= 0,
    }


def audit_round_recompute(conn: sqlite3.Connection, actor: dict, round_id: int) -> dict:
    """轮次复算：用封存快照重算应锁定额，与该轮次分录净额逐笔核对。"""
    require_role(actor, "admin", "auditor")
    round_row = _get_round(conn, round_id)
    entries = conn.execute(
        "SELECT * FROM ledger_entries WHERE round_id=? ORDER BY id", (round_id,)
    ).fetchall()
    net_by_award: dict[int, int] = {}
    for entry in entries:
        if entry["award_id"] is None or entry["type"] not in ("LOCK", "RELEASE"):
            continue
        delta = entry["amount"] if entry["type"] == "LOCK" else -entry["amount"]
        net_by_award[entry["award_id"]] = net_by_award.get(entry["award_id"], 0) + delta
    awards = conn.execute(
        """SELECT * FROM awards
           WHERE sealed_round_id=? OR round_id=? OR id IN (
               SELECT DISTINCT award_id FROM ledger_entries
               WHERE round_id=? AND award_id IS NOT NULL)
           ORDER BY id""",
        (round_id, round_id, round_id),
    ).fetchall()
    items = []
    consistent = True
    for award in awards:
        snapshot = json.loads(award["snapshot"])
        rate = snapshot["fx"]["rate"]
        if rate is None:
            expected = snapshot["award_amount"]
        else:
            expected = convert_minor(
                snapshot["award_amount"], rate["num"], rate["den"], snapshot["fx"]["rounding"]
            )
        snapshot_ok = expected == award["locked_amount"]
        net = net_by_award.get(award["id"], 0)
        if award["status"] == "ACTIVE" and award["round_id"] == round_id:
            entries_ok = net == award["locked_amount"]
        else:
            entries_ok = net == 0
        ok = snapshot_ok and entries_ok
        consistent = consistent and ok
        items.append(
            {
                "award_id": award["id"],
                "applicant_id": award["applicant_id"],
                "status": award["status"],
                "current_round_id": award["round_id"],
                "expected_locked": expected,
                "stored_locked": award["locked_amount"],
                "net_entries_in_round": net,
                "snapshot_consistent": snapshot_ok,
                "entries_consistent": entries_ok,
                "consistent": ok,
            }
        )
    return {
        "round_id": round_id,
        "round_status": round_row["status"],
        "awards": items,
        "batch_occupancy": ledger.round_occupancy(conn, round_id),
        "consistent": consistent,
    }
