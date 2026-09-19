"""测试夹具：快速搭建带授权、快照、授信账户的客户。"""

from credit.app import BankApp

SCOPES_ALL = ["income_read", "debt_read", "credit_report_read"]
PURPOSES_ALL = ["credit_assessment", "ongoing_monitoring", "hardship_review"]


def make_app():
    return BankApp(":memory:")


def healthy_income(monthly=20000_00):
    """月收入 20000 元，最近四个月稳定。"""
    return {
        "monthly_income_cents": monthly,
        "employment_status": "employed",
        "employer": "某科技公司",
        "months": [
            {"month": "2026-05", "income_cents": monthly},
            {"month": "2026-06", "income_cents": monthly},
            {"month": "2026-07", "income_cents": monthly},
            {"month": "2026-08", "income_cents": monthly},
        ],
    }


def healthy_debt(existing_obligation=2000_00, facilities=None):
    return {
        "monthly_obligation_cents": existing_obligation,
        "facilities": facilities or [],
    }


def healthy_credit(status="normal"):
    return {"status": status, "source": "PBOC"}


def bootstrap_customer(app, customer_id="c1", name="张三",
                       income=None, debt=None, credit=None,
                       amount_yuan="50000", annual_rate="0.072", term=12,
                       purpose="家居装修", category="home_improvement",
                       marketing=None):
    app.customers.create(customer_id, name)
    app.consents.grant(customer_id, SCOPES_ALL, PURPOSES_ALL, "GRANT-001")
    if marketing is not None:
        app.consents.set_marketing(customer_id, marketing, ["sms", "app_message"])
    app.consents.record_read(customer_id, "income", "credit_assessment",
                             "payroll_bank", "2026-08-31", income or healthy_income())
    app.consents.record_read(customer_id, "debt", "credit_assessment",
                             "credit_bureau", "2026-08-31", debt or healthy_debt())
    app.consents.record_read(customer_id, "credit_report", "credit_assessment",
                             "credit_bureau", "2026-08-31", credit or healthy_credit())
    application = app.underwriting.submit_application(
        customer_id, amount_yuan, annual_rate, term, purpose, category)
    assessment = app.underwriting.assess(application["id"])
    return {"application": application, "assessment": assessment}


def approved_account(app, customer_id="c1", **kwargs):
    result = bootstrap_customer(app, customer_id, **kwargs)
    assert result["assessment"]["decision"] == "approved", result["assessment"]
    account_id = app.store.query_one(
        "SELECT id FROM credit_accounts WHERE customer_id=?", (customer_id,))["id"]
    result["account_id"] = account_id
    return result


def draw_and_disburse(app, customer_id, account_id, amount_yuan,
                      purpose="家居装修", category="home_improvement",
                      evidence_amount=None):
    drw = app.credit_lines.reserve(customer_id, account_id, amount_yuan,
                                   purpose, "M-1", category)
    evidence_amount = evidence_amount if evidence_amount is not None else amount_yuan
    app.credit_lines.submit_evidence(drw["id"], {
        "items": [{"category": category, "amount_cents": _cents(evidence_amount),
                   "merchant_id": "M-1"}]
    })
    loan = app.credit_lines.disburse(drw["id"])
    return drw, loan


def _cents(yuan):
    from credit.money import yuan_to_cents
    return yuan_to_cents(yuan)
