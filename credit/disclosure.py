"""客户披露：用客户能看懂的语言呈现利率、总成本与额度变化原因。

数据来源全部为台账中的留痕记录，不做任何"美化"或营销性表述。
"""

from decimal import Decimal

from credit.money import cents_to_yuan
from credit.rules import REASON_CODES
from credit.store import Store


def _yuan(cents) -> str:
    return cents_to_yuan(int(cents or 0))


class DisclosureService:
    def __init__(self, store: Store):
        self.store = store

    def rate_card(self, application_id: str | None = None, account_id: str | None = None) -> dict:
        """利率与总成本卡片：年化利率、总利息、月供、总成本。"""
        if account_id:
            account = self.store.require("credit_accounts", account_id)
            rate = Decimal(account["annual_rate"])
            limit = int(account["limit_cents"])
            term = int(account["term_months"])
            loans = self.store.query(
                "SELECT * FROM loans WHERE account_id=? ORDER BY created_at", (account_id,))
            loan_cards = [self._loan_cost_card(r) for r in loans]
            return {
                "annual_rate": str(rate),
                "annual_rate_percent": _percent(rate),
                "credit_limit_yuan": _yuan(limit),
                "term_months": term,
                "interest_method": "等额本息；年化利率（APR）= 月利率 × 12，无其他费用",
                "loans": loan_cards,
            }
        app = self.store.require("applications", application_id)
        asm = self.store.query_one(
            "SELECT * FROM assessments WHERE application_id=? ORDER BY created_at DESC LIMIT 1",
            (application_id,))
        if asm is None or asm["decision"] != "approved":
            return {"application_id": application_id, "status": asm["decision"] if asm else "none",
                    "message": "申请尚未通过可负担性评估，暂无可展示的利率与额度"}
        return {
            "application_id": application_id,
            "annual_rate": asm["annual_rate"],
            "annual_rate_percent": _percent(Decimal(asm["annual_rate"])),
            "approved_limit_yuan": _yuan(asm["approved_limit_cents"]),
            "term_months": asm["term_months"],
            "interest_method": "等额本息；年化利率（APR）= 月利率 × 12，无其他费用",
        }

    def _loan_cost_card(self, loan_row) -> dict:
        schedule = Store.loads(loan_row["schedule"])
        total_pay = sum(r["payment_cents"] for r in schedule if r["status"] != "reversed")
        # reversed 期次已被原路冲减，不计入客户成本
        active_rows = [r for r in schedule if r["status"] != "reversed"]
        total_pay = sum(r["payment_cents"] for r in active_rows)
        principal = int(loan_row["principal_cents"]) - int(loan_row["refunded_principal_cents"])
        total_interest = max(0, total_pay - principal)
        monthly = active_rows[0]["payment_cents"] if active_rows else 0
        return {
            "loan_id": loan_row["id"],
            "status": loan_row["status"],
            "principal_yuan": _yuan(principal),
            "monthly_payment_yuan": _yuan(monthly),
            "total_interest_yuan": _yuan(total_interest),
            "total_repayment_yuan": _yuan(total_pay),
            "term_months": int(loan_row["term_months"]),
        }

    def limit_change_reasons(self, account_id: str) -> list[dict]:
        rows = self.store.query(
            "SELECT * FROM limit_change_events WHERE account_id=? ORDER BY created_at",
            (account_id,),
        )
        result = []
        for r in rows:
            result.append({
                "changed_at": r["created_at"],
                "old_limit_yuan": _yuan(r["old_limit_cents"]) if r["old_limit_cents"] is not None else None,
                "new_limit_yuan": _yuan(r["new_limit_cents"]),
                "reason_code": r["reason_code"],
                "reason_in_plain_language": self._plain_reason(r),
                "source": r["source"],
                "review_case_id": r["review_case_id"],
            })
        return result

    @staticmethod
    def _plain_reason(event_row) -> str:
        code = event_row["reason_code"]
        base = {
            "initial_assessment": "根据您授权读取的收入、征信和存量负债，按可负担性规则核定初始额度",
            "manual_limit_reduction": "风控复核后下调额度",
            "system_risk_reduction": "贷后监测到风险信号，系统临时下调额度",
        }.get(code, code)
        if event_row["reason_detail"]:
            return f"{base}：{event_row['reason_detail']}"
        return base

    def decision_explanation(self, assessment_id: str) -> dict:
        """向客户解释一次授信结论：通过/拒绝/转人工的具体原因（人话）。"""
        asm = self.store.require("assessments", assessment_id)
        reasons = Store.loads(asm["triggered_reasons"])
        calc = Store.loads(asm["calculation"])
        return {
            "assessment_id": assessment_id,
            "decision": asm["decision"],
            "decision_text": {
                "approved": "通过",
                "rejected": "未通过",
                "manual_review": "转人工复核",
            }[asm["decision"]],
            "rule_set_version": asm["rule_set_version"],
            "reasons": [{"code": c, "text": REASON_CODES.get(c, c)} for c in reasons],
            "key_numbers": {
                "monthly_income_yuan": _yuan(calc.get("monthly_income_cents")),
                "existing_monthly_obligation_yuan": _yuan(calc.get("existing_monthly_obligation_cents")),
                "proposed_monthly_payment_yuan": _yuan(calc.get("proposed_monthly_payment_cents")),
                "dti_all_in": calc.get("dti_all_in"),
                "disposable_yuan": _yuan(calc.get("disposable_cents")),
            },
            "data_as_of": {
                k: {"snapshot_id": v["id"], "as_of": v["as_of"], "source": v["source"]}
                for k, v in Store.loads(asm["inputs"])["snapshots"].items()
            },
            "right_to_reconsider": "如对结论有异议，可提交收入证明或申请困难协商要求人工复核。",
        }


def _percent(rate: Decimal) -> str:
    return f"{(rate * 100).quantize(Decimal('0.01'))}%"
