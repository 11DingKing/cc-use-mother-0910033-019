"""退货索赔协同服务层测试：原子性、补偿事件与三视图对账。"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract
from return_claim import (
    DomainError,
    PermissionDenied,
    ReturnClaimService,
    Store,
    ValidationError,
)

BUYER = "采购计划员"
SUPPLIER = "供应商"
QUALITY = "质量工程师"
WAREHOUSE = "仓储管理员"


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        contract = load_contract(ROOT / "domain" / "contract.json")
        self.svc = ReturnClaimService(self.store, contract)

    def tearDown(self) -> None:
        self.store.close()

    # --------------------------------------------------------------
    # 辅助
    # --------------------------------------------------------------

    def _batch(self, qty=100, price="12.50", batch_no="B-001"):
        return self.svc.register_defect_batch(
            actor=QUALITY, batch_no=batch_no, material="M-7701",
            supplier="苏州精工", defect_qty=qty, unit_price=price)

    def _approved_line(self, qty=100, batch_qty=100, price="12.50"):
        """造一条已批准的退货行，返回 (order, line_id)。"""
        batch = self._batch(qty=batch_qty, price=price)
        order = self.svc.create_return_order(
            actor=BUYER, order_no="RO-001",
            lines=[{"batch_id": batch["batch_id"], "qty": qty}])
        self.svc.submit_return_order(actor=BUYER, order_id=order["order_id"])
        order = self.svc.approve_return_order(actor=BUYER, order_id=order["order_id"])
        return order, order["lines"][0]["line_id"], batch["batch_id"]

    def _counts(self, table):
        with self.store.read() as conn:
            return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]

    # --------------------------------------------------------------
    # 批准退货：原子调整隔离库存与应收
    # --------------------------------------------------------------

    def test_approve_atomically_adjusts_quarantine_and_receivable(self):
        order, line_id, batch_id = self._approved_line(qty=100)
        self.assertEqual(order["state"], "已下达")
        self.assertEqual(order["lines"][0]["outstanding_claim_qty"], Decimal("100"))
        self.assertEqual(order["lines"][0]["net_receivable"], Decimal("1250.00"))
        batch = self.svc.get_batch(batch_id)
        self.assertEqual(batch["quarantine_balance"], Decimal("0"))

    def test_approve_rolls_back_when_quarantine_insufficient(self):
        batch = self._batch(qty=100)
        order = self.svc.create_return_order(
            actor=BUYER, order_no="RO-002",
            lines=[{"batch_id": batch["batch_id"], "qty": 100}])
        self.svc.submit_return_order(actor=BUYER, order_id=order["order_id"])
        # 另一张单先提走 60，剩余 40 不够第二张单
        order2 = self.svc.create_return_order(
            actor=BUYER, order_no="RO-003",
            lines=[{"batch_id": batch["batch_id"], "qty": 60}])
        self.svc.submit_return_order(actor=BUYER, order_id=order2["order_id"])
        self.svc.approve_return_order(actor=BUYER, order_id=order2["order_id"])
        with self.assertRaises(DomainError):
            self.svc.approve_return_order(actor=BUYER, order_id=order["order_id"])
        # 回滚验证：单据仍在待确认，没有产生任何分录
        self.assertEqual(self.svc.get_order(order["order_id"])["state"], "待确认")

    def test_approve_rolls_back_all_lines_when_any_line_fails(self):
        batch1 = self._batch(qty=100, batch_no="B-101")
        batch2 = self._batch(qty=50, batch_no="B-102")
        # 先占走批次二的 40，隔离区只剩 10
        other = self.svc.create_return_order(
            actor=BUYER, order_no="RO-004A",
            lines=[{"batch_id": batch2["batch_id"], "qty": 40}])
        self.svc.submit_return_order(actor=BUYER, order_id=other["order_id"])
        self.svc.approve_return_order(actor=BUYER, order_id=other["order_id"])
        # 多行单：第一行库存充足、第二行不足，批准必须整体回滚
        order = self.svc.create_return_order(
            actor=BUYER, order_no="RO-004",
            lines=[{"batch_id": batch1["batch_id"], "qty": 100},
                   {"batch_id": batch2["batch_id"], "qty": 20}])
        self.svc.submit_return_order(actor=BUYER, order_id=order["order_id"])
        with self.assertRaises(DomainError):
            self.svc.approve_return_order(actor=BUYER, order_id=order["order_id"])
        # 第一行的库存移动与索赔分录也必须回滚
        self.assertEqual(self._counts("inventory_movement"), 4)  # 2 批次入库 + RO-004A 出库/在途
        self.assertEqual(self._counts("claim_entry"), 1)  # 仅 RO-004A 的原始索赔
        self.assertEqual(self.svc.get_batch(batch1["batch_id"])["quarantine_balance"],
                         Decimal("100"))
        self.assertEqual(self.svc.get_order(order["order_id"])["state"], "待确认")

    # --------------------------------------------------------------
    # 部分接受：库存与索赔同步修正（核心痛点场景）
    # --------------------------------------------------------------

    def test_partial_acceptance_reconciles_inventory_and_claim(self):
        _, line_id, batch_id = self._approved_line(qty=100)
        # 实际只发出并签收 60
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=60)
        self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=60)
        # 供应商只接受 60，其余 40 冲减索赔并退回隔离区
        result = self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="部分接受",
            payload={"accepted_qty": 60})

        self.assertEqual(result["liability"]["outstanding_claim_qty"], Decimal("60"))
        self.assertEqual(result["amount"]["net_receivable"], Decimal("750.00"))
        self.assertEqual(result["physical"]["returned_to_quarantine_qty"], Decimal("40"))
        self.assertEqual(self.svc.get_batch(batch_id)["quarantine_balance"], Decimal("40"))
        # 四条不变量全部成立：库存扣 60、索赔也按 60，不再脱节
        self.assertTrue(all(c["ok"] for c in result["checks"]),
                        [c["detail"] for c in result["checks"] if not c["ok"]])

    def test_partial_acceptance_rejects_qty_above_outstanding(self):
        _, line_id, _ = self._approved_line(qty=100)
        with self.assertRaises(DomainError):
            self.svc.apply_compensation(
                actor=SUPPLIER, line_id=line_id, compensation_type="部分接受",
                payload={"accepted_qty": 120})

    # --------------------------------------------------------------
    # 换货抵扣 / 争议复核 / 撤销
    # --------------------------------------------------------------

    def test_replacement_offset_reduces_receivable_and_restocks(self):
        _, line_id, _ = self._approved_line(qty=100)
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=100)
        self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=100)
        result = self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="换货抵扣",
            payload={"replacement_qty": 30})
        self.assertEqual(result["liability"]["outstanding_claim_qty"], Decimal("70"))
        self.assertEqual(result["amount"]["net_receivable"], Decimal("875.00"))
        self.assertEqual(result["physical"]["replacement_received_qty"], Decimal("30"))
        self.assertTrue(all(c["ok"] for c in result["checks"]),
                        [c["detail"] for c in result["checks"] if not c["ok"]])

    def test_dispute_review_adjusts_to_revised_qty(self):
        _, line_id, _ = self._approved_line(qty=100)
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=100)
        self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=100)
        self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="换货抵扣",
            payload={"replacement_qty": 20})
        # 复核认定责任 30：目标未决 = 30 - 已抵扣 20 = 10
        result = self.svc.apply_compensation(
            actor=QUALITY, line_id=line_id, compensation_type="争议复核",
            payload={"revised_qty": 30, "reason": "复检确认部分为运输损伤"})
        self.assertEqual(result["liability"]["outstanding_claim_qty"], Decimal("10"))
        self.assertEqual(result["amount"]["net_receivable"], Decimal("125.00"))
        self.assertEqual(result["liability"]["latest_determined_qty"], Decimal("30"))
        # 认定链完整：换货 20 + 未决 10 = 认定 30
        determination_check = next(
            c for c in result["checks"] if c["name"] == "责任数量认定")
        self.assertTrue(determination_check["ok"], determination_check["detail"])

    def test_cancellation_reverses_claim_and_restores_quarantine(self):
        _, line_id, batch_id = self._approved_line(qty=100)
        result = self.svc.apply_compensation(
            actor=BUYER, line_id=line_id, compensation_type="撤销",
            payload={"reason": "供应商要求改为换货流程"})
        self.assertEqual(result["liability"]["outstanding_claim_qty"], Decimal("0"))
        self.assertEqual(result["amount"]["net_receivable"], Decimal("0.00"))
        self.assertEqual(result["line_state"], "已关闭")
        self.assertEqual(self.svc.get_batch(batch_id)["quarantine_balance"], Decimal("100"))
        # 单行全关 → 整单关闭
        order = self.svc.get_order(result["order_id"])
        self.assertEqual(order["state"], "已关闭")

    def test_cancelled_line_rejects_further_events(self):
        _, line_id, _ = self._approved_line(qty=100)
        self.svc.apply_compensation(
            actor=BUYER, line_id=line_id, compensation_type="撤销",
            payload={"reason": "取消"})
        with self.assertRaises(DomainError):
            self.svc.apply_compensation(
                actor=SUPPLIER, line_id=line_id, compensation_type="部分接受",
                payload={"accepted_qty": 10})

    # --------------------------------------------------------------
    # 责任认定
    # --------------------------------------------------------------

    def test_liability_determination_adjusts_claim_once(self):
        _, line_id, _ = self._approved_line(qty=100)
        result = self.svc.determine_liability(
            actor=QUALITY, line_id=line_id, determined_qty=60, reason="40 件为自损")
        self.assertEqual(result["liability"]["outstanding_claim_qty"], Decimal("60"))
        self.assertEqual(result["amount"]["net_receivable"], Decimal("750.00"))
        with self.assertRaises(DomainError):
            self.svc.determine_liability(
                actor=QUALITY, line_id=line_id, determined_qty=50, reason="重复认定")

    # --------------------------------------------------------------
    # 证据规则与状态机
    # --------------------------------------------------------------

    def test_evidence_quantity_rules(self):
        _, line_id, _ = self._approved_line(qty=100)
        with self.assertRaises(DomainError):
            self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=150)
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=60)
        with self.assertRaises(DomainError):
            self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=70)

    def test_state_machine_rejects_out_of_order_ops(self):
        batch = self._batch()
        order = self.svc.create_return_order(
            actor=BUYER, order_no="RO-009",
            lines=[{"batch_id": batch["batch_id"], "qty": 10}])
        with self.assertRaises(DomainError):
            self.svc.approve_return_order(actor=BUYER, order_id=order["order_id"])
        line_id = self.svc.get_order(order["order_id"])["lines"][0]["line_id"]
        with self.assertRaises(DomainError):
            self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=5)

    def test_permissions_follow_contract_roles(self):
        batch = self._batch()
        order = self.svc.create_return_order(
            actor=BUYER, order_no="RO-010",
            lines=[{"batch_id": batch["batch_id"], "qty": 10}])
        self.svc.submit_return_order(actor=BUYER, order_id=order["order_id"])
        with self.assertRaises(PermissionDenied):
            self.svc.approve_return_order(actor=QUALITY, order_id=order["order_id"])
        with self.assertRaises(ValidationError):
            self.svc.approve_return_order(actor="老板", order_id=order["order_id"])
        with self.assertRaises(PermissionDenied):
            self.svc.register_defect_batch(
                actor=BUYER, batch_no="B-900", material="M", supplier="S",
                defect_qty=1, unit_price="1.00")

    # --------------------------------------------------------------
    # 对账：三视图与调整依据
    # --------------------------------------------------------------

    def test_reconciliation_shows_adjustment_basis(self):
        _, line_id, _ = self._approved_line(qty=100)
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=60)
        self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=60)
        result = self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="部分接受",
            payload={"accepted_qty": 60})

        physical = result["physical"]
        self.assertEqual(physical["approved_qty"], Decimal("100"))
        self.assertEqual(physical["received_qty"], Decimal("60"))
        self.assertEqual(physical["in_transit_qty"], Decimal("0"))
        amount = result["amount"]
        self.assertEqual(amount["gross_claimed"], Decimal("1250.00"))
        self.assertEqual(amount["total_reversed"], Decimal("-500.00"))

        trail = result["adjustments"]
        self.assertTrue(trail)
        for item in trail:
            self.assertIn("seq", item)
            self.assertTrue(item["basis"]["type"])
            self.assertTrue(item["basis"]["ref"])
        kinds = [item["kind"] for item in trail if item["category"] == "索赔分录"]
        self.assertEqual(kinds, ["原始索赔", "部分接受冲减"])
        # 每笔冲减都能追到补偿事件依据
        reversal = next(item for item in trail if item["kind"] == "部分接受冲减")
        self.assertEqual(reversal["basis"]["type"], "补偿事件")
        self.assertTrue(reversal["basis"]["ref"].startswith("CE-"))

    def test_batch_reconciliation_aggregates_lines(self):
        _, line_id, batch_id = self._approved_line(qty=100)
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=60)
        self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=60)
        self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="部分接受",
            payload={"accepted_qty": 60})
        result = self.svc.reconcile_batch(batch_id)
        self.assertEqual(result["quarantine_balance"], Decimal("40"))
        self.assertEqual(result["totals"]["net_receivable"], Decimal("750.00"))
        self.assertEqual(len(result["lines"]), 1)
        self.assertTrue(all(c["ok"] for c in result["checks"]),
                        [c["detail"] for c in result["checks"] if not c["ok"]])

    def test_full_flow_closes_order(self):
        """完整链路：批准 → 发货签收 → 部分接受 → 换货抵扣 → 复核 → 撤销尾差 → 关闭。"""
        _, line_id, _ = self._approved_line(qty=100)
        self.svc.record_evidence(actor=WAREHOUSE, line_id=line_id, kind="发货", qty=60)
        self.svc.record_evidence(actor=SUPPLIER, line_id=line_id, kind="签收", qty=60)
        self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="部分接受",
            payload={"accepted_qty": 60})
        self.svc.apply_compensation(
            actor=SUPPLIER, line_id=line_id, compensation_type="换货抵扣",
            payload={"replacement_qty": 20})
        self.svc.apply_compensation(
            actor=QUALITY, line_id=line_id, compensation_type="争议复核",
            payload={"revised_qty": 30, "reason": "复检确认"})
        result = self.svc.apply_compensation(
            actor=BUYER, line_id=line_id, compensation_type="撤销",
            payload={"reason": "尾差免于追偿"})
        self.assertEqual(result["amount"]["net_receivable"], Decimal("0.00"))
        self.assertEqual(result["line_state"], "已关闭")
        self.assertEqual(result["order_state"], "已关闭")


if __name__ == "__main__":
    unittest.main()
