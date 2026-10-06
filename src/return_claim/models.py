"""领域模型：缺陷批次、退货行、物流证据、责任认定、索赔分录。

只保存状态结构，不包含行为；行为全部由 ``commands`` 以事件方式驱动。
金额统一用整数「分」存储，杜绝浮点误差；数量按物料基本单位计量的整数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def yuan(value: int) -> str:
    """把整数分渲染成两位小数字符串，仅用于展示。"""
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ClaimStatus(str, Enum):
    """退货索赔单的生命周期，对齐领域契约的五个状态。"""

    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    IN_PROGRESS = "履行中"
    CLOSED = "已关闭"


class EventType(str, Enum):
    """事件类型。

    批准退货（RETURN_APPROVED）是唯一原子地联动「隔离库存 + 应收索赔」
    的事件；部分接受、换货抵扣、争议复核、撤销均为后续补偿事件，
    冲销/修正全部以反向或增量事件体现，历史事件永不修改。
    """

    CASE_OPENED = "case_opened"
    DEFECT_BATCH_REGISTERED = "defect_batch_registered"
    RETURN_LINE_ADDED = "return_line_added"
    LOGISTICS_EVIDENCE_ATTACHED = "logistics_evidence_attached"
    LIABILITY_DETERMINED = "liability_determined"
    RETURN_APPROVED = "return_approved"
    GOODS_RECEIVED = "goods_received"
    PARTIAL_ACCEPTED = "partial_accepted"
    EXCHANGE_OFFSET = "exchange_offset"
    DISPUTE_REVIEWED = "dispute_reviewed"
    REVOKED = "revoked"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """只追加的领域事件。

    ``seq`` 由事件存储分配，从 1 开始，case 内连续、单调，是所有调整
    依据的最终排序凭证；``reason`` 必须人工填写，支撑「展示每次调整依据」。
    """

    seq: int
    case_id: str
    type: EventType
    actor: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "case_id": self.case_id,
            "type": self.type.value,
            "actor": self.actor,
            "reason": self.reason,
            "payload": self.payload,
        }


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------

@dataclass
class DefectBatch:
    """缺陷批次：质量工程师登记，描述来料不合格事实。"""

    batch_no: str
    material: str
    defect_qty: int
    defect_desc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_no": self.batch_no,
            "material": self.material,
            "defect_qty": self.defect_qty,
            "defect_desc": self.defect_desc,
        }


@dataclass
class ReturnLine:
    """退货行：采购/质量提出的退货意向数量，关联缺陷批次。"""

    line_no: int
    batch_no: str
    material: str
    request_qty: int
    unit_price: int
    # 批准阶段原子确认的数量（<= request_qty）；未批准为 0
    approved_qty: int = 0
    # 供应商实际签退并经物流证据核实的实物数量
    received_qty: int = 0
    # 经责任认定、部分接受、争议复核后确认的责任数量
    liable_qty: int = 0
    # 换货补偿（供应商补发合格品）数量，不再向供应商收钱
    exchange_qty: int = 0
    # 批准后当前实际由供应商持有的数量（在途/已签收，扣除已退回）
    held_by_supplier: int = 0
    revoked: bool = False

    @property
    def claimable_qty(self) -> int:
        """可索赔数量 = min(实物签退, 责任数量) - 换货抵扣，下限为 0。

        这是防止「库存已扣减但索赔仍按全量计算」的核心口径：
        索赔绝不能超过实物，也绝不能超过责任认定，换货部分必须扣除。
        """
        return max(0, min(self.received_qty, self.liable_qty) - self.exchange_qty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "batch_no": self.batch_no,
            "material": self.material,
            "request_qty": self.request_qty,
            "unit_price": self.unit_price,
            "unit_price_yuan": yuan(self.unit_price),
            "approved_qty": self.approved_qty,
            "received_qty": self.received_qty,
            "liable_qty": self.liable_qty,
            "exchange_qty": self.exchange_qty,
            "held_by_supplier": self.held_by_supplier,
            "claimable_qty": self.claimable_qty,
            "revoked": self.revoked,
        }


@dataclass
class LogisticsEvidence:
    """物流证据：出库单、签收回单等，证明实物实际退回数量。"""

    evidence_id: str
    doc_type: str
    doc_no: str
    carrier: str
    confirmed_return_qty: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "doc_type": self.doc_type,
            "doc_no": self.doc_no,
            "carrier": self.carrier,
            "confirmed_return_qty": self.confirmed_return_qty,
            "note": self.note,
        }


@dataclass
class ClaimEntry:
    """索赔分录：应收供应商金额的一条明细，由事件投影生成。

    每个非冲销事件只会对一行追加金额增量；补偿事件以负增量冲销，
    并通过 ``basis_seq`` 指回触发事件，实现「每次调整有依据」。
    """

    line_no: int
    amount_delta: int
    basis_seq: int
    basis_type: EventType
    reason: str
    actor: str

    @property
    def is_reversal(self) -> bool:
        return self.amount_delta < 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "amount_delta": self.amount_delta,
            "amount_delta_yuan": yuan(self.amount_delta),
            "basis_seq": self.basis_seq,
            "basis_type": self.basis_type.value,
            "reason": self.reason,
            "actor": self.actor,
            "reversal": self.is_reversal,
        }


@dataclass
class StockMovement:
    """库存流水，由事件投影生成。

    ``zone`` 标识数量所在位置：``隔离库存``（厂内 quarantine）或
    ``供应商``（在途/供应商已签收）。任一时刻隔离区流水求和必须 >= 0。
    """

    line_no: int
    batch_no: str
    zone: str
    qty_delta: int
    basis_seq: int
    basis_type: EventType
    reason: str
    actor: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "batch_no": self.batch_no,
            "zone": self.zone,
            "qty_delta": self.qty_delta,
            "basis_seq": self.basis_seq,
            "basis_type": self.basis_type.value,
            "reason": self.reason,
            "actor": self.actor,
        }
