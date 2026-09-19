"""催收联系边界。

规则（随规则版本固化）：
- 到期后有宽限期（默认 3 天自然日）：宽限期内仅允许一次还款提醒（reminder），
  不得进行催收施压、不得联系第三方；
- 困难协商未决（arrangement=requested）期间暂停一切催收联系，转人工协商通道；
  安排生效后客户按新计划履约，再次违约回到正常催收；
- 每日联系时段限制（默认 08:00–21:00）与每日最多联系次数；
- 每次联系（包括被拦截的尝试）都落 collection_contacts，合规可复现联系边界。
"""

from .rules import Rule

COLLECTION_RULE_VERSION = "collections-v1.0"

COLLECTION_PARAMS = {
    "grace_days": 3,
    "daily_contact_limit": 3,
    "contact_window_start_hour": 8,
    "contact_window_end_hour": 21,
}

REMINDER_CHANNELS = {"sms", "app_push", "email"}
PRESSURE_CHANNELS = {"phone", "field_visit"}


def _loan_overdue_days(storage, loan_id, today):
    rows = storage.schedule_rows(loan_id, active_only=True)
    days = 0
    for r in rows:
        from datetime import date

        due = date.fromisoformat(r["due_date"])
        unpaid = (r["principal_cents"] - r["paid_principal_cents"]) + (
            r["interest_cents"] - r["paid_interest_cents"]
        )
        if unpaid > 0 and due < today:
            days = max(days, (today - due).days)
    return days


def evaluate_contact(storage, loan_id, channel, when, actor, customer_id=None):
    """判定一次催收联系是否允许，并一律落库。返回判定 dict。"""
    loan = storage.get_loan(loan_id)
    cid = loan["customer_id"] if loan else customer_id
    today = when.date()
    hour = when.hour

    blocked = None
    in_grace = False
    hardship_hold = False

    arrangement = storage.active_arrangement(loan_id) if loan else None
    # 仅"协商未决（requested）"期间暂停催收；安排生效后客户按新计划履约，
    # 若再次违约则回到正常催收规则（仍受宽限期/时段/频次约束）
    if arrangement is not None and arrangement["status"] == "requested":
        hardship_hold = True
        blocked = "HARDSHIP_ARRANGEMENT_ACTIVE"

    overdue_days = _loan_overdue_days(storage, loan_id, today) if loan else 0
    grace_days = COLLECTION_PARAMS["grace_days"]
    if not blocked and loan and loan["status"] != "closed":
        if 0 < overdue_days <= grace_days:
            in_grace = True
            if channel in PRESSURE_CHANNELS:
                blocked = "GRACE_PERIOD_PRESSURE_PROHIBITED"

    if not blocked:
        start = COLLECTION_PARAMS["contact_window_start_hour"]
        end = COLLECTION_PARAMS["contact_window_end_hour"]
        if hour < start or hour >= end:
            blocked = "OUTSIDE_CONTACT_WINDOW"

    if not blocked:
        existing = [
            c for c in storage.collection_contacts(cid)
            if c["contact_at"][:10] == today.isoformat() and c["allowed"]
        ]
        if len(existing) >= COLLECTION_PARAMS["daily_contact_limit"]:
            blocked = "DAILY_CONTACT_LIMIT_REACHED"

    # 宽限期内的提醒：每自然日至多一次
    if not blocked and in_grace:
        reminders_today = [
            c for c in storage.collection_contacts(cid)
            if c["contact_at"][:10] == today.isoformat() and c["allowed"] and c["in_grace"]
        ]
        if reminders_today:
            blocked = "GRACE_REMINDER_ALREADY_SENT_TODAY"

    allowed = blocked is None
    contact_id = storage.add_collection_contact(
        loan_id, cid, channel, when.isoformat(), allowed, in_grace, hardship_hold,
        actor, blocked_reason=blocked,
    )
    return {
        "contact_id": contact_id,
        "allowed": allowed,
        "blocked_reason": blocked,
        "in_grace_period": in_grace,
        "hardship_hold": hardship_hold,
        "overdue_days": overdue_days,
        "rule_version": COLLECTION_RULE_VERSION,
    }
