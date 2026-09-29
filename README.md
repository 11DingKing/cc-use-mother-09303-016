# 国际奖学金配置

本项目维护国际奖学金配置的领域约定、角色边界与样例数据，并提供完整的服务端实现：资金批次、限制规则、候选材料、回避关系、汇率口径、方案试算、正式授予（原子锁定 + 评审快照封存）、放弃/资格丧失/延期/资金转移的分录结算，以及面向审计的轮次复算与资金占用台账。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scholarship/`：服务端（标准库实现，零外部依赖）。
  - `money.py`：最小单位整数金额与有理数汇率换算（确定性舍入）。
  - `db.py`：SQLite 表结构与写事务；`(申请人, 批次)` 有效授予唯一索引。
  - `ledger.py`：只增不改的资金占用分录与派生余额。
  - `rules.py`：限制规则引擎（试算与授予共用）。
  - `service.py`：核心业务；`api.py`：HTTP 接口；`seed.py`：演示数据。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、换算、业务流与 HTTP 端到端（含并发防重）测试。

## 运行服务端

```bash
PYTHONPATH=src python3 -m scholarship --db scholarship.db --port 8000 --seed
```

`--seed` 写入演示数据并打印演示令牌（仅限本地演示）：`demo-admin`（奖学金办公室）、`demo-funder`（资助方）、`demo-rev1`/`demo-rev2`（评审）、`demo-app1`/`demo-app2`（申请人）、`demo-auditor`（审计）。调用时携带 `Authorization: Bearer <令牌>`。

## 接口概览

| 接口 | 角色 | 说明 |
| --- | --- | --- |
| `POST /fx-rate-sets`、`GET /fx-rate-sets/{id}` | 办公室定义 | 汇率口径：舍入方式 + 有理数汇率，创建后不可变 |
| `POST /batches`、`GET /batches[/{id}]` | 办公室/资助方 | 资金批次；资助方仅限本资助方，响应含派生余额 |
| `POST /rules`、`GET /rules` | 办公室 | 限制规则：单笔上限、人均有效授予数、轮次预算、国籍限制、GPA 下限 |
| `POST /rounds`、`POST /rounds/{id}/seal` | 办公室 | 评审轮次与封存 |
| `POST /candidates`、`POST /candidates/{id}/eligibility` | 办公室/评审 | 候选材料与资格标记 |
| `POST /recusals`、`GET /recusals` | 办公室 | 评审人 × 申请人回避关系 |
| `POST /trials`、`GET /trials/{id}` | 评审/办公室 | 方案试算：同一套换算与规则判定，不锁定额度 |
| `POST /awards`、`GET /awards[/{id}]` | 办公室授予 | 正式授予：单事务原子锁定 + 评审快照封存；`request_id` 幂等 |
| `POST /awards/{id}/adjustments` | 办公室 | 放弃 / 资格丧失（可扣留尾差）/ 延期，全部分录结算 |
| `POST /transfers` | 办公室 | 批次间资金转移，跨币种按源批次汇率口径换算 |
| `GET /me/awards`、`GET /me/candidates` | 申请人 | 只能查询自身结果 |
| `GET /audit/batches/{id}/ledger`、`/reconcile`、`GET /audit/rounds/{id}/recompute` | 审计/办公室 | 逐笔台账、批次对账、轮次复算 |

金额一律使用最小货币单位整数（如“分”）或两位小数字符串；拒绝浮点数。

## 核心机制

- **防重复授予**：正式授予在单个 `BEGIN IMMEDIATE` 事务内完成“校验 + 写入 + 锁定”，并由 `(申请人, 批次)` 有效授予部分唯一索引兜底——两个评审组并发授予同一来源额度时只有一笔成功（409 `AWARD_CONFLICT`）；`request_id` 保证重试幂等。
- **汇率口径**：汇率集创建后不可变，批次创建时绑定；换算只在锁定时发生一次，释放永远按原锁定额，杜绝换算回漂导致的剩余资金对账差异。
- **尾差固定去向**：结算扣留/尾差以 `RESIDUAL` 分录进入批次固定去向（`RESERVE` 准备金或 `AVAILABLE` 回流可用），批次恒等式 `总额 + 转入 − 转出 = 可用 + 锁定 + 准备金` 始终成立。
- **分录结算**：`LOCK / RELEASE / TRANSFER_OUT / TRANSFER_IN / RESIDUAL` 只增不改，所有余额由分录派生；延期通过“原轮次释放 + 新轮次锁定”一对分录迁移占用。
- **复算审计**：审计可对任一轮次用封存快照重算应锁定额并与分录净额逐笔核对，也可回放任一批次每笔资金占用变化（含每笔之后的锁定/可用余额）。
- **角色边界**：申请人仅 `/me/*`；审计只读；资助方限本资助方批次；授奖评审人必须来自轮次评审组且与申请人无回避关系。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
