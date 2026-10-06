# 退货索赔协同

来料不合格后的退货索赔协同后端。质量、采购、供应商分别记录退货、补货（换货）和费用；
针对「部分退回或供应商异议时，库存已扣减但索赔仍按全量计算」的核心痛点，
以**事件溯源 + 补偿事件**保证库存联动、责任数量与应收金额始终可对账。

## 核心设计

所有状态变化都是只追加事件，冲销/修正一律反向事件，历史永不修改：

| 阶段 | 命令 → 事件 | 库存联动（同一事件、同一序号） | 索赔分录 |
|---|---|---|---|
| 责任认定 | `determine-liability` | — | 封顶责任数量 |
| 批准退货 | `approve` | 隔离库存出库 → 在途 | 按 `min(批准,责任)` 计提 |
| 签退结算 | `goods-received` | 在途清账：供方签退 / 短少回隔离区 | 按实物签退收敛（**部分退回不再全量索赔**） |
| 部分接受 | `partial-accept` | 拒收实物可回厂 | 按接受量负向冲销 |
| 换货抵扣 | `exchange` | 供应商持有 → 换货补入 | 按换货量负向冲销 |
| 争议复核 | `dispute-review` | 异议成立实物回厂 | 维持/调整/推翻，差额补偿 |
| 撤销 | `revoke` | 在途截停 / 供方货物回隔离区 | 全额冲销至零 |

**原子性**：每个命令在 case 级 `RLock` 内完成「回放 → 校验 → 暂存事件 → 投影 →
不变量守卫 → 提交落盘」，校验失败不留事件、不动库存；20 线程并发换货测试验证无超扣。

**防错口径**：可索赔数量恒等于 `max(0, min(批准量, 实物签退量, 责任量) − 换货量)`，
撤销行归零；金额 = 数量 × 单价（整数「分」存储，无浮点误差）。

## 目录

- `domain/contract.json`：领域角色（采购计划员/供应商/质量工程师/仓储管理员）、
  状态（草拟/待确认/已下达/履行中/已关闭）、四条不变式。
- `src/return_claim/`：
  - `models.py`：缺陷批次、退货行、物流证据、责任记录、索赔分录、库存流水；
  - `events.py`：线程安全只追加事件存储（内存 + JSONL）；
  - `aggregate.py`：事件投影与应收收敛规则；
  - `commands.py`：命令服务，所有写操作唯一入口，提交前做不变量守卫；
  - `queries.py`：读模型，实物/责任/金额三方对账与逐事件审计轨迹；
  - `api.py` / `__main__.py`：标准库 HTTP API（无第三方依赖）。
- `tools/check_contract.py`：契约命令行摘要检查。
- `tools/demo.py`：不启动 HTTP 的端到端场景演示。
- `tests/`：契约、领域（15 项）、HTTP API（3 项）回归测试。

## 快速开始

```bash
# 纯 Python 3.11+ 标准库，无需安装依赖
python3 tools/demo.py

# 启动 HTTP 服务（--store 指定 JSONL 事件日志，重启自动重放）
PYTHONPATH=src python3 -m return_claim --port 8080 --store data/events.jsonl
```

### API 示例

```bash
# 开立 → 缺陷批次 → 退货行 → 责任认定 → 批准（原子联动）
curl -X POST localhost:8080/cases/RC-1/open -H 'Content-Type: application/json' \
  -d '{"supplier":"华东轴承","actor":"采购计划员","reason":"IQC判退启动"}'
curl -X POST localhost:8080/cases/RC-1/defect-batches -H 'Content-Type: application/json' \
  -d '{"batch_no":"B1","material":"6204","defect_qty":100,"actor":"质量工程师","reason":"硬度不足"}'
curl -X POST localhost:8080/cases/RC-1/lines -H 'Content-Type: application/json' \
  -d '{"line_no":1,"batch_no":"B1","request_qty":100,"unit_price":1000,"actor":"采购计划员","reason":"全额退货"}'
curl -X POST localhost:8080/cases/RC-1/liability -H 'Content-Type: application/json' \
  -d '{"determinations":[{"line_no":1,"liable_qty":100}],"actor":"质量工程师","reason":"8D供方全责"}'
curl -X POST localhost:8080/cases/RC-1/approve -H 'Content-Type: application/json' \
  -d '{"approvals":[{"line_no":1,"approved_qty":100}],"actor":"采购计划员","reason":"证据齐备批准"}'

# 对账与调整依据
curl localhost:8080/cases/RC-1/reconciliation   # 实物/责任/金额 + balanced 标志
curl localhost:8080/cases/RC-1/audit-trail      # 每个事件触发的库存/金额变动
```

所有写请求必须携带 `actor` 与非空 `reason`（调整依据）；金额单位为整数「分」，
数量为非负整数。冲突返回 409（错误类型如 `LiabilityMissing`、`QuantityConflict`），
请求错误返回 400，未知单据返回 404。

## 验证

```bash
python3 -m unittest discover -s tests -v     # 18 项测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
```
