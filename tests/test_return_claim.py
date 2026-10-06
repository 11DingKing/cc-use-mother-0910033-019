"""退货索赔协同后端的领域测试。

重点验证四条不变式：退货库存联动、责任数量认定、索赔分录补偿、实物金额对账。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from return_claim.aggregate import (  # noqa: E402
    ZONE_EXCHANGE,
    ZONE_IN_TRANSIT,
    ZONE_QUARANTINE,
    ZONE_SUPPLIER,
    CaseAggregate,
)
from return_claim.commands import CommandService  # noqa: E402
from return_claim.errors import (  # noqa: E402
    DomainError,
    IllegalState,
    LiabilityMissing,
    QuantityConflict,
)
from return_claim.events import EventStore  # noqa: E402
from return_claim.queries import QueryService  # noqa: E402

# 单价 10.00 元 = 1000 分
PRICE = 1000


class CaseBuilder:
    """测试夹具：快速走到「已批准、在途」这一步。"""

    def __init__(self, service: CommandService, case_id: str = "RC-001",
                 request_qty: int = 100, liable_qty: int = 100,
                 approved_qty: int = 100) -> None:
        self.svc = service
        self.cid = case_id
        service.open_case(case_id, supplier="华东轴承厂",
                          actor="采购计划员", reason="来料检验不合格开立索赔",
                          title="6204 轴承来料不合格退货")
        service.register_defect_batch(
            case_id, batch_no="B20261001", material="6204-轴承",
            defect_qty=request_qty, actor="质量工程师",
            reason="IQC 抽检 AQL 0.65 判定不合格，硬度不足",
            defect_desc="硬度 HRC 低于下限")
        service.add_return_line(
            case_id, line_no=1, batch_no="B20261001",
            request_qty=request_qty, unit_price=PRICE,
            actor="采购计划员", reason="按缺陷批次数量全额申请退货")
        service.determine_liability(
            case_id,
            [{"line_no": 1, "liable_qty": liable_qty,
              "basis": "供应商热处理工艺偏差，全责"}],
            actor="质量工程师", reason="8D 报告确认供方热处理责任")
        service.attach_logistics_evidence(
            case_id, evidence_id="EV-1", doc_type="出库单", doc_no="CK-9001",
            confirmed_return_qty=approved_qty, actor="仓储管理员",
            reason="退货发运出库单已开立", carrier="顺丰物流")
        service.approve_return(
            case_id, [{"line_no": 1, "approved_qty": approved_qty}],
            actor="采购计划员", reason="责任清晰，批准退货并发运")


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()
        self.svc = CommandService(self.store)
        self.q = QueryService(self.store)

    def test_approve_atomically_moves_quarantine_and_books_receivable(self) -> None:
        """批准退货：隔离库存原子出库、在途增加、应收按责任口径计提。"""
        b = CaseBuilder(self.svc)
        s = self.q.load(b.cid)

        self.assertEqual(s.status.value, "已下达")
        self.assertEqual(s.zone_qty(1, ZONE_QUARANTINE), 0)
        self.assertEqual(s.zone_qty(1, ZONE_IN_TRANSIT), 100)
        # 索赔按批准/责任数量 100 件全额计提 = 1000.00 元
        self.assertEqual(s.receivable_amount(), 100 * PRICE)
        report = self.q.reconciliation(b.cid)
        self.assertTrue(report["balanced"])

    def test_cannot_approve_without_liability(self) -> None:
        """缺责任认定时批准必须失败，且不留任何事件、库存、分录。"""
        self.svc.open_case("RC-X", "华东轴承厂", "采购计划员", "开立")
        self.svc.register_defect_batch(
            "RC-X", "B1", "M1", 10, "质量工程师", "缺陷")
        self.svc.add_return_line(
            "RC-X", 1, "B1", 10, PRICE, "采购计划员", "申请退货")
        with self.assertRaises(LiabilityMissing):
            self.svc.approve_return(
                "RC-X", [{"line_no": 1, "approved_qty": 10}],
                "采购计划员", "尝试无责任批准")
        # 事件流停在「新增退货行」，没有批准事件，没有库存出库
        types = [e.type.value for e in self.store.events_for("RC-X")]
        self.assertNotIn("return_approved", types)
        s = self.q.load("RC-X")
        self.assertEqual(s.quarantine_qty(), 10)
        self.assertEqual(s.receivable_amount(), 0)

    def test_over_approval_rejected_atomically(self) -> None:
        """批准数量超申请量时整批失败：不产生任何半条流水。"""
        b = CaseBuilder(self.svc)
        # 加第二行（不同批次），尚未批准
        self.svc.register_defect_batch(
            b.cid, "B20261002", "密封圈", 20, "质量工程师", "第二个缺陷批次")
        self.svc.add_return_line(
            b.cid, 2, "B20261002", 20, 500, "采购计划员", "第二行退货")
        self.svc.determine_liability(
            b.cid, [{"line_no": 2, "liable_qty": 20}],
            "质量工程师", "第二行责任认定")
        before = len(self.store.events_for(b.cid))
        quarantine_before = self.q.load(b.cid).quarantine_qty()
        with self.assertRaises(QuantityConflict):
            self.svc.approve_return(
                b.cid, [{"line_no": 2, "approved_qty": 21}],
                "采购计划员", "批准数量超过申请量")
        self.assertEqual(len(self.store.events_for(b.cid)), before)
        # 隔离库存纹丝不动
        self.assertEqual(self.q.load(b.cid).quarantine_qty(),
                         quarantine_before)


class PartialReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()
        self.svc = CommandService(self.store)
        self.q = QueryService(self.store)

    def test_partial_physical_return_caps_claim(self) -> None:
        """核心痛点：部分退回时，库存已扣减但索赔必须按实物签退重算。"""
        b = CaseBuilder(self.svc)
        # 供应商仅签退 70 件，30 件短少回厂；证据 EV-2 为签收回单
        self.svc.attach_logistics_evidence(
            b.cid, "EV-2", "签收回单", "QS-7002", 70, "仓储管理员",
            "顺丰回单显示供应商实收 70 件", carrier="顺丰物流")
        self.svc.record_goods_received(
            b.cid, [{"line_no": 1, "received_qty": 70}],
            evidence_id="EV-2", actor="仓储管理员",
            reason="签收回单核实：发运 100，供方签退 70，短少 30 回厂")

        s = self.q.load(b.cid)
        self.assertEqual(s.status.value, "履行中")
        # 实物分布：供应商 70 + 隔离区（短少回厂）30 = 100，守恒
        self.assertEqual(s.zone_qty(1, ZONE_SUPPLIER), 70)
        self.assertEqual(s.zone_qty(1, ZONE_QUARANTINE), 30)
        self.assertEqual(s.zone_qty(1, ZONE_IN_TRANSIT), 0)
        # 索赔从 100 件收敛到 70 件：冲销 30 件 = -300.00 元
        self.assertEqual(s.receivable_amount(), 70 * PRICE)
        entries = s.claim_entries
        self.assertEqual([e.amount_delta for e in entries],
                         [100 * PRICE, -30 * PRICE])
        # 冲销分录必须指回签退事件并带依据
        reversal = entries[-1]
        self.assertTrue(reversal.is_reversal)
        self.assertEqual(reversal.basis_type.value, "goods_received")
        self.assertIn("签收回单", reversal.reason)

        report = self.q.reconciliation(b.cid)
        self.assertTrue(report["balanced"])
        line_report = report["lines"][0]
        self.assertTrue(line_report["checks"]["claim_within_physical"])


class CompensationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()
        self.svc = CommandService(self.store)
        self.q = QueryService(self.store)
        self.b = CaseBuilder(self.svc)
        # 全部 100 件签退
        self.svc.attach_logistics_evidence(
            self.b.cid, "EV-2", "签收回单", "QS-7002", 100, "仓储管理员",
            "供方签退 100")
        self.svc.record_goods_received(
            self.b.cid, [{"line_no": 1, "received_qty": 100}],
            evidence_id="EV-2", actor="仓储管理员",
            reason="签收回单核实：供方签退 100 件")

    def test_partial_accept_compensates_claim(self) -> None:
        """供应商部分接受 60 件：责任数量下调，索赔负向冲销 40 件。"""
        self.svc.partial_accept(
            self.b.cid,
            [{"line_no": 1, "accepted_qty": 60,
              "note": "40 件系运输碰伤，供方只认 60"}],
            actor="供应商", reason="供应商异议：40 件运输致损只接受 60 件索赔")
        s = self.q.load(self.b.cid)
        self.assertEqual(s.lines[1].liable_qty, 60)
        self.assertEqual(s.receivable_amount(), 60 * PRICE)
        self.assertEqual(s.claim_entries[-1].amount_delta, -40 * PRICE)
        self.assertTrue(self.q.reconciliation(self.b.cid)["balanced"])

    def test_exchange_offset_reduces_claim_and_stocks_replacement(self) -> None:
        """换货抵扣 20 件：索赔减少 20 件，库存出现换货补入 20 件。"""
        self.svc.exchange_offset(
            self.b.cid, [{"line_no": 1, "exchange_qty": 20}],
            actor="采购计划员",
            reason="协商 20 件由供方补发合格品，不再收钱")
        s = self.q.load(self.b.cid)
        self.assertEqual(s.zone_qty(1, ZONE_EXCHANGE), 20)
        self.assertEqual(s.zone_qty(1, ZONE_SUPPLIER), 80)
        self.assertEqual(s.receivable_amount(), 80 * PRICE)
        self.assertTrue(self.q.reconciliation(self.b.cid)["balanced"])

    def test_exchange_cannot_exceed_claimable(self) -> None:
        """换货不能超过可索赔数量（实物/责任/已换货后的余量）。"""
        self.svc.partial_accept(
            self.b.cid,
            [{"line_no": 1, "accepted_qty": 60, "note": "只认 60"}],
            actor="供应商", reason="部分接受 60")
        with self.assertRaises(QuantityConflict):
            self.svc.exchange_offset(
                self.b.cid, [{"line_no": 1, "exchange_qty": 61}],
                "采购计划员", "换货超出可抵扣量")
        # 失败不落事件，金额仍是 60 件口径
        self.assertEqual(self.q.load(self.b.cid).receivable_amount(), 60 * PRICE)

    def test_dispute_review_rejected_reverses_full_claim_and_returns_goods(self) -> None:
        """争议复核推翻责任：应收归零，供方持有实物退回厂内。"""
        self.svc.dispute_review(
            self.b.cid,
            [{"line_no": 1, "outcome": "rejected", "return_qty": 100,
              "note": "复检硬度合格，系我方量具失准"}],
            actor="质量工程师",
            reason="复检确认来料合格，撤销供方责任，实物退回隔离区")
        s = self.q.load(self.b.cid)
        self.assertEqual(s.lines[1].liable_qty, 0)
        self.assertEqual(s.receivable_amount(), 0)
        self.assertEqual(s.zone_qty(1, ZONE_SUPPLIER), 0)
        self.assertEqual(s.zone_qty(1, ZONE_QUARANTINE), 100)
        self.assertTrue(self.q.reconciliation(self.b.cid)["balanced"])

    def test_dispute_review_adjusted_sets_new_liable_qty(self) -> None:
        """争议复核调整责任为 75 件。"""
        self.svc.dispute_review(
            self.b.cid,
            [{"line_no": 1, "outcome": "adjusted", "liable_qty": 75,
              "return_qty": 25, "note": "按 75% 责任分摊"}],
            actor="质量工程师", reason="复核认定供方承担 75% 责任")
        s = self.q.load(self.b.cid)
        self.assertEqual(s.lines[1].liable_qty, 75)
        self.assertEqual(s.receivable_amount(), 75 * PRICE)
        self.assertEqual(s.zone_qty(1, ZONE_SUPPLIER), 75)
        self.assertEqual(s.zone_qty(1, ZONE_QUARANTINE), 25)
        self.assertTrue(self.q.reconciliation(self.b.cid)["balanced"])

    def test_revoke_before_settlement_returns_goods_and_zeroes_claim(self) -> None:
        """在途阶段撤销：货物截退回隔离区，索赔全额冲销。"""
        # 新开一张只批准、未签退的单
        store2 = EventStore()
        svc2 = CommandService(store2)
        q2 = QueryService(store2)
        b2 = CaseBuilder(svc2, case_id="RC-R")
        svc2.revoke(b2.cid, [1], actor="采购计划员",
                    reason="生产急用以让步接收替代退货，撤销批准")
        s = q2.load(b2.cid)
        self.assertTrue(s.lines[1].revoked)
        self.assertEqual(s.zone_qty(1, ZONE_IN_TRANSIT), 0)
        self.assertEqual(s.zone_qty(1, ZONE_QUARANTINE), 100)
        self.assertEqual(s.receivable_amount(), 0)
        self.assertTrue(q2.reconciliation(b2.cid)["balanced"])

    def test_dispute_review_upheld_keeps_current_liability(self) -> None:
        """争议复核维持：不传责任数量即维持现值，金额不变。"""
        _, ev = self.svc.dispute_review(
            self.b.cid,
            [{"line_no": 1, "outcome": "upheld",
              "note": "第三方鉴定确认供方全责"}],
            actor="质量工程师", reason="复核维持供方 100 件责任")
        s = self.q.load(self.b.cid)
        self.assertEqual(s.lines[1].liable_qty, 100)
        self.assertEqual(s.receivable_amount(), 100 * PRICE)
        # 目标口径未变，不应产生新分录
        self.assertEqual(ev.seq, len(self.store.events_for(self.b.cid)))

    def test_revoked_line_rejects_further_adjustment(self) -> None:
        self.svc.revoke(self.b.cid, [1], "采购计划员", "撤销")
        with self.assertRaises(DomainError):
            self.svc.partial_accept(
                self.b.cid, [{"line_no": 1, "accepted_qty": 10}],
                "供应商", "已撤销不能再调整")


class ReplayTests(unittest.TestCase):
    def test_replay_from_jsonl_is_identical(self) -> None:
        """换一个 EventStore 从 JSONL 重放，状态与对账必须逐分一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            store = EventStore(path)
            svc = CommandService(store)
            b = CaseBuilder(svc, case_id="RC-PERSIST")
            svc.attach_logistics_evidence(
                b.cid, "EV-2", "签收回单", "QS-1", 60, "仓储管理员", "签退60")
            svc.record_goods_received(
                b.cid, [{"line_no": 1, "received_qty": 60}],
                evidence_id="EV-2", actor="仓储管理员",
                reason="部分签退 60，短少 40 回厂")
            svc.partial_accept(
                b.cid, [{"line_no": 1, "accepted_qty": 50}],
                "供应商", "供方只接受 50")
            svc.exchange_offset(
                b.cid, [{"line_no": 1, "exchange_qty": 10}],
                "采购计划员", "10 件换货")

            replayed_store = EventStore(path)
            q1 = QueryService(store)
            q2 = QueryService(replayed_store)
            self.assertEqual(
                q1.reconciliation(b.cid)["totals"],
                q2.reconciliation(b.cid)["totals"],
            )
            s1, s2 = q1.load(b.cid), q2.load(b.cid)
            self.assertEqual(s1.receivable_amount(), s2.receivable_amount())
            self.assertEqual(s1.quarantine_qty(), s2.quarantine_qty())
            # 最终：索赔 40 件（签退 60、责任 50、换货 10）= 400.00 元
            self.assertEqual(s2.receivable_amount(), 40 * PRICE)
            # 守恒：短少回厂隔离 40 + 供应商 50（60 签退 − 10 换货）
            #       + 换货补入 10 = 100
            self.assertEqual(
                s2.zone_qty(1, ZONE_QUARANTINE)
                + s2.zone_qty(1, ZONE_SUPPLIER)
                + s2.zone_qty(1, ZONE_EXCHANGE), 100)

    def test_audit_trail_links_every_adjustment_to_event(self) -> None:
        """每次库存/金额调整都能在审计轨迹中找到事件依据。"""
        store3 = EventStore()
        svc3 = CommandService(store3)
        q3 = QueryService(store3)
        b3 = CaseBuilder(svc3, case_id="RC-AUDIT")
        svc3.attach_logistics_evidence(
            b3.cid, "EV-2", "签收回单", "QS-1", 80, "仓储管理员", "签退80")
        svc3.record_goods_received(
            b3.cid, [{"line_no": 1, "received_qty": 80}],
            evidence_id="EV-2", actor="仓储管理员", reason="签退 80")
        trail = q3.audit_trail(b3.cid)
        goods_event = [e for e in trail["events"]
                       if e["type"] == "goods_received"][0]
        # 同一事件带出三条库存流水（在途清账、供应商持有、短少回厂）
        zones = {m["zone"] for m in goods_event["stock_effects"]}
        self.assertEqual(
            zones, {ZONE_IN_TRANSIT, ZONE_SUPPLIER, ZONE_QUARANTINE})
        # 同事件带出一条负向索赔冲销
        self.assertEqual(len(goods_event["claim_effects"]), 1)
        self.assertLess(goods_event["claim_effects"][0]["amount_delta"], 0)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_exchange_never_overshoots_stock(self) -> None:
        """20 个线程同时换货 1 件：串行化后结存与索赔必须精确、对账平衡。"""
        import threading

        store = EventStore()
        svc = CommandService(store)
        q = QueryService(store)
        b = CaseBuilder(svc, case_id="RC-CONC")
        svc.attach_logistics_evidence(
            b.cid, "EV-2", "签收回单", "QS", 100, "仓储管理员", "签退100")
        svc.record_goods_received(
            b.cid, [{"line_no": 1, "received_qty": 100}],
            evidence_id="EV-2", actor="仓储管理员", reason="签退 100")

        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                svc.exchange_offset(
                    b.cid, [{"line_no": 1, "exchange_qty": 1}],
                    actor="采购计划员", reason=f"并发换货第 {i} 笔")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        s = q.load(b.cid)
        self.assertEqual(s.zone_qty(1, ZONE_SUPPLIER), 80)
        self.assertEqual(s.zone_qty(1, ZONE_EXCHANGE), 20)
        self.assertEqual(s.lines[1].exchange_qty, 20)
        self.assertEqual(s.receivable_amount(), 80 * PRICE)
        self.assertTrue(q.reconciliation(b.cid)["balanced"])


class MultiLineTests(unittest.TestCase):
    def test_lines_progress_independently_in_one_case(self) -> None:
        """同一单据多批次：行1 已签退进入履行，行2 仍可新增、认定、批准。"""
        store = EventStore()
        svc = CommandService(store)
        q = QueryService(store)
        b = CaseBuilder(svc)  # 行1 批准在途
        svc.attach_logistics_evidence(
            b.cid, "EV2", "回单", "D2", 100, "仓储管理员", "签退100")
        svc.record_goods_received(
            b.cid, [{"line_no": 1, "received_qty": 100}],
            evidence_id="EV2", actor="仓储管理员", reason="行1 全部签退")
        self.assertEqual(q.load(b.cid).status.value, "履行中")

        svc.register_defect_batch(
            b.cid, "B2", "密封圈", 20, "质量工程师", "第二批缺陷")
        svc.add_return_line(
            b.cid, 2, "B2", 20, 500, "采购计划员", "行2 退货申请")
        svc.determine_liability(
            b.cid, [{"line_no": 2, "liable_qty": 20}],
            "质量工程师", "行2 责任认定")
        svc.approve_return(
            b.cid, [{"line_no": 2, "approved_qty": 20}],
            "采购计划员", "行2 批准")
        svc.attach_logistics_evidence(
            b.cid, "EV3", "回单", "D3", 10, "仓储管理员", "行2 签退10")
        svc.record_goods_received(
            b.cid, [{"line_no": 2, "received_qty": 10}],
            evidence_id="EV3", actor="仓储管理员",
            reason="行2 只签退 10，短少 10 回厂")

        report = q.reconciliation(b.cid)
        self.assertTrue(report["balanced"])
        totals = {l["line_no"]: l for l in report["lines"]}
        self.assertEqual(totals[1]["claim"]["receivable_yuan"], "1000.00")
        self.assertEqual(totals[2]["claim"]["receivable_yuan"], "50.00")
        # 关闭前必须结清：行2 仍有后续调整空间但已结算，可关闭
        _, ev = svc.close(b.cid, "采购计划员", "两行均已签退结算，关闭")
        self.assertEqual(ev.type.value, "closed")
        with self.assertRaises(IllegalState):
            svc.partial_accept(
                b.cid, [{"line_no": 1, "accepted_qty": 90}],
                "供应商", "已关闭不能调整")


if __name__ == "__main__":
    unittest.main()
