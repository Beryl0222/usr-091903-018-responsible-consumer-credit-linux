"""贷款台账：放款、还款冲账、商户退款/分期取消的原路冲减、困难重组重算。

关键边界：
- repayment（客户还款）按到期顺序冲利息与本金，超额部分提前还本并重算后续计划；
- merchant_refund / installment_cancel 是对原放款交易的反向冲减，
  绝不记为客户还款（不产生"已还期次"），只削减本金、按原期限重算后续计划；
- 每笔变动写 ledger_entries，带 original_route / ref，资金去向可追溯。
"""

from datetime import date

from .errors import Conflict, NotFound, ValidationFailed
from .util import build_schedule, cents_to_yuan


def _today(clock):
    return clock.now().date()


def _parse_day(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _active_rows(storage, loan_id):
    return storage.schedule_rows(loan_id, active_only=True)


def outstanding_principal(storage, loan_id):
    return sum(r["principal_cents"] - r["paid_principal_cents"] for r in _active_rows(storage, loan_id))


def outstanding_interest(storage, loan_id):
    return sum(r["interest_cents"] - r["paid_interest_cents"] for r in _active_rows(storage, loan_id))


def loan_summary(storage, loan_id, today=None):
    loan = storage.get_loan(loan_id)
    if loan is None:
        raise NotFound("贷款不存在")
    rows = _active_rows(storage, loan_id)
    principal_out = sum(r["principal_cents"] - r["paid_principal_cents"] for r in rows)
    interest_out = sum(r["interest_cents"] - r["paid_interest_cents"] for r in rows)
    total_interest = sum(r["interest_cents"] for r in rows)
    next_due = None
    overdue_cents = 0
    if today is None:
        today = date.today()
    for r in rows:
        unpaid = (r["principal_cents"] - r["paid_principal_cents"]) + (
            r["interest_cents"] - r["paid_interest_cents"]
        )
        if unpaid > 0 and _parse_day(r["due_date"]) <= today:
            overdue_cents += unpaid
        if unpaid > 0 and next_due is None:
            next_due = r["due_date"]
    return {
        "loan_id": loan_id,
        "status": loan["status"],
        "principal_cents": loan["principal_cents"],
        "annual_rate": loan["annual_rate"],
        "term_months": loan["term_months"],
        "outstanding_principal_cents": principal_out,
        "outstanding_interest_cents": interest_out,
        "total_interest_cents": total_interest,
        "total_cost_cents": loan["principal_cents"] + total_interest,
        "next_due_date": next_due,
        "overdue_cents": overdue_cents,
        "schedule": [
            {
                "period": r["period_no"],
                "due_date": r["due_date"],
                "principal_cents": r["principal_cents"],
                "interest_cents": r["interest_cents"],
                "paid_principal_cents": r["paid_principal_cents"],
                "paid_interest_cents": r["paid_interest_cents"],
                "status": r["row_status"],
            }
            for r in rows
        ],
    }


def disburse(storage, withdrawal, annual_rate, term_months, clock, first_due=None):
    """放款：建立贷款与还款计划，写放款台账。"""
    now = clock.now().isoformat()
    today = _today(clock)
    if first_due is None:
        from .util import add_months

        first_due = add_months(today, 1).isoformat()

    def work(cur):
        loan_id = storage.create_loan(
            withdrawal["id"], withdrawal["customer_id"], withdrawal["amount_cents"],
            annual_rate, term_months, first_due, now, now,
        )
        rows = build_schedule(withdrawal["amount_cents"], annual_rate, term_months, _parse_day(first_due))
        for period, due, principal, interest in rows:
            storage.insert_schedule_row(
                loan_id, period, due.isoformat(), principal, interest
            )
        storage.add_ledger_entry(
            withdrawal["customer_id"], loan_id, "disbursement",
            withdrawal["amount_cents"], withdrawal["amount_cents"], now,
            original_route=f"merchant:{withdrawal['merchant_id']}" if withdrawal.get("merchant_id") else "bank",
            ref_type="withdrawal", ref_id=withdrawal["id"],
        )
        return loan_id

    return storage.transaction(work)


def _rebuild_remaining(storage, loan_id, new_principal_cents, annual_rate, first_due=None,
                       period_count=None, *, preserve_partial=True):
    """作废尾部未还期次并按新本金重建；保留已还清期次，期次号续编。

    preserve_partial=True  仅作废"完全未还"的期次，部分还款期次保留；
                           起始日/期数默认沿用被作废的尾部期次；
    preserve_partial=False 部分还款期次也并入新计划（用于重组/展期，
                           其已还金额仍保留在台账与 payment_allocations 中），
                           起始日/期数必须由调用方显式给出。
    """
    active = _active_rows(storage, loan_id)
    if preserve_partial:
        tail = [r for r in active
                if r["paid_principal_cents"] == 0 and r["paid_interest_cents"] == 0]
        storage.supersede_fully_unpaid_schedule(loan_id)
    else:
        tail = [r for r in active
                if r["paid_principal_cents"] < r["principal_cents"]
                or r["paid_interest_cents"] < r["interest_cents"]]
        storage.execute(
            "UPDATE schedule_rows SET superseded=1, row_status='cancelled' "
            "WHERE loan_id=? AND superseded=0 AND "
            "(paid_principal_cents<principal_cents OR paid_interest_cents<interest_cents)",
            (loan_id,),
        )
    kept = [r for r in active if r not in tail]
    start_period = (max(r["period_no"] for r in kept) + 1) if kept else 1
    if first_due is None:
        first_due = tail[0]["due_date"] if tail else None
    if period_count is None:
        period_count = len(tail)
    if new_principal_cents <= 0 or period_count <= 0 or first_due is None:
        return
    rows = build_schedule(new_principal_cents, annual_rate, period_count, _parse_day(first_due))
    for offset, (_period, due, principal, interest) in enumerate(rows):
        storage.insert_schedule_row(
            loan_id, start_period + offset, due.isoformat(), principal, interest
        )


def _allocate_repayment(storage, loan_id, entry_id, amount_cents, today):
    """按到期顺序把还款摊到各期（先息后本），返回 (剩余金额, 是否结清)。"""
    rows = _active_rows(storage, loan_id)
    remaining = amount_cents
    paid_off = True
    for r in rows:
        unpaid_interest = r["interest_cents"] - r["paid_interest_cents"]
        unpaid_principal = r["principal_cents"] - r["paid_principal_cents"]
        if unpaid_interest == 0 and unpaid_principal == 0:
            continue
        # 未到期期次不在常规还款冲账顺序内（提前还本走重算路径）
        if _parse_day(r["due_date"]) > today and remaining > 0:
            paid_off = False
            continue
        pay_interest = min(remaining, unpaid_interest)
        remaining -= pay_interest
        pay_principal = min(remaining, unpaid_principal)
        remaining -= pay_principal
        new_interest = r["paid_interest_cents"] + pay_interest
        new_principal = r["paid_principal_cents"] + pay_principal
        status = "paid" if (
            new_interest >= r["interest_cents"] and new_principal >= r["principal_cents"]
        ) else "partial"
        if status != "paid":
            paid_off = False
        storage.update_schedule_row_paid(r["id"], new_principal, new_interest, status)
        storage.add_allocation(entry_id, r["id"], pay_principal, pay_interest)
    return remaining, paid_off


def repay(storage, loan_id, amount_cents, clock, *, route="customer_account", ref_id=None):
    """客户正常还款。到期期次先息后本，溢缴提前归还本金并重算剩余计划。"""
    if amount_cents <= 0:
        raise ValidationFailed("还款金额必须为正")
    now = clock.now().isoformat()
    today = _today(clock)
    loan = storage.get_loan(loan_id)
    if loan is None:
        raise NotFound("贷款不存在")
    if loan["status"] not in ("repaying", "overdue"):
        raise Conflict(f"贷款状态 {loan['status']} 不可还款", details={"status": loan["status"]})
    # 提前结清口径：剩余本金 + 已到期未付利息；未到期利息随提前还本豁免
    due_interest = sum(
        r["interest_cents"] - r["paid_interest_cents"]
        for r in _active_rows(storage, loan_id)
        if _parse_day(r["due_date"]) <= today
    )
    payoff = outstanding_principal(storage, loan_id) + due_interest
    if amount_cents > payoff:
        raise ValidationFailed(
            "还款金额超过应还总额",
            details={"amount_cents": amount_cents, "payoff_cents": payoff},
        )

    customer_id = loan["customer_id"]
    line = storage.get_line(customer_id)
    principal_before = outstanding_principal(storage, loan_id)

    def work(cur):
        entry_id = storage.add_ledger_entry(
            customer_id, loan_id, "repayment", amount_cents, -1, now,
            original_route=route, ref_type="repayment", ref_id=ref_id,
        )
        leftover, _ = _allocate_repayment(storage, loan_id, entry_id, amount_cents, today)

        prepaid_principal = 0
        if leftover > 0:
            # 溢缴部分提前归还未来本金，按剩余期数、同利率重算
            future = [r for r in _active_rows(storage, loan_id) if _parse_day(r["due_date"]) > today]
            future_principal = sum(r["principal_cents"] for r in future)
            prepaid_principal = min(leftover, future_principal)
            new_principal = future_principal - prepaid_principal
            if new_principal <= 0:
                storage.supersede_fully_unpaid_schedule(loan_id)
            elif future:
                _rebuild_remaining(
                    storage, loan_id, new_principal, loan["annual_rate"],
                )

        balance_after = outstanding_principal(storage, loan_id)
        principal_repaid = principal_before - balance_after
        # 修正该条台账的本金余额
        storage.execute(
            "UPDATE ledger_entries SET principal_balance_after_cents=? WHERE id=?",
            (balance_after, entry_id),
        )
        if line:
            storage.release_available(line["id"], principal_repaid)

        new_status = "closed" if balance_after == 0 and outstanding_interest(storage, loan_id) == 0 else (
            "overdue" if loan_summary(storage, loan_id, today)["overdue_cents"] > 0 else "repaying"
        )
        storage.set_loan_status(loan_id, new_status)
        return {
            "ledger_entry_id": entry_id,
            "applied_cents": amount_cents - leftover,
            "prepaid_principal_cents": prepaid_principal,
            "principal_balance_after_cents": balance_after,
            "loan_status": new_status,
        }

    return storage.transaction(work)


def merchant_reversal(storage, withdrawal_id, amount_cents, clock, *, kind, route, ref_id=None):
    """商户退款 / 分期取消：对原放款的原路冲减，不是客户还款。

    kind=merchant_refund：已放款后的部分或全额退货退款；
    kind=installment_cancel：未放款时直接释放占用并取消；已放款则全额冲减。
    """
    now = clock.now().isoformat()
    today = _today(clock)
    withdrawal = storage.get_withdrawal(withdrawal_id)
    if withdrawal is None:
        raise NotFound("提款不存在")
    if amount_cents <= 0 or amount_cents > withdrawal["amount_cents"]:
        raise ValidationFailed("冲减金额超出原提款金额")

    line = storage.get_line(withdrawal["customer_id"])
    loan = storage.get_loan_by_withdrawal(withdrawal_id)

    # 未放款：解除全额占用并取消提款，不产生任何还款记录
    if withdrawal["status"] in ("reserved", "held"):
        if amount_cents != withdrawal["amount_cents"]:
            raise ValidationFailed(
                "未放款分期只能全额取消，不支持部分取消",
                details={"withdrawal_cents": withdrawal["amount_cents"],
                         "requested_cents": amount_cents},
            )

        def release_work(cur):
            if line:
                storage.release_available(line["id"], amount_cents)
            storage.set_withdrawal_status(
                withdrawal_id, "cancelled", now
            )
            if kind == "installment_cancel" and line:
                storage.add_line_change(
                    line["id"], "release", None, None,
                    "INSTALLMENT_CANCELLED", "分期取消，释放未放款占用",
                    "withdrawal", withdrawal_id, now,
                )
            return {"withdrawal_status": "cancelled", "loan_id": None}

        return storage.transaction(release_work)

    if withdrawal["status"] != "disbursed" or loan is None:
        raise Conflict(
            f"提款状态 {withdrawal['status']} 不可冲减",
            details={"status": withdrawal["status"]},
        )

    remaining_principal = outstanding_principal(storage, loan["id"])
    if amount_cents > remaining_principal:
        raise Conflict(
            "退款金额超过剩余本金（已还本金部分不重复冲减）",
            details={"amount_cents": amount_cents, "remaining_principal_cents": remaining_principal},
        )

    def work(cur):
        # 已产生但未支付的利息在全额原路冲减时豁免（退货并非客户违约）
        interest_waived = 0
        if amount_cents == remaining_principal:
            interest_waived = outstanding_interest(storage, loan["id"])

        new_principal = remaining_principal - amount_cents
        if new_principal <= 0:
            # 全额冲减：作废全部未结清期次（未付利息随豁免）
            storage.execute(
                "UPDATE schedule_rows SET superseded=1, row_status='cancelled' "
                "WHERE loan_id=? AND superseded=0 AND "
                "(paid_principal_cents<principal_cents OR paid_interest_cents<interest_cents)",
                (loan["id"],),
            )
        else:
            _rebuild_remaining(storage, loan["id"], new_principal, loan["annual_rate"])

        balance_after = outstanding_principal(storage, loan["id"])
        entry_id = storage.add_ledger_entry(
            withdrawal["customer_id"], loan["id"], kind, amount_cents, balance_after, now,
            original_route=route, ref_type="withdrawal", ref_id=withdrawal_id,
            detail={"interest_waived_cents": interest_waived},
        )
        if line:
            storage.release_available(line["id"], amount_cents)
            storage.add_line_change(
                line["id"], "reversal", None, None,
                "MERCHANT_REFUND" if kind == "merchant_refund" else "INSTALLMENT_CANCELLED",
                "原路冲减本金，非客户还款", "ledger", entry_id, now,
            )

        new_status = "closed" if balance_after == 0 and outstanding_interest(storage, loan["id"]) == 0 else "repaying"
        storage.set_loan_status(loan["id"], new_status)
        if new_status == "closed" and amount_cents == withdrawal["amount_cents"]:
            storage.set_withdrawal_status(withdrawal_id, "reversed", now)
        else:
            storage.set_withdrawal_status(withdrawal_id, "partially_reversed", now)
        return {
            "ledger_entry_id": entry_id,
            "loan_id": loan["id"],
            "reversed_principal_cents": amount_cents,
            "interest_waived_cents": interest_waived,
            "principal_balance_after_cents": balance_after,
            "loan_status": new_status,
            "treated_as_repayment": False,
        }

    return storage.transaction(work)


def restructure(storage, loan_id, plan, clock, *, decided_by, case_id):
    """人工困难决定落地：extension 展期 / restructure 重组 / forbearance 缓还。

    plan:
      type=extension  : new_term_months
      type=restructure: new_term_months, new_annual_rate, principal_forgiveness_cents?
      type=forbearance: defer_months（期间不催收，不计罚息；利息按 capitalize_interest 决定是否资本化）
    """
    now = clock.now().isoformat()
    today = _today(clock)
    loan = storage.get_loan(loan_id)
    if loan is None:
        raise NotFound("贷款不存在")

    plan_type = plan["type"]
    if plan_type not in ("extension", "restructure", "forbearance"):
        raise ValidationFailed(f"不支持的安排类型：{plan_type}")

    from .util import add_months

    def work(cur):
        remaining_principal = outstanding_principal(storage, loan_id)
        unpaid_interest = outstanding_interest(storage, loan_id)
        forgiveness = int(plan.get("principal_forgiveness_cents", 0))
        forgiveness = min(forgiveness, remaining_principal)
        remaining_principal -= forgiveness

        rate = loan["annual_rate"]
        next_due = add_months(today, 1)
        term_count = 0

        if plan_type == "forbearance":
            # 缓还：不改变金额与利率，仅把未结清期次整体递延
            defer_months = int(plan["defer_months"])
            storage.shift_unpaid_schedule(loan_id, defer_months)
            active_after = _active_rows(storage, loan_id)
            unsettled = [r for r in active_after
                         if r["principal_cents"] > r["paid_principal_cents"]
                         or r["interest_cents"] > r["paid_interest_cents"]]
            next_due = _parse_day(unsettled[0]["due_date"]) if unsettled else next_due
            term_count = len(active_after)
        else:
            # extension / restructure：未付余额按新期限/利率重排，已还期次保留、期号续编
            if plan_type == "extension":
                term_count = int(plan["new_term_months"])
                rate = str(plan.get("new_annual_rate", loan["annual_rate"]))
            else:
                term_count = int(plan["new_term_months"])
                rate = str(plan["new_annual_rate"])
            new_balance = remaining_principal
            if plan.get("capitalize_interest", True):
                new_balance += unpaid_interest
            _rebuild_remaining(
                storage, loan_id, new_balance, rate,
                first_due=next_due.isoformat(), period_count=term_count,
                preserve_partial=False,
            )
            remaining_principal = new_balance

        storage.execute(
            "UPDATE loans SET annual_rate=?, term_months=?, status='repaying' WHERE id=?",
            (rate, term_count, loan_id),
        )
        entry_id = storage.add_ledger_entry(
            loan["customer_id"], loan_id, "restructuring", forgiveness,
            remaining_principal, now,
            original_route="manual_review", ref_type="manual_case", ref_id=case_id,
            detail={"plan": plan_type, **plan, "forgiven_cents": forgiveness},
        )
        return {
            "arrangement": plan_type,
            "new_annual_rate": rate,
            "new_term_months": term_count,
            "new_principal_cents": remaining_principal,
            "forgiven_cents": forgiveness,
            "next_due_date": next_due.isoformat() if hasattr(next_due, "isoformat") else next_due,
            "ledger_entry_id": entry_id,
        }

    return storage.transaction(work)


def total_cost_disclosure(loan):
    """客户可读的利率与总成本信息。"""
    rows = build_schedule(loan["principal_cents"], loan["annual_rate"], loan["term_months"],
                          _parse_day(loan["first_due"]))
    total_interest = sum(r[3] for r in rows)
    return {
        "principal_yuan": cents_to_yuan(loan["principal_cents"]),
        "annual_rate": loan["annual_rate"],
        "term_months": loan["term_months"],
        "total_interest_yuan": cents_to_yuan(total_interest),
        "total_repayment_yuan": cents_to_yuan(loan["principal_cents"] + total_interest),
        "monthly_payment_first_yuan": cents_to_yuan(rows[0][2] + rows[0][3]),
    }
