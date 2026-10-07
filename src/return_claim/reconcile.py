"""对账：实物数量、责任数量、金额三视图，以及每次调整的依据链。

四条检查与 domain/contract.json 的 invariants 一一对应：
- 退货库存联动：离开隔离区的数量 = 在途 + 已签收 + 退回隔离区（实物守恒）；
- 责任数量认定：未决索赔 + 换货抵扣 = 最近一次责任依据（责任认定/部分接受/原始批准）；
- 索赔分录补偿：所有冲减分录都必须挂在补偿事件/责任认定上，原始分录不被修改；
- 实物金额对账：净应收 = 未决索赔数量 × 单价，且未决索赔数量与实物口径一致。
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from .errors import NotFoundError
from .models import Account, money

_ZERO = Decimal("0")

_LINE_SQL = """
SELECT l.*, o.order_no, o.state AS order_state,
       b.batch_no, b.material, b.supplier, b.defect_qty
FROM return_line l
JOIN return_order o ON o.id = l.order_id
JOIN defect_batch b ON b.id = l.batch_id
WHERE l.id = ?
"""


def _sum(rows: list[sqlite3.Row], key: str) -> Decimal:
    return sum((Decimal(r[key]) for r in rows), _ZERO)


def _positive(rows: list[sqlite3.Row], key: str) -> Decimal:
    return sum((Decimal(r[key]) for r in rows if Decimal(r[key]) > 0), _ZERO)


def _negative(rows: list[sqlite3.Row], key: str) -> Decimal:
    return sum((Decimal(r[key]) for r in rows if Decimal(r[key]) < 0), _ZERO)


def _movements_by_account(moves: list[sqlite3.Row], account: str) -> list[sqlite3.Row]:
    return [m for m in moves if m["account"] == account]


def _adjustment_trail(entries: list[sqlite3.Row], moves: list[sqlite3.Row]) -> list[dict]:
    """合并索赔分录与库存流水，按时间排序，逐条给出调整依据。"""
    trail: list[dict] = []
    for entry in entries:
        trail.append({
            "_sort": (entry["created_at"], 1, entry["id"]),
            "at": entry["created_at"],
            "actor": entry["actor"],
            "category": "索赔分录",
            "kind": entry["kind"],
            "summary": f"{entry['kind']}：数量 {entry['qty']}，金额 {entry['amount']}",
            "basis": {"type": entry["basis_type"], "ref": entry["basis_id"], "note": entry["note"]},
        })
    for move in moves:
        trail.append({
            "_sort": (move["created_at"], 0, move["id"]),
            "at": move["created_at"],
            "actor": move["actor"],
            "category": "库存移动",
            "kind": move["account"],
            "summary": f"{move['account']} {move['qty_delta']}（{move['reason']}）",
            "basis": {"type": move["basis_type"], "ref": move["basis_id"], "note": move["reason"]},
        })
    trail.sort(key=lambda item: item["_sort"])
    for seq, item in enumerate(trail, start=1):
        del item["_sort"]
        item["seq"] = seq
    return trail


def line_reconciliation(conn: sqlite3.Connection, line_id: int) -> dict:
    """退货行对账：并列实物、责任、金额三个口径，并附调整依据链。"""
    line = conn.execute(_LINE_SQL, (line_id,)).fetchone()
    if line is None:
        raise NotFoundError(f"退货行不存在：{line_id}")
    entries = conn.execute(
        "SELECT * FROM claim_entry WHERE line_id = ? ORDER BY id", (line_id,)).fetchall()
    moves = conn.execute(
        "SELECT * FROM inventory_movement WHERE line_id = ? ORDER BY id", (line_id,)).fetchall()
    evidence = conn.execute(
        "SELECT * FROM logistics_evidence WHERE line_id = ? ORDER BY id", (line_id,)).fetchall()
    determinations = conn.execute(
        "SELECT * FROM liability_determination WHERE line_id = ? ORDER BY id", (line_id,)).fetchall()
    events = conn.execute(
        "SELECT * FROM compensation_event WHERE line_id = ? ORDER BY id", (line_id,)).fetchall()

    unit_price = Decimal(line["unit_price"])
    request_qty = Decimal(line["request_qty"])

    # 实物口径
    shipped = _sum([e for e in evidence if e["kind"] == "发货"], "qty")
    received = _sum([e for e in evidence if e["kind"] == "签收"], "qty")
    quarantine_moves = _movements_by_account(moves, Account.QUARANTINE.value)
    released = -_negative(quarantine_moves, "qty_delta")
    returned_to_quarantine = _positive(quarantine_moves, "qty_delta")
    in_transit = _sum(_movements_by_account(moves, Account.IN_TRANSIT.value), "qty_delta")
    replacement = _positive(_movements_by_account(moves, Account.GOOD_STOCK.value), "qty_delta")

    # 责任口径
    latest_determined = Decimal(determinations[-1]["determined_qty"]) if determinations else None

    # 金额口径
    outstanding_qty = _sum(entries, "qty")
    gross_claimed = _positive(entries, "amount")
    total_reversed = _negative(entries, "amount")
    net_receivable = _sum(entries, "amount")
    expected_receivable = money(outstanding_qty * unit_price)

    conservation = released == in_transit + received + returned_to_quarantine
    amount_consistent = net_receivable == expected_receivable
    physical_expected = received - replacement
    physical_match = outstanding_qty == physical_expected

    # 责任依据：最近一次责任认定或供应商部分接受；都没有则退回原始批准数量。
    basis_qty, basis_source = request_qty, "原始批准"
    timeline = [
        (d["created_at"], 0, d["id"], "责任认定", Decimal(d["determined_qty"]))
        for d in determinations
    ]
    for event in events:
        if event["type"] == "部分接受":
            accepted = Decimal(str(json.loads(event["payload"])["accepted_qty"]))
            timeline.append((event["created_at"], 1, event["id"], "部分接受", accepted))
    if timeline:
        timeline.sort()
        _, _, _, basis_source, basis_qty = timeline[-1]
    if outstanding_qty == _ZERO:
        determination_ok = True
        determination_detail = "无未决索赔，无需责任认定"
    else:
        determination_ok = outstanding_qty + replacement == basis_qty
        determination_detail = (
            f"责任依据[{basis_source}] {basis_qty} = "
            f"未决索赔 {outstanding_qty} + 换货抵扣 {replacement}")
    compensation_ok = all(
        e["basis_type"] in ("补偿事件", "责任认定") for e in entries if Decimal(e["qty"]) < 0)
    compensation_detail = f"冲减分录 {sum(1 for e in entries if Decimal(e['qty']) < 0)} 笔，全部挂接补偿依据"

    # 实物金额对账：金额自洽，且未决索赔与实物口径一致，或差异已被明确责任依据解释。
    basis_explains_gap = basis_source != "原始批准" and determination_ok
    if outstanding_qty == _ZERO:
        physical_amount_ok = amount_consistent
        physical_detail = "未决索赔已清零"
    elif physical_match:
        physical_amount_ok = amount_consistent
        physical_detail = (
            f"未决索赔 {outstanding_qty} 与实物口径 {physical_expected}"
            f"（签收 {received} - 换货 {replacement}）一致")
    elif basis_explains_gap:
        physical_amount_ok = amount_consistent
        physical_detail = (
            f"未决索赔 {outstanding_qty} 与实物口径 {physical_expected} 的差异"
            f"已由[{basis_source}] {basis_qty} 认定")
    else:
        physical_amount_ok = False
        physical_detail = (
            f"未决索赔 {outstanding_qty} 与实物口径 {physical_expected}"
            f"（签收 {received} - 换货 {replacement}）不符，且无责任依据")

    checks = [
        {"name": "退货库存联动", "ok": conservation,
         "detail": f"隔离区出库 {released} = 在途 {in_transit} + 已签收 {received} + 退回隔离区 {returned_to_quarantine}"},
        {"name": "责任数量认定", "ok": determination_ok, "detail": determination_detail},
        {"name": "索赔分录补偿", "ok": compensation_ok, "detail": compensation_detail},
        {"name": "实物金额对账", "ok": physical_amount_ok,
         "detail": (f"净应收 {net_receivable} = 未决数量 {outstanding_qty} × 单价 {unit_price}；"
                    + physical_detail)},
    ]

    return {
        "line_id": line["id"],
        "order_id": line["order_id"],
        "order_no": line["order_no"],
        "order_state": line["order_state"],
        "line_state": line["state"],
        "close_reason": line["close_reason"],
        "batch_no": line["batch_no"],
        "material": line["material"],
        "supplier": line["supplier"],
        "unit_price": unit_price,
        "physical": {
            "defect_qty": Decimal(line["defect_qty"]),
            "approved_qty": request_qty,
            "shipped_qty": shipped,
            "received_qty": received,
            "in_transit_qty": in_transit,
            "returned_to_quarantine_qty": returned_to_quarantine,
            "replacement_received_qty": replacement,
        },
        "liability": {
            "latest_determined_qty": latest_determined,
            "outstanding_claim_qty": outstanding_qty,
            "determinations": [
                {"id": d["id"], "determined_qty": Decimal(d["determined_qty"]),
                 "reason": d["reason"], "actor": d["actor"], "at": d["created_at"]}
                for d in determinations
            ],
        },
        "amount": {
            "gross_claimed": gross_claimed,
            "total_reversed": total_reversed,
            "net_receivable": net_receivable,
            "expected_receivable": expected_receivable,
        },
        "checks": checks,
        "adjustments": _adjustment_trail(entries, moves),
    }


def batch_reconciliation(conn: sqlite3.Connection, batch_id: int) -> dict:
    """缺陷批次对账：批次级隔离库存余额 + 各退货行汇总。"""
    batch = conn.execute("SELECT * FROM defect_batch WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        raise NotFoundError(f"缺陷批次不存在：{batch_id}")
    moves = conn.execute(
        "SELECT * FROM inventory_movement WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
    line_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM return_line WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()]
    lines = [line_reconciliation(conn, line_id) for line_id in line_ids]

    quarantine_balance = _sum(
        _movements_by_account(moves, Account.QUARANTINE.value), "qty_delta")
    good_stock = _sum(_movements_by_account(moves, Account.GOOD_STOCK.value), "qty_delta")
    returned = _sum(_movements_by_account(moves, Account.RETURNED.value), "qty_delta")

    check_names = ["退货库存联动", "责任数量认定", "索赔分录补偿", "实物金额对账"]
    checks = []
    for name in check_names:
        line_checks = [c for line in lines for c in line["checks"] if c["name"] == name]
        checks.append({
            "name": name,
            "ok": all(c["ok"] for c in line_checks),
            "detail": "；".join(
                f"行{c_line['line_id']}：{c['detail']}"
                for c_line in lines for c in c_line["checks"] if c["name"] == name) or "无退货行",
        })

    return {
        "batch_id": batch["id"],
        "batch_no": batch["batch_no"],
        "material": batch["material"],
        "supplier": batch["supplier"],
        "defect_qty": Decimal(batch["defect_qty"]),
        "unit_price": Decimal(batch["unit_price"]),
        "quarantine_balance": quarantine_balance,
        "good_stock_qty": good_stock,
        "returned_to_supplier_qty": returned,
        "totals": {
            "approved_qty": sum((l["physical"]["approved_qty"] for l in lines), _ZERO),
            "received_qty": sum((l["physical"]["received_qty"] for l in lines), _ZERO),
            "outstanding_claim_qty": sum((l["liability"]["outstanding_claim_qty"] for l in lines), _ZERO),
            "net_receivable": sum((l["amount"]["net_receivable"] for l in lines), _ZERO),
        },
        "checks": checks,
        "lines": lines,
    }
