"""HTTP 端到端测试：真实起服务、走鉴权与并发。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scholarship import db
from scholarship.api import make_server
from scholarship.seed import seed

ADMIN = "demo-admin"
FUNDER = "demo-funder"
REV1 = "demo-rev1"
APP1 = "demo-app1"
APP2 = "demo-app2"
AUDITOR = "demo-auditor"


class ApiTestBase(unittest.TestCase):
    """每个用例独立数据库与独立服务进程，互不影响。"""

    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = db.connect(self.db_path)
        db.init_db(conn)
        seed(conn)
        conn.close()
        self.server = make_server(self.db_path, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def request(self, method: str, path: str, token: str | None = None, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class AuthzTest(ApiTestBase):
    def test_health_is_public(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_missing_token_is_401(self) -> None:
        status, body = self.request("GET", "/batches")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

    def test_wrong_role_is_403(self) -> None:
        status, _ = self.request("GET", "/batches", token=APP1)
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/batches", token=AUDITOR, body={})
        self.assertEqual(status, 403)

    def test_unknown_route_is_404(self) -> None:
        status, body = self.request("GET", "/nope", token=ADMIN)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_applicant_reads_only_own_results(self) -> None:
        status, own = self.request("GET", "/me/awards", token=APP1)
        self.assertEqual(status, 200)
        self.assertTrue(all(item["batch_name"] for item in own))
        # 申请人不能访问管理/审计接口，即使查自己的授予
        status, _ = self.request("GET", "/awards/1", token=APP1)
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/audit/batches/1/ledger", token=APP1)
        self.assertEqual(status, 403)

    def test_funder_scoped_to_own_batches(self) -> None:
        status, batches = self.request("GET", "/batches", token=FUNDER)
        self.assertEqual(status, 200)
        self.assertTrue(batches)
        self.assertTrue(all(b["funder_id"] == "FUNDER-RIVERSIDE" for b in batches))
        # 资助方开批次时 funder_id 强制为本资助方
        status, created = self.request("POST", "/batches", token=FUNDER, body={
            "name": "资助方自建池", "funder_id": "SOMEONE-ELSE", "currency": "USD",
            "total_amount": 1000, "fx_rate_set_id": 1,
        })
        self.assertEqual(status, 201)
        self.assertEqual(created["funder_id"], "FUNDER-RIVERSIDE")


class AwardFlowTest(ApiTestBase):
    """完整流程：试算 → 授予（幂等）→ 申请人自查 → 放弃 → 审计复算。"""

    def test_full_lifecycle(self) -> None:
        # 1. 评审组试算：EUR 1000.00 → USD 1080.00
        status, trial = self.request("POST", "/trials", token=REV1, body={
            "round_id": 1,
            "lines": [{"candidate_id": 1, "batch_id": 1,
                       "award_amount": 100_000, "award_currency": "EUR"}],
        })
        self.assertEqual(status, 201)
        self.assertEqual(trial["lines"][0]["locked_amount"], 108_000)
        self.assertEqual(trial["violations"], [])

        # 2. 正式授予：原子锁定 + 快照封存
        status, award = self.request("POST", "/awards", token=ADMIN, body={
            "round_id": 1, "candidate_id": 1, "batch_id": 1,
            "award_amount": 100_000, "award_currency": "EUR",
            "panel": ["rev-chen"], "request_id": "lc-1",
        })
        self.assertEqual(status, 201)
        self.assertEqual(award["locked_amount"], 108_000)
        self.assertEqual(award["snapshot"]["panel"], ["rev-chen"])

        # 3. 幂等重试：同 request_id 返回同一授予，不重复锁定
        status, again = self.request("POST", "/awards", token=ADMIN, body={
            "round_id": 1, "candidate_id": 1, "batch_id": 1,
            "award_amount": 100_000, "award_currency": "EUR",
            "panel": ["rev-chen"], "request_id": "lc-1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(again["id"], award["id"])

        # 4. 重复授予同一来源额度：拒绝
        status, dup = self.request("POST", "/awards", token=ADMIN, body={
            "round_id": 1, "candidate_id": 1, "batch_id": 1,
            "award_amount": 50_000, "award_currency": "EUR",
            "panel": ["rev-chen"], "request_id": "lc-2",
        })
        self.assertEqual(status, 409)
        self.assertEqual(dup["error"]["code"], "AWARD_CONFLICT")

        # 5. 批次余额反映锁定
        status, batch = self.request("GET", "/batches/1", token=AUDITOR)
        self.assertEqual(batch["totals"]["locked"], 108_000)

        # 6. 申请人只能看到自己的结果
        status, mine = self.request("GET", "/me/awards", token=APP1)
        self.assertEqual([item["id"] for item in mine], [award["id"]])
        status, other = self.request("GET", "/me/awards", token=APP2)
        self.assertEqual(other, [])

        # 7. 回避关系：rev-wang 与 S002 有回避，不能参与授予
        status, recused = self.request("POST", "/awards", token=ADMIN, body={
            "round_id": 1, "candidate_id": 2, "batch_id": 1,
            "award_amount": 10_000, "award_currency": "USD",
            "panel": ["rev-wang"],
        })
        self.assertEqual(status, 422)
        self.assertEqual(recused["error"]["code"], "RECUSAL_CONFLICT")

        # 8. 放弃：分录结算，额度分文不差释放
        status, adjusted = self.request(
            "POST", f"/awards/{award['id']}/adjustments", token=ADMIN,
            body={"type": "WITHDRAWAL", "reason": "学生放弃"})
        self.assertEqual(status, 200)
        self.assertEqual(adjusted["award"]["status"], "WITHDRAWN")

        # 9. 审计复算：轮次一致、批次平衡
        status, recompute = self.request("GET", "/audit/rounds/1/recompute", token=AUDITOR)
        self.assertEqual(status, 200)
        self.assertTrue(recompute["consistent"], recompute)
        status, reconcile = self.request("GET", "/audit/batches/1/reconcile", token=AUDITOR)
        self.assertTrue(reconcile["balanced"], reconcile)
        self.assertEqual(reconcile["totals"]["available"], reconcile["totals"]["total"])


class ConcurrencyTest(ApiTestBase):
    def test_concurrent_double_award_only_one_wins(self) -> None:
        """两个评审组同时授予同一申请人同一来源额度：只有一笔成功。"""
        barrier = threading.Barrier(2)
        results: list[tuple[int, dict]] = []
        lock = threading.Lock()

        def worker(request_id: str) -> None:
            barrier.wait()
            status, body = self.request("POST", "/awards", token=ADMIN, body={
                "round_id": 1, "candidate_id": 2, "batch_id": 1,
                "award_amount": 10_000, "award_currency": "USD",
                "panel": ["rev-chen"], "request_id": request_id,
            })
            with lock:
                results.append((status, body))

        threads = [
            threading.Thread(target=worker, args=("race-a",)),
            threading.Thread(target=worker, args=("race-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [201, 409], results)
        conflict_body = next(body for status, body in results if status == 409)
        self.assertEqual(conflict_body["error"]["code"], "AWARD_CONFLICT")

        # 账本只锁定了一笔
        status, reconcile = self.request("GET", "/audit/batches/1/reconcile", token=AUDITOR)
        self.assertTrue(reconcile["balanced"], reconcile)
        self.assertEqual(reconcile["locked_per_ledger"], 10_000)
        self.assertEqual(reconcile["locked_per_awards"], 10_000)


if __name__ == "__main__":
    unittest.main()
