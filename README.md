# 消费贷审慎额度管理（responsible-consumer-credit）

覆盖**申请 → 授信 → 提款 → 用途核验 → 放款 → 还款 → 困难协商/催收**的后端服务。
目标是在促消费与长期偿付风险之间守住清晰边界：额度只由可负担性规则与人工风控决定，
营销目标在任何路径上都不能覆盖风险结论。

- 纯 Python 标准库实现（HTTP + SQLite），无第三方依赖；金额对内一律整数分，利息用 `Decimal`。
- 运行：`python3 service.py --check` 自检；`python3 service.py --port 8000` 启动，`GET /health` 验身份。
- 测试：`npm test`（56 个契约/并发/端到端用例）。

## 风险底线如何落地

| 要求 | 实现位置 | 保证方式 |
| --- | --- | --- |
| 仅在明确授权内读取收入/征信/存量负债 | `credit/consent.py` | scope（读什么）× purpose（为什么读）双重校验；授权可撤销、有期限；每次读取落 `snapshot_reads`（来源、数据时点、授权编号） |
| 保存每次可负担性判断的规则与数据时点 | `credit/rules.py`、`assessments` 表 | 规则集版本化（`v1.0` 阈值冻结）；评估记录保存规则版本、三份快照 ID/`as_of`、关键输入、中间计算 |
| 合规可复现一次决策 | `credit/compliance.py` | 用落库时的不可变快照重跑纯计算 `compute()`，比对结论；篡改结论会被 `matches=false` 检出，并附完整时间线 |
| 并发提款不突破仍有效的总额度 | `credit/creditline.py` | 客户级锁 + `BEGIN IMMEDIATE` 事务；提款先预留（`reserved_cents`），放款转占用；`预留+在贷 ≤ 总额度`（含 8 线程并发与 20 轮混合压测） |
| 退款/分期取消原路冲减，不算还款 | `creditline._reverse_principal` | 独立流水类型 `merchant_refund` / `installment_cancel`，只减本金与在贷余额，带 `not_a_repayment` 标记；`repayment` 是另一条流水 |
| 收入骤降/疑似借新还旧/用途冲突暂停未放款部分 | `credit/monitoring.py`、`creditline.submit_evidence` | 账户置 `suspended`，所有未放款提款置 `suspended` 并冻结，开立 open 人工案件 |
| 人工降额/展期/重组 | `credit/review.py` | 仅 `risk_officer/admin` 可决定；降额只能下调（提额必须重新评估）；展期/重组重算摊还计划并全程留痕 |
| 营销不得覆盖风险结论 | `review.decide` + HTTP 角色闸门 | `marketing` 角色出风险决定直接 403 |
| 客户看懂利率、总成本、额度原因 | `credit/disclosure.py` | APR、月供、总利息、总成本；额度变化给出人话原因和依据案件 |
| 催收联系边界可复现 | `credit/collections.py` | 08:00–21:00（客户时区）、每日≤1、滚动7日≤3、困难协商期间停止、仅对逾期贷款；每次联系留痕 |
| 敏感画像不用于未经同意的促销 | `credit/marketing.py` | 风控授权与营销同意是两张独立表；无同意/已撤销/使用 `dti_*`、`risk_*` 等敏感画像一律拦截，拒绝也留痕 |

## 授信规则 v1.0（`credit/rules.py`，阈值随版本冻结）

- 准入：月收入 ≥ 3000 元；收入状态 `interrupted` 直接拒绝；严重逾期（30/60/90+、核销）拒绝。
- **宽限期（grace）不自动批、不拒绝，转人工复核**。
- 总负债收入比 `(存量月供 + 本笔月供) / 月收入 ≤ 55%`；扣完月供至少保留 20% 且 ≥1500 元。
- 收入骤降：最近月收入较前 3 个月均值下降 ≥30% → 转人工/贷后暂停。
- 疑似借新还旧：90 天内新增机构 ≥2 家，或新增负债月供/收入 ≥30% → 转人工/暂停。
- 额度 = min(申请额, 可负担反推本金, 年收入×0.5, 20 万)；期限 ≤36 期。
- 计息：等额本息，年化利率 APR = 月利率×12，无其他费用。

规则分流：任一硬阻断 → `rejected`；无硬阻断但有风险信号 → `manual_review`（不产生额度）；
否则 `approved` 并开立额度账户。

## 主要 HTTP 接口

请求头：`X-Actor`（操作人）、`X-Role`（`customer`/`risk_officer`/`compliance`/`marketing`/`collector`/`admin`）。

- `POST /v1/customers`，`POST /v1/customers/{id}/consents`，`DELETE /v1/consents/{id}`
- `POST /v1/customers/{id}/snapshots`（kind=income/debt/credit_report + purpose，服务端校验授权）
- `POST /v1/applications` → `POST /v1/applications/{id}/assess` → `GET /v1/assessments/{id}/explanation`
- `POST /v1/accounts/{id}/drawdowns` → `POST /v1/drawdowns/{id}/evidence` → `POST /v1/drawdowns/{id}/disburse`
- `POST /v1/loans/{id}/repay`（客户还款）；`POST /v1/loans/{id}/reverse`（`kind=merchant_refund|installment_cancel`，原路冲减）
- `POST /v1/customers/{id}/monitoring/income|debt`（贷后信号，需 ongoing_monitoring 授权）
- `GET /v1/review-cases`、`POST /v1/review-cases/{id}/decide`（降额/展期/重组/恢复/驳回）
- `POST /v1/customers/{id}/hardship`（困难协商，协商期催收自动停止）
- `POST /v1/customers/{id}/collections`、`/collections/check`、`GET .../collection-boundary`
- `GET /v1/compliance/assessments/{id}/reproduce`、`GET .../consent-usage`（仅 compliance/admin）
- `POST /v1/customers/{id}/campaigns`（营销闸门）、`POST .../marketing-consent`（独立营销同意）
- `GET /v1/disclosure/rates?account_id=...`、`GET /v1/accounts/{id}/limit-reasons`

## 一笔典型业务

1. 客户授权三类 scope（授信用途）→ 银行读取三份当日快照（无授权返回 403 `consent_required`）。
2. 提交申请并评估：通过则按可负担额度开户；宽限期/骤降/多头则转人工案件。
3. 提款先冻结额度；提交用途凭证（类目、金额、商户），冲突（理财/套现/还贷/首付、金额偏差>10%）即暂停放款立案。
4. 凭证通过后放款，生成等额本息计划；商户退款/分期取消按原路径冲减本金并重算后续期次。
5. 贷后读到收入骤降或多头新增负债 → 暂停账户与所有未放款提款 → 风控出具降额/展期/重组决定。
6. 客户可困难协商；协商期间催收系统拒绝一切窗口外/超频/对非逾期贷款的联系。
7. 合规岗位随时复现任意一次授信决策，并查看催收边界与授权使用台账。

## 代码结构

```
credit/
  store.py        SQLite 台账、事务（嵌套 SAVEPOINT）、客户锁、不可变快照
  consent.py      风控数据授权 + 独立营销同意；数据读取留痕
  rules.py        版本化规则集；纯计算 compute() 与评估落库 evaluate()
  underwriting.py 申请→评估→开户/人工案件
  creditline.py   额度台账、提款预留、凭证核验、放款、冲减、还款
  monitoring.py   贷后收入/负债信号 → 暂停未放款部分 + 立案
  review.py       人工决定：降额/展期/重组/恢复/驳回（角色强制）
  hardship.py     困难协商申请
  collections.py  催收时间窗/频次/困难期/逾期前提与联系留痕
  compliance.py   决策复现、催收边界报告、授权使用台账
  marketing.py    促销同意闸门与敏感画像拦截
  disclosure.py   客户披露（利率/总成本/额度变化人话解释）
  money.py        整数分、Decimal、等额本息摊还
  httpapi.py      JSON 路由与岗位闸门
service.py        运行入口（兼容原有 --check / --port / /health 契约）
```

生产部署建议：SQLite 单进程适合作为单实例服务；多实例部署时把 `Store` 换成
PostgreSQL（行级锁 `SELECT … FOR UPDATE`）即可，领域层的事务与客户锁语义保持不变。
