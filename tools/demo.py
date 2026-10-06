"""端到端场景演示（不经过 HTTP，直接驱动领域服务）。

运行：python3 tools/demo.py
覆盖：缺陷批次 → 退货行 → 责任认定 → 批准原子联动 → 部分退回
     → 供应商部分接受 → 换货抵扣 → 争议复核 → 撤销/关闭，
最后打印三方对账报告与逐事件调整依据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from return_claim.commands import CommandService
from return_claim.events import EventStore
from return_claim.queries import QueryService


def main() -> None:
    cid = "0910033-019-A"
    store = EventStore()
    svc = CommandService(store)
    q = QueryService(store)

    svc.open_case(cid, supplier="华东轴承有限公司", actor="采购计划员",
                  reason="IQC 20261003 批次判退，启动退货索赔",
                  title="6204-轴承来料硬度不合格")
    svc.register_defect_batch(
        cid, "B20261003-01", "6204-轴承", 100, "质量工程师",
        "IQC 抽检硬度 HRC52，低于规格下限 HRC58", defect_desc="热处理不足")
    svc.add_return_line(
        cid, 1, "B20261003-01", 100, 1000, "采购计划员",
        "按缺陷批次 100 件全额申请退货，单价 10.00 元")
    svc.determine_liability(
        cid, [{"line_no": 1, "liable_qty": 100,
               "responsible": "供应商", "basis": "8D 报告：热处理炉温偏差"}],
        "质量工程师", "质量依据 8D-20261003 认定供方全责")
    svc.attach_logistics_evidence(
        cid, "EV-CK-01", "出库单", "CK20261006", 100, "仓储管理员",
        "退货发运出库，顺丰运单 SF10001", carrier="顺丰速运")
    svc.approve_return(
        cid, [{"line_no": 1, "approved_qty": 100}], "采购计划员",
        "责任与证据齐备，批准退货：隔离库存出库并计提应收 1000.00 元")

    # 痛点场景：只签退 70，30 件短少回厂
    svc.attach_logistics_evidence(
        cid, "EV-QS-01", "签收回单", "SF10001", 70, "仓储管理员",
        "顺丰回单显示供方实收 70 件，30 件运输途中拒收退回",
        carrier="顺丰速运")
    svc.record_goods_received(
        cid, [{"line_no": 1, "received_qty": 70}],
        evidence_id="EV-QS-01", actor="仓储管理员",
        reason="签收回单核实物：签退 70、短少 30 回厂，索赔按 70 收敛")

    # 供应商异议：只接受 60
    svc.partial_accept(
        cid, [{"line_no": 1, "accepted_qty": 60, "return_qty": 0,
               "note": "10 件系运输碰伤非来料缺陷"}],
        "供应商", "供方异议：10 件运输致损，只认 60 件责任")

    # 其中 20 件换货补发
    svc.exchange_offset(
        cid, [{"line_no": 1, "exchange_qty": 20}], "采购计划员",
        "协商 20 件由供方补发合格品，冲减索赔 200.00 元")

    print(json.dumps(q.reconciliation(cid), ensure_ascii=False, indent=2))
    print("\n===== 逐事件调整依据 =====")
    for e in q.audit_trail(cid)["events"]:
        print(f"#{e['seq']} {e['type']}｜{e['actor']}｜依据：{e['reason']}")
        for m in e["stock_effects"]:
            print(f"    库存 行{m['line_no']} {m['zone']} {m['qty_delta']:+d}")
        for x in e["claim_effects"]:
            print(f"    索赔 行{x['line_no']} {x['amount_delta_yuan']} 元")


if __name__ == "__main__":
    main()
