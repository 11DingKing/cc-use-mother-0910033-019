"""聚合根：把只追加事件回放成退货索赔单的当前状态。

投影同时维护三类口径，全部来自同一串事件，天然可对账：

1. 实物数量：``stock_movements`` 流水，四个位置——
   - 隔离库存：厂内不合格品隔离区；
   - 在途：已发运、等待供应商签退；
   - 供应商：供应商已签退持有；
   - 换货补入：供应商以补发合格品方式换货入库的数量。
2. 责任数量：每条退货行的 ``liable_qty``（责任认定 → 部分接受 → 争议复核）；
3. 金额：``claim_entries`` 分录，每次向目标口径收敛时追加一条增量，
   补偿事件生成负增量，``basis_seq`` 指回依据事件。

命令层只负责校验，状态变化全部在 ``apply`` 中发生，从而保证
「重放事件」与「实时执行」得到完全一致的结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import CaseNotFound
from .models import (
    ClaimEntry,
    ClaimStatus,
    DefectBatch,
    Event,
    EventType,
    LogisticsEvidence,
    ReturnLine,
    StockMovement,
)

ZONE_QUARANTINE = "隔离库存"
ZONE_IN_TRANSIT = "在途"
ZONE_SUPPLIER = "供应商"
ZONE_EXCHANGE = "换货补入"


@dataclass
class LiabilityRecord:
    """一次责任认定/复核的结论（历史保留，当前值投影在行上）。"""

    line_no: int
    liable_qty: int
    responsible: str
    share_pct: int
    basis: str
    basis_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "liable_qty": self.liable_qty,
            "responsible": self.responsible,
            "share_pct": self.share_pct,
            "basis": self.basis,
            "basis_seq": self.basis_seq,
        }


@dataclass
class CaseState:
    """单个退货索赔单的投影状态。"""

    case_id: str
    supplier: str = ""
    title: str = ""
    status: ClaimStatus = ClaimStatus.DRAFT
    defect_batches: dict[str, DefectBatch] = field(default_factory=dict)
    lines: dict[int, ReturnLine] = field(default_factory=dict)
    evidences: list[LogisticsEvidence] = field(default_factory=list)
    liabilities: list[LiabilityRecord] = field(default_factory=list)
    stock_movements: list[StockMovement] = field(default_factory=list)
    claim_entries: list[ClaimEntry] = field(default_factory=list)
    # 行级投影辅助：是否已有签退结论 / 责任结论 / 已结算 / 已入账索赔数量
    _settled: set[int] = field(default_factory=set)
    _has_physical: set[int] = field(default_factory=set)
    _has_liability: set[int] = field(default_factory=set)
    _claimed_qty: dict[int, int] = field(default_factory=dict)

    # ---- 派生口径 --------------------------------------------------------

    def target_claim_qty(self, line: ReturnLine) -> int:
        """应收索赔数量的目标口径。

        * 批准时先按批准数量全额计提应收；
        * 一旦有签退结论，封顶为实物签退数量（治「部分退回仍全量索赔」）；
        * 一旦有责任认定，封顶为责任数量（治「供应商异议仍全量索赔」）；
        * 换货抵扣逐笔扣减；撤销则归零。
        """
        if line.revoked:
            return 0
        base = line.approved_qty
        if line.line_no in self._has_physical:
            base = min(base, line.received_qty)
        if line.line_no in self._has_liability:
            base = min(base, line.liable_qty)
        return max(0, base - line.exchange_qty)

    def zone_qty(self, line_no: int, zone: str) -> int:
        return sum(
            m.qty_delta
            for m in self.stock_movements
            if m.line_no == line_no and m.zone == zone
        )

    def quarantine_qty(self, line_no: int | None = None) -> int:
        """隔离库存当前结存（全部行或指定行）。"""
        return sum(
            m.qty_delta
            for m in self.stock_movements
            if m.zone == ZONE_QUARANTINE and (line_no is None or m.line_no == line_no)
        )

    def booked_claim_qty(self, line_no: int) -> int:
        """已由索赔分录入账的索赔数量（投影内部口径，供对账使用）。"""
        return self._claimed_qty.get(line_no, 0)

    def receivable_amount(self, line_no: int | None = None) -> int:
        """应收金额当前余额 = 索赔分录增量之和。"""
        return sum(
            e.amount_delta
            for e in self.claim_entries
            if line_no is None or e.line_no == line_no
        )


class CaseAggregate:
    """从 ``EventStore`` 回放指定 case 的聚合。"""

    def __init__(self, store: Any) -> None:
        self._store = store

    def load(self, case_id: str) -> CaseState:
        events = self._store.events_for(case_id)
        if not events:
            raise CaseNotFound(f"退货索赔单不存在：{case_id}")
        state = CaseState(case_id=case_id)
        for event in events:
            self.apply(state, event)
        return state

    # ---- 投影 ------------------------------------------------------------

    def apply(self, s: CaseState, e: Event) -> None:
        p = e.payload
        t = e.type

        if t is EventType.CASE_OPENED:
            s.supplier = p["supplier"]
            s.title = p.get("title", "")
            s.status = ClaimStatus.DRAFT

        elif t is EventType.DEFECT_BATCH_REGISTERED:
            s.defect_batches[p["batch_no"]] = DefectBatch(
                batch_no=p["batch_no"],
                material=p["material"],
                defect_qty=p["defect_qty"],
                defect_desc=p.get("defect_desc", ""),
            )

        elif t is EventType.RETURN_LINE_ADDED:
            line = ReturnLine(
                line_no=p["line_no"],
                batch_no=p["batch_no"],
                material=p["material"],
                request_qty=p["request_qty"],
                unit_price=p["unit_price"],
            )
            s.lines[line.line_no] = line
            # 来料判退：该批不合格品已在隔离区，按退货申请量建卡
            self._move(s, line, ZONE_QUARANTINE, line.request_qty, e,
                       f"建退货行 {line.request_qty}，来料判退入不合格品隔离区")

        elif t is EventType.LOGISTICS_EVIDENCE_ATTACHED:
            s.evidences.append(
                LogisticsEvidence(
                    evidence_id=p["evidence_id"],
                    doc_type=p["doc_type"],
                    doc_no=p["doc_no"],
                    carrier=p.get("carrier", ""),
                    confirmed_return_qty=p["confirmed_return_qty"],
                    note=p.get("note", ""),
                )
            )

        elif t is EventType.LIABILITY_DETERMINED:
            if s.status == ClaimStatus.DRAFT:
                s.status = ClaimStatus.PENDING
            for item in p["determinations"]:
                line = s.lines[item["line_no"]]
                line.liable_qty = item["liable_qty"]
                s._has_liability.add(line.line_no)
                s.liabilities.append(
                    LiabilityRecord(
                        line_no=line.line_no,
                        liable_qty=item["liable_qty"],
                        responsible=item.get("responsible", "供应商"),
                        share_pct=item.get("share_pct", 100),
                        basis=item.get("basis", ""),
                        basis_seq=e.seq,
                    )
                )
                self._reconcile_claim(s, line, e)

        elif t is EventType.RETURN_APPROVED:
            if s.status in (ClaimStatus.DRAFT, ClaimStatus.PENDING):
                s.status = ClaimStatus.RELEASED
            for item in p["approvals"]:
                line = s.lines[item["line_no"]]
                qty = item["approved_qty"]
                line.approved_qty = qty
                # 原子库存联动：隔离区出库、转入在途，同事件同序号
                self._move(s, line, ZONE_QUARANTINE, -qty, e,
                           f"批准退货 {qty}，隔离库存出库")
                self._move(s, line, ZONE_IN_TRANSIT, qty, e,
                           f"批准退货 {qty}，发运在途待供应商签退")
                self._reconcile_claim(s, line, e)

        elif t is EventType.GOODS_RECEIVED:
            if s.status == ClaimStatus.RELEASED:
                s.status = ClaimStatus.IN_PROGRESS
            for item in p["receipts"]:
                line = s.lines[item["line_no"]]
                signed = item["received_qty"]
                line.received_qty += signed
                line.held_by_supplier += signed
                s._has_physical.add(line.line_no)
                s._settled.add(line.line_no)
                # 签退结算：在途清零；签退部分归供应商，短少部分回隔离区
                short = line.approved_qty - line.received_qty
                self._move(s, line, ZONE_IN_TRANSIT, -line.approved_qty, e,
                           f"物流签退结算：供应商签退 {signed}，在途清账")
                self._move(s, line, ZONE_SUPPLIER, signed, e,
                           f"供应商签退持有 {signed}")
                if short > 0:
                    self._move(s, line, ZONE_QUARANTINE, short, e,
                               f"部分退回：短少 {short} 回厂入隔离区")
                self._reconcile_claim(s, line, e)

        elif t is EventType.PARTIAL_ACCEPTED:
            for item in p["accepted"]:
                line = s.lines[item["line_no"]]
                line.liable_qty = item["accepted_qty"]
                s._has_liability.add(line.line_no)
                s.liabilities.append(
                    LiabilityRecord(
                        line_no=line.line_no,
                        liable_qty=item["accepted_qty"],
                        responsible="供应商",
                        share_pct=100,
                        basis=item.get("note", "供应商部分接受"),
                        basis_seq=e.seq,
                    )
                )
                # 供应商拒收的争议实物可随复核退回厂内隔离区
                self._physical_return(s, line, item.get("return_qty", 0), e,
                                      "部分接受：拒收争议实物回厂")
                self._reconcile_claim(s, line, e)

        elif t is EventType.EXCHANGE_OFFSET:
            for item in p["exchanges"]:
                line = s.lines[item["line_no"]]
                qty = item["exchange_qty"]
                source = item.get("from_zone", ZONE_SUPPLIER)
                line.exchange_qty += qty
                self._move(s, line, source, -qty, e,
                           f"换货抵扣 {qty}：{source} 结存扣减")
                self._move(s, line, ZONE_EXCHANGE, qty, e,
                           f"换货抵扣 {qty}：供方补发合格品入库")
                if source == ZONE_SUPPLIER:
                    line.held_by_supplier -= qty
                self._reconcile_claim(s, line, e)

        elif t is EventType.DISPUTE_REVIEWED:
            for item in p["reviews"]:
                line = s.lines[item["line_no"]]
                outcome = item["outcome"]
                if outcome in ("upheld", "adjusted"):
                    line.liable_qty = item["liable_qty"]
                elif outcome == "rejected":
                    line.liable_qty = 0
                else:  # pragma: no cover - 命令层已校验
                    raise ValueError(f"未知争议复核结论：{outcome}")
                s._has_liability.add(line.line_no)
                s.liabilities.append(
                    LiabilityRecord(
                        line_no=line.line_no,
                        liable_qty=line.liable_qty,
                        responsible=item.get("responsible", "供应商"),
                        share_pct=item.get("share_pct", 100),
                        basis=item.get("note", f"争议复核：{outcome}"),
                        basis_seq=e.seq,
                    )
                )
                self._physical_return(s, line, item.get("return_qty", 0), e,
                                      "争议复核：异议成立实物回厂")
                self._reconcile_claim(s, line, e)

        elif t is EventType.REVOKED:
            for item in p["lines"]:
                line = s.lines[item["line_no"]]
                line.revoked = True
                # 供应商持有的货物退回厂内隔离区
                held = s.zone_qty(line.line_no, ZONE_SUPPLIER)
                if held > 0:
                    self._move(s, line, ZONE_SUPPLIER, -held, e,
                               "撤销退货：供应商持有货物退回")
                    self._move(s, line, ZONE_QUARANTINE, held, e,
                               "撤销退货：货物回厂入隔离区待处置")
                    line.held_by_supplier -= held
                # 在途货物截退回厂
                transit = s.zone_qty(line.line_no, ZONE_IN_TRANSIT)
                if transit > 0:
                    self._move(s, line, ZONE_IN_TRANSIT, -transit, e,
                               "撤销退货：截停在途货物")
                    self._move(s, line, ZONE_QUARANTINE, transit, e,
                               "撤销退货：在途货物回厂入隔离区")
                self._reconcile_claim(s, line, e)

        elif t is EventType.CLOSED:
            s.status = ClaimStatus.CLOSED

        else:  # pragma: no cover
            raise ValueError(f"未知事件类型：{t}")

    # ---- 投影辅助 --------------------------------------------------------

    def _move(self, s: CaseState, line: ReturnLine, zone: str, delta: int,
              event: Event, reason: str) -> None:
        s.stock_movements.append(
            StockMovement(
                line_no=line.line_no,
                batch_no=line.batch_no,
                zone=zone,
                qty_delta=delta,
                basis_seq=event.seq,
                basis_type=event.type,
                reason=reason,
                actor=event.actor,
            )
        )

    def _physical_return(self, s: CaseState, line: ReturnLine, qty: int,
                         event: Event, reason: str) -> None:
        """争议/部分接受中，供应商把拒收的实物退回厂内。"""
        if qty <= 0:
            return
        self._move(s, line, ZONE_SUPPLIER, -qty, event, reason)
        self._move(s, line, ZONE_QUARANTINE, qty, event, reason)
        line.held_by_supplier -= qty

    def _reconcile_claim(self, s: CaseState, line: ReturnLine, event: Event) -> None:
        """把该行应收索赔数量收敛到目标口径，差额作为一条分录增量入账。

        正向事件产生正数计提；签退短少/部分接受/换货/争议/撤销产生负数
        冲销。每个事件对每行至多一条分录，单调逼近目标，绝不重复计提。
        """
        target = s.target_claim_qty(line)
        prev = s._claimed_qty.get(line.line_no, 0)
        delta_qty = target - prev
        if delta_qty == 0:
            return
        s._claimed_qty[line.line_no] = target
        s.claim_entries.append(
            ClaimEntry(
                line_no=line.line_no,
                amount_delta=delta_qty * line.unit_price,
                basis_seq=event.seq,
                basis_type=event.type,
                reason=event.reason,
                actor=event.actor,
            )
        )
