"""限制规则引擎：方案试算与正式授予共用同一套判定。

每条规则作用于全部批次（scope_batch_id 为空）或指定批次；
评估结果是一组违规项，试算如实报告，正式授予遇违规即拒绝。
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation

from .money import convert_minor

RULE_TYPES = (
    "MAX_AMOUNT_PER_AWARD",
    "MAX_ACTIVE_AWARDS_PER_APPLICANT",
    "ROUND_BUDGET_CAP",
    "EXCLUDED_NATIONALITY",
    "MIN_GPA",
)


def evaluate(conn: sqlite3.Connection, *, lines: list[dict], round_id: int) -> list[dict]:
    """评估全部启用规则，返回违规列表（空列表表示全部通过）。

    lines 为 service._build_line 的产物，携带候选、批次、汇率与换算后额度。
    """
    violations: list[dict] = []
    active = conn.execute("SELECT * FROM rules WHERE active=1 ORDER BY id").fetchall()
    for rule in active:
        scoped = [
            line
            for line in lines
            if rule["scope_batch_id"] is None or line["batch"]["id"] == rule["scope_batch_id"]
        ]
        if not scoped:
            continue
        params = json.loads(rule["params"])
        _HANDLERS[rule["type"]](conn, rule, params, scoped, round_id, violations)
    return violations


def _violation(rule, message, out, candidate_id=None):
    item = {"rule_id": rule["id"], "type": rule["type"], "message": message}
    if candidate_id is not None:
        item["candidate_id"] = candidate_id
    out.append(item)


def _to_rule_currency(conn, rule, line, currency, out):
    """把明细金额换算到规则币种；缺汇率时记违规并返回 None。"""
    if currency == line["award_currency"]:
        return line["award_amount"]
    if currency == line["batch"]["currency"]:
        return line["locked_amount"]
    rate = conn.execute(
        "SELECT * FROM fx_rates WHERE set_id=? AND base=? AND quote=?",
        (line["batch"]["fx_rate_set_id"], line["award_currency"], currency),
    ).fetchone()
    if rate is None:
        _violation(
            rule,
            f"缺少 {line['award_currency']}→{currency} 汇率，无法核验金额上限",
            out,
            line["candidate"]["id"],
        )
        return None
    return convert_minor(
        line["award_amount"], rate["num"], rate["den"], line["fx_set"]["rounding"]
    )


def _max_amount_per_award(conn, rule, params, lines, round_id, out):
    for line in lines:
        amount = _to_rule_currency(conn, rule, line, params["currency"], out)
        if amount is not None and amount > params["amount"]:
            _violation(
                rule,
                f"单笔金额折算后 {amount} 超过上限 {params['amount']}（{params['currency']} 最小单位）",
                out,
                line["candidate"]["id"],
            )


def _max_active_per_applicant(conn, rule, params, lines, round_id, out):
    limit = params["count"]
    by_applicant: dict[str, list[dict]] = {}
    for line in lines:
        by_applicant.setdefault(line["applicant_id"], []).append(line)
    for applicant_id, owned in by_applicant.items():
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM awards WHERE applicant_id=? AND status='ACTIVE'",
            (applicant_id,),
        ).fetchone()["c"]
        if active + len(owned) > limit:
            _violation(
                rule,
                f"申请人 {applicant_id} 有效授予数将达 {active + len(owned)}，超过上限 {limit}",
                out,
                owned[0]["candidate"]["id"],
            )


def _round_budget_cap(conn, rule, params, lines, round_id, out):
    batch_id = rule["scope_batch_id"]
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN type='LOCK' THEN amount ELSE -amount END), 0) AS net
           FROM ledger_entries
           WHERE batch_id=? AND round_id=? AND type IN ('LOCK','RELEASE')""",
        (batch_id, round_id),
    ).fetchone()
    incoming = sum(line["locked_amount"] for line in lines)
    if row["net"] + incoming > params["amount"]:
        _violation(
            rule,
            f"轮次预算超限：已占用 {row['net']}，新增 {incoming}，上限 {params['amount']}",
            out,
        )


def _excluded_nationality(conn, rule, params, lines, round_id, out):
    blocked = set(params["nationalities"])
    for line in lines:
        nationality = line["materials"].get("nationality")
        if nationality in blocked:
            _violation(rule, f"国籍 {nationality} 在限制名单内", out, line["candidate"]["id"])


def _min_gpa(conn, rule, params, lines, round_id, out):
    threshold = Decimal(str(params["value"]))
    for line in lines:
        raw = line["materials"].get("gpa")
        try:
            gpa = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            gpa = None
        if gpa is None or gpa < threshold:
            _violation(rule, f"GPA {raw!r} 低于要求 {threshold}", out, line["candidate"]["id"])


_HANDLERS = {
    "MAX_AMOUNT_PER_AWARD": _max_amount_per_award,
    "MAX_ACTIVE_AWARDS_PER_APPLICANT": _max_active_per_applicant,
    "ROUND_BUDGET_CAP": _round_budget_cap,
    "EXCLUDED_NATIONALITY": _excluded_nationality,
    "MIN_GPA": _min_gpa,
}
