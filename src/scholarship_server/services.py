"""奖学金配置核心业务服务。

关键设计：
- 额度锁定：授予在单个 IMMEDIATE 事务内完成占用校验、唯一性兜底、
  快照封存与分录入账，两个评审组并发授予同一来源额度时只有一笔成功。
- 汇率口径：每个轮次锁定一套汇率（含反向），轮内所有换算只用该口径。
- 尾差去向：换算尾差以有理数精确记入对应币种的尾差归集批次。
- 分录结算：放弃、资格丧失、延期、资金转移全部以账本分录记录，
  任何批次的占用都可由分录重放复算。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction

from .errors import ApiError
from .money import fx_convert, to_json_amount

RULE_TYPES = {
    "MIN_GPA",
    "DEGREE_LEVEL",
    "NATIONALITY_DENY",
    "MAX_TOTAL_PER_APPLICANT",
    "MAX_AWARDS_PER_APPLICANT",
}

RULE_REQUIRED_PARAMS = {
    "MIN_GPA": ("min",),
    "DEGREE_LEVEL": ("allow",),
    "NATIONALITY_DENY": ("deny",),
    "MAX_TOTAL_PER_APPLICANT": ("currency", "amount_minor"),
    "MAX_AWARDS_PER_APPLICANT": ("count",),
}

SETTLE_ACTIONS = {"withdraw", "forfeit", "defer"}


# ---------- 基础工具 ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_role(actor: sqlite3.Row, *roles: str) -> None:
    if actor is None or actor["role"] not in roles:
        raise ApiError(403, "FORBIDDEN", "当前角色无权执行该操作")


def require_fields(body: dict, *fields: str) -> list:
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise ApiError(400, "BAD_REQUEST", "缺少字段：" + "、".join(missing))
    return [body[f] for f in fields]


def require_positive_int(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ApiError(400, "BAD_REQUEST", f"{name} 必须是正整数（最小货币单位）")
    return value


def norm_currency(value, name: str = "币种") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "BAD_REQUEST", f"{name}不能为空")
    return value.strip().upper()


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """IMMEDIATE 事务：进入即持有写锁，事务内读到的即是可提交的真相。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")


def insert_ledger(conn, *, batch_id, entry_type, amount_num, currency, actor_id,
                  amount_den=1, round_id=None, award_id=None, transfer_id=None, note=""):
    conn.execute(
        "INSERT INTO ledger (batch_id, round_id, award_id, transfer_id, type,"
        " amount_num, amount_den, currency, actor_id, note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, round_id, award_id, transfer_id, entry_type,
         amount_num, amount_den, currency, actor_id, note, now_iso()),
    )


# ---------- 通用读取 ----------

def get_batch_row(conn, batch_id):
    row = conn.execute("SELECT * FROM fund_batches WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"资金批次不存在：{batch_id}")
    return row


def get_round(conn, round_id):
    row = conn.execute("SELECT * FROM rounds WHERE id=?", (round_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"轮次不存在：{round_id}")
    return row


def get_award_row(conn, award_id):
    row = conn.execute("SELECT * FROM awards WHERE id=?", (award_id,)).fetchone()
    if row is None:
        raise ApiError(404, "NOT_FOUND", f"授予记录不存在：{award_id}")
    return row


AWARD_ENTRY_TYPES = ("LOCK", "RELEASE", "FORFEIT", "DEFER_RELEASE", "DEFER_LOCK")
BALANCE_ENTRY_TYPES = ("TRANSFER_OUT", "TRANSFER_IN", "ROUNDING")


def locked_amount(conn, batch_id) -> Fraction:
    """授予占用：LOCK/DEFER_LOCK 增加，RELEASE/FORFEIT/DEFER_RELEASE 冲减。"""
    total = Fraction(0)
    for row in conn.execute(
            "SELECT amount_num, amount_den FROM ledger WHERE batch_id=?"
            " AND type IN ('LOCK','RELEASE','FORFEIT','DEFER_RELEASE','DEFER_LOCK')",
            (batch_id,)):
        total += Fraction(row["amount_num"], row["amount_den"])
    return total


def balance_amount(conn, batch_id) -> Fraction:
    """批次余额：总额 ± 转移分录；尾差归集批次即尾差分录之和。"""
    row = conn.execute(
        "SELECT total_minor FROM fund_batches WHERE id=?", (batch_id,)).fetchone()
    total = Fraction(row["total_minor"])
    for e in conn.execute(
            "SELECT amount_num, amount_den FROM ledger WHERE batch_id=?"
            " AND type IN ('TRANSFER_OUT','TRANSFER_IN','ROUNDING')",
            (batch_id,)):
        total += Fraction(e["amount_num"], e["amount_den"])
    return total


def available_amount(conn, batch_id) -> Fraction:
    """可用额度 = 批次余额 - 授予占用。"""
    return balance_amount(conn, batch_id) - locked_amount(conn, batch_id)


def round_rate(conn, round_id, from_ccy: str, to_ccy: str) -> tuple[int, int]:
    """取轮次锁定的汇率口径：1 from = num/den to。"""
    if from_ccy == to_ccy:
        return 1, 1
    row = conn.execute(
        "SELECT num, den FROM round_rates WHERE round_id=? AND base_currency=? AND quote_currency=?",
        (round_id, from_ccy, to_ccy)).fetchone()
    if row is None:
        raise ApiError(422, "RATE_MISSING", f"轮次缺少汇率口径 {from_ccy}->{to_ccy}")
    return row["num"], row["den"]


def residual_batch_id(conn, currency: str) -> str:
    """尾差固定去向：对应币种的归集批次，不存在则由系统建立。"""
    row = conn.execute(
        "SELECT id FROM fund_batches WHERE currency=? AND is_residual_destination=1",
        (currency,)).fetchone()
    if row:
        return row["id"]
    batch_id = f"residual-{currency.lower()}"
    try:
        conn.execute(
            "INSERT INTO fund_batches (id, funder, name, currency, total_minor,"
            " is_residual_destination, created_at) VALUES (?,?,?,?,0,1,?)",
            (batch_id, "SYSTEM", f"{currency} 尾差归集", currency, now_iso()))
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id FROM fund_batches WHERE currency=? AND is_residual_destination=1",
            (currency,)).fetchone()
        if row:
            return row["id"]
        batch_id = new_id("residual")
        conn.execute(
            "INSERT INTO fund_batches (id, funder, name, currency, total_minor,"
            " is_residual_destination, created_at) VALUES (?,?,?,?,0,1,?)",
            (batch_id, "SYSTEM", f"{currency} 尾差归集", currency, now_iso()))
    return batch_id


def latest_materials(conn, applicant_id):
    return conn.execute(
        "SELECT * FROM materials WHERE applicant_id=? ORDER BY version DESC LIMIT 1",
        (applicant_id,)).fetchone()


# ---------- 视图 ----------

def batch_view(conn, row) -> dict:
    locked = locked_amount(conn, row["id"])
    balance = balance_amount(conn, row["id"])
    view = {
        "id": row["id"],
        "funder": row["funder"],
        "name": row["name"],
        "currency": row["currency"],
        "total_minor": row["total_minor"],
        "is_residual_destination": bool(row["is_residual_destination"]),
        "locked": to_json_amount(locked),
        "balance": to_json_amount(balance),
        "created_at": row["created_at"],
    }
    if row["is_residual_destination"]:
        view["residual_balance"] = to_json_amount(balance)
    else:
        view["available"] = to_json_amount(balance - locked)
    return view


def round_view(conn, row) -> dict:
    rates = [dict(r) for r in conn.execute(
        "SELECT base_currency, quote_currency, num, den FROM round_rates"
        " WHERE round_id=? ORDER BY base_currency, quote_currency", (row["id"],))]
    return {
        "id": row["id"],
        "name": row["name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "sealed_at": row["sealed_at"],
        "pinned_rates": rates,
    }


def award_view(conn, row) -> dict:
    snap = conn.execute(
        "SELECT content_hash FROM snapshots WHERE award_id=?", (row["id"],)).fetchone()
    return {
        "id": row["id"],
        "round_id": row["round_id"],
        "panel_id": row["panel_id"],
        "applicant_id": row["applicant_id"],
        "batch_id": row["batch_id"],
        "award_currency": row["award_currency"],
        "award_amount_minor": row["award_amount_minor"],
        "lock_amount_minor": row["lock_amount_minor"],
        "rate": {"num": row["rate_num"], "den": row["rate_den"]},
        "status": row["status"],
        "deferred_to_round_id": row["deferred_to_round_id"],
        "snapshot_id": row["snapshot_id"],
        "snapshot_hash": snap["content_hash"] if snap else None,
        "created_at": row["created_at"],
    }


def materials_view(row) -> dict:
    return {
        "applicant_id": row["applicant_id"],
        "version": row["version"],
        "payload": json.loads(row["payload"]),
        "content_hash": row["content_hash"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "verified_by": row["verified_by"],
    }


# ---------- 资金批次 ----------

def create_batch(conn, actor, body):
    require_role(actor, "office")
    funder, name = require_fields(body, "funder", "name")
    currency = norm_currency(body.get("currency"))
    total = body.get("total_minor", 0)
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ApiError(400, "BAD_REQUEST", "total_minor 必须是非负整数")
    is_residual = 1 if body.get("is_residual_destination") else 0
    if is_residual and total != 0:
        raise ApiError(400, "BAD_REQUEST", "尾差归集批次的 total_minor 必须为 0")
    batch_id = body.get("id") or new_id("bat")
    try:
        conn.execute(
            "INSERT INTO fund_batches (id, funder, name, currency, total_minor,"
            " is_residual_destination, created_at) VALUES (?,?,?,?,?,?,?)",
            (batch_id, funder, name, currency, total, is_residual, now_iso()))
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "BATCH_CONFLICT", "批次编号重复，或该币种已存在尾差归集批次") from exc
    return 201, batch_view(conn, get_batch_row(conn, batch_id))


def list_batches(conn, actor):
    require_role(actor, "office", "auditor", "panel")
    rows = conn.execute("SELECT * FROM fund_batches ORDER BY created_at, id").fetchall()
    return 200, {"batches": [batch_view(conn, r) for r in rows]}


def get_batch(conn, actor, batch_id):
    require_role(actor, "office", "auditor", "panel")
    return 200, batch_view(conn, get_batch_row(conn, batch_id))


# ---------- 汇率口径 ----------

def create_exchange_rate(conn, actor, body):
    require_role(actor, "office")
    base = norm_currency(body.get("base_currency"), "base_currency")
    quote = norm_currency(body.get("quote_currency"), "quote_currency")
    if base == quote:
        raise ApiError(400, "BAD_REQUEST", "同一币种无需汇率")
    num = require_positive_int(body.get("num"), "num")
    den = require_positive_int(body.get("den"), "den")
    effective_date = body.get("effective_date") or now_iso()[:10]
    rate_id = body.get("id") or new_id("fx")
    try:
        conn.execute(
            "INSERT INTO exchange_rates (id, base_currency, quote_currency, num, den,"
            " effective_date, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rate_id, base, quote, num, den, effective_date,
             body.get("note", ""), now_iso()))
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "RATE_CONFLICT", "汇率定义编号重复") from exc
    return 201, {"id": rate_id, "base_currency": base, "quote_currency": quote,
                 "num": num, "den": den, "effective_date": effective_date}


def list_exchange_rates(conn, actor):
    require_role(actor, "office", "auditor", "panel")
    rows = conn.execute(
        "SELECT * FROM exchange_rates ORDER BY created_at, id").fetchall()
    return 200, {"exchange_rates": [dict(r) for r in rows]}


# ---------- 评审轮次 ----------

def create_round(conn, actor, body):
    require_role(actor, "office")
    name, = require_fields(body, "name")
    rates = list(body.get("rates") or [])
    for rate_id in body.get("rate_ids") or []:
        row = conn.execute(
            "SELECT * FROM exchange_rates WHERE id=?", (rate_id,)).fetchone()
        if row is None:
            raise ApiError(404, "NOT_FOUND", f"汇率定义不存在：{rate_id}")
        rates.append({"base_currency": row["base_currency"],
                      "quote_currency": row["quote_currency"],
                      "num": row["num"], "den": row["den"]})
    round_id = body.get("id") or new_id("rnd")
    with immediate_tx(conn):
        conn.execute(
            "INSERT INTO rounds (id, name, status, created_at) VALUES (?,?, 'OPEN', ?)",
            (round_id, name, now_iso()))
        seen = set()
        for item in rates:
            base = norm_currency(item.get("base_currency"), "base_currency")
            quote = norm_currency(item.get("quote_currency"), "quote_currency")
            if base == quote:
                raise ApiError(400, "BAD_REQUEST", "同一币种无需汇率口径")
            num = require_positive_int(item.get("num"), "num")
            den = require_positive_int(item.get("den"), "den")
            # 正反向同时锁定，轮内换算口径唯一
            for b, q, n, d in ((base, quote, num, den), (quote, base, den, num)):
                if (b, q) in seen:
                    raise ApiError(400, "BAD_REQUEST", f"汇率口径重复或冲突：{b}->{q}")
                seen.add((b, q))
                conn.execute(
                    "INSERT INTO round_rates (round_id, base_currency, quote_currency,"
                    " num, den) VALUES (?,?,?,?,?)", (round_id, b, q, n, d))
    return 201, round_view(conn, get_round(conn, round_id))


def list_rounds(conn, actor):
    require_role(actor, "office", "auditor", "panel")
    rows = conn.execute("SELECT * FROM rounds ORDER BY created_at, id").fetchall()
    return 200, {"rounds": [round_view(conn, r) for r in rows]}


def get_round_view(conn, actor, round_id):
    require_role(actor, "office", "auditor", "panel")
    return 200, round_view(conn, get_round(conn, round_id))


def seal_round(conn, actor, round_id):
    require_role(actor, "office")
    with immediate_tx(conn):
        row = get_round(conn, round_id)
        if row["status"] == "SEALED":
            raise ApiError(409, "ROUND_SEALED", "轮次已封存")
        conn.execute(
            "UPDATE rounds SET status='SEALED', sealed_at=? WHERE id=?",
            (now_iso(), round_id))
    return 200, round_view(conn, get_round(conn, round_id))


# ---------- 候选材料 ----------

def submit_materials(conn, actor, body):
    require_role(actor, "applicant")
    applicant_id = actor["applicant_id"]
    if not applicant_id:
        raise ApiError(400, "BAD_REQUEST", "该账号未关联申请人身份")
    payload = body.get("payload")
    if not isinstance(payload, dict) or not payload:
        raise ApiError(400, "BAD_REQUEST", "payload 必须是非空对象")
    row = conn.execute(
        "SELECT MAX(version) AS v FROM materials WHERE applicant_id=?",
        (applicant_id,)).fetchone()
    version = (row["v"] or 0) + 1
    content_hash = sha256_text(canonical(payload))
    conn.execute(
        "INSERT INTO materials (applicant_id, version, payload, content_hash, status,"
        " submitted_at) VALUES (?,?,?,?, 'SUBMITTED', ?)",
        (applicant_id, version, canonical(payload), content_hash, now_iso()))
    return 201, materials_view(latest_materials(conn, applicant_id))


def my_materials(conn, actor):
    require_role(actor, "applicant")
    rows = conn.execute(
        "SELECT * FROM materials WHERE applicant_id=? ORDER BY version",
        (actor["applicant_id"],)).fetchall()
    return 200, {"materials": [materials_view(r) for r in rows]}


def list_candidates(conn, actor):
    require_role(actor, "office", "panel", "auditor")
    rows = conn.execute(
        "SELECT m.* FROM materials m JOIN (SELECT applicant_id, MAX(version) AS v"
        " FROM materials GROUP BY applicant_id) t"
        " ON m.applicant_id=t.applicant_id AND m.version=t.v"
        " ORDER BY m.applicant_id").fetchall()
    return 200, {"candidates": [materials_view(r) for r in rows]}


def verify_materials(conn, actor, applicant_id):
    require_role(actor, "office")
    mat = latest_materials(conn, applicant_id)
    if mat is None:
        raise ApiError(404, "NOT_FOUND", "该申请人尚未提交材料")
    if mat["status"] != "VERIFIED":
        conn.execute(
            "UPDATE materials SET status='VERIFIED', verified_by=?"
            " WHERE applicant_id=? AND version=?",
            (actor["actor_id"], applicant_id, mat["version"]))
    return 200, materials_view(latest_materials(conn, applicant_id))


# ---------- 回避关系与评审 ----------

def create_recusal(conn, actor, body):
    require_role(actor, "office")
    reviewer_id, applicant_id = require_fields(body, "reviewer_id", "applicant_id")
    reason = body.get("reason", "")
    try:
        cur = conn.execute(
            "INSERT INTO recusals (reviewer_id, applicant_id, reason, created_at)"
            " VALUES (?,?,?,?)", (reviewer_id, applicant_id, reason, now_iso()))
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "RECUSAL_EXISTS", "该回避关系已存在") from exc
    return 201, {"id": cur.lastrowid, "reviewer_id": reviewer_id,
                 "applicant_id": applicant_id, "reason": reason}


def list_recusals(conn, actor):
    require_role(actor, "office", "auditor", "panel")
    rows = conn.execute("SELECT * FROM recusals ORDER BY id").fetchall()
    return 200, {"recusals": [dict(r) for r in rows]}


def submit_review(conn, actor, round_id, body):
    require_role(actor, "panel")
    applicant_id, reviewer_id = require_fields(body, "applicant_id", "reviewer_id")
    score = body.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise ApiError(400, "BAD_REQUEST", "score 必须是 0-100 的整数")
    rnd = get_round(conn, round_id)
    if rnd["status"] != "OPEN":
        raise ApiError(409, "ROUND_SEALED", "轮次已封存，无法提交评审")
    if conn.execute(
            "SELECT 1 FROM recusals WHERE reviewer_id=? AND applicant_id=?",
            (reviewer_id, applicant_id)).fetchone():
        raise ApiError(409, "RECUSAL_REQUIRED", "评审人与申请人存在回避关系，须回避")
    try:
        cur = conn.execute(
            "INSERT INTO reviews (round_id, panel_id, reviewer_id, applicant_id, score,"
            " comment, created_at) VALUES (?,?,?,?,?,?,?)",
            (round_id, actor["actor_id"], reviewer_id, applicant_id, score,
             body.get("comment", ""), now_iso()))
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "REVIEW_EXISTS", "该评审人已提交过此申请人的评审") from exc
    return 201, {"id": cur.lastrowid, "round_id": round_id,
                 "panel_id": actor["actor_id"], "reviewer_id": reviewer_id,
                 "applicant_id": applicant_id, "score": score}


def list_reviews(conn, actor, round_id):
    require_role(actor, "office", "auditor", "panel")
    get_round(conn, round_id)
    rows = conn.execute(
        "SELECT * FROM reviews WHERE round_id=? ORDER BY id", (round_id,)).fetchall()
    return 200, {"reviews": [dict(r) for r in rows]}


# ---------- 限制规则 ----------

def create_rule(conn, actor, body):
    require_role(actor, "office")
    rtype, = require_fields(body, "type")
    if rtype not in RULE_TYPES:
        raise ApiError(400, "BAD_REQUEST",
                       "type 必须是：" + "、".join(sorted(RULE_TYPES)))
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise ApiError(400, "BAD_REQUEST", "params 必须是对象")
    missing = [k for k in RULE_REQUIRED_PARAMS[rtype] if k not in params]
    if missing:
        raise ApiError(400, "BAD_REQUEST", "params 缺少：" + "、".join(missing))
    batch_id = body.get("batch_id")
    if batch_id:
        get_batch_row(conn, batch_id)
    cur = conn.execute(
        "INSERT INTO rules (batch_id, type, params, created_at) VALUES (?,?,?,?)",
        (batch_id, rtype, canonical(params), now_iso()))
    return 201, {"id": cur.lastrowid, "batch_id": batch_id, "type": rtype,
                 "params": params}


def list_rules(conn, actor):
    require_role(actor, "office", "auditor", "panel")
    rows = conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
    return 200, {"rules": [{**dict(r), "params": json.loads(r["params"])}
                           for r in rows]}


def evaluate_rules(conn, round_id, applicant_id, batch_id,
                   amount_minor, currency) -> list:
    """评估全局与批次级规则，返回违规列表（空列表表示通过）。"""
    violations = []
    mat = latest_materials(conn, applicant_id)
    payload = json.loads(mat["payload"]) if mat else {}
    rows = conn.execute(
        "SELECT * FROM rules WHERE batch_id IS NULL OR batch_id=?",
        (batch_id,)).fetchall()
    for rule in rows:
        params = json.loads(rule["params"])
        rtype = rule["type"]
        label = f"规则#{rule['id']}({rtype})"
        if rtype == "MIN_GPA":
            gpa = payload.get("gpa")
            try:
                ok = gpa is not None and float(gpa) >= float(params["min"])
            except (TypeError, ValueError, KeyError):
                ok = False
            if not ok:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：GPA 未达门槛 {params.get('min')}"})
        elif rtype == "DEGREE_LEVEL":
            allow = params.get("allow") or []
            if payload.get("degree_level") not in allow:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：学位层次不在允许范围"})
        elif rtype == "NATIONALITY_DENY":
            deny = params.get("deny") or []
            if payload.get("nationality") in deny:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：国籍属于限制范围"})
        elif rtype == "MAX_AWARDS_PER_APPLICANT":
            limit = int(params.get("count", 0))
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM awards WHERE round_id=? AND applicant_id=?"
                " AND status IN ('ACTIVE','DEFERRED')",
                (round_id, applicant_id)).fetchone()["c"]
            if count + 1 > limit:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：本轮有效授予数将超过 {limit}"})
        elif rtype == "MAX_TOTAL_PER_APPLICANT":
            target_ccy = norm_currency(params.get("currency"), "params.currency")
            limit = int(params.get("amount_minor", 0))
            total = 0
            rate_missing = False
            existing = conn.execute(
                "SELECT award_currency, award_amount_minor FROM awards"
                " WHERE round_id=? AND applicant_id=?"
                " AND status IN ('ACTIVE','DEFERRED')",
                (round_id, applicant_id)).fetchall()
            for a in existing:
                try:
                    n, d = round_rate(conn, round_id, a["award_currency"], target_ccy)
                except ApiError:
                    rate_missing = True
                    break
                conv, _ = fx_convert(a["award_amount_minor"], n, d)
                total += conv
            if rate_missing:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：缺少汇率口径，无法评估总额限制"})
                continue
            try:
                n, d = round_rate(conn, round_id, currency, target_ccy)
            except ApiError:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：缺少汇率口径，无法评估总额限制"})
                continue
            new_conv, _ = fx_convert(amount_minor, n, d)
            if total + new_conv > limit:
                violations.append({"rule_id": rule["id"], "type": rtype,
                                   "message": f"{label}：本轮授予总额将超过"
                                              f" {limit}（{target_ccy} 最小单位）"})
    return violations


# ---------- 方案试算 ----------

def trial(conn, actor, round_id, body):
    """方案试算：只读不写，逐行给出锁定额、尾差、可用额度与规则结论。"""
    require_role(actor, "office", "panel")
    rnd = get_round(conn, round_id)
    if rnd["status"] != "OPEN":
        raise ApiError(409, "ROUND_SEALED", "轮次已封存，无法试算")
    lines = body.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ApiError(400, "BAD_REQUEST", "lines 必须是非空列表")
    simulated: dict[str, Fraction] = {}
    results = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise ApiError(400, "BAD_REQUEST", f"第 {index} 行不是对象")
        applicant_id, batch_id = require_fields(line, "applicant_id", "batch_id")
        amount = require_positive_int(line.get("amount_minor"), "amount_minor")
        currency = norm_currency(line.get("currency"))
        batch = get_batch_row(conn, batch_id)
        if batch["is_residual_destination"]:
            raise ApiError(400, "BAD_REQUEST", "尾差归集批次不能用于授予")
        num, den = round_rate(conn, round_id, currency, batch["currency"])
        lock, dust = fx_convert(amount, num, den)
        locked = simulated.get(batch_id)
        if locked is None:
            locked = locked_amount(conn, batch_id)
        available = balance_amount(conn, batch_id) - locked
        violations = evaluate_rules(conn, round_id, applicant_id, batch_id,
                                    amount, currency)
        simulated[batch_id] = locked + lock
        results.append({
            "index": index,
            "applicant_id": applicant_id,
            "batch_id": batch_id,
            "award_currency": currency,
            "award_amount_minor": amount,
            "batch_currency": batch["currency"],
            "lock_amount_minor": lock,
            "rate": {"num": num, "den": den},
            "dust": to_json_amount(dust),
            "available_before": to_json_amount(available),
            "available_after": to_json_amount(available - lock),
            "quota_sufficient": lock <= available,
            "violations": violations,
            "ok": lock <= available and not violations,
        })
    return 200, {"round_id": round_id,
                 "all_ok": all(r["ok"] for r in results),
                 "lines": results}


# ---------- 正式授予（原子锁定 + 快照封存） ----------

class _IdempotentReplay(Exception):
    def __init__(self, award_id: str):
        super().__init__(award_id)
        self.award_id = award_id


def build_snapshot(conn, rnd, actor, mat, applicant_id, batch, currency,
                   amount, lock, num, den, dust, award_id) -> dict:
    rates = [dict(r) for r in conn.execute(
        "SELECT base_currency, quote_currency, num, den FROM round_rates"
        " WHERE round_id=? ORDER BY base_currency, quote_currency", (rnd["id"],))]
    reviews = [dict(r) for r in conn.execute(
        "SELECT panel_id, reviewer_id, score, comment, created_at FROM reviews"
        " WHERE round_id=? AND applicant_id=? ORDER BY id", (rnd["id"], applicant_id))]
    recusals = [dict(r) for r in conn.execute(
        "SELECT reviewer_id, reason FROM recusals WHERE applicant_id=? ORDER BY id",
        (applicant_id,))]
    rules = [dict(r) for r in conn.execute(
        "SELECT id, batch_id, type, params FROM rules"
        " WHERE batch_id IS NULL OR batch_id=? ORDER BY id", (batch["id"],))]
    return {
        "snapshot_version": 1,
        "award_id": award_id,
        "sealed_at": now_iso(),
        "round": {"id": rnd["id"], "name": rnd["name"], "pinned_rates": rates},
        "panel_id": actor["actor_id"],
        "applicant_id": applicant_id,
        "materials": {"version": mat["version"],
                      "content_hash": mat["content_hash"],
                      "status": mat["status"]},
        "reviews": reviews,
        "recusals_applied": recusals,
        "rules_evaluated": rules,
        "award": {
            "award_currency": currency,
            "award_amount_minor": amount,
            "batch_id": batch["id"],
            "batch_currency": batch["currency"],
            "lock_amount_minor": lock,
            "rate_num": num,
            "rate_den": den,
            "rounding_dust": to_json_amount(dust),
        },
    }


def create_award(conn, actor, round_id, body):
    """正式授予：单事务内完成校验、额度锁定、快照封存与分录入账。"""
    require_role(actor, "panel")
    applicant_id, batch_id = require_fields(body, "applicant_id", "batch_id")
    amount = require_positive_int(body.get("amount_minor"), "amount_minor")
    currency = norm_currency(body.get("currency"))
    idem = body.get("idempotency_key") or None
    if idem:
        row = conn.execute(
            "SELECT id FROM awards WHERE idempotency_key=?", (idem,)).fetchone()
        if row:
            return 200, {**award_view(conn, get_award_row(conn, row["id"])),
                         "idempotent_replay": True}
    try:
        with immediate_tx(conn):
            rnd = get_round(conn, round_id)
            if rnd["status"] != "OPEN":
                raise ApiError(409, "ROUND_SEALED", "轮次已封存，无法授予")
            batch = get_batch_row(conn, batch_id)
            if batch["is_residual_destination"]:
                raise ApiError(400, "BAD_REQUEST", "尾差归集批次不能用于授予")
            mat = latest_materials(conn, applicant_id)
            if mat is None or mat["status"] != "VERIFIED":
                raise ApiError(422, "MATERIALS_NOT_VERIFIED", "候选材料未核验，不能授予")
            reviewed = conn.execute(
                "SELECT 1 FROM reviews WHERE round_id=? AND panel_id=? AND applicant_id=?",
                (round_id, actor["actor_id"], applicant_id)).fetchone()
            if not reviewed:
                raise ApiError(422, "NOT_REVIEWED", "本评审组尚未评审该申请人，不能授予")
            num, den = round_rate(conn, round_id, currency, batch["currency"])
            lock, dust = fx_convert(amount, num, den)
            violations = evaluate_rules(conn, round_id, applicant_id, batch_id,
                                        amount, currency)
            if violations:
                raise ApiError(422, "RULE_VIOLATION", "触发限制规则", details=violations)
            if Fraction(lock) > available_amount(conn, batch_id):
                raise ApiError(409, "QUOTA_EXHAUSTED", "资金批次可用额度不足")
            award_id = new_id("awd")
            try:
                conn.execute(
                    "INSERT INTO awards (id, round_id, panel_id, applicant_id, batch_id,"
                    " award_currency, award_amount_minor, lock_amount_minor, rate_num,"
                    " rate_den, status, idempotency_key, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?, 'ACTIVE', ?, ?)",
                    (award_id, round_id, actor["actor_id"], applicant_id, batch_id,
                     currency, amount, lock, num, den, idem, now_iso()))
            except sqlite3.IntegrityError as exc:
                if idem:
                    row = conn.execute(
                        "SELECT id FROM awards WHERE idempotency_key=?",
                        (idem,)).fetchone()
                    if row:
                        raise _IdempotentReplay(row["id"]) from exc
                raise ApiError(409, "AWARD_CONFLICT",
                               "同一轮次内该申请人已持有此资金批次的有效授予，"
                               "禁止重复授予") from exc
            snapshot_id = new_id("snp")
            payload = build_snapshot(conn, rnd, actor, mat, applicant_id, batch,
                                     currency, amount, lock, num, den, dust, award_id)
            digest = sha256_text(canonical(payload))
            conn.execute(
                "INSERT INTO snapshots (id, award_id, payload, content_hash, sealed_at)"
                " VALUES (?,?,?,?,?)",
                (snapshot_id, award_id, canonical(payload), digest, now_iso()))
            conn.execute("UPDATE awards SET snapshot_id=? WHERE id=?",
                         (snapshot_id, award_id))
            insert_ledger(conn, batch_id=batch_id, round_id=round_id,
                          award_id=award_id, entry_type="LOCK",
                          amount_num=lock, currency=batch["currency"],
                          actor_id=actor["actor_id"], note="授予锁定")
            if dust:
                insert_ledger(conn, batch_id=residual_batch_id(conn, batch["currency"]),
                              round_id=round_id, award_id=award_id,
                              entry_type="ROUNDING",
                              amount_num=dust.numerator, amount_den=dust.denominator,
                              currency=batch["currency"], actor_id=actor["actor_id"],
                              note="汇率换算尾差")
    except _IdempotentReplay as replay:
        return 200, {**award_view(conn, get_award_row(conn, replay.award_id)),
                     "idempotent_replay": True}
    return 201, award_view(conn, get_award_row(conn, award_id))


def get_award(conn, actor, award_id):
    require_role(actor, "office", "auditor")
    return 200, award_view(conn, get_award_row(conn, award_id))


def list_round_awards(conn, actor, round_id):
    require_role(actor, "office", "auditor")
    get_round(conn, round_id)
    rows = conn.execute(
        "SELECT * FROM awards WHERE round_id=? ORDER BY created_at, id",
        (round_id,)).fetchall()
    return 200, {"awards": [award_view(conn, r) for r in rows]}


def get_snapshot(conn, actor, award_id):
    require_role(actor, "office", "auditor")
    get_award_row(conn, award_id)
    snap = conn.execute(
        "SELECT * FROM snapshots WHERE award_id=?", (award_id,)).fetchone()
    if snap is None:
        raise ApiError(404, "NOT_FOUND", "评审快照不存在")
    verified = sha256_text(snap["payload"]) == snap["content_hash"]
    return 200, {"snapshot_id": snap["id"], "award_id": award_id,
                 "sealed_at": snap["sealed_at"], "content_hash": snap["content_hash"],
                 "verified": verified, "payload": json.loads(snap["payload"])}


def my_awards(conn, actor):
    require_role(actor, "applicant")
    rows = conn.execute(
        "SELECT a.*, b.name AS batch_name, b.funder AS funder, r.name AS round_name"
        " FROM awards a JOIN fund_batches b ON b.id=a.batch_id"
        " JOIN rounds r ON r.id=a.round_id"
        " WHERE a.applicant_id=? ORDER BY a.created_at, a.id",
        (actor["applicant_id"],)).fetchall()
    return 200, {"awards": [{
        "id": r["id"],
        "round_id": r["round_id"],
        "round_name": r["round_name"],
        "batch_id": r["batch_id"],
        "batch_name": r["batch_name"],
        "funder": r["funder"],
        "award_currency": r["award_currency"],
        "award_amount_minor": r["award_amount_minor"],
        "status": r["status"],
        "created_at": r["created_at"],
    } for r in rows]}


# ---------- 分录结算：放弃 / 资格丧失 / 延期 ----------

def settle_award(conn, actor, award_id, body):
    require_role(actor, "office")
    action = body.get("action")
    if action not in SETTLE_ACTIONS:
        raise ApiError(400, "BAD_REQUEST", "action 必须是 withdraw / forfeit / defer")
    note = body.get("note", "")
    with immediate_tx(conn):
        award = get_award_row(conn, award_id)
        if award["status"] not in ("ACTIVE", "DEFERRED"):
            raise ApiError(409, "ALREADY_SETTLED", "该授予已完成结算，不能重复操作")
        current_round = (award["deferred_to_round_id"]
                         if award["status"] == "DEFERRED" else award["round_id"])
        lock = award["lock_amount_minor"]
        batch = get_batch_row(conn, award["batch_id"])
        if action == "defer":
            target = body.get("to_round_id")
            if not target:
                raise ApiError(400, "BAD_REQUEST", "延期必须指定 to_round_id")
            if target == current_round:
                raise ApiError(400, "BAD_REQUEST", "延期目标轮次与当前轮次相同")
            target_round = get_round(conn, target)
            if target_round["status"] != "OPEN":
                raise ApiError(409, "ROUND_SEALED", "目标轮次已封存，无法延期至该轮次")
            conn.execute(
                "UPDATE awards SET status='DEFERRED', deferred_to_round_id=?"
                " WHERE id=?", (target, award_id))
            insert_ledger(conn, batch_id=award["batch_id"], round_id=current_round,
                          award_id=award_id, entry_type="DEFER_RELEASE",
                          amount_num=-lock, currency=batch["currency"],
                          actor_id=actor["actor_id"],
                          note=note or "延期释放原轮次占用")
            insert_ledger(conn, batch_id=award["batch_id"], round_id=target,
                          award_id=award_id, entry_type="DEFER_LOCK",
                          amount_num=lock, currency=batch["currency"],
                          actor_id=actor["actor_id"],
                          note=note or "延期锁定至新轮次")
        else:
            new_status = "WITHDRAWN" if action == "withdraw" else "FORFEITED"
            entry_type = "RELEASE" if action == "withdraw" else "FORFEIT"
            conn.execute("UPDATE awards SET status=? WHERE id=?",
                         (new_status, award_id))
            insert_ledger(conn, batch_id=award["batch_id"], round_id=current_round,
                          award_id=award_id, entry_type=entry_type,
                          amount_num=-lock, currency=batch["currency"],
                          actor_id=actor["actor_id"],
                          note=note or ("放弃结算" if action == "withdraw"
                                        else "资格丧失结算"))
    return 200, award_view(conn, get_award_row(conn, award_id))


# ---------- 资金转移 ----------

def transfer(conn, actor, body):
    require_role(actor, "office")
    from_id, to_id = require_fields(body, "from_batch_id", "to_batch_id")
    amount = require_positive_int(body.get("amount_minor"), "amount_minor")
    if from_id == to_id:
        raise ApiError(400, "BAD_REQUEST", "转出与转入批次不能相同")
    round_id = body.get("round_id")
    note = body.get("note", "")
    with immediate_tx(conn):
        src = get_batch_row(conn, from_id)
        dst = get_batch_row(conn, to_id)
        if src["is_residual_destination"] or dst["is_residual_destination"]:
            raise ApiError(400, "BAD_REQUEST", "尾差归集批次不参与资金转移")
        if src["currency"] == dst["currency"]:
            num = den = 1
            converted, dust = amount, Fraction(0)
        else:
            if not round_id:
                raise ApiError(400, "BAD_REQUEST",
                               "跨币种转移必须指定 round_id 以锁定汇率口径")
            get_round(conn, round_id)
            num, den = round_rate(conn, round_id, src["currency"], dst["currency"])
            converted, dust = fx_convert(amount, num, den)
        if Fraction(amount) > available_amount(conn, from_id):
            raise ApiError(409, "QUOTA_EXHAUSTED", "转出批次可用额度不足")
        transfer_id = new_id("trf")
        conn.execute(
            "INSERT INTO transfers (id, from_batch_id, to_batch_id, amount_minor,"
            " converted_minor, rate_num, rate_den, round_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (transfer_id, from_id, to_id, amount, converted, num, den,
             round_id, now_iso()))
        insert_ledger(conn, batch_id=from_id, round_id=round_id,
                      transfer_id=transfer_id, entry_type="TRANSFER_OUT",
                      amount_num=-amount, currency=src["currency"],
                      actor_id=actor["actor_id"], note=note or "资金转移转出")
        insert_ledger(conn, batch_id=to_id, round_id=round_id,
                      transfer_id=transfer_id, entry_type="TRANSFER_IN",
                      amount_num=converted, currency=dst["currency"],
                      actor_id=actor["actor_id"], note=note or "资金转移转入")
        if dust:
            insert_ledger(conn, batch_id=residual_batch_id(conn, dst["currency"]),
                          round_id=round_id, transfer_id=transfer_id,
                          entry_type="ROUNDING",
                          amount_num=dust.numerator, amount_den=dust.denominator,
                          currency=dst["currency"], actor_id=actor["actor_id"],
                          note="转移换算尾差")
    return 201, {"id": transfer_id, "from_batch_id": from_id, "to_batch_id": to_id,
                 "amount_minor": amount, "converted_minor": converted,
                 "rate": {"num": num, "den": den},
                 "dust": to_json_amount(dust), "round_id": round_id}


def list_transfers(conn, actor):
    require_role(actor, "office", "auditor")
    rows = conn.execute("SELECT * FROM transfers ORDER BY created_at, id").fetchall()
    return 200, {"transfers": [dict(r) for r in rows]}


# ---------- 审计复算 ----------

def recompute_round(conn, actor, round_id):
    """复算任一轮次：重放全部分录，校验占用边界与快照完整性。"""
    require_role(actor, "office", "auditor")
    rnd = get_round(conn, round_id)
    anomalies = []
    batch_reports = []
    for b in conn.execute("SELECT * FROM fund_batches ORDER BY id").fetchall():
        locked = Fraction(0)
        balance = Fraction(b["total_minor"])
        locked_delta = Fraction(0)
        balance_delta = Fraction(0)
        rounding_delta = Fraction(0)
        for e in conn.execute(
                "SELECT * FROM ledger WHERE batch_id=? ORDER BY entry_id",
                (b["id"],)).fetchall():
            amount = Fraction(e["amount_num"], e["amount_den"])
            etype = e["type"]
            if etype in AWARD_ENTRY_TYPES:
                locked += amount
                if e["round_id"] == round_id:
                    locked_delta += amount
            elif etype in ("TRANSFER_OUT", "TRANSFER_IN"):
                balance += amount
                if e["round_id"] == round_id:
                    balance_delta += amount
            else:  # ROUNDING
                balance += amount
                if e["round_id"] == round_id:
                    rounding_delta += amount
            if not b["is_residual_destination"]:
                if locked < 0:
                    anomalies.append(
                        f"批次 {b['id']} 在分录 {e['entry_id']} 后占用为负：{locked}")
                if balance < 0:
                    anomalies.append(
                        f"批次 {b['id']} 在分录 {e['entry_id']} 后余额为负：{balance}")
                if locked > balance:
                    anomalies.append(
                        f"批次 {b['id']} 在分录 {e['entry_id']} 后占用超过余额："
                        f"{locked} > {balance}")
        report = {"batch_id": b["id"], "currency": b["currency"],
                  "funder": b["funder"],
                  "is_residual_destination": bool(b["is_residual_destination"]),
                  "round_locked_delta": to_json_amount(locked_delta),
                  "round_balance_delta": to_json_amount(balance_delta),
                  "round_rounding_delta": to_json_amount(rounding_delta),
                  "locked": to_json_amount(locked),
                  "balance": to_json_amount(balance)}
        if not b["is_residual_destination"]:
            report["total_minor"] = b["total_minor"]
            report["available"] = to_json_amount(balance - locked)
        batch_reports.append(report)
    award_reports = []
    for a in conn.execute(
            "SELECT * FROM awards WHERE round_id=? OR deferred_to_round_id=?"
            " ORDER BY created_at, id", (round_id, round_id)).fetchall():
        snap = conn.execute(
            "SELECT payload, content_hash FROM snapshots WHERE award_id=?",
            (a["id"],)).fetchone()
        verified = bool(snap) and sha256_text(snap["payload"]) == snap["content_hash"]
        if not verified:
            anomalies.append(f"授予 {a['id']} 的评审快照校验失败")
        award_reports.append({
            "id": a["id"], "applicant_id": a["applicant_id"],
            "panel_id": a["panel_id"], "batch_id": a["batch_id"],
            "lock_amount_minor": a["lock_amount_minor"], "status": a["status"],
            "snapshot_hash": snap["content_hash"] if snap else None,
            "snapshot_verified": verified})
    dust_by_ccy: dict[str, Fraction] = {}
    for r in conn.execute(
            "SELECT currency, amount_num, amount_den FROM ledger"
            " WHERE type='ROUNDING' AND round_id=?", (round_id,)).fetchall():
        dust_by_ccy[r["currency"]] = dust_by_ccy.get(r["currency"], Fraction(0)) \
            + Fraction(r["amount_num"], r["amount_den"])
    entries_in_round = conn.execute(
        "SELECT COUNT(*) AS c FROM ledger WHERE round_id=?", (round_id,)).fetchone()["c"]
    return 200, {
        "round": round_view(conn, rnd),
        "batches": batch_reports,
        "awards": award_reports,
        "rounding_dust": [{"currency": c, "amount": to_json_amount(v)}
                          for c, v in sorted(dust_by_ccy.items())],
        "entries_in_round": entries_in_round,
        "anomalies": anomalies,
        "verified": not anomalies,
    }


def batch_ledger(conn, actor, batch_id):
    """每笔资金的占用变化：分录流 + 逐笔占用与余额。"""
    require_role(actor, "office", "auditor")
    batch = get_batch_row(conn, batch_id)
    locked = Fraction(0)
    balance = Fraction(batch["total_minor"])
    entries = []
    for e in conn.execute(
            "SELECT * FROM ledger WHERE batch_id=? ORDER BY entry_id",
            (batch_id,)).fetchall():
        amount = Fraction(e["amount_num"], e["amount_den"])
        if e["type"] in AWARD_ENTRY_TYPES:
            locked += amount
        else:
            balance += amount
        entries.append({
            "entry_id": e["entry_id"], "type": e["type"],
            "amount": to_json_amount(amount),
            "currency": e["currency"], "round_id": e["round_id"],
            "award_id": e["award_id"], "transfer_id": e["transfer_id"],
            "actor_id": e["actor_id"], "note": e["note"],
            "created_at": e["created_at"],
            "running_locked": to_json_amount(locked),
            "running_balance": to_json_amount(balance)})
    return 200, {"batch": batch_view(conn, batch), "entries": entries}


def residuals(conn, actor):
    require_role(actor, "office", "auditor")
    rows = conn.execute(
        "SELECT * FROM fund_batches WHERE is_residual_destination=1"
        " ORDER BY currency").fetchall()
    return 200, {"residuals": [{
        "batch_id": r["id"], "currency": r["currency"],
        "balance": to_json_amount(balance_amount(conn, r["id"]))} for r in rows]}
