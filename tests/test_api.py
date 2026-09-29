"""HTTP 接口测试：认证、角色边界、申请人可见性与并发双授防线。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarship_server.app import create_server
from scholarship_server.db import connect, init_db
from scholarship_server.seed import seed_users


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "api.db")
        init_db(cls.db_path)
        conn = connect(cls.db_path)
        seed_users(conn)
        conn.close()
        cls.server = create_server(cls.db_path, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    @classmethod
    def call(cls, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request(method, path,
                     json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    @classmethod
    def token(cls, actor_id, secret):
        status, data = cls.call("POST", "/tokens",
                                {"actor_id": actor_id, "secret": secret})
        assert status == 200, data
        return data["token"]


class AuthTest(ApiTestBase):
    def test_login_and_bad_credentials(self):
        status, data = self.call("POST", "/tokens",
                                 {"actor_id": "office",
                                  "secret": "office-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(data["role"], "office")
        status, _ = self.call("POST", "/tokens",
                              {"actor_id": "office", "secret": "wrong"})
        self.assertEqual(status, 401)

    def test_missing_token_rejected(self):
        status, data = self.call("GET", "/fund-batches")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "UNAUTHORIZED")

    def test_contract_meta_public(self):
        status, data = self.call("GET", "/meta/contract")
        self.assertEqual(status, 200)
        self.assertEqual(data["product"], "国际奖学金配置")


class RoleBoundaryTest(ApiTestBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.office_t = cls.token("office", "office-secret")
        cls.auditor_t = cls.token("auditor", "auditor-secret")
        cls.panel_t = cls.token("panel-a", "panel-a-secret")
        cls.stu1_t = cls.token("stu-001", "stu-001-secret")
        cls.stu2_t = cls.token("stu-002", "stu-002-secret")

    def test_applicant_cannot_read_batches_or_others(self):
        status, _ = self.call("GET", "/fund-batches", token=self.stu1_t)
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/candidates", token=self.stu1_t)
        self.assertEqual(status, 403)

    def test_auditor_is_read_only(self):
        status, _ = self.call("POST", "/fund-batches",
                              {"funder": "x", "name": "x", "currency": "USD",
                               "total_minor": 1}, token=self.auditor_t)
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/audit/residuals", token=self.auditor_t)
        self.assertEqual(status, 200)

    def test_panel_cannot_manage_batches(self):
        status, _ = self.call("POST", "/fund-batches",
                              {"funder": "x", "name": "x", "currency": "USD",
                               "total_minor": 1}, token=self.panel_t)
        self.assertEqual(status, 403)


class EndToEndTest(ApiTestBase):
    """完整流程：建池 → 锁口径 → 材料 → 评审 → 试算 → 授予 → 结算 → 审计。"""

    def test_full_flow_and_applicant_scoping(self):
        office = self.token("office", "office-secret")
        auditor = self.token("auditor", "auditor-secret")
        panel_a = self.token("panel-a", "panel-a-secret")
        stu1 = self.token("stu-001", "stu-001-secret")
        stu2 = self.token("stu-002", "stu-002-secret")

        status, usd = self.call("POST", "/fund-batches",
                                {"funder": "甲基金会", "name": "美元池",
                                 "currency": "USD", "total_minor": 1_000_000},
                                token=office)
        self.assertEqual(status, 201)
        status, rnd = self.call("POST", "/rounds",
                                {"name": "2026 秋季轮",
                                 "rates": [{"base_currency": "USD",
                                            "quote_currency": "CNY",
                                            "num": 710, "den": 100}]},
                                token=office)
        self.assertEqual(status, 201)
        self.assertEqual(len(rnd["pinned_rates"]), 2)  # 正反向同时锁定

        # 两名学生提交材料，办公室核验
        for stu, token in (("stu-001", stu1), ("stu-002", stu2)):
            status, _ = self.call("POST", "/me/materials",
                                  {"payload": {"gpa": 3.9,
                                               "degree_level": "master",
                                               "nationality": "CN"}},
                                  token=token)
            self.assertEqual(status, 201)
            status, _ = self.call("POST", f"/candidates/{stu}/verify",
                                  token=office)
            self.assertEqual(status, 200)

        # 评审组评审
        for stu in ("stu-001", "stu-002"):
            status, _ = self.call("POST", f"/rounds/{rnd['id']}/reviews",
                                  {"applicant_id": stu, "reviewer_id": "rev-a",
                                   "score": 88}, token=panel_a)
            self.assertEqual(status, 201)

        # 试算
        status, trial = self.call("POST", f"/rounds/{rnd['id']}/trial",
                                  {"lines": [
                                      {"applicant_id": "stu-001",
                                       "batch_id": usd["id"],
                                       "amount_minor": 100_000,
                                       "currency": "CNY"}]},
                                  token=panel_a)
        self.assertEqual(status, 200)
        self.assertTrue(trial["all_ok"])

        # 正式授予两笔
        awards = {}
        for stu, amount in (("stu-001", 100_000), ("stu-002", 200_000)):
            status, award = self.call("POST", f"/rounds/{rnd['id']}/awards",
                                      {"applicant_id": stu,
                                       "batch_id": usd["id"],
                                       "amount_minor": amount,
                                       "currency": "CNY"},
                                      token=panel_a)
            self.assertEqual(status, 201)
            awards[stu] = award

        # 申请人只能查到自己的结果
        status, mine1 = self.call("GET", "/me/awards", token=stu1)
        self.assertEqual(status, 200)
        self.assertEqual(len(mine1["awards"]), 1)
        self.assertEqual(mine1["awards"][0]["id"], awards["stu-001"]["id"])
        status, mine2 = self.call("GET", "/me/awards", token=stu2)
        self.assertEqual(len(mine2["awards"]), 1)
        self.assertEqual(mine2["awards"][0]["id"], awards["stu-002"]["id"])

        # 学生一放弃，办公室结算
        status, settled = self.call(
            "POST", f"/awards/{awards['stu-001']['id']}/settle",
            {"action": "withdraw"}, token=office)
        self.assertEqual(status, 200)
        self.assertEqual(settled["status"], "WITHDRAWN")

        # 审计复算：尾差归集、占用变化可重放
        status, rep = self.call("GET",
                                f"/audit/rounds/{rnd['id']}/recompute",
                                token=auditor)
        self.assertEqual(status, 200)
        self.assertTrue(rep["verified"])
        self.assertEqual(len(rep["rounding_dust"]), 1)
        self.assertEqual(rep["rounding_dust"][0]["currency"], "USD")
        status, ledger = self.call(
            "GET", f"/audit/batches/{usd['id']}/ledger", token=auditor)
        self.assertEqual(status, 200)
        types = [e["type"] for e in ledger["entries"]]
        self.assertEqual(types, ["LOCK", "LOCK", "RELEASE"])

        # 快照可独立校验
        status, snap = self.call(
            "GET", f"/awards/{awards['stu-002']['id']}/snapshot",
            token=auditor)
        self.assertEqual(status, 200)
        self.assertTrue(snap["verified"])


class ConcurrentAwardTest(ApiTestBase):
    def test_concurrent_double_award_race(self):
        """两个评审组并发授予同一申请人同一批次：恰有一笔成功。"""
        office = self.token("office", "office-secret")
        panel_a = self.token("panel-a", "panel-a-secret")
        panel_b = self.token("panel-b", "panel-b-secret")
        stu3 = self.token("stu-003", "stu-003-secret")

        _, usd = self.call("POST", "/fund-batches",
                           {"funder": "甲基金会", "name": "并发池",
                            "currency": "USD", "total_minor": 1_000_000},
                           token=office)
        _, rnd = self.call("POST", "/rounds", {"name": "并发轮"}, token=office)
        self.call("POST", "/me/materials",
                  {"payload": {"gpa": 4.0}}, token=stu3)
        self.call("POST", "/candidates/stu-003/verify", token=office)
        for panel, rev in ((panel_a, "rev-a"), (panel_b, "rev-b")):
            self.call("POST", f"/rounds/{rnd['id']}/reviews",
                      {"applicant_id": "stu-003", "reviewer_id": rev,
                       "score": 95}, token=panel)

        barrier = threading.Barrier(2)
        results = []

        def fire(panel_token):
            barrier.wait()
            results.append(self.call(
                "POST", f"/rounds/{rnd['id']}/awards",
                {"applicant_id": "stu-003", "batch_id": usd["id"],
                 "amount_minor": 500_000, "currency": "USD"},
                token=panel_token))

        threads = [threading.Thread(target=fire, args=(t,))
                   for t in (panel_a, panel_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [201, 409])
        conflict = [r for r in results if r[0] == 409][0]
        self.assertEqual(conflict[1]["error"]["code"], "AWARD_CONFLICT")
        # 额度只被锁定一次
        _, batch = self.call("GET", f"/fund-batches/{usd['id']}",
                             token=office)
        self.assertEqual(batch["locked"], 500_000)


if __name__ == "__main__":
    unittest.main()
