"""可负担性规则引擎（版本化）。

每次评估：
1. 只使用已落库、带数据时点的不可变快照（收入/存量债务/征信）；
2. 采用某个明确版本的规则集，全部阈值随版本冻结；
3. 把规则版本、输入快照（ID+时点+关键字段）、中间计算、触发原因
   一并写入 assessments，合规可凭 assessment_id 复现整次判断。

营销目标不出现在本模块的任何输入或分支中——额度结论只由规则计算得出。
"""

from dataclasses import dataclass
from decimal import Decimal

from credit import clock
from credit.money import annuity_payment_cents, cents_to_decimal, round_cents
from credit.store import Store, new_id

# 规则集：新版本只能新增，不得就地修改已发布版本的阈值
RULE_SETS = {
    "v1.0": {
        "version": "v1.0",
        "effective_from": "2026-01-01",
        "params": {
            # 硬性可负担性
            "min_monthly_income_cents": 300_000,          # 3000 元
            "max_dti_all_in": "0.55",                     # (存量月供+本笔月供)/月收入
            "min_disposable_ratio": "0.20",               # 扣除全部月供后至少保留 20%
            "min_disposable_cents": 150_000,              # 且不低于 1500 元
            # 额度上限
            "max_limit_multiple_annual_income": "0.5",    # 额度不超过年收入 0.5 倍
            "absolute_limit_cents": 200_000_00,           # 单客户产品上限 20 万元
            "max_term_months": 36,
            # 收入稳定性
            "income_drop_block_ratio": "0.30",            # 最近月收入较前 3 月均值降 30%
            "income_interruption_statuses": ["interrupted"],
            # 征信
            "credit_block_statuses": ["overdue_30", "overdue_60", "overdue_90plus", "writeoff"],
            "credit_review_statuses": ["grace"],          # 宽限期：转人工，不自动批
            # 疑似借新还旧/多头借贷
            "rollover_window_days": 90,
            "rollover_new_facilities_block": 2,           # 窗口内新增机构数 >=2
            "rollover_new_debt_payment_ratio": "0.30",    # 新增负债月供/收入 >=30%
        },
    }
}

DEFAULT_RULE_SET = "v1.0"

# 原因码稳定，供披露与人工复核引用
REASON_CODES = {
    "income_below_minimum": "月收入低于准入线",
    "income_interrupted": "收入处于中断状态",
    "dti_exceeded": "总负债收入比超出上限",
    "disposable_below_minimum": "扣除月供后可支配余额不足",
    "income_dropped": "近期收入骤降",
    "credit_in_grace": "征信显示处于宽限期",
    "credit_adverse": "征信存在严重逾期",
    "suspected_rollover": "疑似借新还旧/短期多头借贷",
    "term_too_long": "期限超出产品上限",
}


@dataclass
class AssessmentInput:
    income: dict       # snapshot_reads 行（含 payload）
    debt: dict
    credit: dict


class AffordabilityEngine:
    def __init__(self, store: Store, rule_set_version: str = DEFAULT_RULE_SET):
        self.store = store
        if rule_set_version not in RULE_SETS:
            raise ValueError(f"未知规则集版本: {rule_set_version}")
        self.rule_set_version = rule_set_version

    @property
    def params(self) -> dict:
        return RULE_SETS[self.rule_set_version]["params"]

    def evaluate(self, application_row, snapshots: AssessmentInput, consent_row) -> dict:
        """计算 + 落库。application_row 为 sqlite3.Row。"""
        app = dict(application_row)
        result = self.compute(
            app,
            Store.loads(snapshots.income["payload"]),
            Store.loads(snapshots.debt["payload"]),
            Store.loads(snapshots.credit["payload"]),
        )
        inputs = {
            "rule_set_version": self.rule_set_version,
            "consent_id": consent_row["id"],
            "consent_grant_ref": consent_row["grant_ref"],
            "snapshots": {
                "income": {"id": snapshots.income["id"], "source": snapshots.income["source"],
                           "as_of": snapshots.income["as_of"], "read_at": snapshots.income["read_at"],
                           "key_fields": self._key_fields(Store.loads(snapshots.income["payload"]))},
                "debt": {"id": snapshots.debt["id"], "source": snapshots.debt["source"],
                         "as_of": snapshots.debt["as_of"], "read_at": snapshots.debt["read_at"],
                         "key_fields": self._key_fields(Store.loads(snapshots.debt["payload"]))},
                "credit_report": {"id": snapshots.credit["id"], "source": snapshots.credit["source"],
                                  "as_of": snapshots.credit["as_of"], "read_at": snapshots.credit["read_at"],
                                  "key_fields": self._key_fields(Store.loads(snapshots.credit["payload"]))},
            },
        }
        assessment = {
            "id": new_id("asm"),
            "application_id": app["id"],
            "customer_id": app["customer_id"],
            "rule_set_version": self.rule_set_version,
            "decision": result["decision"],
            "approved_limit_cents": result["approved_limit_cents"],
            "annual_rate": str(result["annual_rate"]),
            "term_months": result["term_months"],
            "triggered_reasons": Store.dumps(result["reasons"]),
            "warnings": Store.dumps([]),
            "consent_id": consent_row["id"],
            "inputs": Store.dumps(inputs),
            "calculation": Store.dumps(result["calculation"]),
            "created_at": clock.now_iso(),
        }
        with self.store.transaction():
            self.store.insert("assessments", assessment)
        return assessment

    @classmethod
    def compute(cls, app: dict, income_payload: dict, debt_payload: dict,
                credit_payload: dict, rule_set_version: str = DEFAULT_RULE_SET) -> dict:
        """纯计算（不落库）：合规复现与引擎内部共用，同样输入+规则版本必得同样输出。"""
        if rule_set_version not in RULE_SETS:
            raise ValueError(f"未知规则集版本: {rule_set_version}")
        p = RULE_SETS[rule_set_version]["params"]

        income = int(income_payload["monthly_income_cents"])
        existing_obligation = int(debt_payload.get("monthly_obligation_cents", 0))
        annual_rate = Decimal(str(app["annual_rate"]))
        months = int(app["term_months"])
        requested = int(app["requested_cents"])

        blocks, review_signals = [], []

        # --- 1. 收入准入与中断 ---
        if income < int(p["min_monthly_income_cents"]):
            blocks.append("income_below_minimum")
        if income_payload.get("employment_status") in p["income_interruption_statuses"]:
            blocks.append("income_interrupted")

        # --- 2. 收入骤降：最近月 vs 前三个月均值 ---
        drop = cls._income_drop(income_payload)
        if drop is not None and drop >= Decimal(p["income_drop_block_ratio"]):
            review_signals.append("income_dropped")

        # --- 3. 征信状态 ---
        credit_status = credit_payload.get("status", "normal")
        if credit_status in p["credit_block_statuses"]:
            blocks.append("credit_adverse")
        elif credit_status in p["credit_review_statuses"]:
            review_signals.append("credit_in_grace")

        # --- 4. 疑似借新还旧 / 多头 ---
        rollover = cls._rollover_metrics(debt_payload, income, rule_set_version)
        if (rollover["new_facilities"] >= int(p["rollover_new_facilities_block"])
                or Decimal(rollover["new_payment_ratio"]) >= Decimal(p["rollover_new_debt_payment_ratio"])):
            review_signals.append("suspected_rollover")

        # --- 5. 期限 ---
        if months > int(p["max_term_months"]):
            blocks.append("term_too_long")

        # --- 6. 本笔月供与可负担性 ---
        proposed_payment = annuity_payment_cents(requested, annual_rate / 12, months)
        dti = ((Decimal(existing_obligation + proposed_payment)) / Decimal(income)
               if income else Decimal("999"))
        if dti > Decimal(p["max_dti_all_in"]):
            blocks.append("dti_exceeded")

        disposable = income - existing_obligation - proposed_payment
        min_disposable = max(
            int(p["min_disposable_cents"]),
            round_cents(cents_to_decimal(income) * Decimal(p["min_disposable_ratio"])),
        )
        if disposable < min_disposable:
            blocks.append("disposable_below_minimum")

        # --- 7. 可负担额度反推 ---
        max_payment = min(
            round_cents(cents_to_decimal(income) * Decimal(p["max_dti_all_in"])) - existing_obligation,
            income - existing_obligation - min_disposable,
        )
        affordable_principal = cls._max_principal(max_payment, annual_rate / 12, months)
        annual_income_cap = round_cents(
            cents_to_decimal(income * 12) * Decimal(p["max_limit_multiple_annual_income"])
        )
        limit_cap = min(int(p["absolute_limit_cents"]), annual_income_cap)
        approved_limit = max(0, min(requested, affordable_principal, limit_cap))

        if blocks:
            decision = "rejected"
            approved_limit = None
        elif review_signals:
            decision = "manual_review"
            approved_limit = None
        else:
            decision = "approved"

        calculation = {
            "monthly_income_cents": income,
            "existing_monthly_obligation_cents": existing_obligation,
            "proposed_monthly_payment_cents": proposed_payment,
            "dti_all_in": str(round(dti, 4)),
            "disposable_cents": disposable,
            "min_disposable_cents": min_disposable,
            "max_payment_cents": max(0, max_payment),
            "affordable_principal_cents": affordable_principal,
            "annual_income_cap_cents": annual_income_cap,
            "absolute_cap_cents": int(p["absolute_limit_cents"]),
            "income_drop_ratio": str(drop) if drop is not None else None,
            "rollover": rollover,
        }
        return {
            "decision": decision,
            "approved_limit_cents": approved_limit,
            "annual_rate": annual_rate,
            "term_months": months,
            "reasons": sorted(set(blocks + review_signals)),
            "block_reasons": sorted(blocks),
            "review_reasons": sorted(review_signals),
            "calculation": calculation,
        }

    # --- 内部计算 -------------------------------------------------------

    @staticmethod
    def _key_fields(payload: dict) -> dict:
        """留档关键字段，避免复现时还要反解整份快照。"""
        keep = {k: v for k, v in payload.items() if k not in ("facilities",)}
        return keep

    @staticmethod
    def _income_drop(income_payload: dict):
        months = income_payload.get("months")
        if not months or len(months) < 2:
            return None
        ordered = sorted(months, key=lambda m: m["month"])
        latest = Decimal(ordered[-1]["income_cents"])
        baseline_months = ordered[-4:-1]
        if not baseline_months:
            return None
        baseline = sum(Decimal(m["income_cents"]) for m in baseline_months) / len(baseline_months)
        if baseline == 0:
            return None
        return (baseline - latest) / baseline

    @staticmethod
    def _rollover_metrics(debt_payload: dict, income: int,
                          rule_set_version: str = DEFAULT_RULE_SET) -> dict:
        facilities = debt_payload.get("facilities", [])
        window = int(RULE_SETS[rule_set_version]["params"]["rollover_window_days"])
        new_facilities = [f for f in facilities if int(f.get("opened_days_ago", 10_000)) <= window]
        new_payment = sum(int(f.get("monthly_payment_cents", 0)) for f in new_facilities)
        ratio = (Decimal(new_payment) / Decimal(income)) if income else Decimal("0")
        return {
            "window_days": window,
            "new_facilities": len(new_facilities),
            "new_monthly_payment_cents": new_payment,
            "new_payment_ratio": str(round(ratio, 4)),
        }

    @staticmethod
    def _max_principal(max_payment_cents: int, monthly_rate: Decimal, months: int) -> int:
        """由可承受月供反推最大本金（年金现值）。"""
        if max_payment_cents <= 0:
            return 0
        payment = cents_to_decimal(max_payment_cents)
        if monthly_rate == 0:
            return round_cents(payment * months)
        factor = (1 + monthly_rate) ** months
        return round_cents(payment * (factor - 1) / (monthly_rate * factor))


def describe_reason(code: str) -> str:
    return REASON_CODES.get(code, code)
