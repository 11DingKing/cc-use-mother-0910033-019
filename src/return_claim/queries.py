"""读模型：单据序列化与三方对账（实物数量 / 责任数量 / 金额）。

对账报告逐行给出三套口径及其差异检查，并列出每条库存流水与索赔分录
所依据的事件序号、类型和理由，满足「展示每次调整依据」。
"""
from __future__ import annotations

from typing import Any

from .aggregate import (
    ZONE_EXCHANGE,
    ZONE_IN_TRANSIT,
    ZONE_QUARANTINE,
    ZONE_SUPPLIER,
    CaseAggregate,
    CaseState,
)
from .events import EventStore
from .models import yuan

ZONES = (ZONE_QUARANTINE, ZONE_IN_TRANSIT, ZONE_SUPPLIER, ZONE_EXCHANGE)


class QueryService:
    """只读查询：加载单据、事件、库存/分录流水、对账报告。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.aggregate = CaseAggregate(store)

    def load(self, case_id: str) -> CaseState:
        return self.aggregate.load(case_id)

    def events(self, case_id: str) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.store.events_for(case_id)]

    def case_detail(self, case_id: str) -> dict[str, Any]:
        state = self.aggregate.load(case_id)
        return {
            "case_id": state.case_id,
            "title": state.title,
            "supplier": state.supplier,
            "status": state.status.value,
            "defect_batches": [b.to_dict()
                               for b in state.defect_batches.values()],
            "lines": [line.to_dict() for line in state.lines.values()],
            "evidences": [e.to_dict() for e in state.evidences],
            "liabilities": [x.to_dict() for x in state.liabilities],
            "stock_movements": [m.to_dict() for m in state.stock_movements],
            "claim_entries": [x.to_dict() for x in state.claim_entries],
            "reconciliation": self.reconciliation(case_id),
        }

    # ---- 对账 ------------------------------------------------------------

    def reconciliation(self, case_id: str) -> dict[str, Any]:
        """三方对账：实物数量、责任数量、金额，并附不平衡清单。"""
        s = self.aggregate.load(case_id)
        lines_report: list[dict[str, Any]] = []
        case_balanced = True

        for line in s.lines.values():
            zones = {z: s.zone_qty(line.line_no, z) for z in ZONES}
            physical_total = sum(zones.values())
            booked_qty = s.booked_claim_qty(line.line_no)
            target_qty = s.target_claim_qty(line)
            amount = s.receivable_amount(line.line_no)
            expected_amount = target_qty * line.unit_price

            checks = {
                # 守恒：四个位置之和必须等于退货申请量（撤销/换货只转移不消灭）
                "physical_conservation": physical_total == line.request_qty,
                # 索赔口径与目标口径一致（事件投影无遗漏/重复）
                "claim_quantity_converged": booked_qty == target_qty,
                # 金额 = 索赔数量 × 单价
                "amount_matches_quantity": amount == expected_amount,
                # 索赔不超过实物：签退后按实物签退封顶，签退前按批准量封顶
                # （治「库存已扣减但索赔按全量」）
                "claim_within_physical": (
                    booked_qty <= line.received_qty
                    if line.line_no in s._settled
                    else booked_qty <= line.approved_qty
                ),
                # 有责任认定后，索赔不超过责任数量
                "claim_within_liability": (
                    booked_qty <= line.liable_qty
                    if line.line_no in s._has_liability
                    else True
                ),
                # 隔离/在途/供应商任何位置都不允许负结存
                "no_negative_stock": all(v >= 0 for v in zones.values()),
            }
            balanced = all(checks.values())
            case_balanced = case_balanced and balanced

            lines_report.append({
                "line_no": line.line_no,
                "batch_no": line.batch_no,
                "material": line.material,
                "balanced": balanced,
                "checks": checks,
                "physical": {
                    "request_qty": line.request_qty,
                    "approved_qty": line.approved_qty,
                    "signed_return_qty": line.received_qty,
                    "zones": zones,
                    "zones_total": physical_total,
                },
                "liability": {
                    "determined": line.line_no in s._has_liability,
                    "liable_qty": line.liable_qty,
                },
                "claim": {
                    "booked_qty": booked_qty,
                    "target_qty": target_qty,
                    "exchange_qty": line.exchange_qty,
                    "revoked": line.revoked,
                    "receivable_amount": amount,
                    "receivable_yuan": yuan(amount),
                    "expected_amount": expected_amount,
                    "expected_yuan": yuan(expected_amount),
                    "unit_price_yuan": yuan(line.unit_price),
                },
            })

        return {
            "case_id": s.case_id,
            "status": s.status.value,
            "balanced": case_balanced,
            "totals": {
                "quarantine_qty": s.quarantine_qty(),
                "receivable_amount": s.receivable_amount(),
                "receivable_yuan": yuan(s.receivable_amount()),
                "claim_entries": len(s.claim_entries),
                "stock_movements": len(s.stock_movements),
            },
            "lines": lines_report,
        }

    def audit_trail(self, case_id: str) -> dict[str, Any]:
        """按事件序号展开：每个事件触发了哪些库存流水与索赔分录。"""
        s = self.aggregate.load(case_id)
        moves_by_seq: dict[int, list[dict[str, Any]]] = {}
        entries_by_seq: dict[int, list[dict[str, Any]]] = {}
        for m in s.stock_movements:
            moves_by_seq.setdefault(m.basis_seq, []).append(m.to_dict())
        for x in s.claim_entries:
            entries_by_seq.setdefault(x.basis_seq, []).append(x.to_dict())
        trail = []
        for e in self.store.events_for(case_id):
            trail.append({
                "seq": e.seq,
                "type": e.type.value,
                "actor": e.actor,
                "reason": e.reason,
                "payload": e.payload,
                "stock_effects": moves_by_seq.get(e.seq, []),
                "claim_effects": entries_by_seq.get(e.seq, []),
            })
        return {"case_id": case_id, "events": trail}
