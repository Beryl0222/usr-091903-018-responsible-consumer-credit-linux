# 消费贷审慎额度管理

在"促消费"与"长期偿付能力"之间设置硬边界的后端：覆盖**申请 → 授信 → 提款 → 用途核验 → 放款 → 还款 → 困难协商**全链路，仅使用 Python 标准库（`http.server` + `sqlite3`），无外部依赖。

## 运行

```bash
python3 service.py --check          # 配置与基础计算自检
python3 service.py --port 8000      # 启动服务（默认内存库；--db 或 CREDIT_DB 指定持久化路径）
curl localhost:8000/health

npm test                            # 运行全部 73 个单元/集成测试
# 或：python3 -m unittest discover -v
```

## 领域边界如何落地

| 需求 | 实现位置 | 机制 |
| --- | --- | --- |
| 仅在明确授权内读取收入/征信/存量债务 | `credit/consent.py` | 三类 scope 逐项授权、可撤销、有效期；用途与 scope 绑定（`PURPOSE_SCOPES`），每次读取写 `snapshot_access_log` |
| 保存每次判断的规则与数据时点 | `credit/rules.py`、`affordability_assessments` | 规则版本 `affordability-v1.0` 参数集中固化；评估固化快照 ID 与 `as_of`；合规可重算复现（`/applications/{id}/replay`） |
| 并发提款不突破仍有效的总额度 | `credit/storage.py` `reserve_available` / `workflow._reserve_and_create` | 额度占用为条件 `UPDATE ... WHERE available_cents>=?`，与建单在同一 `BEGIN IMMEDIATE` 事务；见并发测试 `test_concurrent_withdrawals_never_exceed_valid_total_limit` |
| 商户退款 / 分期取消按原路冲减，不是还款 | `credit/ledger.py` `merchant_reversal` | 台账类型 `merchant_refund`/`installment_cancel` 与 `repayment` 严格区分：不标记任何期次 paid、按原期限重排剩余本金、全额冲减豁免未付利息；未放款分期只能全额取消并释放占用 |
| 收入骤降 / 疑似借新还旧 / 用途冲突暂停未放款部分 | `credit/rules.py` `withdrawal_risk_check`、`credit/workflow.py` | 提款时用**最新快照**复查：`INCOME_DROP`、`SUSPECTED_BORROW_TO_REPAY`、`NEW_MULTI_INSTITUTION_DEBT`、`BORROWER_IN_GRACE_PERIOD`、`EXISTING_OVERDUE`；命中即 `held`+额度冻结+开人工案件，已占用金额不被再次使用 |
| 人工给出降额 / 展期 / 重组决定 | `credit/workflow.py` `decide_manual_case`、`credit/ledger.py` `restructure` | 仅 `risk_officer` 可决定且必须填写 rationale；提款案件：放款/取消/降额放款；困难案件：extension（展期）、restructure（降息重组）、forbearance（缓还，到期日顺延、金额不变） |
| 禁止营销目标覆盖风险结论 | 规则无任何营销入参；`adjust_line` 拒绝调升（调升必须重新评估）；营销角色路由白名单 | 见测试 `test_marketing_campaign_cannot_reduce_risk_conclusion`、`marketing_scope_denied` |
| 客户看得懂利率、总成本、额度变化原因 | 放款返回 `cost_disclosure`（本金/年化/总利息/总还款/首期月供）；额度视图含完整 `change_history` 及人话原因 | `ledger.total_cost_disclosure`、`workflow.customer_line_view` |
| 合规可复现决策与催收联系边界 | 决策重放、快照访问审计、催收尝试（含被拦截）全部落库 | `/replay`、`/snapshot-access`、`/loans/{id}/collection-contacts` |
| 敏感画像不得用于未经同意的促销 | `marketing_use` 是独立 scope；营销准入只查该授权（`risk_data_used=false`）；营销角色访问任何风控路径返回 403 | `credit/consent.py`、`credit/api.py` 角色控制 |

### 催收联系边界（`collections-v1.0`）

- 到期后 3 天宽限期：禁止电话/上门等施压方式，短信/App/邮件提醒每自然日至多 1 次；
- 困难协商**未决期间**一切催收暂停；安排生效后客户按新计划履约，再次违约回到正常催收；
- 联系时段 08:00–21:00、每日最多 3 次；所有尝试（含被拦截原因）写 `collection_contacts`。

### 可负担性规则（`affordability-v1.0`）

硬拒绝（hard block）：收入中断、已在征信宽限期、存在逾期。
通过硬门槛后，按 `月供能力 = min(月收入×55% − 存量月供, 月收入 − 存量月供 − 1500 元最低留存)` 反推最大本金，循环额度按该上限核定（而非按单笔申请金额截断）；3–36 期等额本息，末期吸收四舍五入残差，本金合计精确。

## 主要 HTTP 接口

角色经 `X-Actor-Id` / `X-Actor-Role`（customer / risk_officer / compliance / marketing / collector / system）传入。

```
POST /v1/consents/grant | /revoke            客户管理授权
POST /v1/snapshots                           登记收入/征信/债务快照（system）
POST /v1/applications                        申请并固化可负担性评估
POST /v1/applications/{id}/offer             按评估上限授信
POST /v1/withdrawals                         提款（幂等键 idempotency_key；自动复查与放款）
POST /v1/withdrawals/{id}/documents          提交用途凭证（冲突即冻结开案）
POST /v1/withdrawals/{id}/refund | /cancel   商户退款 / 分期取消（原路冲减）
POST /v1/loans/{id}/repay                    客户还款（先息后本，溢缴提前还本）
POST /v1/loans/{id}/hardship                 申请困难协商（催收即时暂停）
POST /v1/loans/{id}/collection-contact       催收联系（自动执行宽限/时段/频次边界）
GET  /v1/cases ; POST /v1/cases/{id}/decide  人工案件与决定（必须 rationale）
GET  /v1/applications/{id}/replay            合规复现决策
GET  /v1/customers/{id}/snapshot-access      快照读取审计
GET  /v1/marketing/customers/{id}/eligibility 营销准入（只看 marketing_use 授权）
```

## 代码结构

```
credit/
  clock.py       可冻结/快进的时钟（测试确定性）
  errors.py      领域错误与 HTTP 状态映射
  util.py        分/元换算、等额本息与还款计划
  storage.py     SQLite schema、显式事务、原子额度占用
  consent.py     授权 scope、用途绑定、快照读取留痕
  rules.py       版本化可负担性规则、提款风险触发器、用途核验
  ledger.py      放款/还款/原路冲减/展期重组/缓还
  collections.py 催收联系边界
  workflow.py    全链路编排与风控优先原则
  api.py         HTTP 路由与角色访问控制
tests/           73 个测试（含真实 socket 集成与 10 线程并发）
```
