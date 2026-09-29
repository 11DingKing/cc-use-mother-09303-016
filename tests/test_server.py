"""服务端业务层回归测试：额度锁定、尾差归集、分录结算与审计复算。"""
from __future__ import annotations

import sys
import unittest
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarship_server import services
from scholarship_server.db import connect, init_db
from scholarship_server.errors import ApiError
from scholarship_server.seed import seed_users


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        init_db(self.db_path)
        self.conn = connect(self.db_path)
        seed_users(self.conn)
        self.office = self.actor("office")
        self.auditor = self.actor("auditor")
        self.panel_a = self.actor("panel-a")
        self.panel_b = self.actor("panel-b")
        self.stu1 = self.actor("stu-001")
        self.stu2 = self.actor("stu-002")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def actor(self, actor_id):
        return self.conn.execute(
            "SELECT * FROM users WHERE actor_id=?", (actor_id,)).fetchone()

    def make_batches(self):
        _, usd = services.create_batch(self.conn, self.office, {
            "funder": "甲基金会", "name": "美元池", "currency": "USD",
            "total_minor": 1_000_000})
        _, cny = services.create_batch(self.conn, self.office, {
            "funder": "乙基金会", "name": "人民币池", "currency": "CNY",
            "total_minor": 10_000_000})
        return usd, cny

    def make_round(self, rates=None, name="2026 秋季轮"):
        rates = rates if rates is not None else [
            {"base_currency": "USD", "quote_currency": "CNY",
             "num": 710, "den": 100}]
        _, rnd = services.create_round(self.conn, self.office,
                                       {"name": name, "rates": rates})
        return rnd

    def verify_student(self, stu_actor, payload=None):
        payload = payload or {"gpa": 3.8, "degree_level": "master",
                              "nationality": "CN"}
        services.submit_materials(self.conn, stu_actor, {"payload": payload})
        services.verify_materials(self.conn, self.office,
                                  stu_actor["applicant_id"])

    def review(self, panel, round_id, applicant_id, reviewer="rev-1", score=90):
        return services.submit_review(self.conn, panel, round_id, {
            "applicant_id": applicant_id, "reviewer_id": reviewer,
            "score": score})

    def award(self, panel, round_id, applicant_id, batch_id, amount, currency,
              **extra):
        return services.create_award(self.conn, panel, round_id, {
            "applicant_id": applicant_id, "batch_id": batch_id,
            "amount_minor": amount, "currency": currency, **extra})


class AwardLockingTest(ServiceTestBase):
    def test_double_award_same_source_blocked(self):
        """两个评审组同时授予同一来源额度：只有一笔成功。"""
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        self.review(self.panel_b, rnd["id"], "stu-001", reviewer="rev-b")
        status, first = self.award(self.panel_a, rnd["id"], "stu-001",
                                   usd["id"], 500_000, "USD")
        self.assertEqual(status, 201)
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_b, rnd["id"], "stu-001", usd["id"],
                       500_000, "USD")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "AWARD_CONFLICT")
        _, batch = services.get_batch(self.conn, self.office, usd["id"])
        self.assertEqual(batch["locked"], 500_000)

    def test_award_locks_quota_and_seals_snapshot(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        status, award = self.award(self.panel_a, rnd["id"], "stu-001",
                                   usd["id"], 500_000, "USD")
        self.assertEqual(status, 201)
        _, batch = services.get_batch(self.conn, self.office, usd["id"])
        self.assertEqual(batch["locked"], 500_000)
        self.assertEqual(batch["available"], 500_000)
        status, snap = services.get_snapshot(self.conn, self.auditor, award["id"])
        self.assertEqual(status, 200)
        self.assertTrue(snap["verified"])
        self.assertEqual(snap["payload"]["panel_id"], "panel-a")
        self.assertEqual(snap["payload"]["materials"]["status"], "VERIFIED")
        self.assertEqual(len(snap["payload"]["reviews"]), 1)
        self.assertEqual(snap["payload"]["award"]["lock_amount_minor"], 500_000)

    def test_quota_exhaustion_rejected(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_a, rnd["id"], "stu-001", usd["id"],
                       1_000_001, "USD")
        self.assertEqual(ctx.exception.code, "QUOTA_EXHAUSTED")

    def test_award_requires_verified_materials_and_review(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_a, rnd["id"], "stu-001", usd["id"],
                       1000, "USD")
        self.assertEqual(ctx.exception.code, "MATERIALS_NOT_VERIFIED")
        self.verify_student(self.stu1)
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_a, rnd["id"], "stu-001", usd["id"],
                       1000, "USD")
        self.assertEqual(ctx.exception.code, "NOT_REVIEWED")

    def test_idempotent_award_replay(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        status, first = self.award(self.panel_a, rnd["id"], "stu-001",
                                   usd["id"], 500_000, "USD",
                                   idempotency_key="key-1")
        self.assertEqual(status, 201)
        status, replay = self.award(self.panel_a, rnd["id"], "stu-001",
                                    usd["id"], 500_000, "USD",
                                    idempotency_key="key-1")
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["id"], first["id"])
        _, batch = services.get_batch(self.conn, self.office, usd["id"])
        self.assertEqual(batch["locked"], 500_000)

    def test_sealed_round_rejects_award(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        services.seal_round(self.conn, self.office, rnd["id"])
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_a, rnd["id"], "stu-001", usd["id"],
                       1000, "USD")
        self.assertEqual(ctx.exception.code, "ROUND_SEALED")


class FxDustTest(ServiceTestBase):
    def test_cross_currency_award_routes_dust_to_residual(self):
        """汇率换算尾差进入固定归集批次，账面精确对平。"""
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        # 100000 CNY 分 → USD 分：100000 * 100 / 710 = 14084.507… → 锁定 14085
        status, award = self.award(self.panel_a, rnd["id"], "stu-001",
                                   usd["id"], 100_000, "CNY")
        self.assertEqual(status, 201)
        self.assertEqual(award["lock_amount_minor"], 14085)
        _, batch = services.get_batch(self.conn, self.office, usd["id"])
        self.assertEqual(batch["locked"], 14085)
        _, res = services.residuals(self.conn, self.auditor)
        self.assertEqual(len(res["residuals"]), 1)
        entry = res["residuals"][0]
        self.assertEqual(entry["currency"], "USD")
        # 尾差 = 精确值 - 入账整数 = 10000000/710 - 14085 = -35/71
        self.assertEqual(entry["balance"], str(Fraction(100_000 * 100, 710)
                                               - 14085))
        # 锁定整数 + 尾差 = 精确值，账务可对平
        exact = Fraction(100_000 * 100, 710)
        self.assertEqual(Fraction(14085) + Fraction(entry["balance"]), exact)

    def test_same_currency_award_has_no_dust(self):
        _, cny = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        self.award(self.panel_a, rnd["id"], "stu-001", cny["id"], 100_000, "CNY")
        _, res = services.residuals(self.conn, self.auditor)
        self.assertEqual(res["residuals"], [])


class SettlementTest(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.usd, self.cny = self.make_batches()
        self.rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, self.rnd["id"], "stu-001", reviewer="rev-a")
        _, self.award1 = self.award(self.panel_a, self.rnd["id"], "stu-001",
                                    self.cny["id"], 100_000, "CNY")

    def test_withdraw_releases_quota_and_frees_slot(self):
        status, settled = services.settle_award(
            self.conn, self.office, self.award1["id"], {"action": "withdraw"})
        self.assertEqual(status, 200)
        self.assertEqual(settled["status"], "WITHDRAWN")
        _, batch = services.get_batch(self.conn, self.office, self.cny["id"])
        self.assertEqual(batch["locked"], 0)
        # 放弃后同一轮次可重新授予
        status, _ = self.award(self.panel_a, self.rnd["id"], "stu-001",
                               self.cny["id"], 100_000, "CNY")
        self.assertEqual(status, 201)

    def test_forfeit_records_forfeit_entry(self):
        services.settle_award(self.conn, self.office, self.award1["id"],
                              {"action": "forfeit"})
        _, ledger = services.batch_ledger(self.conn, self.auditor,
                                          self.cny["id"])
        types = [e["type"] for e in ledger["entries"]]
        self.assertEqual(types, ["LOCK", "FORFEIT"])
        _, batch = services.get_batch(self.conn, self.office, self.cny["id"])
        self.assertEqual(batch["locked"], 0)

    def test_double_settle_rejected(self):
        services.settle_award(self.conn, self.office, self.award1["id"],
                              {"action": "withdraw"})
        with self.assertRaises(ApiError) as ctx:
            services.settle_award(self.conn, self.office, self.award1["id"],
                                  {"action": "forfeit"})
        self.assertEqual(ctx.exception.code, "ALREADY_SETTLED")

    def test_defer_moves_occupancy_to_next_round(self):
        rnd2 = self.make_round(name="2027 春季轮")
        status, deferred = services.settle_award(
            self.conn, self.office, self.award1["id"],
            {"action": "defer", "to_round_id": rnd2["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(deferred["status"], "DEFERRED")
        # 批次总占用不变，但占用从第一轮移动到第二轮
        _, batch = services.get_batch(self.conn, self.office, self.cny["id"])
        self.assertEqual(batch["locked"], 100_000)
        _, rep1 = services.recompute_round(self.conn, self.auditor,
                                           self.rnd["id"])
        _, rep2 = services.recompute_round(self.conn, self.auditor,
                                           rnd2["id"])
        delta1 = {b["batch_id"]: b["round_locked_delta"] for b in rep1["batches"]}
        delta2 = {b["batch_id"]: b["round_locked_delta"] for b in rep2["batches"]}
        self.assertEqual(delta1[self.cny["id"]], 0)   # LOCK + DEFER_RELEASE 相抵
        self.assertEqual(delta2[self.cny["id"]], 100_000)  # DEFER_LOCK
        self.assertTrue(rep1["verified"])
        self.assertTrue(rep2["verified"])
        # 延期后放弃：释放发生在新轮次
        services.settle_award(self.conn, self.office, self.award1["id"],
                              {"action": "withdraw"})
        _, batch = services.get_batch(self.conn, self.office, self.cny["id"])
        self.assertEqual(batch["locked"], 0)


class TransferTest(ServiceTestBase):
    def test_same_currency_transfer(self):
        usd, _ = self.make_batches()
        _, usd2 = services.create_batch(self.conn, self.office, {
            "funder": "丙基金会", "name": "美元二池", "currency": "USD",
            "total_minor": 500_000})
        status, tr = services.transfer(self.conn, self.office, {
            "from_batch_id": usd["id"], "to_batch_id": usd2["id"],
            "amount_minor": 200_000})
        self.assertEqual(status, 201)
        self.assertEqual(tr["converted_minor"], 200_000)
        _, src = services.get_batch(self.conn, self.office, usd["id"])
        _, dst = services.get_batch(self.conn, self.office, usd2["id"])
        self.assertEqual(src["locked"], 0)          # 转移不占用授予额度
        self.assertEqual(src["balance"], 800_000)   # 余额随转出减少
        self.assertEqual(src["available"], 800_000)
        self.assertEqual(dst["balance"], 700_000)   # 转入增加目标池余额
        self.assertEqual(dst["available"], 700_000)

    def test_cross_currency_transfer_with_dust(self):
        usd, _ = self.make_batches()
        _, eur = services.create_batch(self.conn, self.office, {
            "funder": "丁基金会", "name": "欧元池", "currency": "EUR",
            "total_minor": 800_000})
        rnd = self.make_round(rates=[
            {"base_currency": "USD", "quote_currency": "EUR",
             "num": 9173, "den": 10000}])
        status, tr = services.transfer(self.conn, self.office, {
            "from_batch_id": usd["id"], "to_batch_id": eur["id"],
            "amount_minor": 100_001, "round_id": rnd["id"]})
        self.assertEqual(status, 201)
        # 100001 * 9173 / 10000 = 91730.9173 → 91731，尾差入归集批次
        self.assertEqual(tr["converted_minor"], 91731)
        _, res = services.residuals(self.conn, self.auditor)
        eur_residual = [r for r in res["residuals"] if r["currency"] == "EUR"]
        self.assertEqual(len(eur_residual), 1)
        exact = Fraction(100_001 * 9173, 10_000)
        self.assertEqual(Fraction(eur_residual[0]["balance"]), exact - 91731)
        _, rep = services.recompute_round(self.conn, self.auditor, rnd["id"])
        self.assertTrue(rep["verified"])

    def test_transfer_requires_round_for_cross_currency(self):
        usd, _ = self.make_batches()
        _, eur = services.create_batch(self.conn, self.office, {
            "funder": "丁基金会", "name": "欧元池", "currency": "EUR",
            "total_minor": 800_000})
        with self.assertRaises(ApiError) as ctx:
            services.transfer(self.conn, self.office, {
                "from_batch_id": usd["id"], "to_batch_id": eur["id"],
                "amount_minor": 1000})
        self.assertEqual(ctx.exception.status, 400)

    def test_transfer_quota_check(self):
        usd, _ = self.make_batches()
        _, usd2 = services.create_batch(self.conn, self.office, {
            "funder": "丙基金会", "name": "美元二池", "currency": "USD",
            "total_minor": 500_000})
        with self.assertRaises(ApiError) as ctx:
            services.transfer(self.conn, self.office, {
                "from_batch_id": usd["id"], "to_batch_id": usd2["id"],
                "amount_minor": 1_000_001})
        self.assertEqual(ctx.exception.code, "QUOTA_EXHAUSTED")


class RuleAndRecusalTest(ServiceTestBase):
    def test_recusal_blocks_review(self):
        rnd = self.make_round()
        services.create_recusal(self.conn, self.office, {
            "reviewer_id": "rev-x", "applicant_id": "stu-001",
            "reason": "亲属关系"})
        with self.assertRaises(ApiError) as ctx:
            self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-x")
        self.assertEqual(ctx.exception.code, "RECUSAL_REQUIRED")

    def test_min_gpa_rule_blocks_award(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        services.create_rule(self.conn, self.office, {
            "type": "MIN_GPA", "params": {"min": 3.5}})
        self.verify_student(self.stu2, payload={"gpa": 3.0,
                                                "degree_level": "master",
                                                "nationality": "CN"})
        self.review(self.panel_a, rnd["id"], "stu-002", reviewer="rev-a")
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_a, rnd["id"], "stu-002", usd["id"],
                       1000, "USD")
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")
        self.assertEqual(ctx.exception.details[0]["type"], "MIN_GPA")

    def test_max_total_per_applicant_rule(self):
        usd, cny = self.make_batches()
        rnd = self.make_round()
        services.create_rule(self.conn, self.office, {
            "type": "MAX_TOTAL_PER_APPLICANT",
            "params": {"currency": "CNY", "amount_minor": 510_000}})
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        self.review(self.panel_b, rnd["id"], "stu-001", reviewer="rev-b")
        # 第一笔 500000 CNY 分，通过
        self.award(self.panel_a, rnd["id"], "stu-001", cny["id"],
                   500_000, "CNY")
        # 第二笔 2000 USD 分 ≈ 14200 CNY 分，累计超限
        with self.assertRaises(ApiError) as ctx:
            self.award(self.panel_b, rnd["id"], "stu-001", usd["id"],
                       2_000, "USD")
        self.assertEqual(ctx.exception.code, "RULE_VIOLATION")

    def test_batch_scoped_rule_only_applies_to_that_batch(self):
        usd, cny = self.make_batches()
        rnd = self.make_round()
        services.create_rule(self.conn, self.office, {
            "type": "DEGREE_LEVEL", "batch_id": usd["id"],
            "params": {"allow": ["phd"]}})
        self.verify_student(self.stu1)  # master
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        with self.assertRaises(ApiError):
            self.award(self.panel_a, rnd["id"], "stu-001", usd["id"],
                       1000, "USD")
        # 人民币池不受该规则限制
        status, _ = self.award(self.panel_a, rnd["id"], "stu-001",
                               cny["id"], 1000, "CNY")
        self.assertEqual(status, 201)


class TrialTest(ServiceTestBase):
    def test_trial_simulates_without_writes(self):
        usd, cny = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        status, result = services.trial(self.conn, self.panel_a, rnd["id"], {
            "lines": [
                {"applicant_id": "stu-001", "batch_id": cny["id"],
                 "amount_minor": 100_000, "currency": "CNY"},
                {"applicant_id": "stu-001", "batch_id": cny["id"],
                 "amount_minor": 200_000, "currency": "CNY"},
                {"applicant_id": "stu-001", "batch_id": usd["id"],
                 "amount_minor": 100_000, "currency": "CNY"},
            ]})
        self.assertEqual(status, 200)
        self.assertTrue(result["all_ok"])
        lines = result["lines"]
        # 第二行看到第一行消耗的可用额度
        self.assertEqual(lines[0]["available_before"], 10_000_000)
        self.assertEqual(lines[1]["available_before"], 9_900_000)
        # 跨币种行给出锁定额与尾差
        self.assertEqual(lines[2]["lock_amount_minor"], 14085)
        self.assertEqual(lines[2]["dust"], str(Fraction(100_000 * 100, 710)
                                               - 14085))
        # 试算不产生任何占用
        _, batch = services.get_batch(self.conn, self.office, cny["id"])
        self.assertEqual(batch["locked"], 0)
        _, res = services.residuals(self.conn, self.auditor)
        self.assertEqual(res["residuals"], [])

    def test_trial_flags_insufficient_quota(self):
        usd, _ = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        _, result = services.trial(self.conn, self.panel_a, rnd["id"], {
            "lines": [{"applicant_id": "stu-001", "batch_id": usd["id"],
                       "amount_minor": 2_000_000, "currency": "USD"}]})
        self.assertFalse(result["all_ok"])
        self.assertFalse(result["lines"][0]["quota_sufficient"])


class AuditTest(ServiceTestBase):
    def test_recompute_round_and_batch_ledger(self):
        usd, cny = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        _, award = self.award(self.panel_a, rnd["id"], "stu-001",
                              cny["id"], 100_000, "CNY")
        services.settle_award(self.conn, self.office, award["id"],
                              {"action": "withdraw"})
        status, rep = services.recompute_round(self.conn, self.auditor,
                                               rnd["id"])
        self.assertEqual(status, 200)
        self.assertTrue(rep["verified"])
        self.assertEqual(rep["entries_in_round"], 2)
        cny_report = [b for b in rep["batches"]
                      if b["batch_id"] == cny["id"]][0]
        self.assertEqual(cny_report["round_locked_delta"], 0)
        self.assertEqual(cny_report["locked"], 0)
        self.assertEqual(cny_report["available"], 10_000_000)
        _, ledger = services.batch_ledger(self.conn, self.auditor, cny["id"])
        self.assertEqual([e["type"] for e in ledger["entries"]],
                         ["LOCK", "RELEASE"])
        self.assertEqual(ledger["entries"][0]["running_locked"], 100_000)
        self.assertEqual(ledger["entries"][1]["running_locked"], 0)

    def test_recompute_detects_snapshot_tampering(self):
        _, cny = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        _, award = self.award(self.panel_a, rnd["id"], "stu-001",
                              cny["id"], 100_000, "CNY")
        # 直接篡改快照内容，审计复算必须发现
        self.conn.execute(
            "UPDATE snapshots SET payload = replace(payload, '100000', '999999')"
            " WHERE award_id=?", (award["id"],))
        _, rep = services.recompute_round(self.conn, self.auditor, rnd["id"])
        self.assertFalse(rep["verified"])
        self.assertTrue(any("快照" in a for a in rep["anomalies"]))


class RoleTest(ServiceTestBase):
    def test_office_only_operations(self):
        with self.assertRaises(ApiError) as ctx:
            services.create_batch(self.conn, self.auditor, {
                "funder": "x", "name": "x", "currency": "USD",
                "total_minor": 1})
        self.assertEqual(ctx.exception.status, 403)

    def test_panel_cannot_settle(self):
        _, cny = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        _, award = self.award(self.panel_a, rnd["id"], "stu-001",
                              cny["id"], 1000, "CNY")
        with self.assertRaises(ApiError) as ctx:
            services.settle_award(self.conn, self.panel_a, award["id"],
                                  {"action": "withdraw"})
        self.assertEqual(ctx.exception.status, 403)

    def test_applicant_sees_only_own_awards(self):
        _, cny = self.make_batches()
        rnd = self.make_round()
        self.verify_student(self.stu1)
        self.verify_student(self.stu2)
        self.review(self.panel_a, rnd["id"], "stu-001", reviewer="rev-a")
        self.review(self.panel_a, rnd["id"], "stu-002", reviewer="rev-a")
        self.award(self.panel_a, rnd["id"], "stu-001", cny["id"], 1000, "CNY")
        self.award(self.panel_a, rnd["id"], "stu-002", cny["id"], 2000, "CNY")
        _, mine = services.my_awards(self.conn, self.stu1)
        self.assertEqual(len(mine["awards"]), 1)
        self.assertEqual(mine["awards"][0]["award_amount_minor"], 1000)


if __name__ == "__main__":
    unittest.main()
