"""版本化可负担性规则与提款风险触发器。

每次决策都记录 rule_version 与所用快照的数据时点（as_of），
合规可据此完整复现。规则只产出风险结论，任何调用方不得以营销目标覆盖
（see evaluate() 的 allow_override 设计：结论中 hard_block 不可被参数放宽）。
"""

from decimal import Decimal

from .util import max_principal_for_payment, monthly_payment_cents, yuan_to_cents

RULE_VERSION = "affordability-v1.0"

# 规则参数集中声明并随版本固化，禁止调用方按营销目标临时改阈值
RULE_PARAMS = {
    "max_dti": "0.55",              # (存量月供+拟新增月供)/月收入 上限
    "min_residual_income_cents": 150000,  # 扣除全部月供后最低生活留存（1500 元）
    "min_repayment_months": 3,
    "max_repayment_months": 36,
    "income_drop_hold_ratio": "0.30",     # 提款时收入较授信时下降≥30% 暂停
    "new_debt_hold_dti_delta": "0.05",    # 多头新增负债推高 DTI ≥5pp 暂停
    "evidence_amount_tolerance": "0.05",  # 用途凭证金额容差
}


class Rule:
    version = RULE_VERSION
    params = RULE_PARAMS

    # ---------- 授信可负担性 ----------
    def assess_affordability(self, bundle, requested_cents, term_months, annual_rate):
        """返回 dict：decision/max_amount_cents/payment_cents/reasons/metrics/flags。"""
        income = bundle.get("income") or {}
        credit = bundle.get("credit_report") or {}
        debt = bundle.get("debt_snapshot") or {}

        monthly_income = int(income.get("monthly_income_cents", 0))
        income_status = income.get("status", "unknown")
        interrupted = income_status in ("interrupted", "unemployed") or monthly_income <= 0

        existing_obligation = int(debt.get("monthly_obligation_cents", 0))
        institution_count = int(debt.get("institution_count", 0))

        grace = bool(credit.get("in_grace_period"))
        overdue = bool(credit.get("has_overdue"))
        overdue_days = int(credit.get("max_overdue_days", 0))

        max_dti = Decimal(self.params["max_dti"])
        min_residual = int(self.params["min_residual_income_cents"])

        reasons = []
        hard_blocks = []

        if interrupted:
            hard_blocks.append("INCOME_INTERRUPTED")
        if grace:
            hard_blocks.append("BORROWER_IN_GRACE_PERIOD")
        if overdue or overdue_days > 0:
            hard_blocks.append("EXISTING_OVERDUE")

        capacity_payment = 0
        proposed_payment = 0
        dti = None
        residual = None
        max_amount = 0

        if monthly_income > 0:
            # 可用于新增月供的空间 = 收入×上限 - 存量月供 - 最低生活留存
            capacity_payment = max(
                0,
                int(Decimal(monthly_income) * max_dti)
                - existing_obligation,
            )
            # 先扣生活留存的硬约束
            residual_capacity = monthly_income - existing_obligation - min_residual
            capacity_payment = min(capacity_payment, max(0, residual_capacity))

            term = self._clamp_term(term_months)
            max_amount = max_principal_for_payment(
                capacity_payment, annual_rate, term
            )
            if requested_cents > 0:
                proposed_payment = monthly_payment_cents(
                    requested_cents, annual_rate, term
                )
                total_obligation = existing_obligation + proposed_payment
                dti = (Decimal(total_obligation) / Decimal(monthly_income)).quantize(
                    Decimal("0.0001")
                )
                residual = monthly_income - total_obligation
                if dti > max_dti:
                    reasons.append("DTI_EXCEEDS_LIMIT")
                if residual < min_residual:
                    reasons.append("RESIDUAL_INCOME_BELOW_FLOOR")

        if existing_obligation > 0 and institution_count >= 3:
            reasons.append("MULTI_INSTITUTION_DEBT")
        if capacity_payment <= 0 and not hard_blocks:
            reasons.append("NO_AFFORDABLE_PAYMENT_CAPACITY")

        approved = not hard_blocks and requested_cents <= max_amount and requested_cents > 0
        if requested_cents > max_amount and not hard_blocks:
            reasons.append("REQUESTED_ABOVE_AFFORDABLE_LIMIT")

        decision = "approved" if approved else "rejected"
        if not approved and not reasons and not hard_blocks:
            reasons.append("UNKNOWN_RISK")

        return {
            "rule_version": self.version,
            "decision": decision,
            "max_amount_cents": min(max_amount, requested_cents) if approved else max_amount,
            "approved_limit_cents": max_amount,
            "payment_cents": proposed_payment,
            "reasons": hard_blocks + reasons,
            "hard_blocks": hard_blocks,
            "metrics": {
                "monthly_income_cents": monthly_income,
                "existing_monthly_obligation_cents": existing_obligation,
                "institution_count": institution_count,
                "affordable_payment_cents": capacity_payment,
                "proposed_payment_cents": proposed_payment,
                "dti": str(dti) if dti is not None else None,
                "residual_income_cents": residual,
                "term_months": self._clamp_term(term_months),
            },
            "flags": {
                "income_interrupted": interrupted,
                "in_grace_period": grace,
                "has_overdue": overdue,
                "max_overdue_days": overdue_days,
                "multi_institution": institution_count >= 3,
            },
        }

    def _clamp_term(self, term_months):
        lo, hi = self.params["min_repayment_months"], self.params["max_repayment_months"]
        return max(lo, min(hi, int(term_months or 12)))

    # ---------- 提款时风险复查 ----------
    def withdrawal_risk_check(self, current_bundle, assessment_inputs):
        """对比授信时固化的数据，返回 (triggers, details)。任一触发即暂停未放款部分。"""
        triggers = []
        details = {}

        cur_income = (current_bundle.get("income") or {}).get("monthly_income_cents", 0)
        old_income = assessment_inputs["metrics"]["monthly_income_cents"]
        if old_income > 0 and cur_income >= 0:
            drop = (old_income - cur_income) / old_income
            details["income_change"] = {
                "old_cents": old_income,
                "current_cents": cur_income,
                "drop_ratio": round(drop, 4),
            }
            if drop >= float(self.params["income_drop_hold_ratio"]):
                triggers.append("INCOME_DROP")

        cur_credit = current_bundle.get("credit_report") or {}
        if cur_credit.get("in_grace_period"):
            triggers.append("BORROWER_IN_GRACE_PERIOD")
        if cur_credit.get("has_overdue") or int(cur_credit.get("max_overdue_days", 0)) > 0:
            triggers.append("EXISTING_OVERDUE")

        cur_debt = current_bundle.get("debt_snapshot") or {}
        old_debt_institutions = assessment_inputs["metrics"].get("institution_count", 0)
        cur_obligation = int(cur_debt.get("monthly_obligation_cents", 0))
        # 疑似借新还旧：授信后在其他机构新增短期负债，或负债机构数增加且资金用途指向还贷
        new_short_term = int(cur_debt.get("new_short_term_debt_cents_since_assessment", 0))
        cur_institutions = int(cur_debt.get("institution_count", 0))
        purpose = assessment_inputs.get("proposed_usage_purpose")
        rollover_signal = (
            new_short_term > 0
            and (
                cur_institutions > old_debt_institutions
                or purpose in ("debt_repayment", "balance_transfer")
            )
        )
        details["debt_change"] = {
            "new_short_term_debt_cents": new_short_term,
            "institutions": {"old": old_debt_institutions, "current": cur_institutions},
            "current_obligation_cents": cur_obligation,
        }
        if rollover_signal:
            triggers.append("SUSPECTED_BORROW_TO_REPAY")

        # 多头新增负债导致 DTI 恶化
        cur_dti_input = cur_obligation + assessment_inputs["metrics"]["proposed_payment_cents"]
        if cur_income > 0:
            cur_dti = cur_dti_input / cur_income
            old_dti = float(assessment_inputs["metrics"]["dti"] or 0)
            details["debt_change"]["dti"] = {
                "old": round(old_dti, 4),
                "current": round(cur_dti, 4),
            }
            if cur_dti - old_dti >= float(self.params["new_debt_hold_dti_delta"]):
                triggers.append("NEW_MULTI_INSTITUTION_DEBT")

        return triggers, details

    # ---------- 用途凭证核验 ----------
    def verify_usage_document(self, withdrawal, document):
        """返回 (status, conflicts)。status=verified/conflict/more_evidence。"""
        conflicts = []
        tolerance = float(self.params["evidence_amount_tolerance"])

        claimed_amount = document.get("claimed_amount_cents")
        if claimed_amount is not None:
            diff = abs(int(claimed_amount) - withdrawal["amount_cents"])
            if diff > withdrawal["amount_cents"] * tolerance:
                conflicts.append(
                    {
                        "code": "AMOUNT_MISMATCH",
                        "withdrawal_cents": withdrawal["amount_cents"],
                        "document_cents": int(claimed_amount),
                    }
                )

        claimed_merchant = document.get("claimed_merchant")
        if claimed_merchant and withdrawal.get("merchant_id"):
            if claimed_merchant != withdrawal["merchant_id"]:
                conflicts.append(
                    {
                        "code": "MERCHANT_MISMATCH",
                        "withdrawal_merchant": withdrawal["merchant_id"],
                        "document_merchant": claimed_merchant,
                    }
                )

        claimed_purpose = document.get("claimed_purpose")
        if claimed_purpose and withdrawal.get("usage_purpose"):
            if claimed_purpose != withdrawal["usage_purpose"]:
                conflicts.append(
                    {
                        "code": "PURPOSE_MISMATCH",
                        "withdrawal_purpose": withdrawal["usage_purpose"],
                        "document_purpose": claimed_purpose,
                    }
                )

        if conflicts:
            return "conflict", conflicts
        if document.get("doc_type") not in ("receipt", "invoice", "contract"):
            return "more_evidence", [{"code": "UNSUPPORTED_DOC_TYPE"}]
        return "verified", []


def assess(bundle, requested_yuan, term_months, annual_rate):
    requested_cents = yuan_to_cents(requested_yuan)
    result = Rule().assess_affordability(bundle, requested_cents, term_months, annual_rate)
    result["inputs"] = _freeze_inputs(bundle, result)
    return result


def _freeze_inputs(bundle, result):
    """固化参与判断的数据（不含身份明细），供复现。"""
    income = bundle.get("income") or {}
    credit = bundle.get("credit_report") or {}
    debt = bundle.get("debt_snapshot") or {}
    return {
        "income": {
            "monthly_income_cents": income.get("monthly_income_cents"),
            "status": income.get("status"),
            "as_of": income.get("as_of"),
        },
        "credit_report": {
            "in_grace_period": credit.get("in_grace_period"),
            "has_overdue": credit.get("has_overdue"),
            "max_overdue_days": credit.get("max_overdue_days"),
            "as_of": credit.get("as_of"),
        },
        "debt_snapshot": {
            "monthly_obligation_cents": debt.get("monthly_obligation_cents"),
            "institution_count": debt.get("institution_count"),
            "total_outstanding_cents": debt.get("total_outstanding_cents"),
            "as_of": debt.get("as_of"),
        },
        "metrics": result["metrics"],
        "flags": result["flags"],
        "proposed_usage_purpose": None,  # 由 workflow 在提款复查前填入
    }
