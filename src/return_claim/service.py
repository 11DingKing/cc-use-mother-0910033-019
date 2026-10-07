"""退货索赔协同的应用服务：状态机、原子调整与补偿事件。

核心约束（对应 domain/contract.json 的 invariants）：
- 退货库存联动：批准退货在同一事务内扣减隔离库存并计提应收，任一行失败整体回滚；
- 责任数量认定：责任认定与争议复核只通过调整分录改变索赔数量，认定历史留痕；
- 索赔分录补偿：原始分录永不修改，部分接受/换货抵扣/争议复核/撤销一律追加补偿分录；
- 实物金额对账：对账接口并列实物数量、责任数量与金额，并给出每次调整的依据。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from . import reconcile
from .errors import DomainError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    Account,
    Actor,
    CompensationType,
    EntryKind,
    OrderState,
    money,
    to_decimal,
)
from .store import Store

_ZERO = Decimal("0")

_EVIDENCE_KINDS = ("发货", "签收")
_EVIDENCE_ACTORS = {
    "发货": {Actor.WAREHOUSE.value},
    "签收": {Actor.SUPPLIER.value},
}
_COMPENSATION_ACTORS = {
    CompensationType.PARTIAL_ACCEPT.value: {Actor.SUPPLIER.value},
    CompensationType.REPLACEMENT.value: {Actor.SUPPLIER.value},
    CompensationType.DISPUTE_REVIEW.value: {Actor.QUALITY.value},
    CompensationType.CANCEL.value: {Actor.BUYER.value},
}
_ACTIVE_LINE_STATES = (OrderState.RELEASED.value, OrderState.FULFILLING.value)


def _now() -> str:
    # 微秒精度：同一秒内的连续操作也能稳定排序（对账依据链按时间归并）
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ReturnClaimService:
    """退货索赔协同应用服务。contract 为领域契约（可选），用于校验角色合法性。"""

    def __init__(self, store: Store, contract: dict | None = None) -> None:
        self.store = store
        self._contract_actors = set(contract["actors"]) if contract else None

    # ------------------------------------------------------------------
    # 缺陷批次
    # ------------------------------------------------------------------

    def register_defect_batch(self, *, actor: str, batch_no: str, material: str,
                              supplier: str, defect_qty: object, unit_price: object) -> dict:
        """质量工程师登记缺陷批次，缺陷数量同步转入隔离区。"""
        self._check_actor(actor, {Actor.QUALITY.value})
        if not batch_no or not material or not supplier:
            raise ValidationError("batch_no、material、supplier 不能为空")
        qty = to_decimal(defect_qty, field="defect_qty")
        price = money(to_decimal(unit_price, field="unit_price"))
        if qty <= _ZERO:
            raise ValidationError("defect_qty 必须为正数")
        if price < _ZERO:
            raise ValidationError("unit_price 不能为负")
        with self.store.transaction() as conn:
            try:
                batch_id = self._insert(
                    conn, "defect_batch", batch_no=batch_no, material=material,
                    supplier=supplier, defect_qty=str(qty), unit_price=str(price),
                    created_by=actor, created_at=_now())
            except sqlite3.IntegrityError:
                raise DomainError(f"缺陷批次号已存在：{batch_no}") from None
            self._move(conn, batch_id=batch_id, line_id=None,
                       account=Account.QUARANTINE.value, qty_delta=qty,
                       reason="来料检验不合格，转入隔离区",
                       basis_type="缺陷批次", basis_id=batch_no, actor=actor)
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> dict:
        with self.store.read() as conn:
            batch = self._batch_row(conn, batch_id)
            return {
                "batch_id": batch["id"],
                "batch_no": batch["batch_no"],
                "material": batch["material"],
                "supplier": batch["supplier"],
                "defect_qty": Decimal(batch["defect_qty"]),
                "unit_price": Decimal(batch["unit_price"]),
                "quarantine_balance": self._sum_movements(
                    conn, batch_id=batch["id"], account=Account.QUARANTINE.value),
                "created_by": batch["created_by"],
                "created_at": batch["created_at"],
            }

    # ------------------------------------------------------------------
    # 退货单生命周期：草拟 → 待确认 → 已下达 → 履行中 → 已关闭
    # ------------------------------------------------------------------

    def create_return_order(self, *, actor: str, order_no: str, lines: list[dict]) -> dict:
        """采购计划员起草退货单，每行关联一个缺陷批次。"""
        self._check_actor(actor, {Actor.BUYER.value})
        if not order_no:
            raise ValidationError("order_no 不能为空")
        if not isinstance(lines, list) or not lines:
            raise ValidationError("退货单至少包含一行")
        with self.store.transaction() as conn:
            try:
                order_id = self._insert(
                    conn, "return_order", order_no=order_no,
                    state=OrderState.DRAFT.value, created_by=actor, created_at=_now())
            except sqlite3.IntegrityError:
                raise DomainError(f"退货单号已存在：{order_no}") from None
            for item in lines:
                if not isinstance(item, dict) or item.get("batch_id") is None:
                    raise ValidationError("退货行缺少 batch_id")
                batch = self._batch_row(conn, item["batch_id"])
                qty = to_decimal(item.get("qty"), field="qty")
                if qty <= _ZERO:
                    raise ValidationError("退货数量必须为正数")
                defect = Decimal(batch["defect_qty"])
                if qty > defect:
                    raise ValidationError(
                        f"退货数量 {qty} 超过缺陷数量 {defect}（批次 {batch['batch_no']}）")
                self._insert(
                    conn, "return_line", order_id=order_id, batch_id=batch["id"],
                    request_qty=str(qty), unit_price=batch["unit_price"],
                    state=OrderState.DRAFT.value, close_reason=None)
        return self.get_order(order_id)

    def submit_return_order(self, *, actor: str, order_id: int) -> dict:
        """提交退货单：草拟 → 待确认。"""
        self._check_actor(actor, {Actor.BUYER.value})
        with self.store.transaction() as conn:
            order = self._order_row(conn, order_id)
            self._require_state(order["state"], OrderState.DRAFT, "提交")
            conn.execute("UPDATE return_order SET state = ? WHERE id = ?",
                         (OrderState.PENDING.value, order["id"]))
            conn.execute("UPDATE return_line SET state = ? WHERE order_id = ?",
                         (OrderState.PENDING.value, order["id"]))
        return self.get_order(order_id)

    def approve_return_order(self, *, actor: str, order_id: int) -> dict:
        """批准退货：同一事务内扣减隔离库存、转供应商在途并计提应收。

        任一行隔离库存不足或校验失败，整个事务回滚——
        库存与应收要么一起调整，要么都不调整。
        """
        self._check_actor(actor, {Actor.BUYER.value})
        with self.store.transaction() as conn:
            order = self._order_row(conn, order_id)
            self._require_state(order["state"], OrderState.PENDING, "批准")
            lines = conn.execute(
                "SELECT * FROM return_line WHERE order_id = ? ORDER BY id",
                (order["id"],)).fetchall()
            for line in lines:
                qty = Decimal(line["request_qty"])
                price = Decimal(line["unit_price"])
                batch = self._batch_row(conn, line["batch_id"])
                quarantine = self._sum_movements(
                    conn, batch_id=batch["id"], account=Account.QUARANTINE.value)
                if quarantine < qty:
                    raise DomainError(
                        f"隔离库存不足：批次 {batch['batch_no']} 可用 {quarantine}，需要 {qty}")
                ref = order["order_no"]
                self._move(conn, batch_id=batch["id"], line_id=line["id"],
                           account=Account.QUARANTINE.value, qty_delta=-qty,
                           reason="退货批准，隔离区出库", basis_type="退货批准",
                           basis_id=ref, actor=actor)
                self._move(conn, batch_id=batch["id"], line_id=line["id"],
                           account=Account.IN_TRANSIT.value, qty_delta=qty,
                           reason="退货批准，转供应商在途", basis_type="退货批准",
                           basis_id=ref, actor=actor)
                self._entry(conn, line_id=line["id"], kind=EntryKind.ORIGINAL.value,
                            qty=qty, unit_price=price, basis_type="退货批准",
                            basis_id=ref, actor=actor,
                            note=f"批准退货 {qty}，按单价 {price} 计提应收")
                conn.execute("UPDATE return_line SET state = ? WHERE id = ?",
                             (OrderState.RELEASED.value, line["id"]))
            conn.execute("UPDATE return_order SET state = ? WHERE id = ?",
                         (OrderState.RELEASED.value, order["id"]))
        return self.get_order(order_id)

    def cancel_return_order(self, *, actor: str, order_id: int, reason: str = "") -> dict:
        """批准前撤销整张退货单（无账务影响，直接关闭）。"""
        self._check_actor(actor, {Actor.BUYER.value})
        with self.store.transaction() as conn:
            order = self._order_row(conn, order_id)
            if order["state"] not in (OrderState.DRAFT.value, OrderState.PENDING.value):
                raise DomainError(
                    f"当前状态 {order['state']} 不允许整单撤销；已下达的退货请走补偿事件-撤销")
            conn.execute("UPDATE return_order SET state = ? WHERE id = ?",
                         (OrderState.CLOSED.value, order["id"]))
            conn.execute(
                "UPDATE return_line SET state = ?, close_reason = ? WHERE order_id = ?",
                (OrderState.CLOSED.value, f"撤销：{reason}" if reason else "撤销", order["id"]))
        return self.get_order(order_id)

    def close_return_order(self, *, actor: str, order_id: int) -> dict:
        """人工关闭退货单：要求所有退货行实物清零（在途为 0），应收可挂账。"""
        self._check_actor(actor, {Actor.BUYER.value})
        with self.store.transaction() as conn:
            order = self._order_row(conn, order_id)
            if order["state"] not in (OrderState.RELEASED.value, OrderState.FULFILLING.value):
                raise DomainError(f"当前状态 {order['state']} 不能关闭")
            lines = conn.execute(
                "SELECT * FROM return_line WHERE order_id = ?", (order["id"],)).fetchall()
            for line in lines:
                if line["state"] == OrderState.CLOSED.value:
                    continue
                in_transit = self._sum_movements(
                    conn, line_id=line["id"], account=Account.IN_TRANSIT.value)
                if in_transit != _ZERO:
                    raise DomainError(
                        f"退货行 {line['id']} 仍有供应商在途 {in_transit}，不能关闭")
                conn.execute(
                    "UPDATE return_line SET state = ?, close_reason = ? WHERE id = ?",
                    (OrderState.CLOSED.value, "人工关闭", line["id"]))
            conn.execute("UPDATE return_order SET state = ? WHERE id = ?",
                         (OrderState.CLOSED.value, order["id"]))
        return self.get_order(order_id)

    def get_order(self, order_id: int) -> dict:
        with self.store.read() as conn:
            order = self._order_row(conn, order_id)
            lines = conn.execute(
                """SELECT l.*, b.batch_no, b.material, b.supplier
                   FROM return_line l JOIN defect_batch b ON b.id = l.batch_id
                   WHERE l.order_id = ? ORDER BY l.id""", (order["id"],)).fetchall()
            return {
                "order_id": order["id"],
                "order_no": order["order_no"],
                "state": order["state"],
                "created_by": order["created_by"],
                "created_at": order["created_at"],
                "lines": [self._line_summary(conn, line) for line in lines],
            }

    # ------------------------------------------------------------------
    # 物流证据与责任认定
    # ------------------------------------------------------------------

    def record_evidence(self, *, actor: str, line_id: int, kind: str,
                        qty: object, carrier: str = "", tracking_no: str = "") -> dict:
        """登记物流证据：仓储管理员登记发货，供应商登记签收。

        签收联动库存：供应商在途 → 已退供应商，退货行进入履行中。
        """
        if kind not in _EVIDENCE_KINDS:
            raise ValidationError(f"证据类型必须是：{'、'.join(_EVIDENCE_KINDS)}")
        self._check_actor(actor, _EVIDENCE_ACTORS[kind])
        qty = to_decimal(qty, field="qty")
        if qty <= _ZERO:
            raise ValidationError("证据数量必须为正数")
        with self.store.transaction() as conn:
            line = self._line_row(conn, line_id)
            self._require_line_active(line, "登记物流证据")
            sums = self._evidence_sums(conn, line["id"])
            if kind == "发货":
                if sums["发货"] + qty > Decimal(line["request_qty"]):
                    raise DomainError(
                        f"累计发货 {sums['发货'] + qty} 超过批准数量 {line['request_qty']}")
            else:
                if sums["签收"] + qty > sums["发货"]:
                    raise DomainError(
                        f"累计签收 {sums['签收'] + qty} 超过累计发货 {sums['发货']}")
                in_transit = self._sum_movements(
                    conn, line_id=line["id"], account=Account.IN_TRANSIT.value)
                if in_transit < qty:
                    raise DomainError(f"供应商在途 {in_transit} 不足以签收 {qty}")
            evidence_id = self._insert(
                conn, "logistics_evidence", line_id=line["id"], kind=kind, qty=str(qty),
                carrier=carrier or "", tracking_no=tracking_no or "",
                actor=actor, created_at=_now())
            if kind == "签收":
                ref = f"EV-{evidence_id}"
                self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                           account=Account.IN_TRANSIT.value, qty_delta=-qty,
                           reason="供应商签收，退出在途", basis_type="物流证据",
                           basis_id=ref, actor=actor)
                self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                           account=Account.RETURNED.value, qty_delta=qty,
                           reason="供应商签收，货物已退供应商", basis_type="物流证据",
                           basis_id=ref, actor=actor)
                self._mark_fulfilling(conn, line)
        return {"evidence_id": evidence_id, "line_id": int(line_id), "kind": kind, "qty": qty}

    def determine_liability(self, *, actor: str, line_id: int,
                            determined_qty: object, reason: str) -> dict:
        """质量工程师首次责任认定：索赔数量一次性调整到认定数量。"""
        self._check_actor(actor, {Actor.QUALITY.value})
        if not reason:
            raise ValidationError("责任认定必须填写原因")
        determined = to_decimal(determined_qty, field="determined_qty")
        with self.store.transaction() as conn:
            line = self._line_row(conn, line_id)
            self._require_line_active(line, "责任认定")
            if determined < _ZERO or determined > Decimal(line["request_qty"]):
                raise ValidationError(
                    f"认定数量 {determined} 必须在 0 与批准数量 {line['request_qty']} 之间")
            existing = conn.execute(
                "SELECT COUNT(*) AS c FROM liability_determination WHERE line_id = ?",
                (line["id"],)).fetchone()["c"]
            if existing:
                raise DomainError("责任已认定，如需调整请走补偿事件-争议复核")
            det_id = self._insert(
                conn, "liability_determination", line_id=line["id"],
                determined_qty=str(determined), reason=reason,
                actor=actor, created_at=_now())
            self._adjust_to_target(conn, line, target=determined,
                                   kind=EntryKind.LIABILITY_ADJUST.value,
                                   basis_type="责任认定", basis_id=f"LD-{det_id}",
                                   note=f"责任认定 {determined}：{reason}", actor=actor)
        return self.reconcile_line(line_id)

    # ------------------------------------------------------------------
    # 补偿事件：部分接受 / 换货抵扣 / 争议复核 / 撤销
    # ------------------------------------------------------------------

    def apply_compensation(self, *, actor: str, line_id: int,
                           compensation_type: str, payload: dict | None = None) -> dict:
        """登记补偿事件并生成对应的调整分录与库存联动，返回事件后的对账结果。"""
        try:
            ctype = CompensationType(compensation_type)
        except ValueError:
            valid = "、".join(t.value for t in CompensationType)
            raise ValidationError(f"未知补偿类型：{compensation_type}（可选：{valid}）") from None
        self._check_actor(actor, _COMPENSATION_ACTORS[ctype.value])
        payload = dict(payload or {})
        with self.store.transaction() as conn:
            line = self._line_row(conn, line_id)
            self._require_line_active(line, f"补偿事件-{ctype.value}")
            event_id = self._insert(
                conn, "compensation_event", line_id=line["id"], type=ctype.value,
                payload=json.dumps(payload, ensure_ascii=False),
                actor=actor, created_at=_now())
            ref = f"CE-{event_id}"
            if ctype is CompensationType.PARTIAL_ACCEPT:
                self._partial_accept(conn, line, actor, ref, payload)
            elif ctype is CompensationType.REPLACEMENT:
                self._replacement_offset(conn, line, actor, ref, payload)
            elif ctype is CompensationType.DISPUTE_REVIEW:
                self._dispute_review(conn, line, actor, ref, payload)
            else:
                self._cancel_line(conn, line, actor, ref, payload)
            self._after_line_change(conn, line["id"])
            result = reconcile.line_reconciliation(conn, line["id"])
        result["compensation_id"] = event_id
        result["compensation_type"] = ctype.value
        return result

    def _partial_accept(self, conn, line, actor, ref, payload) -> None:
        """部分接受：供应商只认一部分，未接受部分冲减索赔并退回隔离区。"""
        if "accepted_qty" not in payload:
            raise ValidationError("部分接受需要 accepted_qty")
        accepted = to_decimal(payload["accepted_qty"], field="accepted_qty")
        outstanding, _ = self._claim_sums(conn, line["id"])
        if accepted < _ZERO or accepted > outstanding:
            raise DomainError(f"接受数量 {accepted} 必须在 0 与未决索赔 {outstanding} 之间")
        unaccepted = outstanding - accepted
        if unaccepted == _ZERO:
            return
        in_transit = self._sum_movements(
            conn, line_id=line["id"], account=Account.IN_TRANSIT.value)
        if in_transit < unaccepted:
            raise DomainError(
                f"供应商在途 {in_transit} 不足以退回未接受数量 {unaccepted}，请先核对签收证据")
        price = Decimal(line["unit_price"])
        self._entry(conn, line_id=line["id"], kind=EntryKind.PARTIAL_ACCEPT.value,
                    qty=-unaccepted, unit_price=price, basis_type="补偿事件", basis_id=ref,
                    note=f"供应商仅接受 {accepted}，冲减未接受 {unaccepted}", actor=actor)
        self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                   account=Account.IN_TRANSIT.value, qty_delta=-unaccepted,
                   reason="部分接受，未接受数量退出在途", basis_type="补偿事件",
                   basis_id=ref, actor=actor)
        self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                   account=Account.QUARANTINE.value, qty_delta=unaccepted,
                   reason="部分接受，未接受数量退回隔离区", basis_type="补偿事件",
                   basis_id=ref, actor=actor)

    def _replacement_offset(self, conn, line, actor, ref, payload) -> None:
        """换货抵扣：供应商补良品入良品仓，按数量抵扣应收。"""
        if "replacement_qty" not in payload:
            raise ValidationError("换货抵扣需要 replacement_qty")
        replacement = to_decimal(payload["replacement_qty"], field="replacement_qty")
        outstanding, _ = self._claim_sums(conn, line["id"])
        if replacement <= _ZERO or replacement > outstanding:
            raise DomainError(f"换货数量 {replacement} 必须在 0 与未决索赔 {outstanding} 之间")
        price = Decimal(line["unit_price"])
        note = payload.get("note") or f"供应商换货 {replacement}，抵扣应收"
        self._entry(conn, line_id=line["id"], kind=EntryKind.REPLACEMENT_OFFSET.value,
                    qty=-replacement, unit_price=price, basis_type="补偿事件", basis_id=ref,
                    note=note, actor=actor)
        self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                   account=Account.GOOD_STOCK.value, qty_delta=replacement,
                   reason="供应商换货补入良品仓", basis_type="补偿事件",
                   basis_id=ref, actor=actor)

    def _dispute_review(self, conn, line, actor, ref, payload) -> None:
        """争议复核：重新认定责任总量，索赔调整到「认定量 − 已换货抵扣量」。"""
        if "revised_qty" not in payload:
            raise ValidationError("争议复核需要 revised_qty")
        reason = payload.get("reason") or ""
        if not reason:
            raise ValidationError("争议复核必须填写原因")
        revised = to_decimal(payload["revised_qty"], field="revised_qty")
        if revised < _ZERO:
            raise ValidationError("复核认定数量不能为负")
        det_id = self._insert(
            conn, "liability_determination", line_id=line["id"],
            determined_qty=str(revised), reason=f"争议复核：{reason}",
            actor=actor, created_at=_now())
        replacement = self._sum_movements(
            conn, line_id=line["id"], account=Account.GOOD_STOCK.value)
        self._adjust_to_target(conn, line, target=revised - replacement,
                               kind=EntryKind.DISPUTE_REVIEW.value,
                               basis_type="补偿事件", basis_id=ref,
                               note=f"争议复核认定 {revised}（已抵扣换货 {replacement}）：{reason}",
                               actor=actor)

    def _cancel_line(self, conn, line, actor, ref, payload) -> None:
        """撤销：冲回全部未决索赔，在途货物退回隔离区，退货行关闭。"""
        reason = payload.get("reason") or ""
        if not reason:
            raise ValidationError("撤销必须填写原因")
        outstanding, _ = self._claim_sums(conn, line["id"])
        in_transit = self._sum_movements(
            conn, line_id=line["id"], account=Account.IN_TRANSIT.value)
        if outstanding == _ZERO and in_transit == _ZERO:
            raise DomainError("该退货行无未决索赔且在途为 0，无可撤销内容")
        price = Decimal(line["unit_price"])
        if outstanding != _ZERO:
            self._entry(conn, line_id=line["id"], kind=EntryKind.CANCEL_REVERSAL.value,
                        qty=-outstanding, unit_price=price, basis_type="补偿事件",
                        basis_id=ref, note=f"撤销退货，冲回未决索赔：{reason}", actor=actor)
        if in_transit != _ZERO:
            self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                       account=Account.IN_TRANSIT.value, qty_delta=-in_transit,
                       reason="撤销退货，在途退出", basis_type="补偿事件",
                       basis_id=ref, actor=actor)
            self._move(conn, batch_id=line["batch_id"], line_id=line["id"],
                       account=Account.QUARANTINE.value, qty_delta=in_transit,
                       reason="撤销退货，在途退回隔离区", basis_type="补偿事件",
                       basis_id=ref, actor=actor)
        conn.execute("UPDATE return_line SET state = ?, close_reason = ? WHERE id = ?",
                     (OrderState.CLOSED.value, f"撤销：{reason}", line["id"]))

    # ------------------------------------------------------------------
    # 对账
    # ------------------------------------------------------------------

    def reconcile_line(self, line_id: int) -> dict:
        """退货行对账：实物数量、责任数量、金额三视图 + 每次调整依据。"""
        with self.store.read() as conn:
            return reconcile.line_reconciliation(conn, int(line_id))

    def reconcile_batch(self, batch_id: int) -> dict:
        """缺陷批次对账：批次级隔离库存余额与全部退货行汇总。"""
        with self.store.read() as conn:
            return reconcile.batch_reconciliation(conn, int(batch_id))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _check_actor(self, actor: str, allowed: set[str]) -> None:
        if not actor:
            raise ValidationError("缺少操作角色 actor")
        if self._contract_actors is not None and actor not in self._contract_actors:
            raise ValidationError(f"未知角色：{actor}（领域契约未登记）")
        if actor not in allowed:
            raise PermissionDenied(
                f"{actor} 无权执行该操作，需要：{'、'.join(sorted(allowed))}")

    @staticmethod
    def _insert(conn, table: str, **fields) -> int:
        columns = ", ".join(fields)
        marks = ", ".join(["?"] * len(fields))
        cur = conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                           tuple(fields.values()))
        return int(cur.lastrowid)

    def _move(self, conn, *, batch_id, line_id, account, qty_delta,
              reason, basis_type, basis_id, actor) -> None:
        self._insert(conn, "inventory_movement", batch_id=batch_id, line_id=line_id,
                     account=account, qty_delta=str(qty_delta), reason=reason,
                     basis_type=basis_type, basis_id=str(basis_id),
                     actor=actor, created_at=_now())

    def _entry(self, conn, *, line_id, kind, qty, unit_price,
               basis_type, basis_id, note, actor) -> None:
        amount = money(qty * unit_price)
        self._insert(conn, "claim_entry", line_id=line_id, kind=kind, qty=str(qty),
                     unit_price=str(unit_price), amount=str(amount),
                     basis_type=basis_type, basis_id=str(basis_id), note=note,
                     actor=actor, created_at=_now())

    def _adjust_to_target(self, conn, line, *, target, kind, basis_type,
                          basis_id, note, actor) -> None:
        """把未决索赔数量调整到 target，差额生成一笔调整分录。"""
        outstanding, _ = self._claim_sums(conn, line["id"])
        delta = target - outstanding
        if delta == _ZERO:
            return
        self._entry(conn, line_id=line["id"], kind=kind, qty=delta,
                    unit_price=Decimal(line["unit_price"]),
                    basis_type=basis_type, basis_id=basis_id,
                    note=f"{note}（调整 {delta}）", actor=actor)

    def _after_line_change(self, conn, line_id: int) -> None:
        """补偿事件后刷新关闭状态：索赔清零且在途清零的行自动核销，单行全关则整单关闭。"""
        line = self._line_row(conn, line_id)
        if line["state"] != OrderState.CLOSED.value:
            outstanding, _ = self._claim_sums(conn, line_id)
            in_transit = self._sum_movements(
                conn, line_id=line_id, account=Account.IN_TRANSIT.value)
            if outstanding == _ZERO and in_transit == _ZERO:
                conn.execute(
                    "UPDATE return_line SET state = ?, close_reason = ? WHERE id = ?",
                    (OrderState.CLOSED.value, "已核销", line_id))
        open_lines = conn.execute(
            "SELECT COUNT(*) AS c FROM return_line WHERE order_id = ? AND state <> ?",
            (line["order_id"], OrderState.CLOSED.value)).fetchone()["c"]
        if open_lines == 0:
            conn.execute("UPDATE return_order SET state = ? WHERE id = ?",
                         (OrderState.CLOSED.value, line["order_id"]))

    def _mark_fulfilling(self, conn, line) -> None:
        if line["state"] == OrderState.RELEASED.value:
            conn.execute("UPDATE return_line SET state = ? WHERE id = ?",
                         (OrderState.FULFILLING.value, line["id"]))
            conn.execute(
                "UPDATE return_order SET state = ? WHERE id = ? AND state = ?",
                (OrderState.FULFILLING.value, line["order_id"], OrderState.RELEASED.value))

    @staticmethod
    def _require_state(current: str, expected: OrderState, action: str) -> None:
        if current != expected.value:
            raise DomainError(f"当前状态 {current} 不能{action}，需要状态 {expected.value}")

    @staticmethod
    def _require_line_active(line, action: str) -> None:
        if line["state"] not in _ACTIVE_LINE_STATES:
            raise DomainError(f"退货行状态 {line['state']} 不允许{action}")

    def _line_summary(self, conn, line) -> dict:
        outstanding, net = self._claim_sums(conn, line["id"])
        return {
            "line_id": line["id"],
            "batch_id": line["batch_id"],
            "batch_no": line["batch_no"],
            "material": line["material"],
            "supplier": line["supplier"],
            "request_qty": Decimal(line["request_qty"]),
            "unit_price": Decimal(line["unit_price"]),
            "state": line["state"],
            "close_reason": line["close_reason"],
            "outstanding_claim_qty": outstanding,
            "net_receivable": net,
        }

    def _batch_row(self, conn, batch_id):
        row = conn.execute("SELECT * FROM defect_batch WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"缺陷批次不存在：{batch_id}")
        return row

    def _order_row(self, conn, order_id):
        row = conn.execute("SELECT * FROM return_order WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"退货单不存在：{order_id}")
        return row

    def _line_row(self, conn, line_id):
        row = conn.execute("SELECT * FROM return_line WHERE id = ?", (line_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"退货行不存在：{line_id}")
        return row

    @staticmethod
    def _sum_movements(conn, *, batch_id=None, line_id=None, account=None) -> Decimal:
        sql = "SELECT qty_delta FROM inventory_movement WHERE 1 = 1"
        params: list = []
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        if line_id is not None:
            sql += " AND line_id = ?"
            params.append(line_id)
        if account is not None:
            sql += " AND account = ?"
            params.append(account)
        rows = conn.execute(sql, params).fetchall()
        return sum((Decimal(r["qty_delta"]) for r in rows), _ZERO)

    @staticmethod
    def _claim_sums(conn, line_id) -> tuple[Decimal, Decimal]:
        rows = conn.execute(
            "SELECT qty, amount FROM claim_entry WHERE line_id = ?", (line_id,)).fetchall()
        qty = sum((Decimal(r["qty"]) for r in rows), _ZERO)
        amount = sum((Decimal(r["amount"]) for r in rows), _ZERO)
        return qty, amount

    @staticmethod
    def _evidence_sums(conn, line_id) -> dict[str, Decimal]:
        rows = conn.execute(
            "SELECT kind, qty FROM logistics_evidence WHERE line_id = ?", (line_id,)).fetchall()
        result = {"发货": _ZERO, "签收": _ZERO}
        for row in rows:
            result[row["kind"]] += Decimal(row["qty"])
        return result
