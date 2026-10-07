"""退货索赔协同 HTTP API 冒烟测试。"""
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

from domain_contract.validator import load_contract
from return_claim import ReturnClaimService, Store, make_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        contract = load_contract(ROOT / "domain" / "contract.json")
        cls.service = ReturnClaimService(Store(), contract)
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, path, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        return self._open(request)

    def _get(self, path):
        return self._open(urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}"))

    @staticmethod
    def _open(request):
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, health = self._get("/api/health")
        self.assertEqual((status, health["status"]), (200, "ok"))

        status, batch = self._post("/api/defect-batches", {
            "actor": "质量工程师", "batch_no": "API-B1", "material": "M-7701",
            "supplier": "苏州精工", "defect_qty": 100, "unit_price": "12.50"})
        self.assertEqual(status, 201, batch)
        self.assertEqual(batch["quarantine_balance"], "100")

        status, order = self._post("/api/return-orders", {
            "actor": "采购计划员", "order_no": "API-RO-1",
            "lines": [{"batch_id": batch["batch_id"], "qty": 100}]})
        self.assertEqual(status, 201, order)
        order_id = order["order_id"]
        line_id = order["lines"][0]["line_id"]

        for action in ("submit", "approve"):
            status, order = self._post(
                f"/api/return-orders/{order_id}/{action}", {"actor": "采购计划员"})
            self.assertEqual(status, 200, order)
        self.assertEqual(order["lines"][0]["net_receivable"], "1250.00")

        status, _ = self._post(f"/api/return-lines/{line_id}/evidence", {
            "actor": "仓储管理员", "kind": "发货", "qty": 60,
            "carrier": "顺丰", "tracking_no": "SF-123"})
        self.assertEqual(status, 201)
        status, _ = self._post(f"/api/return-lines/{line_id}/evidence", {
            "actor": "供应商", "kind": "签收", "qty": 60})
        self.assertEqual(status, 201)

        status, result = self._post(f"/api/return-lines/{line_id}/compensations", {
            "actor": "供应商", "type": "部分接受", "payload": {"accepted_qty": 60}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["amount"]["net_receivable"], "750.00")
        self.assertEqual(result["physical"]["returned_to_quarantine_qty"], "40")

        status, recon = self._get(f"/api/return-lines/{line_id}/reconciliation")
        self.assertEqual(status, 200)
        self.assertEqual(recon["liability"]["outstanding_claim_qty"], "60")
        self.assertTrue(all(c["ok"] for c in recon["checks"]))
        self.assertTrue(all(item["basis"]["ref"] for item in recon["adjustments"]))

        status, batch_recon = self._get(
            f"/api/defect-batches/{batch['batch_id']}/reconciliation")
        self.assertEqual(status, 200)
        self.assertEqual(batch_recon["quarantine_balance"], "40")
        self.assertEqual(batch_recon["totals"]["net_receivable"], "750.00")

    def test_error_mapping(self):
        status, body = self._get("/api/return-orders/9999")
        self.assertEqual((status, body["error"]), (404, "NotFoundError"))

        status, body = self._post("/api/defect-batches", {
            "actor": "采购计划员", "batch_no": "API-B2", "material": "M",
            "supplier": "S", "defect_qty": 1, "unit_price": "1.00"})
        self.assertEqual((status, body["error"]), (403, "PermissionDenied"))

        status, body = self._post("/api/defect-batches", {"actor": "质量工程师"})
        self.assertEqual((status, body["error"]), (400, "ValidationError"))

        status, body = self._get("/api/no-such-path")
        self.assertEqual(status, 404)

    def test_duplicate_order_no_conflict(self):
        status, batch = self._post("/api/defect-batches", {
            "actor": "质量工程师", "batch_no": "API-B3", "material": "M",
            "supplier": "S", "defect_qty": 5, "unit_price": "2.00"})
        self.assertEqual(status, 201)
        payload = {"actor": "采购计划员", "order_no": "API-RO-DUP",
                   "lines": [{"batch_id": batch["batch_id"], "qty": 1}]}
        status, _ = self._post("/api/return-orders", payload)
        self.assertEqual(status, 201)
        status, body = self._post("/api/return-orders", payload)
        self.assertEqual((status, body["error"]), (409, "DomainError"))


if __name__ == "__main__":
    unittest.main()
