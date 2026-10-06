"""命令服务：所有写操作的唯一入口。

每个命令都在 case 级锁内原子完成：

    with store.lock_for(case_id):
        state = aggregate.load(case_id)   # 回放当前状态
        validate(state, command)          # 全部业务校验
        event = store.append(...)         # 只追加一条事件
        aggregate.apply(state, event)     # 同步投影，返回最新状态

任一步校验失败都会抛 ``DomainError``，事件存储里不会留下半成品；
部分接受、换货抵扣、争议复核、撤销全部体现为补偿事件（负库存/负金额），
历史事件永不修改。
"""
from __future__ import annotations

from typing import Any

from .aggregate import (
    ZONE_IN_TRANSIT,
    ZONE_QUARANTINE,
    ZONE_SUPPLIER,
    CaseAggregate,
    CaseState,
)
from .errors import (
    EvidenceMissing,
    IllegalState,
    LiabilityMissing,
    LineNotFound,
    QuantityConflict,
    RevokedConflict,
)
from .events import EventStore
from .models import ClaimStatus, Event, EventType

# 与 domain/contract.json 的 actors 对齐
ACTORS = ("采购计划员", "供应商", "质量工程师", "仓储管理员")

_REVIEW_OUTCOMES = ("upheld", "adjusted", "rejected")


class CommandService:
    """退货索赔协同命令服务。"""

    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.aggregate = CaseAggregate(store)

    # ---- 通用机制 --------------------------------------------------------

    def _execute(self, case_id: str, event_type: EventType, actor: str,
                 reason: str, payload: dict[str, Any] | Any,
                 validate: Any) -> tuple[CaseState, Event]:
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("reason 不能为空：每次调整必须记录依据")
        with self.store.lock_for(case_id):
            state = self.aggregate.load(case_id)
            validate(state)
            resolved_payload = payload(state) if callable(payload) else payload
            # 先暂存事件并投影到内存，全部不变量通过后才提交落盘；
            # 任一环节失败，事件存储与持久化文件都不会被污染。
            event = self.store.stage(case_id, event_type, actor, reason,
                                     resolved_payload)
            self.aggregate.apply(state, event)
            self._guard_invariants(state)
            self.store.commit(event)
            return state, event

    def _guard_invariants(self, state: CaseState) -> None:
        """提交前不变量守卫。

        正常路径下命令校验已保证这些条件，守卫是防止编程错误破坏对账口径
        的最后一道防线；一旦触发说明命令校验有漏洞（属于代码缺陷），
        事件不会被提交。
        """
        zones: dict[tuple[int, str], int] = {}
        line_zones: dict[int, int] = {}
        for m in state.stock_movements:
            key = (m.line_no, m.zone)
            zones[key] = zones.get(key, 0) + m.qty_delta
        for (line_no, zone), balance in zones.items():
            if balance < 0:
                raise AssertionError(
                    f"不变量被破坏：行 {line_no} 位置 {zone} 结存为负 {balance}"
                )
            line_zones[line_no] = line_zones.get(line_no, 0) + balance
        for line in state.lines.values():
            # 实物守恒：四个位置之和恒等于退货申请量（事件只转移、不消灭实物）
            if line_zones.get(line.line_no, 0) != line.request_qty:
                raise AssertionError(
                    f"不变量被破坏：行 {line.line_no} 实物不守恒 "
                    f"{line_zones.get(line.line_no, 0)} != {line.request_qty}"
                )
            booked = state.booked_claim_qty(line.line_no)
            target = state.target_claim_qty(line)
            # 索赔分录必须已收敛到目标口径
            if booked != target:
                raise AssertionError(
                    f"不变量被破坏：行 {line.line_no} 索赔未收敛 "
                    f"{booked} != {target}"
                )
            claimed = state.receivable_amount(line.line_no)
            if claimed < 0:
                raise AssertionError(f"不变量被破坏：行 {line.line_no} 应收为负")
            if claimed != booked * line.unit_price:
                raise AssertionError(
                    f"不变量被破坏：行 {line.line_no} 金额与索赔数量不符"
                )
            if line.approved_qty > 0 and line.received_qty > line.approved_qty:
                raise AssertionError(
                    f"不变量被破坏：行 {line.line_no} 实物超过批准量"
                )
            if line.approved_qty == 0 and (line.received_qty or line.exchange_qty):
                raise AssertionError(
                    f"不变量被破坏：行 {line.line_no} 未批准却出现履行数据"
                )

    @staticmethod
    def _line(state: CaseState, line_no: int):
        try:
            return state.lines[line_no]
        except KeyError:
            raise LineNotFound(f"退货行不存在：{line_no}") from None

    @staticmethod
    def _alive(line) -> None:
        if line.revoked:
            raise RevokedConflict(f"退货行 {line.line_no} 已撤销，不能再调整")

    @staticmethod
    def _not_closed(state: CaseState) -> None:
        if state.status == ClaimStatus.CLOSED:
            raise IllegalState("退货索赔单已关闭，不允许任何调整")

    @staticmethod
    def _qty(value: Any, name: str, allow_zero: bool = False) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} 必须是非负整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise QuantityConflict(f"{name} 必须 {'非负' if allow_zero else '大于 0'}")
        return value

    # ---- 草拟阶段 --------------------------------------------------------

    def open_case(self, case_id: str, supplier: str, actor: str,
                  reason: str, title: str = "") -> tuple[CaseState, Event]:
        case_id = case_id.strip()
        if not case_id:
            raise ValueError("case_id 不能为空")
        if not supplier.strip():
            raise ValueError("supplier 不能为空")
        if self.store.events_for(case_id):
            raise IllegalState(f"退货索赔单已存在：{case_id}")

        def validate(_state: CaseState) -> None:
            pass  # 新单无历史可校验

        # 新单没有历史，_execute 的 load 会失败：单独原子处理
        with self.store.lock_for(case_id):
            event = self.store.append(
                case_id, EventType.CASE_OPENED, actor, reason,
                {"supplier": supplier.strip(), "title": title.strip()},
            )
            state = self.aggregate.load(case_id)
            return state, event

    def register_defect_batch(self, case_id: str, batch_no: str, material: str,
                              defect_qty: int, actor: str, reason: str,
                              defect_desc: str = "") -> tuple[CaseState, Event]:
        self._qty(defect_qty, "缺陷数量")

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            if batch_no in s.defect_batches:
                raise IllegalState(f"缺陷批次已登记：{batch_no}")

        return self._execute(case_id, EventType.DEFECT_BATCH_REGISTERED, actor,
                             reason,
                             {"batch_no": batch_no, "material": material,
                              "defect_qty": defect_qty, "defect_desc": defect_desc},
                             validate)

    def add_return_line(self, case_id: str, line_no: int, batch_no: str,
                        request_qty: int, unit_price: int, actor: str,
                        reason: str) -> tuple[CaseState, Event]:
        self._qty(request_qty, "退货申请数量")
        self._qty(unit_price, "单价", allow_zero=True)

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            if line_no in s.lines:
                raise IllegalState(f"退货行号已存在：{line_no}")
            batch = s.defect_batches.get(batch_no)
            if batch is None:
                raise IllegalState(f"缺陷批次未登记：{batch_no}")
            if request_qty > batch.defect_qty:
                raise QuantityConflict(
                    f"退货申请 {request_qty} 超过缺陷批次数量 {batch.defect_qty}"
                )

        def payload(s: CaseState) -> dict[str, Any]:
            return {"line_no": line_no, "batch_no": batch_no,
                    "material": s.defect_batches[batch_no].material,
                    "request_qty": request_qty, "unit_price": unit_price}

        return self._execute(case_id, EventType.RETURN_LINE_ADDED, actor, reason,
                             payload, validate)

    def attach_logistics_evidence(self, case_id: str, evidence_id: str,
                                  doc_type: str, doc_no: str,
                                  confirmed_return_qty: int, actor: str,
                                  reason: str, carrier: str = "",
                                  note: str = "") -> tuple[CaseState, Event]:
        self._qty(confirmed_return_qty, "签退确认数量", allow_zero=True)

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            if any(e.evidence_id == evidence_id for e in s.evidences):
                raise IllegalState(f"物流证据已存在：{evidence_id}")

        return self._execute(case_id, EventType.LOGISTICS_EVIDENCE_ATTACHED,
                             actor, reason,
                             {"evidence_id": evidence_id, "doc_type": doc_type,
                              "doc_no": doc_no, "carrier": carrier,
                              "confirmed_return_qty": confirmed_return_qty,
                              "note": note}, validate)

    # ---- 责任认定 / 批准 -------------------------------------------------

    def determine_liability(self, case_id: str, determinations: list[dict[str, Any]],
                            actor: str, reason: str) -> tuple[CaseState, Event]:
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for d in determinations:
            line_no = d["line_no"]
            if line_no in seen:
                raise IllegalState(f"同一事件中行 {line_no} 重复认定")
            seen.add(line_no)
            qty = self._qty(d["liable_qty"], "责任数量")
            share = d.get("share_pct", 100)
            if not isinstance(share, int) or not 0 <= share <= 100:
                raise ValueError("责任比例 share_pct 必须是 0~100 的整数")
            clean.append({"line_no": line_no, "liable_qty": qty,
                          "responsible": d.get("responsible", "供应商"),
                          "share_pct": share, "basis": d.get("basis", "")})

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            for item in clean:
                line = self._line(s, item["line_no"])
                self._alive(line)
                if line.line_no in s._has_liability:
                    if line.approved_qty:
                        raise IllegalState(
                            f"行 {line.line_no} 已批准，责任调整请走争议复核"
                        )
                    # 批准前的修正窗口：允许重定责任，投影以最新结论为准
                if item["liable_qty"] > line.request_qty:
                    raise QuantityConflict(
                        f"责任数量 {item['liable_qty']} 超过退货申请 "
                        f"{line.request_qty}（行 {line.line_no}）"
                    )

        return self._execute(case_id, EventType.LIABILITY_DETERMINED, actor,
                             reason, {"determinations": clean}, validate)

    def approve_return(self, case_id: str, approvals: list[dict[str, Any]],
                       actor: str, reason: str) -> tuple[CaseState, Event]:
        """批准退货：原子联动隔离库存出库与应收金额计提。

        前提：每行都有责任认定；批准数量 <= 申请数量。一个事件同时产生
        隔离区出库流水、在途入库流水和按批准量的索赔计提分录。
        """
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for a in approvals:
            line_no = a["line_no"]
            if line_no in seen:
                raise IllegalState(f"同一事件中行 {line_no} 重复批准")
            seen.add(line_no)
            clean.append({"line_no": line_no,
                          "approved_qty": self._qty(a["approved_qty"], "批准数量")})

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            # 多批次单据按行独立批准：已有行进入履行时，后续行仍可批准
            if s.status not in (ClaimStatus.DRAFT, ClaimStatus.PENDING,
                                ClaimStatus.RELEASED, ClaimStatus.IN_PROGRESS):
                raise IllegalState("当前状态不允许批准退货")
            for item in clean:
                line = self._line(s, item["line_no"])
                self._alive(line)
                if line.approved_qty:
                    raise IllegalState(f"行 {line.line_no} 已批准，不能重复批准")
                if line.line_no not in s._has_liability:
                    raise LiabilityMissing(
                        f"行 {line.line_no} 缺少责任认定，不能批准退货"
                    )
                qty = item["approved_qty"]
                if qty > line.request_qty:
                    raise QuantityConflict(
                        f"批准数量 {qty} 超过申请数量 {line.request_qty}"
                        f"（行 {line.line_no}）"
                    )
                if s.zone_qty(line.line_no, ZONE_QUARANTINE) < qty:
                    raise QuantityConflict(
                        f"行 {line.line_no} 隔离库存不足，无法出库 {qty}"
                    )

        return self._execute(case_id, EventType.RETURN_APPROVED, actor, reason,
                             {"approvals": clean}, validate)

    # ---- 实物签退 --------------------------------------------------------

    def record_goods_received(self, case_id: str, receipts: list[dict[str, Any]],
                              evidence_id: str, actor: str,
                              reason: str) -> tuple[CaseState, Event]:
        """物流签退结算：在途清账，供应商签退 vs 短少回厂，索赔同步收敛。"""
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for r in receipts:
            line_no = r["line_no"]
            if line_no in seen:
                raise IllegalState(f"同一事件中行 {line_no} 重复签退")
            seen.add(line_no)
            clean.append({"line_no": line_no,
                          "received_qty": self._qty(r["received_qty"], "签退数量",
                                                    allow_zero=True)})

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            if not any(e.evidence_id == evidence_id for e in s.evidences):
                raise EvidenceMissing(f"物流证据不存在：{evidence_id}")
            for item in clean:
                line = self._line(s, item["line_no"])
                self._alive(line)
                if not line.approved_qty:
                    raise IllegalState(f"行 {line.line_no} 尚未批准，不能签退")
                if line.line_no in s._settled:
                    raise IllegalState(f"行 {line.line_no} 已完成签退结算")
                if s.zone_qty(line.line_no, ZONE_IN_TRANSIT) <= 0:
                    raise QuantityConflict(f"行 {line.line_no} 在途无货可结算")
                if item["received_qty"] > line.approved_qty:
                    raise QuantityConflict(
                        f"签退数量 {item['received_qty']} 超过批准数量 "
                        f"{line.approved_qty}（行 {line.line_no}）"
                    )

        payload = {"evidence_id": evidence_id, "receipts": clean}
        return self._execute(case_id, EventType.GOODS_RECEIVED, actor, reason,
                             payload, validate)

    # ---- 补偿事件 --------------------------------------------------------

    def partial_accept(self, case_id: str, accepted: list[dict[str, Any]],
                       actor: str, reason: str) -> tuple[CaseState, Event]:
        """供应商部分接受：重定责任数量，拒收实物可退回厂内，索赔差额冲销。"""
        clean = self._clean_liability_adjust(accepted, "accepted_qty")

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            self._require_settled(s, clean)
            for item in clean:
                line = self._line(s, item["line_no"])
                if item["accepted_qty"] > line.received_qty:
                    raise QuantityConflict(
                        f"接受数量 {item['accepted_qty']} 超过实物签退 "
                        f"{line.received_qty}（行 {line.line_no}）"
                    )
                self._check_return(s, line, item.get("return_qty", 0))

        return self._execute(case_id, EventType.PARTIAL_ACCEPTED, actor, reason,
                             {"accepted": clean}, validate)

    def exchange_offset(self, case_id: str, exchanges: list[dict[str, Any]],
                        actor: str, reason: str) -> tuple[CaseState, Event]:
        """换货抵扣：供方补发合格品，按换货量冲减索赔，库存转入换货补入。"""
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for x in exchanges:
            line_no = x["line_no"]
            if line_no in seen:
                raise IllegalState(f"同一事件中行 {line_no} 重复换货")
            seen.add(line_no)
            qty = self._qty(x["exchange_qty"], "换货数量")
            clean.append({"line_no": line_no, "exchange_qty": qty,
                          "from_zone": x.get("from_zone", ZONE_SUPPLIER)})

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            for item in clean:
                line = self._line(s, item["line_no"])
                self._alive(line)
                if line.line_no not in s._settled:
                    raise IllegalState(f"行 {line.line_no} 未签退结算，不能换货")
                zone = item["from_zone"]
                if zone not in (ZONE_SUPPLIER, ZONE_QUARANTINE):
                    raise ValueError(f"换货来源位置非法：{zone}")
                if s.zone_qty(line.line_no, zone) < item["exchange_qty"]:
                    raise QuantityConflict(
                        f"行 {line.line_no} {zone}结存不足换货 {item['exchange_qty']}"
                    )
                available = max(
                    0, min(line.received_qty, line.liable_qty) - line.exchange_qty
                )
                if item["exchange_qty"] > available:
                    raise QuantityConflict(
                        f"换货 {item['exchange_qty']} 超过可抵扣索赔数量 "
                        f"{available}（行 {line.line_no}）"
                    )

        return self._execute(case_id, EventType.EXCHANGE_OFFSET, actor, reason,
                             {"exchanges": clean}, validate)

    def dispute_review(self, case_id: str, reviews: list[dict[str, Any]],
                       actor: str, reason: str) -> tuple[CaseState, Event]:
        """质量工程师对供应商异议做争议复核：维持/调整/推翻责任认定。"""
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for r in reviews:
            line_no = r["line_no"]
            if line_no in seen:
                raise IllegalState(f"同一事件中行 {line_no} 重复复核")
            seen.add(line_no)
            outcome = r["outcome"]
            if outcome not in _REVIEW_OUTCOMES:
                raise ValueError(
                    f"复核结论必须是 {_REVIEW_OUTCOMES} 之一"
                )
            raw_qty = r.get("liable_qty")
            if outcome == "upheld" and raw_qty is None:
                qty: int | None = None  # 维持：在校验阶段取当前责任数量
            else:
                qty = self._qty(raw_qty or 0, "复核责任数量", allow_zero=True)
                if outcome in ("upheld", "adjusted") and qty == 0:
                    raise QuantityConflict(f"{outcome} 结论的责任数量必须大于 0")
            clean.append({"line_no": line_no, "outcome": outcome,
                          "liable_qty": qty,
                          "return_qty": self._qty(r.get("return_qty", 0),
                                                  "退回实物数量", allow_zero=True),
                          "responsible": r.get("responsible", "供应商"),
                          "share_pct": r.get("share_pct", 100),
                          "note": r.get("note", "")})

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            self._require_settled(s, clean)
            for item in clean:
                line = self._line(s, item["line_no"])
                if item["liable_qty"] is None:
                    item["liable_qty"] = line.liable_qty
                if item["liable_qty"] > line.received_qty:
                    raise QuantityConflict(
                        f"复核责任数量 {item['liable_qty']} 超过实物签退 "
                        f"{line.received_qty}（行 {line.line_no}）"
                    )
                self._check_return(s, line, item["return_qty"])

        return self._execute(case_id, EventType.DISPUTE_REVIEWED, actor, reason,
                             {"reviews": clean}, validate)

    def revoke(self, case_id: str, line_nos: list[int], actor: str,
               reason: str) -> tuple[CaseState, Event]:
        """撤销批准的退货：在途/供应商持有货物回厂，应收索赔全额冲销。"""
        if not line_nos:
            raise ValueError("至少指定一行撤销")

        def validate(s: CaseState) -> None:
            self._not_closed(s)
            for line_no in line_nos:
                line = self._line(s, line_no)
                if line.revoked:
                    raise RevokedConflict(f"行 {line_no} 已撤销")
                if not line.approved_qty:
                    raise IllegalState(f"行 {line_no} 未批准，不能撤销")

        return self._execute(case_id, EventType.REVOKED, actor, reason,
                             {"lines": [{"line_no": n} for n in line_nos]},
                             validate)

    def close(self, case_id: str, actor: str, reason: str) -> tuple[CaseState, Event]:
        def validate(s: CaseState) -> None:
            if s.status not in (ClaimStatus.RELEASED, ClaimStatus.IN_PROGRESS):
                raise IllegalState("只有已下达/履行中的退货索赔单可以关闭")
            for line in s.lines.values():
                if not line.approved_qty:
                    raise IllegalState(
                        f"行 {line.line_no} 尚未批准，不能关闭（请先批准或删除）"
                    )
                if not line.revoked and line.line_no not in s._settled:
                    raise IllegalState(f"行 {line.line_no} 未完成签退结算")
                if s.zone_qty(line.line_no, ZONE_IN_TRANSIT) > 0:
                    raise IllegalState(f"行 {line.line_no} 仍有在途货物")

        return self._execute(case_id, EventType.CLOSED, actor, reason, {},
                             validate)

    # ---- 校验辅助 --------------------------------------------------------

    def _clean_liability_adjust(self, items: list[dict[str, Any]],
                                qty_field: str) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in items:
            line_no = item["line_no"]
            if line_no in seen:
                raise IllegalState(f"同一事件中行 {line_no} 重复调整")
            seen.add(line_no)
            qty = self._qty(item[qty_field], qty_field)
            ret = self._qty(item.get("return_qty", 0), "退回实物数量",
                            allow_zero=True)
            clean.append({"line_no": line_no, qty_field: qty,
                          "return_qty": ret, "note": item.get("note", "")})
        return clean

    def _require_settled(self, s: CaseState, items: list[dict[str, Any]]) -> None:
        for item in items:
            line = self._line(s, item["line_no"])
            self._alive(line)
            if line.line_no not in s._settled:
                raise IllegalState(
                    f"行 {line.line_no} 尚未完成物流签退结算，不能调整责任"
                )

    def _check_return(self, s: CaseState, line, return_qty: int) -> None:
        if return_qty <= 0:
            return
        held = s.zone_qty(line.line_no, ZONE_SUPPLIER)
        if return_qty > held:
            raise QuantityConflict(
                f"行 {line.line_no} 供应商仅持有 {held}，不能退回 {return_qty}"
            )
