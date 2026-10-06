"""HTTP API 端到端测试：真实起线程跑 http.server，走 socket 调用。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from return_claim.api import create_server  # noqa: E402
from return_claim.events import EventStore  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = create_server("127.0.0.1", 0, None)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_lifecycle_and_reconciliation_api(self) -> None:
        cid = "0910033-019-A"
        actor_r = {"actor": "采购计划员", "reason": "API 端到端测试依据"}

        code, _ = self.call("POST", f"/cases/{cid}/open",
                            {"supplier": "华东轴承厂", "title": "轴承退货",
                             **actor_r})
        self.assertEqual(code, 201)

        code, _ = self.call("POST", f"/cases/{cid}/defect-batches",
                            {"batch_no": "B1", "material": "6204",
                             "defect_qty": 100, "defect_desc": "硬度不足",
                             "actor": "质量工程师", "reason": "IQC 判退"})
        self.assertEqual(code, 201)

        code, _ = self.call("POST", f"/cases/{cid}/lines",
                            {"line_no": 1, "batch_no": "B1",
                             "request_qty": 100, "unit_price": 1000,
                             **actor_r})
        self.assertEqual(code, 201)

        # 缺责任认定先批准 → 409，且错误类型明确
        code, body = self.call("POST", f"/cases/{cid}/approve",
                               {"approvals": [{"line_no": 1,
                                               "approved_qty": 100}],
                                **actor_r})
        self.assertEqual(code, 409)
        self.assertEqual(body["error"], "LiabilityMissing")

        code, _ = self.call("POST", f"/cases/{cid}/liability",
                            {"determinations": [{"line_no": 1,
                                                 "liable_qty": 100}],
                             "actor": "质量工程师", "reason": "8D 供方全责"})
        self.assertEqual(code, 201)

        code, body = self.call("POST", f"/cases/{cid}/approve",
                               {"approvals": [{"line_no": 1,
                                               "approved_qty": 100}],
                                **actor_r})
        self.assertEqual(code, 201)
        self.assertEqual(body["status"], "已下达")
        self.assertTrue(body["reconciliation"]["balanced"])

        # 签退 70：部分退回，索赔必须立即从 1000.00 收敛到 700.00
        code, _ = self.call("POST", f"/cases/{cid}/evidences",
                            {"evidence_id": "EV2", "doc_type": "签收回单",
                             "doc_no": "Q1", "confirmed_return_qty": 70,
                             "actor": "仓储管理员", "reason": "回单 70"})
        self.assertEqual(code, 201)
        code, body = self.call("POST", f"/cases/{cid}/goods-received",
                               {"evidence_id": "EV2",
                                "receipts": [{"line_no": 1,
                                              "received_qty": 70}],
                                "actor": "仓储管理员",
                                "reason": "签退 70，短少 30 回厂"})
        self.assertEqual(code, 201)
        self.assertEqual(body["event"]["type"], "goods_received")
        self.assertEqual(
            body["reconciliation"]["totals"]["receivable_yuan"], "700.00")

        # 供应商争议，只接受 60
        code, body = self.call("POST", f"/cases/{cid}/partial-accept",
                               {"accepted": [{"line_no": 1,
                                              "accepted_qty": 60}],
                                "actor": "供应商",
                                "reason": "10 件运输致损，只认 60"})
        self.assertEqual(code, 201)
        self.assertEqual(
            body["reconciliation"]["totals"]["receivable_yuan"], "600.00")

        # 换货 10 件 → 500.00
        code, body = self.call("POST", f"/cases/{cid}/exchange",
                               {"exchanges": [{"line_no": 1,
                                               "exchange_qty": 10}],
                                **actor_r | {"reason": "10 件换货补发"}})
        self.assertEqual(code, 201)
        recon = body["reconciliation"]
        self.assertEqual(recon["totals"]["receivable_yuan"], "500.00")
        self.assertTrue(recon["balanced"])

        # 审计轨迹：所有事件都带理由，调整都有依据
        code, trail = self.call("GET", f"/cases/{cid}/audit-trail")
        self.assertEqual(code, 200)
        for event in trail["events"]:
            self.assertTrue(event["reason"])
        entry_events = [e for e in trail["events"] if e["claim_effects"]]
        # 批准计提 + 签退冲销 + 部分接受冲销 + 换货冲销，共 4 次金额调整
        self.assertEqual(len(entry_events), 4)

        # 单据详情可查
        code, detail = self.call("GET", f"/cases/{cid}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["lines"][0]["claimable_qty"], 50)

        # 列表
        code, listing = self.call("GET", "/cases")
        self.assertEqual(code, 200)
        self.assertTrue(any(c["case_id"] == cid for c in listing["cases"]))

    def test_missing_reason_rejected(self) -> None:
        cid = "RC-NR"
        code, body = self.call("POST", f"/cases/{cid}/open",
                               {"supplier": "S1", "actor": "采购计划员"})
        self.assertEqual(code, 400)
        self.assertIn("reason", body["message"])

    def test_unknown_case_returns_404(self) -> None:
        code, body = self.call("GET", "/cases/NOPE")
        self.assertEqual(code, 404)
        self.assertEqual(body["error"], "CaseNotFound")


if __name__ == "__main__":
    unittest.main()
