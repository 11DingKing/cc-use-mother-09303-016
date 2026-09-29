# 国际奖学金配置

本项目维护国际奖学金配置的领域约定、角色边界与样例数据，并提供完整服务端：
多资助方资金批次、限制规则、候选材料、回避关系、汇率口径、方案试算、
原子授予（额度锁定 + 评审快照封存）、分录结算与审计复算。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scholarship_server/`：奖学金配置服务端（仅标准库）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与服务端回归测试。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

## 启动服务

```bash
PYTHONPATH=src python3 -m scholarship_server --db scholarship.db --port 8000 --seed
```

`--seed` 写入演示账号（口令均为 `<账号>-secret`）：

| 账号 | 角色 | 权限 |
| --- | --- | --- |
| `office` | 奖学金办公室 | 全部管理操作与结算 |
| `panel-a` / `panel-b` | 评审组 | 评审、试算、正式授予 |
| `stu-001` … `stu-003` | 申请学生 | 提交本人材料、查询本人结果 |
| `auditor` | 审计 | 只读复算与分录追踪 |

认证：`POST /tokens` 用 `actor_id` + `secret` 换取令牌，之后请求携带
`Authorization: Bearer <token>`。

## 核心机制

- **额度原子锁定**：正式授予在单个 IMMEDIATE 事务内完成材料/评审/规则/
  可用额度校验、授予写入、快照封存与锁定分录；`(轮次, 申请人, 批次)` 上的
  部分唯一索引兜底——两个评审组并发授予同一来源额度时恰有一笔成功（409
  `AWARD_CONFLICT`）。支持 `idempotency_key` 幂等重放。
- **汇率口径**：每个轮次锁定一套正反向汇率（`round_rates`），轮内所有换算
  只使用该口径；金额一律整数最小单位，换算四舍五入（半数向上）。
- **尾差固定去向**：换算尾差 = 精确值 − 入账整数，以有理数精确记入对应币种
  的尾差归集批次（每币种至多一个，系统自动建立），账面恒可对平。
- **分录结算**：账本分录是资金占用的唯一事实来源——`LOCK`/`RELEASE`/
  `FORFEIT`/`DEFER_*` 影响授予占用，`TRANSFER_OUT`/`TRANSFER_IN` 影响批次
  余额，`ROUNDING` 归集尾差；可用额度 = 余额 − 占用。放弃、资格丧失、延期
  （占用随分录迁移到新轮次）、资金转移全部经分录完成。
- **评审快照封存**：授予时把轮次口径、评审记录、回避关系、材料版本哈希、
  规则集与金额口径整体封存为不可变快照（SHA-256 校验）。
- **审计复算**：`GET /audit/rounds/{id}/recompute` 重放全部分录，逐笔校验
  占用/余额边界与快照哈希；`GET /audit/batches/{id}/ledger` 给出每笔资金的
  占用变化流水。

## 接口概览

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| POST | `/tokens` | 公开 |
| GET | `/meta/contract` | 公开 |
| POST/GET | `/fund-batches` | 办公室 / 读角色 |
| POST/GET | `/exchange-rates` | 办公室 / 读角色 |
| POST/GET | `/rounds`，POST `/rounds/{id}/seal` | 办公室 |
| POST | `/rounds/{id}/reviews` | 评审组 |
| POST | `/rounds/{id}/trial` | 办公室、评审组 |
| POST | `/rounds/{id}/awards` | 评审组 |
| GET | `/rounds/{id}/awards` | 办公室、审计 |
| POST/GET | `/me/materials`，GET `/me/awards` | 申请学生（仅本人） |
| GET | `/candidates`，POST `/candidates/{id}/verify` | 读角色 / 办公室 |
| POST/GET | `/recusals`、`/rules` | 办公室 / 读角色 |
| GET | `/awards/{id}`、`/awards/{id}/snapshot` | 办公室、审计 |
| POST | `/awards/{id}/settle` | 办公室（withdraw/forfeit/defer） |
| POST/GET | `/transfers` | 办公室 / 审计 |
| GET | `/audit/rounds/{id}/recompute`、`/audit/batches/{id}/ledger`、`/audit/residuals` | 办公室、审计 |

限制规则类型：`MIN_GPA`、`DEGREE_LEVEL`、`NATIONALITY_DENY`、
`MAX_TOTAL_PER_APPLICANT`、`MAX_AWARDS_PER_APPLICANT`（可全局或按批次生效）。

## 典型流程

```bash
# 1. 办公室建池、锁口径、开轮次
curl -X POST :8000/fund-batches  -d '{"funder":"甲基金会","name":"美元池","currency":"USD","total_minor":1000000}'
curl -X POST :8000/rounds -d '{"name":"2026 秋季轮","rates":[{"base_currency":"USD","quote_currency":"CNY","num":710,"den":100}]}'

# 2. 学生提交材料 → 办公室核验 → 评审组评审 → 试算 → 正式授予
curl -X POST :8000/me/materials -d '{"payload":{"gpa":3.9,"degree_level":"master","nationality":"CN"}}'
curl -X POST :8000/candidates/stu-001/verify
curl -X POST :8000/rounds/{rid}/reviews -d '{"applicant_id":"stu-001","reviewer_id":"rev-a","score":92}'
curl -X POST :8000/rounds/{rid}/trial   -d '{"lines":[{"applicant_id":"stu-001","batch_id":"...","amount_minor":100000,"currency":"CNY"}]}'
curl -X POST :8000/rounds/{rid}/awards  -d '{"applicant_id":"stu-001","batch_id":"...","amount_minor":100000,"currency":"CNY","idempotency_key":"k1"}'

# 3. 结算与审计
curl -X POST :8000/awards/{aid}/settle -d '{"action":"defer","to_round_id":"..."}'
curl :8000/audit/rounds/{rid}/recompute
```
