"""测试公共夹具：固定时钟、已授权且已摄取快照的客户构造器。"""

from datetime import datetime, timezone

from credit.api import App
from credit.clock import Clock
from credit.storage import Storage

T0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)

RISK_SCOPES = ["income", "credit_report", "debt_snapshot"]


def make_app(start=T0):
    clock = Clock(start)
    app = App(Storage(), clock)
    return app


def seed_customer(app, customer_id="cust-1", *, income_cents=2000000, income_status="employed",
                  obligation_cents=300000, institutions=1, in_grace=False, overdue_days=0,
                  new_short_term_cents=0, scopes=None, grant_purpose="affordability_assessment",
                  as_of_income="2026-09-01", as_of_credit="2026-09-10",
                  total_outstanding=5000000):
    from credit import consent

    s = app.storage
    now = app.clock.now().isoformat()
    consent.grant(s, customer_id, scopes or RISK_SCOPES, grant_purpose, now)
    consent.ingest_snapshot(
        s, customer_id, "income", "payroll", as_of_income,
        {"monthly_income_cents": income_cents, "status": income_status}, now,
    )
    consent.ingest_snapshot(
        s, customer_id, "credit_report", "pboc", as_of_credit,
        {"in_grace_period": in_grace, "has_overdue": overdue_days > 0,
         "max_overdue_days": overdue_days}, now,
    )
    consent.ingest_snapshot(
        s, customer_id, "debt_snapshot", "pboc", as_of_credit,
        {"monthly_obligation_cents": obligation_cents, "institution_count": institutions,
         "total_outstanding_cents": total_outstanding,
         "new_short_term_debt_cents_since_assessment": new_short_term_cents}, now,
    )
    return customer_id


def update_snapshots(app, customer_id, *, income_cents=None, income_status=None,
                     obligation_cents=None, institutions=None, in_grace=None,
                     overdue_days=None, new_short_term_cents=None,
                     as_of="2026-09-20"):
    """写入一版更新的快照（模拟授信后外部数据变化）。"""
    from credit import consent

    s = app.storage
    now = app.clock.now().isoformat()
    if income_cents is not None or income_status is not None:
        old = s.latest_snapshot(customer_id, "income")
        import json
        payload = json.loads(old["payload"])
        if income_cents is not None:
            payload["monthly_income_cents"] = income_cents
        if income_status is not None:
            payload["status"] = income_status
        consent.ingest_snapshot(s, customer_id, "income", "payroll", as_of, payload, now)
    debt_changes = {}
    if obligation_cents is not None:
        debt_changes["monthly_obligation_cents"] = obligation_cents
    if institutions is not None:
        debt_changes["institution_count"] = institutions
    if new_short_term_cents is not None:
        debt_changes["new_short_term_debt_cents_since_assessment"] = new_short_term_cents
    if debt_changes:
        old = s.latest_snapshot(customer_id, "debt_snapshot")
        payload = __import__("json").loads(old["payload"])
        payload.update(debt_changes)
        consent.ingest_snapshot(s, customer_id, "debt_snapshot", "pboc", as_of, payload, now)
    if in_grace is not None or overdue_days is not None:
        old = s.latest_snapshot(customer_id, "credit_report")
        payload = __import__("json").loads(old["payload"])
        if in_grace is not None:
            payload["in_grace_period"] = in_grace
        if overdue_days is not None:
            payload["has_overdue"] = overdue_days > 0
            payload["max_overdue_days"] = overdue_days
        consent.ingest_snapshot(s, customer_id, "credit_report", "pboc", as_of, payload, now)


def approved_line(app, customer_id="cust-1", requested_yuan=50000, term_months=12):
    res = app.apply(
        {"customer_id": customer_id, "requested_yuan": requested_yuan,
         "term_months": term_months},
        {"id": customer_id, "role": "customer"},
    )
    assert res["assessment"]["decision"] == "approved", res["assessment"]["reasons"]
    line = app.offer(res["application_id"], {}, {"id": "system", "role": "system"})
    return res["application_id"], line


def first_period_payment_yuan(loan_view):
    row = loan_view["schedule"][0]
    return (row["principal_cents"] + row["interest_cents"]) / 100
