"""核心业务流测试：锁定、防重、规则、回避、结算、尾差与复算。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarship import db, ledger, service
from scholarship.errors import ApiError

ADMIN = {"username": "office", "role": "admin", "applicant_ref": None, "funder_ref": None, "reviewer_ref": None}


def applicant(ref: str) -> dict:
    return {"username": f"app-{ref}", "role": "applicant", "applicant_ref": ref,
            "funder_ref": None, "reviewer_ref": None}


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_db(self.conn)
        self.fx = service.create_fx_rate_set(self.conn, ADMIN, {
            "name": "测试口径", "rounding": "HALF_EVEN",
            "rates": [
                {"base": "EUR", "quote": "USD", "num": 108, "den": 100},
                {"base": "USD", "quote": "EUR", "num": 100, "den": 108},
            ],
        })
        self.usd = service.create_batch(self.conn, ADMIN, {
            "name": "美元池", "funder_id": "F1", "currency": "USD",
            "total_amount": 10_000_000, "fx_rate_set_id": self.fx["id"],
            "residual_destination": "RESERVE",
        })
        self.eur = service.create_batch(self.conn, ADMIN, {
            "name": "欧元池", "funder_id": "F1", "currency": "EUR",
            "total_amount": 5_000_000, "fx_rate_set_id": self.fx["id"],
            "residual_destination": "RESERVE",
        })
        self.round1 = service.create_round(self.conn, ADMIN, {"name": "R1", "committee": ["r1", "r2"]})
        self.round2 = service.create_round(self.conn, ADMIN, {"name": "R2", "committee": ["r1", "r2"]})
        self.cand1 = service.create_candidate(self.conn, ADMIN, {
            "applicant_id": "S1", "round_id": self.round1["id"],
            "materials": {"gpa": "3.8", "nationality": "CN"},
        })
        self.cand2 = service.create_candidate(self.conn, ADMIN, {
            "applicant_id": "S2", "round_id": self.round1["id"],
            "materials": {"gpa": "3.6", "nationality": "CN"},
        })

    def tearDown(self) -> None:
        self.conn.close()

    def award_payload(self, **overrides):
        payload = {
            "round_id": self.round1["id"],
            "candidate_id": self.cand1["id"],
            "batch_id": self.usd["id"],
            "award_amount": 100_000,  # 1000.00 EUR
            "award_currency": "EUR",
            "panel": ["r1"],
        }
        payload.update(overrides)
        return payload

    def award(self, **overrides):
        view, created = service.create_award(self.conn, ADMIN, self.award_payload(**overrides))
        self.assertTrue(created)
        return view

    def totals(self, batch_id):
        return ledger.batch_totals(self.conn, batch_id)


class AwardLockingTest(ServiceTestBase):
    def test_award_locks_quota_and_seals_snapshot(self) -> None:
        view = self.award()
        self.assertEqual(view["locked_amount"], 108_000)  # 1000.00 EUR × 1.08
        self.assertEqual(view["status"], "ACTIVE")
        snapshot = view["snapshot"]
        self.assertEqual(snapshot["fx"]["rate"], {"base": "EUR", "quote": "USD", "num": 108, "den": 100})
        self.assertEqual(snapshot["panel"], ["r1"])
        self.assertEqual(len(snapshot["materials_sha256"]), 64)
        totals = self.totals(self.usd["id"])
        self.assertEqual(totals["locked"], 108_000)
        self.assertEqual(totals["available"], 10_000_000 - 108_000)

    def test_double_award_same_source_rejected(self) -> None:
        self.award()
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(request_id="retry-other"))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "AWARD_CONFLICT")
        # 账本只锁定了一次
        self.assertEqual(self.totals(self.usd["id"])["locked"], 108_000)

    def test_same_applicant_other_batch_also_checked_by_rule(self) -> None:
        service.create_rule(self.conn, ADMIN, {
            "type": "MAX_ACTIVE_AWARDS_PER_APPLICANT", "params": {"count": 1},
        })
        self.award()
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(
                batch_id=self.eur["id"], award_currency="EUR"))
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")

    def test_insufficient_funds(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(award_amount=10_000_000))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_FUNDS")

    def test_idempotent_request_id(self) -> None:
        first, created1 = service.create_award(self.conn, ADMIN, self.award_payload(request_id="req-1"))
        second, created2 = service.create_award(self.conn, ADMIN, self.award_payload(request_id="req-1"))
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first["id"], second["id"])
        entries = [e for e in ledger.entries_for_batch(self.conn, self.usd["id"]) if e["type"] == "LOCK"]
        self.assertEqual(len(entries), 1)

    def test_recusal_blocks_panel(self) -> None:
        service.create_recusal(self.conn, ADMIN, {
            "reviewer_id": "r1", "applicant_id": "S1", "reason": "亲属关系",
        })
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(panel=["r1"]))
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.code, "RECUSAL_CONFLICT")
        # 未回避的评审人可以授予
        view = self.award(panel=["r2"])
        self.assertEqual(view["status"], "ACTIVE")

    def test_panel_must_come_from_committee(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(panel=["outsider"]))
        self.assertEqual(ctx.exception.code, "PANEL_NOT_IN_COMMITTEE")

    def test_missing_fx_rate_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(award_currency="GBP"))
        self.assertEqual(ctx.exception.code, "NO_FX_RATE")


class RuleEngineTest(ServiceTestBase):
    def test_max_amount_rule_blocks_award_but_not_trial(self) -> None:
        service.create_rule(self.conn, ADMIN, {
            "type": "MAX_AMOUNT_PER_AWARD",
            "params": {"amount": 50_000, "currency": "USD"},  # 500.00 USD
        })
        trial = service.create_trial(self.conn, ADMIN, {
            "round_id": self.round1["id"],
            "lines": [{
                "candidate_id": self.cand1["id"], "batch_id": self.usd["id"],
                "award_amount": 100_000, "award_currency": "EUR"}],
        })
        self.assertEqual(len(trial["violations"]), 1)
        self.assertEqual(trial["violations"][0]["type"], "MAX_AMOUNT_PER_AWARD")
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload())
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")
        self.assertEqual(ctx.exception.details[0]["type"], "MAX_AMOUNT_PER_AWARD")

    def test_min_gpa_rule(self) -> None:
        service.create_rule(self.conn, ADMIN, {"type": "MIN_GPA", "params": {"value": "3.7"}})
        with self.assertRaises(ApiError) as ctx:
            # cand2 GPA 3.6 不达标
            service.create_award(self.conn, ADMIN, self.award_payload(candidate_id=self.cand2["id"]))
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")
        self.assertEqual(ctx.exception.details[0]["type"], "MIN_GPA")

    def test_round_budget_cap(self) -> None:
        service.create_rule(self.conn, ADMIN, {
            "type": "ROUND_BUDGET_CAP", "params": {"amount": 150_000},
            "scope_batch_id": self.usd["id"],
        })
        self.award()  # 锁定 108_000
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload(candidate_id=self.cand2["id"]))
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")
        self.assertEqual(ctx.exception.details[0]["type"], "ROUND_BUDGET_CAP")

    def test_excluded_nationality(self) -> None:
        service.create_rule(self.conn, ADMIN, {
            "type": "EXCLUDED_NATIONALITY", "params": {"nationalities": ["CN"]},
        })
        with self.assertRaises(ApiError) as ctx:
            service.create_award(self.conn, ADMIN, self.award_payload())
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")


class TrialTest(ServiceTestBase):
    def test_trial_computes_without_locking(self) -> None:
        before = self.totals(self.usd["id"])
        trial = service.create_trial(self.conn, ADMIN, {
            "round_id": self.round1["id"],
            "lines": [
                {"candidate_id": self.cand1["id"], "batch_id": self.usd["id"],
                 "award_amount": 100_000, "award_currency": "EUR"},
                {"candidate_id": self.cand2["id"], "batch_id": self.usd["id"],
                 "award_amount": 50_000, "award_currency": "EUR"},
            ],
        })
        self.assertEqual(trial["violations"], [])
        self.assertEqual(trial["lines"][0]["locked_amount"], 108_000)
        projection = trial["projection"][0]
        self.assertEqual(projection["locked_by_trial"], 162_000)
        self.assertTrue(projection["sufficient"])
        after = self.totals(self.usd["id"])
        self.assertEqual(before, after)  # 试算不占用任何额度
        # 试算结果持久化，可供审计查看
        stored = service.get_trial(self.conn, ADMIN, trial["id"])
        self.assertEqual(stored["result"]["lines"][0]["locked_amount"], 108_000)


class AdjustmentTest(ServiceTestBase):
    def test_withdrawal_releases_exact_locked_amount(self) -> None:
        # 奇数金额制造换算舍入：33333 EUR 分 → 36000 USD 分
        view = self.award(award_amount=33_333)
        self.assertEqual(view["locked_amount"], 36_000)
        result = service.adjust_award(self.conn, ADMIN, view["id"], {
            "type": "WITHDRAWAL", "reason": "学生放弃",
        })
        self.assertEqual(result["award"]["status"], "WITHDRAWN")
        totals = self.totals(self.usd["id"])
        # 释放严格按原锁定额，可用额度分文不差地恢复 —— 汇率换算不再导致对账差异
        self.assertEqual(totals["locked"], 0)
        self.assertEqual(totals["available"], 10_000_000)
        reconcile = service.audit_batch_reconcile(self.conn, ADMIN, self.usd["id"])
        self.assertTrue(reconcile["balanced"])

    def test_disqualification_retain_goes_to_fixed_residual_destination(self) -> None:
        view = self.award()
        service.adjust_award(self.conn, ADMIN, view["id"], {
            "type": "DISQUALIFICATION", "reason": "材料造假", "retain_amount": 500,
        })
        totals = self.totals(self.usd["id"])
        self.assertEqual(totals["locked"], 0)
        self.assertEqual(totals["reserve"], 500)  # 尾差进入固定去向：准备金
        self.assertEqual(totals["available"], 10_000_000 - 500)
        # 尾差有对应分录，审计可逐笔复算
        entries = ledger.entries_for_batch(self.conn, self.usd["id"])
        residual = [e for e in entries if e["type"] == "RESIDUAL"]
        self.assertEqual(len(residual), 1)
        self.assertEqual(residual[0]["amount"], 500)

    def test_deferral_moves_occupancy_between_rounds(self) -> None:
        view = self.award()
        service.adjust_award(self.conn, ADMIN, view["id"], {
            "type": "DEFERRAL", "target_round_id": self.round2["id"],
        })
        occ1 = ledger.round_occupancy(self.conn, self.round1["id"])
        occ2 = ledger.round_occupancy(self.conn, self.round2["id"])
        self.assertEqual(occ1[0]["net_locked"], 0)
        self.assertEqual(occ2[0]["net_locked"], 108_000)
        moved = service.get_award(self.conn, ADMIN, view["id"])
        self.assertEqual(moved["round_id"], self.round2["id"])
        self.assertEqual(moved["sealed_round_id"], self.round1["id"])  # 封存轮次不变

    def test_adjustment_requires_active_award(self) -> None:
        view = self.award()
        service.adjust_award(self.conn, ADMIN, view["id"], {
            "type": "WITHDRAWAL", "reason": "学生放弃",
        })
        with self.assertRaises(ApiError) as ctx:
            service.adjust_award(self.conn, ADMIN, view["id"], {
                "type": "WITHDRAWAL", "reason": "重复操作",
            })
        self.assertEqual(ctx.exception.code, "AWARD_NOT_ACTIVE")


class TransferTest(ServiceTestBase):
    def test_cross_currency_transfer(self) -> None:
        result = service.transfer(self.conn, ADMIN, {
            "from_batch_id": self.usd["id"], "to_batch_id": self.eur["id"],
            "amount": 108_000, "currency": "USD",
        })
        self.assertEqual(result["out_amount"], 108_000)
        self.assertEqual(result["in_amount"], 100_000)  # × 100/108
        usd_totals = self.totals(self.usd["id"])
        eur_totals = self.totals(self.eur["id"])
        self.assertEqual(usd_totals["transferred_out"], 108_000)
        self.assertEqual(usd_totals["available"], 10_000_000 - 108_000)
        self.assertEqual(eur_totals["transferred_in"], 100_000)
        self.assertEqual(eur_totals["available"], 5_000_000 + 100_000)
        self.assertTrue(service.audit_batch_reconcile(self.conn, ADMIN, self.usd["id"])["balanced"])
        self.assertTrue(service.audit_batch_reconcile(self.conn, ADMIN, self.eur["id"])["balanced"])

    def test_transfer_beyond_available_rejected(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            service.transfer(self.conn, ADMIN, {
                "from_batch_id": self.usd["id"], "to_batch_id": self.eur["id"],
                "amount": 20_000_000, "currency": "USD",
            })
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_FUNDS")


class AuditTest(ServiceTestBase):
    def test_round_recompute_consistent_across_lifecycle(self) -> None:
        first = self.award()
        second = self.award(candidate_id=self.cand2["id"], award_currency="USD",
                            award_amount=50_000, batch_id=self.usd["id"])
        # 第一笔延期到 R2，第二笔放弃
        service.adjust_award(self.conn, ADMIN, first["id"], {
            "type": "DEFERRAL", "target_round_id": self.round2["id"],
        })
        service.adjust_award(self.conn, ADMIN, second["id"], {
            "type": "WITHDRAWAL", "reason": "学生放弃",
        })
        recompute1 = service.audit_round_recompute(self.conn, ADMIN, self.round1["id"])
        recompute2 = service.audit_round_recompute(self.conn, ADMIN, self.round2["id"])
        self.assertTrue(recompute1["consistent"], recompute1)
        self.assertTrue(recompute2["consistent"], recompute2)
        # R1 内两笔净占用均为 0（一笔延期转出、一笔放弃释放）
        nets = {item["award_id"]: item["net_entries_in_round"] for item in recompute1["awards"]}
        self.assertEqual(nets[first["id"]], 0)
        self.assertEqual(nets[second["id"]], 0)
        # R2 内第一笔净占用等于锁定额
        nets2 = {item["award_id"]: item["net_entries_in_round"] for item in recompute2["awards"]}
        self.assertEqual(nets2[first["id"]], 108_000)

    def test_batch_ledger_running_balances(self) -> None:
        view = self.award()
        service.adjust_award(self.conn, ADMIN, view["id"], {
            "type": "DISQUALIFICATION", "reason": "资格丧失", "retain_amount": 1,
        })
        ledger_view = service.audit_batch_ledger(self.conn, ADMIN, self.usd["id"])
        entries = ledger_view["entries"]
        self.assertEqual([e["type"] for e in entries], ["LOCK", "RELEASE", "RESIDUAL"])
        self.assertEqual(entries[0]["locked_after"], 108_000)
        self.assertEqual(entries[1]["available_after"], 10_000_000)
        self.assertEqual(entries[2]["available_after"], 10_000_000 - 1)


class ApplicantScopeTest(ServiceTestBase):
    def test_applicant_sees_only_own_results(self) -> None:
        self.award()
        self.award(candidate_id=self.cand2["id"], batch_id=self.eur["id"], award_currency="EUR")
        own = service.my_awards(self.conn, applicant("S1"))
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["award_amount"], 100_000)
        other = service.my_awards(self.conn, applicant("S2"))
        self.assertEqual(len(other), 1)
        nobody = service.my_awards(self.conn, applicant("S3"))
        self.assertEqual(nobody, [])
        # 申请人角色不能调用管理接口
        with self.assertRaises(ApiError) as ctx:
            service.list_batches(self.conn, applicant("S1"))
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
