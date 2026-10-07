"""退货索赔协同的领域模型：状态、角色、账户与金额约定。

状态与角色取值与 domain/contract.json 保持一致。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum

from .errors import ValidationError

CENT = Decimal("0.01")


def to_decimal(value: object, *, field: str) -> Decimal:
    """把外部输入解析为 Decimal，拒绝非数字与非有限值。"""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{field} 必须是数字，当前值：{value!r}") from None
    if not result.is_finite():
        raise ValidationError(f"{field} 必须是有限数字，当前值：{value!r}")
    return result


def money(value: Decimal) -> Decimal:
    """金额统一保留两位小数（ half-up ）。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class OrderState(str, Enum):
    """退货单/退货行状态，取值对齐领域契约 states。"""

    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    FULFILLING = "履行中"
    CLOSED = "已关闭"


class Actor(str, Enum):
    """领域契约中的四类角色。"""

    BUYER = "采购计划员"
    SUPPLIER = "供应商"
    QUALITY = "质量工程师"
    WAREHOUSE = "仓储管理员"


class Account(str, Enum):
    """库存账户：隔离库存的流转轨迹。"""

    QUARANTINE = "隔离区"
    IN_TRANSIT = "供应商在途"
    GOOD_STOCK = "良品仓"
    RETURNED = "已退供应商"


class EntryKind(str, Enum):
    """索赔分录类型：原始计提 + 各类补偿调整。"""

    ORIGINAL = "原始索赔"
    LIABILITY_ADJUST = "责任认定调整"
    PARTIAL_ACCEPT = "部分接受冲减"
    REPLACEMENT_OFFSET = "换货抵扣"
    DISPUTE_REVIEW = "争议复核调整"
    CANCEL_REVERSAL = "撤销冲回"


class CompensationType(str, Enum):
    """补偿事件类型。"""

    PARTIAL_ACCEPT = "部分接受"
    REPLACEMENT = "换货抵扣"
    DISPUTE_REVIEW = "争议复核"
    CANCEL = "撤销"
