"""端到端演示：来料不合格 → 退货 → 部分接受 → 换货抵扣 → 争议复核 → 对账。

复现「库存已扣减但索赔仍按全量计算」的场景，展示补偿事件如何让
实物数量、责任数量、金额三个口径重新对齐。

运行：python3 tools/demo_flow.py
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract
from return_claim import ReturnClaimService, Store


def show(title: str, payload: dict) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def brief(recon: dict) -> dict:
    return {
        "实物": recon["physical"],
        "责任": {"未决索赔数量": recon["liability"]["outstanding_claim_qty"],
                 "最新认定": recon["liability"]["latest_determined_qty"]},
        "金额": recon["amount"],
        "检查": {c["name"]: "✓" if c["ok"] else f"✗ {c['detail']}" for c in recon["checks"]},
    }


def main() -> None:
    contract = load_contract(ROOT / "domain" / "contract.json")
    svc = ReturnClaimService(Store(), contract)

    batch = svc.register_defect_batch(
        actor="质量工程师", batch_no="LOT-0910A", material="M-7701 精密轴",
        supplier="苏州精工", defect_qty=100, unit_price="12.50")
    show("① 质量工程师登记缺陷批次：100 件转入隔离区", batch)

    order = svc.create_return_order(
        actor="采购计划员", order_no="RO-2026-001",
        lines=[{"batch_id": batch["batch_id"], "qty": 100}])
    svc.submit_return_order(actor="采购计划员", order_id=order["order_id"])
    order = svc.approve_return_order(actor="采购计划员", order_id=order["order_id"])
    line_id = order["lines"][0]["line_id"]
    show("② 批准退货（原子事务）：隔离区 100→0，计提应收 100×12.50=1250.00",
         order["lines"][0])

    svc.record_evidence(actor="仓储管理员", line_id=line_id, kind="发货", qty=60,
                        carrier="顺丰", tracking_no="SF-20261007-01")
    svc.record_evidence(actor="供应商", line_id=line_id, kind="签收", qty=60)
    show("③ 实际只发出并签收 60 件：对账立刻标红——库存扣 100、索赔仍按 100 计",
         brief(svc.reconcile_line(line_id)))

    recon = svc.apply_compensation(
        actor="供应商", line_id=line_id, compensation_type="部分接受",
        payload={"accepted_qty": 60})
    show("④ 补偿事件-部分接受 60：冲减 40×12.50=500.00，40 件退回隔离区", brief(recon))

    recon = svc.apply_compensation(
        actor="供应商", line_id=line_id, compensation_type="换货抵扣",
        payload={"replacement_qty": 20})
    show("⑤ 补偿事件-换货抵扣 20：良品仓 +20，应收再减 250.00", brief(recon))

    recon = svc.apply_compensation(
        actor="质量工程师", line_id=line_id, compensation_type="争议复核",
        payload={"revised_qty": 30, "reason": "复检确认 30 件为供方责任，其余为运输损伤"})
    show("⑥ 补偿事件-争议复核认定 30：未决索赔调整为 30−20=10 件", brief(recon))

    show("⑦ 每次调整的依据链", {"调整依据": recon["adjustments"]})
    show("⑧ 批次级对账", svc.reconcile_batch(batch["batch_id"]))


if __name__ == "__main__":
    main()
