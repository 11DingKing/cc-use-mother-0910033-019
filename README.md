# 退货索赔协同

本项目维护退货索赔协同的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖采购计划员、供应商、质量工程师、仓储管理员，并明确退货库存联动、责任数量认定、索赔分录补偿、实物金额对账等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/return_claim/`：退货索赔后端（状态机、原子调整、补偿事件、对账）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/demo_flow.py`：端到端场景演示（部分退回 + 供应商异议）。
- `tests/`：契约完整性回归测试与后端服务/API 测试。

## 后端设计

针对「库存已扣减但索赔仍按全量计算」的脱节问题，后端把五条线索落入同一本账：

- **缺陷批次**：质量工程师登记，缺陷数量同步转入隔离区；
- **退货行**：采购计划员起草 → 提交 → 批准，状态机对齐契约（草拟/待确认/已下达/履行中/已关闭）；
- **物流证据**：仓储登记发货、供应商登记签收，签收联动「供应商在途 → 已退供应商」；
- **责任认定**：质量工程师认定责任数量，索赔分录一次性调整到认定量；
- **索赔分录**：只追加不修改，原始计提与每次调整都带依据（退货批准/物流证据/责任认定/补偿事件）。

**批准退货是原子操作**：同一事务内扣减隔离库存、转供应商在途并按退货行计提应收；任一行库存不足，整单回滚，库存与应收要么一起动、要么都不动。

**差异一律走补偿事件**，每笔事件同时生成调整分录与库存联动：

| 补偿事件 | 角色 | 效果 |
| --- | --- | --- |
| 部分接受 | 供应商 | 未接受数量冲减索赔并退回隔离区 |
| 换货抵扣 | 供应商 | 良品仓入库，按数量抵扣应收 |
| 争议复核 | 质量工程师 | 重新认定责任总量，索赔调整到「认定量 − 已抵扣量」 |
| 撤销 | 采购计划员 | 冲回全部未决索赔，在途退回隔离区，行关闭 |

**对账接口**并列三个口径并给出每次调整依据：实物数量（隔离/在途/签收/退回/换货）、责任数量（认定历史与未决索赔）、金额（计提/冲减/净应收），并执行与契约不变量同名的四条检查（退货库存联动、责任数量认定、索赔分录补偿、实物金额对账）。部分退回未补偿时，「实物金额对账」会标红提示「未决索赔与实物口径不符，且无责任依据」。

## API

启动：`PYTHONPATH=src python3 -m return_claim.api --port 8000 --db data/return_claim.db --contract domain/contract.json`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/defect-batches` | 登记缺陷批次（质量工程师） |
| GET | `/api/defect-batches/{id}/reconciliation` | 批次级对账 |
| POST | `/api/return-orders` | 起草退货单（采购计划员） |
| POST | `/api/return-orders/{id}/submit` · `/approve` · `/close` · `/cancel` | 状态推进；批准即原子调整 |
| GET | `/api/return-orders/{id}` | 单据与行汇总 |
| POST | `/api/return-lines/{id}/evidence` | 物流证据（发货/签收） |
| POST | `/api/return-lines/{id}/liability` | 责任认定（质量工程师） |
| POST | `/api/return-lines/{id}/compensations` | 补偿事件，`type` 为部分接受/换货抵扣/争议复核/撤销 |
| GET | `/api/return-lines/{id}/reconciliation` | 行级三视图对账 + 调整依据链 |

错误码：400 入参校验、403 角色越权、404 单据不存在、409 业务规则冲突。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

场景演示：`python3 tools/demo_flow.py`
