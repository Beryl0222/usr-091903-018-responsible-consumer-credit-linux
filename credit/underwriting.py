"""申请与授信。

流程：提交申请 → 在授权范围内读取三类快照 → 规则引擎评估 →
approved：开立额度账户并记录额度变化原因；
manual_review：立案人工审查，不产生额度；
rejected：申请拒绝。
评估结论与依据全部落 assessments，任何营销目标都无法进入该结论。
"""

from decimal import Decimal

from credit import clock
from credit.errors import conflict, validation_error
from credit.rules import AffordabilityEngine, AssessmentInput, DEFAULT_RULE_SET
from credit.store import Store, new_id

PURPOSE = "credit_assessment"


class UnderwritingService:
    def __init__(self, store: Store, consents):
        self.store = store
        self.consents = consents

    def submit_application(self, customer_id: str, requested_yuan, annual_rate: str,
                           term_months: int, declared_purpose: str,
                           merchant_category: str | None = None) -> dict:
        from credit.money import yuan_to_cents
        try:
            requested = yuan_to_cents(requested_yuan)
        except (ValueError, ArithmeticError):
            raise validation_error("申请金额格式不正确")
        if requested <= 0:
            raise validation_error("申请金额必须大于零")
        if term_months <= 0:
            raise validation_error("期限必须为正整数")
        try:
            rate = Decimal(annual_rate)
        except Exception:
            raise validation_error("年利率格式不正确")
        if not (Decimal("0") <= rate < Decimal("1")):
            raise validation_error("年利率需为 0~1 之间的小数（如 0.072）")
        if not declared_purpose:
            raise validation_error("必须声明贷款用途")

        row = {
            "id": new_id("app"),
            "customer_id": customer_id,
            "requested_cents": requested,
            "term_months": term_months,
            "declared_purpose": declared_purpose,
            "merchant_category": merchant_category,
            "annual_rate": str(rate),
            "status": "submitted",
            "created_at": clock.now_iso(),
        }
        with self.store.transaction():
            self.store.require("customers", customer_id)
            self.store.insert("applications", row)
        return row

    def assess(self, application_id: str, rule_set_version: str = DEFAULT_RULE_SET) -> dict:
        """执行一次可负担性评估并落库；申请状态随之更新。"""
        app = self.store.require("applications", application_id)
        if app["status"] != "submitted":
            raise conflict(f"申请已处理，当前状态: {app['status']}")
        customer_id = app["customer_id"]

        # 三类数据分别校验授权（缺任一 scope 即 403，不做降级评估）
        consent = self.consents.require_scope(customer_id, "income_read", PURPOSE)
        for scope in ("debt_read", "credit_report_read"):
            self.consents.require_scope(customer_id, scope, PURPOSE)

        income = self.store.latest_snapshot(customer_id, "income")
        debt = self.store.latest_snapshot(customer_id, "debt")
        credit = self.store.latest_snapshot(customer_id, "credit_report")
        missing = [name for name, snap in
                   (("income", income), ("debt", debt), ("credit_report", credit))
                   if snap is None]
        if missing:
            raise validation_error("缺少评估所需数据快照", {"missing": missing})

        with self.store.customer_lock(customer_id), self.store.transaction():
            engine = AffordabilityEngine(self.store, rule_set_version)
            assessment = engine.evaluate(app, AssessmentInput(income, debt, credit), consent)
            new_status = {"approved": "approved", "rejected": "rejected",
                          "manual_review": "manual_review"}[assessment["decision"]]
            self.store._conn.execute(
                "UPDATE applications SET status=? WHERE id=?",
                (new_status, application_id),
            )
            if assessment["decision"] == "approved":
                self._open_account(assessment, app)
            elif assessment["decision"] == "manual_review":
                self._open_underwriting_case(assessment)
        return self.store.row_to_dict(self.store.require("assessments", assessment["id"]))

    def _open_account(self, assessment: dict, app) -> dict:
        now = clock.now_iso()
        account = {
            "id": new_id("acc"),
            "customer_id": app["customer_id"],
            "application_id": app["id"],
            "limit_cents": assessment["approved_limit_cents"],
            "reserved_cents": 0,
            "outstanding_cents": 0,
            "annual_rate": assessment["annual_rate"],
            "term_months": assessment["term_months"],
            "status": "active",
            "opened_at": now,
        }
        self.store.insert("credit_accounts", account)
        self.store.insert("limit_change_events", {
            "id": new_id("lce"),
            "account_id": account["id"],
            "old_limit_cents": None,
            "new_limit_cents": account["limit_cents"],
            "reason_code": "initial_assessment",
            "reason_detail": f"依据规则集 {assessment['rule_set_version']} 的可负担性评估",
            "source": "assessment",
            "review_case_id": None,
            "actor": "system",
            "created_at": now,
        })
        return account

    def _open_underwriting_case(self, assessment: dict) -> dict:
        case = {
            "id": new_id("rev"),
            "customer_id": assessment["customer_id"],
            "account_id": None,
            "drawdown_id": None,
            "loan_id": None,
            "application_id": assessment["application_id"],
            "signal_ids": Store.dumps([]),
            "topic": "underwriting",
            "status": "open",
            "decision": None,
            "new_limit_cents": None,
            "new_term_months": None,
            "new_annual_rate": None,
            "reason_detail": None,
            "reviewer": None,
            "decided_at": None,
            "created_at": clock.now_iso(),
        }
        self.store.insert("review_cases", case)
        self.store._conn.execute(
            "UPDATE assessments SET warnings=? WHERE id=?",
            (Store.dumps({"review_case_id": case["id"]}), assessment["id"]),
        )
        return case

    def get_assessment(self, assessment_id: str) -> dict:
        return self.store.row_to_dict(self.store.require("assessments", assessment_id))
